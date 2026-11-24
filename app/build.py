"""建置題目範本與自我測試。

可從後台觸發，或命令列：
    python -m app.build <qid> [--no-selftest]
    python -m app.build --selftest <qid>
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime

from . import db
from .config import build_ip, cfg, instance_ip
from .grader import run_playbook, score_of
from .pve import client
from .questions import Question, get

log = logging.getLogger("build")
_build_lock = asyncio.Lock()
building: set[str] = set()


class BuildError(Exception):
    pass


class _Log:
    def __init__(self, qid: str) -> None:
        self.qid = qid
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        log.info("%s: %s", self.qid, msg)
        self.lines.append(line)
        db.execute("UPDATE templates SET log=? WHERE qid=?", ("\n".join(self.lines)[-200_000:], self.qid))


def _free_vmid(lo: int, hi: int) -> int:
    used = set(client().resources().keys()) | {r["vmid"] for r in db.query("SELECT vmid FROM instances")}
    used |= {r["vmid"] for r in db.query("SELECT vmid FROM templates WHERE vmid IS NOT NULL")}
    for v in range(lo, hi + 1):
        if v not in used:
            return v
    raise BuildError("沒有可用的 VMID")


def _wait_ssh_sync(ip: str, timeout: int) -> None:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((ip, 22), 3) as s:
                if s.recv(4).startswith(b"SSH"):
                    return
        except OSError:
            pass
        time.sleep(1)
    raise BuildError(f"{ip} SSH 沒有回應")


def _resources_kw(q: Question) -> dict:
    r = q.resources
    kw = {"cores": int(r.get("cores", 1)), "memory": int(r.get("memory", 512))}
    if q.kind == "lxc":
        kw["swap"] = int(r.get("swap", 256))
    return kw


def build_sync(q: Question, selftest: bool = True) -> None:
    pve = client()
    out = _Log(q.qid)
    old = db.one("SELECT * FROM templates WHERE qid=?", (q.qid,))
    if old is None:
        db.execute("INSERT INTO templates(qid,status) VALUES(?, 'building')", (q.qid,))
    else:
        db.execute("UPDATE templates SET status='building', log='' WHERE qid=?", (q.qid,))
    base = cfg.base_template_vmid if q.kind == "lxc" else cfg.base_vm_template_vmid
    vmid = _free_vmid(cfg.template_vmid_min, cfg.template_vmid_max)
    ip = build_ip(vmid)
    try:
        if q.errors:
            raise BuildError("題目格式錯誤：" + "；".join(q.errors))
        out(f"從 base {base} 完整複製為 {vmid}（{q.kind}）")
        pve.full_clone(q.kind, base, vmid, f"tmpl-{q.qid}"[:63], f"lsa template q={q.qid} v={q.build_version}")
        pve.set_network(q.kind, vmid, ip, gateway=cfg.build_gateway, nameserver=cfg.build_dns)
        pve.set_config(q.kind, vmid, tags=f"lsa-template;q-{q.qid}", **_resources_kw(q))
        if q.kind == "lxc":
            pve.set_config(q.kind, vmid, hostname=q.hostname)
        else:
            # VM：cloud-init 設定主機名稱；額外磁碟（例如 fstab 題）
            pve.set_config(q.kind, vmid, ciupgrade=0)
            for i, size in enumerate(q.resources.get("extra_disks") or [], start=1):
                out(f"加入額外磁碟 scsi{i} {size}G")
                pve.set_config(q.kind, vmid, **{f"scsi{i}": f"{cfg.pve_storage}:{int(size)}"})
        out(f"啟動 {vmid}，IP {ip}")
        pve.start(q.kind, vmid)
        if cfg.mock:
            out("（mock 模式：略過 setup.yml）")
        else:
            _wait_ssh_sync(ip, 240 if q.kind == "qemu" else 90)
            out("執行 setup.yml ...")
            rr = run_playbook(q.path, "setup.yml", ip, timeout=1800)
            out(rr.stdout)
            if not rr.success:
                raise BuildError(f"setup.yml 執行失敗（{rr.status}）")
        out("關機並轉為範本")
        pve.shutdown(q.kind, vmid)
        # 範本不留對外網路設定；作答者實例 clone 時會重設 net0
        pve.set_network(q.kind, vmid, ip)
        pve.to_template(q.kind, vmid)
    except Exception as e:
        out(f"建置失敗：{e}")
        try:
            pve.destroy(q.kind, vmid)
        except Exception:  # noqa: BLE001
            pass
        if old and old.get("vmid"):  # 舊範本仍可用
            db.execute("UPDATE templates SET status='ready' WHERE qid=?", (q.qid,))
            out("保留舊版範本")
        else:
            db.execute("UPDATE templates SET status='failed' WHERE qid=?", (q.qid,))
        raise
    db.execute(
        "UPDATE templates SET vmid=?, kind=?, version=?, status='ready', built_at=?, selftest_json=NULL "
        "WHERE qid=?",
        (vmid, q.kind, q.build_version, datetime.now().isoformat(timespec="seconds"), q.qid))
    out(f"範本完成：{vmid}")
    if old and old.get("vmid") and old["vmid"] != vmid:
        try:
            pve.destroy(old.get("kind") or q.kind, old["vmid"])
            out(f"已刪除舊範本 {old['vmid']}")
        except Exception as e:  # noqa: BLE001 - 還有作答者實例 linked clone 在用
            out(f"舊範本 {old['vmid']} 無法刪除（可能仍有作答者的實例使用）：{e}")
    if selftest:
        selftest_sync(q, out)


def selftest_sync(q: Question, out: _Log | None = None) -> dict:
    """從範本開一個測試實例：未修正時應 0 分；跑 solution.yml 後應滿分"""
    out = out or _Log(q.qid)
    pve = client()
    tmpl = db.one("SELECT * FROM templates WHERE qid=? AND status='ready'", (q.qid,))
    if not tmpl:
        raise BuildError("範本尚未建置")
    vmid = _free_vmid(cfg.vmid_max - 200, cfg.vmid_max)  # 自我測試用尾端 VMID
    ip = instance_ip(vmid)
    result: dict = {"at": datetime.now().isoformat(timespec="seconds")}
    if cfg.mock:   # 不連 PVE、不跑 Ansible：假裝通過，讓本機開發與測試可以走完整流程
        s0, total, items0 = score_of(q, {})
        result.update(broken_score=0.0, total=total, broken_items=items0, broken_status="ok",
                      solved_score=total, solved_items=[dict(i, passed=True) for i in items0], solved_status="ok",
                      ok=True, mock=True)
        out("（mock 模式：自我測試略過，視為通過）")
        db.execute("UPDATE templates SET selftest_json=? WHERE qid=?", (json.dumps(result, ensure_ascii=False), q.qid))
        return result
    try:
        out(f"自我測試：clone {tmpl['vmid']} -> {vmid}")
        pve.clone(q.kind, tmpl["vmid"], vmid, q.hostname, f"lsa selftest q={q.qid}")
        pve.set_network(q.kind, vmid, ip)
        pve.start(q.kind, vmid)
        ssh_ok = True
        try:
            _wait_ssh_sync(ip, 240 if q.kind == "qemu" else 90)
        except BuildError:
            if q.terminal_default != "console":
                raise
            ssh_ok = False      # terminal: console／console-only 的題目：初始狀態本來就可能 SSH 不通
        if ssh_ok:
            rr = run_playbook(q.path, "check.yml", ip)
            s0, total, items0 = score_of(q, rr.results)
            status0 = rr.status
        else:
            s0, total, items0 = score_of(q, {})
            status0 = "unreachable"
            out("初始狀態 SSH 不通（terminal: console／console-only）：未修正狀態以 0 分計")
        result.update(broken_score=s0, total=total, broken_items=items0, broken_status=status0)
        out(f"未修正狀態得分：{s0}/{total}（{status0}）")
        if s0 > 0:
            out("⚠ 未修正時就有得分，請確認 check.yml 是否正確")
        if q.has_solution and not ssh_ok:
            result["partial"] = True    # 只驗證了「未修正為 0 分」；解答要出題者用主控台手動驗證
            result["note"] = "初始狀態 SSH 不通，無法自動套用 solution.yml；請用主控台手動驗證解答"
            out("⚠ " + result["note"])
        elif q.has_solution:
            sr = run_playbook(q.path, "solution.yml", ip, timeout=900)
            out(sr.stdout)
            rr = run_playbook(q.path, "check.yml", ip)
            s1, _, items1 = score_of(q, rr.results)
            result.update(solved_score=s1, solved_items=items1, solved_status=rr.status)
            out(f"套用 solution 後得分：{s1}/{total}")
            if s1 < total:
                out("⚠ 套用解答後未達滿分，請確認 check.yml / solution.yml")
        result["ok"] = s0 == 0 and (not q.has_solution or not ssh_ok or result.get("solved_score") == total)
    except Exception as e:
        result.update(ok=False, error=str(e))
        out(f"自我測試失敗：{e}")
    finally:
        try:
            pve.destroy(q.kind, vmid)
        except Exception:  # noqa: BLE001
            out(f"無法刪除測試實例 {vmid}")
    db.execute("UPDATE templates SET selftest_json=? WHERE qid=?", (json.dumps(result, ensure_ascii=False), q.qid))
    return result


async def build(qid: str, selftest: bool = True) -> None:
    q = get(qid)
    if q is None:
        raise BuildError("題目不存在")
    building.add(qid)
    try:
        async with _build_lock:
            await asyncio.to_thread(build_sync, q, selftest)
    finally:
        building.discard(qid)


async def selftest(qid: str) -> None:
    q = get(qid)
    if q is None:
        raise BuildError("題目不存在")
    building.add(qid)
    try:
        async with _build_lock:
            await asyncio.to_thread(selftest_sync, q)
    finally:
        building.discard(qid)


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.init()
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--selftest":
        q = get(argv[1])
        print(json.dumps(selftest_sync(q), ensure_ascii=False, indent=2))
        return 0
    q = get(argv[0])
    if q is None:
        print("題目不存在")
        return 1
    build_sync(q, selftest="--no-selftest" not in argv)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
