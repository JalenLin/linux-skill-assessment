"""作答者實例生命週期：建立(linked clone) / 啟動 / 停止 / 重置。"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime

from . import db
from .config import cfg, instance_ip
from .pve import PVEError, client
from .questions import Question

log = logging.getLogger("instances")

_locks: dict[tuple[int, int, str], asyncio.Lock] = defaultdict(asyncio.Lock)
_alloc_lock = asyncio.Lock()
_clone_sem: asyncio.Semaphore | None = None
# PVE clone 時會鎖住來源範本：同一範本一次只 clone 一個，不同範本可同時進行
_template_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# 目前有開著 web shell 的實例 {vmid: 連線數}
active_shells: dict[int, int] = defaultdict(int)
# 同一帳號的「開題」排隊處理：避免同時開兩題時都沒算到對方而超過上限
_acct_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# 開著的 web shell 註冊在這裡；環境被暫停前先通知它們顯示訊息並斷線 {vmid: {Event}}
shell_kicks: dict[int, set[asyncio.Event]] = defaultdict(set)


def kick_shells(vmid: int) -> None:
    for ev in list(shell_kicks.get(vmid, ())):
        ev.set()


class InstanceError(Exception):
    pass


class _nolock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sem() -> asyncio.Semaphore:
    global _clone_sem
    if _clone_sem is None:
        _clone_sem = asyncio.Semaphore(cfg.clone_concurrency)
    return _clone_sem


def get(account_id: int, sid: int, qid: str) -> dict | None:
    return db.one("SELECT * FROM instances WHERE account_id=? AND set_id=? AND qid=?", (account_id, sid, qid))


def touch(inst: dict) -> None:
    db.execute("UPDATE instances SET last_access=? WHERE id=?", (_now(), inst["id"]))


def set_status(inst_id: int, status: str) -> None:
    db.execute("UPDATE instances SET status=? WHERE id=?", (status, inst_id))


def template_for(qid: str) -> dict | None:
    return db.one("SELECT * FROM templates WHERE qid=? AND status='ready'", (qid,))


async def _allocate_vmid(account_id: int, sid: int, q: Question, tmpl: dict) -> dict:
    async with _alloc_lock:
        used = {r["vmid"] for r in db.query("SELECT vmid FROM instances")}
        used |= set(await asyncio.to_thread(lambda: client().resources().keys()))
        for vmid in range(cfg.vmid_min, cfg.vmid_max + 1):
            if vmid not in used:
                break
        else:
            raise InstanceError("VMID 範圍已用完")
        inst_id = db.execute(
            "INSERT INTO instances(account_id,set_id,qid,vmid,kind,ip,status,template_version,last_access)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (account_id, sid, q.qid, vmid, q.kind, instance_ip(vmid), "creating", tmpl["version"], _now()),
        )
        return db.one("SELECT * FROM instances WHERE id=?", (inst_id,))


async def _create(account: dict, sid: int, q: Question) -> dict:
    tmpl = template_for(q.qid)
    if not tmpl:
        raise InstanceError("此題目環境尚未建置完成，請通知管理者")
    inst = await _allocate_vmid(account["id"], sid, q, tmpl)
    pve = client()
    desc = f"lsa account={account['username']} code={account['participant_code'] or '-'} set={sid} q={q.qid}"
    try:
        async with _template_locks[tmpl["vmid"]], _sem():
            await asyncio.to_thread(pve.clone, q.kind, tmpl["vmid"], inst["vmid"], q.hostname, desc)
        await asyncio.to_thread(pve.set_network, q.kind, inst["vmid"], inst["ip"])
        await asyncio.to_thread(pve.set_config, q.kind, inst["vmid"],
                                tags=f"lsa;s{sid};q-{q.qid};{account['username']}")
    except Exception as e:
        log.exception("clone failed")
        db.execute("DELETE FROM instances WHERE id=?", (inst["id"],))
        await asyncio.to_thread(_safe_destroy, q.kind, inst["vmid"])
        raise InstanceError(f"建立環境失敗: {e}") from e
    set_status(inst["id"], "stopped")
    return get(account["id"], sid, q.qid)


def _safe_destroy(kind: str, vmid: int) -> None:
    try:
        client().destroy(kind, vmid)
    except Exception:  # noqa: BLE001
        log.exception("destroy %s failed", vmid)


async def wait_ssh(ip: str, timeout: float = 90) -> bool:
    if cfg.mock:
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, 22), 3)
            banner = await asyncio.wait_for(r.readline(), 3)
            w.close()
            if banner.startswith(b"SSH-"):
                return True
        except (OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(1)
    return False


async def stop(inst: dict) -> None:
    """正常關機（內容保留），關不掉才強制停止。
    不直接斷電：VM 剛寫入、還在記憶體快取的檔案會遺失（實測：開機後馬上斷電，SSH 主機金鑰變成 0 bytes，sshd 起不來）"""
    await asyncio.to_thread(client().shutdown, inst["kind"], inst["vmid"], 40 if inst["kind"] == "qemu" else 20)
    set_status(inst["id"], "stopped")
    touch(inst)


async def stop_many(rows: list[dict], concurrency: int = 8) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def one(inst):
        async with sem:
            try:
                await stop(inst)
            except Exception:  # noqa: BLE001
                log.exception("stop %s failed", inst["vmid"])
    await asyncio.gather(*(one(r) for r in rows))


async def pause(inst: dict) -> None:
    """暫停（正常關機，內容保留）：先讓終端機顯示訊息斷線，再關機；關不掉才強制停止。
    VM 直接斷電可能遺失剛寫入、還在記憶體快取裡的檔案，資料庫等服務也可能損毀，所以要正常關機。"""
    kick_shells(inst["vmid"])
    await asyncio.sleep(0.5)
    await stop(inst)


async def _enforce_limit(account_id: int, keep: int | None) -> None:
    """keep：目前要開的實例 id（不算在內）。上限跨題組計算。"""
    limit = int(db.get_setting("max_running") or 1)
    running = db.query(
        "SELECT * FROM instances WHERE account_id=? AND status='running' AND id<>? "
        "ORDER BY last_access ASC", (account_id, keep or 0))
    victims = []
    while len(running) >= limit:
        victims.append(running.pop(0))
    for v in victims:
        log.info("pause LRU instance %s", v["vmid"])
    await asyncio.gather(*(pause(v) for v in victims))


async def ensure_running(account: dict, sid: int, q: Question, enforce_limit: bool = True,
                         need_ssh: bool = True) -> dict:
    """確保作答者在這個題組這題的實例存在且運作中，回傳 instance row。
    need_ssh=False（開終端機）：SSH 沒回應也照樣回傳，終端機會改用主控台連線。"""
    async with _locks[(account["id"], sid, q.qid)]:
        inst = get(account["id"], sid, q.qid)
        pve = client()
        if inst:
            st = await asyncio.to_thread(pve.status, inst["kind"], inst["vmid"])
            if st is None:  # 在 PVE 上被刪了
                db.execute("DELETE FROM instances WHERE id=?", (inst["id"],))
                inst = None
        if inst is None:
            inst = await _create(account, sid, q)
            st = "stopped"
        async with _acct_locks[account["id"]] if enforce_limit else _nolock():
            if enforce_limit:
                await _enforce_limit(account["id"], inst["id"])
            if st != "running":
                try:
                    await asyncio.to_thread(pve.start, inst["kind"], inst["vmid"])
                except PVEError as e:
                    raise InstanceError(f"啟動環境失敗: {e}") from e
            set_status(inst["id"], "running")
        touch(inst)
        # 剛開機要等開機完成；本來就在運作、只是開終端機時，SSH 沒回應多半是被改壞了，不用久等（會改用主控台）
        boot_wait = 180 if inst["kind"] == "qemu" else 60
        if not await wait_ssh(inst["ip"], boot_wait if (need_ssh or st != "running") else 8):
            if not need_ssh:
                log.info("instance %s running but SSH not responding; terminal will use the console", inst["vmid"])
                return get(account["id"], sid, q.qid)
            raise InstanceError("環境已啟動但 SSH 沒有回應。若你修改過 sshd 或網路設定，請重置本題。")
        return get(account["id"], sid, q.qid)


async def prepare(account: dict, sid: int, q: Question) -> bool:
    """預先建立（不啟動）。回傳是否新建。"""
    async with _locks[(account["id"], sid, q.qid)]:
        if get(account["id"], sid, q.qid):
            return False
        await _create(account, sid, q)
        return True


async def destroy(inst: dict) -> None:
    await asyncio.to_thread(client().destroy, inst["kind"], inst["vmid"])
    db.execute("DELETE FROM instances WHERE id=?", (inst["id"],))


async def reset(account: dict, sid: int, q: Question) -> None:
    async with _locks[(account["id"], sid, q.qid)]:
        inst = get(account["id"], sid, q.qid)
        if inst:
            await destroy(inst)


async def sync_status() -> None:
    """與 PVE 實際狀態對齊（背景定期呼叫）。"""
    res = await asyncio.to_thread(client().resources)
    now = datetime.now()
    for inst in db.query("SELECT * FROM instances WHERE status IN ('running','stopped')"):
        r = res.get(inst["vmid"])
        if r is None:
            continue
        # /cluster/resources 由 pvestatd 每約 10 秒更新：剛啟動/暫停的實例可能還是舊狀態，
        # 不要拿舊資料蓋掉（否則剛開好的環境會被標成停止，終端機連不上）
        lk = _locks.get((inst["account_id"], inst["set_id"], inst["qid"]))
        if lk is not None and lk.locked():
            continue
        if inst["last_access"] and (now - datetime.fromisoformat(inst["last_access"])).total_seconds() < 90:
            continue
        st = "running" if r.get("status") == "running" else "stopped"
        if st != inst["status"]:
            set_status(inst["id"], st)


async def stop_idle(idle_min: int) -> None:
    now = datetime.now()
    for inst in db.query("SELECT * FROM instances WHERE status='running'"):
        if active_shells.get(inst["vmid"]):
            continue
        last = datetime.fromisoformat(inst["last_access"]) if inst["last_access"] else now
        if (now - last).total_seconds() > idle_min * 60:
            log.info("stop idle instance %s", inst["vmid"])
            try:
                await stop(inst)
            except Exception:  # noqa: BLE001
                log.exception("stop idle failed")
