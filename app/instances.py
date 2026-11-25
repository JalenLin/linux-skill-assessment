"""作答者實例生命週期：建立(linked clone) / 啟動 / 停止 / 重置。

多節點題（環境組）：一個 instances 列 = 一組，每台機器在 instance_nodes；instances.vmid/kind/ip 是第一台。
所有操作都經過 nodes_of()：單機題回傳一台（name 為空字串）。"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime

from . import db
from .config import cfg, instance_ip
from .pve import PVEError, client
from .questions import Node, Question

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


def nodes_of(inst: dict) -> list[dict]:
    """[{name, vmid, kind, ip}]，依題目的節點順序"""
    rows = db.query("SELECT name, vmid, kind, ip FROM instance_nodes WHERE instance_id=? ORDER BY rowid",
                    (inst["id"],))
    return rows or [{"name": "", "vmid": inst["vmid"], "kind": inst["kind"], "ip": inst["ip"]}]


def node_target(inst: dict, name: str) -> dict | None:
    """終端機要連的那一台：instance 列，vmid/kind/ip 換成該節點的"""
    n = next((n for n in nodes_of(inst) if n["name"] == name), None)
    return None if n is None else dict(inst, vmid=n["vmid"], kind=n["kind"], ip=n["ip"], node=name)


def playbook_target(inst: dict, q: Question):
    """給 grader.run_playbook：單機題是 IP，多節點題是每台的 {name, ip, nics}"""
    if not q.is_group:
        return inst["ip"]
    ips = {n["name"]: n["ip"] for n in nodes_of(inst)}
    return [{"name": n.name, "ip": ips[n.name], "nics": n.nics} for n in q.nodes if n.name in ips]


def nic_specs(q: Question, node: Node, vlans: dict[str, int], gateway: bool = True) -> list[tuple]:
    """pve.set_nics 的參數：節點的 nics 依寫的順序是 eth1、eth2…。
    預設路由只設在第一個有 gateway、而且 gateway 不是自己的網路（router 節點自己就是 gateway）"""
    out, gw_used = [], False
    for i, (net, ip) in enumerate(node.nics.items(), start=1):
        gw = q.networks[net]["gateway"] if gateway and not gw_used and ip else None
        if gw and ip.split("/")[0] == gw:
            gw = None
        gw_used = gw_used or bool(gw)
        out.append((i, vlans[net], ip, gw))
    return out


def machine_count() -> int:
    """全部作答者環境的機器數（多節點題每台都算）"""
    return (db.one("SELECT COUNT(*) n FROM instances WHERE id NOT IN (SELECT instance_id FROM instance_nodes)")["n"]
            + db.one("SELECT COUNT(*) n FROM instance_nodes")["n"])


def _graders(q: Question | None) -> set[str]:
    return {n.name for n in q.nodes if n.is_grader} if q else set()


def _question(inst: dict) -> Question | None:
    from .questions import get
    return get(inst["qid"])


async def start_graders(inst: dict, q: Question) -> list[dict]:
    """按「檢查」時才開 grader 節點，回傳開了哪幾台（之後交給 stop_graders）"""
    gs = [n for n in nodes_of(inst) if n["name"] in _graders(q)]
    if not gs:
        return []
    pve = client()
    await asyncio.gather(*(asyncio.to_thread(pve.start, n["kind"], n["vmid"]) for n in gs))
    oks = await asyncio.gather(*(wait_ssh(n["ip"], 60) for n in gs))
    if not all(oks):
        log.warning("grader node(s) of %s not reachable", inst["vmid"])
    return gs


async def stop_graders(gs: list[dict]) -> None:
    pve = client()
    await asyncio.gather(*(asyncio.to_thread(pve.shutdown, n["kind"], n["vmid"], 20) for n in gs),
                         return_exceptions=True)


def shells_of(inst: dict) -> int:
    return sum(active_shells.get(n["vmid"], 0) for n in nodes_of(inst))


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


def template_nodes(qid: str) -> dict[str, dict]:
    """多節點題：{節點名稱: {vmid, kind}}"""
    return {r["name"]: r for r in db.query("SELECT * FROM template_nodes WHERE qid=?", (qid,))}


def used_vmids() -> set[int]:
    return ({r["vmid"] for r in db.query("SELECT vmid FROM instances")}
            | {r["vmid"] for r in db.query("SELECT vmid FROM instance_nodes")})


def used_vlans() -> set[int]:
    out: set[int] = set()
    for r in db.query("SELECT vlans FROM instances WHERE vlans IS NOT NULL"):
        out |= set(json.loads(r["vlans"]).values())
    return out


async def _allocate_vmid(account_id: int, sid: int, q: Question, tmpl: dict) -> dict:
    """配置 VMID（多節點題每台一個）與 VLAN（每個題目網路一個），寫入資料庫"""
    async with _alloc_lock:
        used = used_vmids() | set(await asyncio.to_thread(lambda: client().resources().keys()))
        want = max(1, len(q.nodes))
        vmids = [v for v in range(cfg.vmid_min, cfg.vmid_max + 1) if v not in used][:want]
        if len(vmids) < want:
            raise InstanceError("VMID 範圍已用完")
        vlans: dict[str, int] = {}
        if q.networks:
            free = (v for v in range(cfg.vlan_min, cfg.vlan_max + 1) if v not in used_vlans())
            vlans = {net: next(free, None) for net in q.networks}
            if None in vlans.values():
                raise InstanceError("VLAN 已用完")
        kind0 = q.nodes[0].kind if q.nodes else q.kind
        with db.tx() as c:
            inst_id = c.execute(
                "INSERT INTO instances(account_id,set_id,qid,vmid,kind,ip,status,template_version,last_access,vlans)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (account_id, sid, q.qid, vmids[0], kind0, instance_ip(vmids[0]), "creating", tmpl["version"],
                 _now(), json.dumps(vlans) if q.networks else None)).lastrowid
            for node, vmid in zip(q.nodes, vmids):
                c.execute("INSERT INTO instance_nodes(instance_id,name,vmid,kind,ip) VALUES(?,?,?,?,?)",
                          (inst_id, node.name, vmid, node.kind, instance_ip(vmid)))
        return db.one("SELECT * FROM instances WHERE id=?", (inst_id,))


async def _create(account: dict, sid: int, q: Question) -> dict:
    tmpl = template_for(q.qid)
    if not tmpl:
        raise InstanceError("此題目環境尚未建置完成，請通知管理者")
    tnodes = template_nodes(q.qid)
    if q.is_group and any(n.name not in tnodes for n in q.nodes):
        raise InstanceError("此題目環境尚未建置完成，請通知管理者")
    inst = await _allocate_vmid(account["id"], sid, q, tmpl)
    nodes = nodes_of(inst)
    vlans = json.loads(inst["vlans"]) if inst.get("vlans") else {}
    pve = client()
    desc = f"lsa account={account['username']} code={account['participant_code'] or '-'} set={sid} q={q.qid}"
    tags = f"lsa;s{sid};q-{q.qid};{account['username']}"

    async def one(n: dict) -> None:
        node = q.node(n["name"])
        src = tnodes[n["name"]]["vmid"] if node else tmpl["vmid"]
        async with _template_locks[src], _sem():
            await asyncio.to_thread(pve.clone, n["kind"], src, n["vmid"], node.name if node else q.hostname, desc)
        await asyncio.to_thread(pve.set_network, n["kind"], n["vmid"], n["ip"])
        if node and node.nics:
            await asyncio.to_thread(pve.set_nics, n["kind"], n["vmid"], nic_specs(q, node, vlans))
        await asyncio.to_thread(pve.set_config, n["kind"], n["vmid"],
                                tags=tags + (f";node-{node.name}" if node else ""))

    errs = [e for e in await asyncio.gather(*(one(n) for n in nodes), return_exceptions=True)
            if isinstance(e, BaseException)]
    if errs:
        log.error("clone failed: %r", errs[0])
        db.execute("DELETE FROM instances WHERE id=?", (inst["id"],))
        await asyncio.gather(*(asyncio.to_thread(_safe_destroy, n["kind"], n["vmid"]) for n in nodes))
        raise InstanceError(f"建立環境失敗: {errs[0]}") from errs[0]
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
    await asyncio.gather(*(asyncio.to_thread(client().shutdown, n["kind"], n["vmid"], 40 if n["kind"] == "qemu" else 20)
                           for n in nodes_of(inst)))
    set_status(inst["id"], "stopped")
    touch(inst)


async def start(inst: dict) -> None:
    """啟動全部節點（不等 SSH）；grader 節點除外，它只在檢查時開機"""
    skip = _graders(_question(inst))
    await asyncio.gather(*(asyncio.to_thread(client().start, n["kind"], n["vmid"])
                           for n in nodes_of(inst) if n["name"] not in skip))
    set_status(inst["id"], "running")


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
    for n in nodes_of(inst):
        kick_shells(n["vmid"])
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
        sts: list[str | None] = []
        if inst:
            nodes = nodes_of(inst)
            sts = list(await asyncio.gather(*(asyncio.to_thread(pve.status, n["kind"], n["vmid"]) for n in nodes)))
            if None in sts:  # 在 PVE 上被刪了（多節點題只少一台也整組重建）
                await destroy(inst)
                inst = None
        if inst is None:
            inst = await _create(account, sid, q)
            sts = ["stopped"] * len(nodes_of(inst))
        # grader 節點平常關機：開題、等 SSH 都不算它（檢查時由 grader.py 開關）
        graders = _graders(q)
        pairs = [(n, st) for n, st in zip(nodes_of(inst), sts) if n["name"] not in graders]
        nodes, sts = [n for n, _ in pairs], [st for _, st in pairs]
        async with _acct_locks[account["id"]] if enforce_limit else _nolock():
            if enforce_limit:
                await _enforce_limit(account["id"], inst["id"])
            try:
                await asyncio.gather(*(asyncio.to_thread(pve.start, n["kind"], n["vmid"])
                                       for n, st in zip(nodes, sts) if st != "running"))
            except PVEError as e:
                raise InstanceError(f"啟動環境失敗: {e}") from e
            set_status(inst["id"], "running")
        touch(inst)

        def wait_for(n: dict, st: str | None) -> int:
            # 剛開機要等開機完成；本來就在運作、只是開終端機時，SSH 沒回應多半是被改壞了，不用久等（會改用主控台）
            # terminal: console／console-only 的題目 SSH 不通是常態：開終端機不等 SSH，檢查也只短暫等一下
            node = q.node(n["name"])
            term = node.terminal_default if node else q.terminal_default
            boot_wait = 180 if n["kind"] == "qemu" else 60
            if term == "console":
                return 0 if not need_ssh else (boot_wait if st != "running" else 15)
            return boot_wait if (need_ssh or st != "running") else 8

        waits = [wait_for(n, st) for n, st in zip(nodes, sts)]
        oks = await asyncio.gather(*(wait_ssh(n["ip"], w) if w else asyncio.sleep(0, True)
                                     for n, w in zip(nodes, waits)))
        if not all(oks):
            bad = [n["vmid"] for n, ok in zip(nodes, oks) if not ok]
            if not need_ssh or q.is_group:
                # 多節點題：某台 SSH 不通照樣檢查（Ansible 會把那台算 unreachable，其他台照常評分）
                log.info("instance %s running but SSH not responding (%s)", inst["vmid"], bad)
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
    nodes = nodes_of(inst)
    errs = [e for e in await asyncio.gather(*(asyncio.to_thread(client().destroy, n["kind"], n["vmid"])
                                              for n in nodes), return_exceptions=True) if isinstance(e, BaseException)]
    if errs:
        raise errs[0]
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
    group_vmids: dict[int, list[int]] = defaultdict(list)
    for r in db.query("SELECT instance_id, vmid FROM instance_nodes"):
        group_vmids[r["instance_id"]].append(r["vmid"])
    for inst in db.query("SELECT * FROM instances WHERE status IN ('running','stopped')"):
        rs = [res.get(v) for v in group_vmids.get(inst["id"], [inst["vmid"]])]
        if None in rs:
            continue
        # 多節點題：任一台在運作就算運作中
        r = {"status": "running" if any(x.get("status") == "running" for x in rs) else "stopped"}
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
        if shells_of(inst):
            continue
        last = datetime.fromisoformat(inst["last_access"]) if inst["last_access"] else now
        if (now - last).total_seconds() > idle_min * 60:
            log.info("stop idle instance %s", inst["vmid"])
            try:
                await stop(inst)
            except Exception:  # noqa: BLE001
                log.exception("stop idle failed")
