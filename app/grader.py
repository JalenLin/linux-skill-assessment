"""用 ansible-runner 執行 check.yml 並計分。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db, instances
from .config import cfg
from .i18n import _
from .questions import Check, Question

log = logging.getLogger("grader")
# ansible-runner 需要在 PATH 找到 ansible-playbook（venv 未 activate 時）
_bindir = os.path.dirname(sys.executable)
if _bindir not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _bindir + os.pathsep + os.environ.get("PATH", "")
TASK_RE = re.compile(r"^check:([A-Za-z0-9_.-]+)")
_sem: asyncio.Semaphore | None = None


class GradeError(Exception):
    pass


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(cfg.grader_concurrency)
    return _sem


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RunResult:
    results: dict[str, bool]   # {check_id: passed}
    status: str                # ok / unreachable / failed / timeout
    stdout: str
    success: bool              # playbook 整體是否成功（setup / solution 用）


def _host(ip: str) -> dict:
    return {
        "ansible_host": ip,
        "ansible_user": "root",
        "ansible_ssh_private_key_file": cfg.ssh_key,
        "ansible_python_interpreter": "/usr/bin/python3",
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                                   "-o ConnectTimeout=10",
        "target_ip": ip,
    }


def inventory_for(target: str | list[dict]) -> dict:
    """單機題：一台 host「target」。多節點題：target 是 [{name, ip, nics}]，host 名稱就是節點名稱；
    每台另有 node_ip（第一個題目網路的 IP），全部節點的資料放在 lsa_nodes"""
    if isinstance(target, str):
        return {"all": {"hosts": {"target": _host(target)}}}
    nodes = {n["name"]: {"mgmt_ip": n["ip"], "ips": {k: v.split("/")[0] for k, v in n["nics"].items() if v}}
             for n in target}
    hosts = {}
    for n in target:
        ips = nodes[n["name"]]["ips"]
        hosts[n["name"]] = {**_host(n["ip"]), "node_ip": next(iter(ips.values()), n["ip"])}
    return {"all": {"hosts": hosts, "vars": {"lsa_nodes": nodes}}}


def run_playbook(qdir: Path, playbook: str, target: str | list[dict], timeout: int | None = None) -> RunResult:
    """同步執行 playbook；target 見 inventory_for"""
    import ansible_runner

    tmp = Path(tempfile.mkdtemp(prefix="lsa-run-"))
    try:
        inventory = inventory_for(target)
        r = ansible_runner.run(
            private_data_dir=str(tmp),
            project_dir=str(qdir),
            playbook=playbook,
            inventory=inventory,
            quiet=True,
            timeout=timeout or cfg.check_timeout,
            envvars={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "ANSIBLE_HOST_KEY_CHECKING": "False",
                "ANSIBLE_PIPELINING": "True",
                "ANSIBLE_NOCOLOR": "True",
                "ANSIBLE_RETRY_FILES_ENABLED": "False",
                "ANSIBLE_DEPRECATION_WARNINGS": "False",
                "ANSIBLE_LOCALHOST_WARNING": "False",
            },
        )
        results: dict[str, bool] = {}
        unreachable = False
        for ev in r.events:
            kind = ev.get("event")
            data = ev.get("event_data") or {}
            if kind == "runner_on_unreachable":
                unreachable = True
            m = TASK_RE.match(str(data.get("task", "")))
            if not m:
                continue
            cid = m.group(1)
            if kind == "runner_on_ok":
                results.setdefault(cid, True)
            elif kind in ("runner_on_failed", "runner_on_skipped", "runner_on_unreachable"):
                results[cid] = False
        stdout = r.stdout.read() if r.stdout else ""
        if r.status == "timeout":
            status = "timeout"
        elif unreachable:
            status = "unreachable"
        elif r.status == "successful" or results:
            status = "ok"
        else:
            status = "failed"
        return RunResult(results, status, stdout[-100_000:], r.status == "successful")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def score_of(q: Question, results: dict[str, bool], checks: list[Check] | None = None
             ) -> tuple[float, float, list[dict]]:
    """checks：題組內的配分（sets.effective_checks）；None 用 question.yaml 預設配分"""
    items, score, total = [], 0.0, 0.0
    for c in (checks if checks is not None else q.checks):
        passed = bool(results.get(c.id))
        total += c.points
        score += c.points if passed else 0
        items.append({"id": c.id, "desc": c.desc, "points": c.points, "passed": passed})
    return score, total, items


def last_run(account_id: int, sid: int, qid: str) -> dict | None:
    return db.one("SELECT * FROM check_runs WHERE account_id=? AND set_id=? AND qid=? ORDER BY id DESC LIMIT 1",
                  (account_id, sid, qid))


def can_check(account_id: int, s: dict, qid: str) -> str | None:
    """回傳不能檢查的原因；None 表示可以"""
    last = last_run(account_id, s["id"], qid)
    if not last:
        return None
    if last["status"] in ("queued", "running"):
        return _("上一次檢查還在進行中")
    cooldown = int(s["cooldown_sec"] or 0)
    elapsed = (datetime.now() - datetime.fromisoformat(last["started_at"])).total_seconds()
    if elapsed < cooldown:
        return _("請等待 {n} 秒後再檢查", n=int(cooldown - elapsed) + 1)
    return None


def enqueue(account: dict, sid: int, q: Question, final: bool = False) -> int:
    run_id = db.execute(
        "INSERT INTO check_runs(account_id,set_id,qid,status,is_final,started_at) VALUES(?,?,?,?,?,?)",
        (account["id"], sid, q.qid, "queued", int(final), _now()))
    asyncio.get_running_loop().create_task(_execute(run_id, account, sid, q, final))
    return run_id


async def _execute(run_id: int, account: dict, sid: int, q: Question, final: bool) -> None:
    from .sets import effective_checks
    checks = effective_checks(q, sid)

    def finish(status, msg, score=None, total=None, items=None, out=""):
        db.execute(
            "UPDATE check_runs SET status=?, message=?, score=?, max_score=?, results_json=?, log=?, "
            "finished_at=? WHERE id=?",
            (status, msg, score, total, json.dumps(items, ensure_ascii=False) if items is not None else None,
             out, _now(), run_id))

    try:
        async with _semaphore():
            db.execute("UPDATE check_runs SET status='running' WHERE id=?", (run_id,))
            inst = instances.get(account["id"], sid, q.qid)
            if inst is None:
                score, total, items = score_of(q, {}, checks)
                finish("done", "尚未開啟此題環境", score, total, items)
                return
            was_running = inst["status"] == "running"
            try:
                inst = await instances.ensure_running(account, sid, q, enforce_limit=not final)
            except instances.InstanceError as e:
                score, total, items = score_of(q, {}, checks)
                finish("done", f"無法連線到環境：{e}", score, total, items)
                return
            graders = await instances.start_graders(inst, q)   # mock 模式也走這段（MockPVE）
            try:
                if cfg.mock:
                    await asyncio.sleep(0.3)
                    from .pve import client
                    rr = RunResult(client().mock_results(inst["vmid"], [c.id for c in q.checks]), "ok", "(mock)", True)
                else:
                    target = instances.playbook_target(inst, q)
                    rr = await asyncio.to_thread(run_playbook, q.path, "check.yml", target)
                    if rr.status == "unreachable":
                        # 主機忙碌時（大量 VM 同時開機/重開）可能暫時連不上：等一下再完整重跑一次，避免誤判 0 分
                        log.warning("check unreachable, retrying: %s %s", account["username"], q.qid)
                        await asyncio.sleep(30)
                        rr = await asyncio.to_thread(run_playbook, q.path, "check.yml", target)
            finally:
                if graders:   # 檢查完就關掉，作答時看不到 grader
                    asyncio.get_running_loop().create_task(instances.stop_graders(graders))
            results, status, out = rr.results, rr.status, rr.stdout
            if final and not was_running:
                # 正常關機要幾秒～幾十秒：放到背景做，不佔檢查名額
                asyncio.get_running_loop().create_task(instances.stop_many([inst]))
            score, total, items = score_of(q, results, checks)
            msg = {
                "ok": "檢查完成",
                "unreachable": ("評分程式無法登入部分機器（root SSH 被修改？）—那幾台的項目以 0 分計，可重置本題"
                                if q.is_group else "評分程式無法登入環境（root SSH 被修改？）—以 0 分計，可重置本題"),
                "timeout": "檢查逾時",
                "failed": "檢查程式執行失敗",
            }[status]
            finish("done", msg, score, total, items, out)
    except Exception as e:  # noqa: BLE001
        log.exception("grading failed")
        finish("error", f"系統錯誤：{e}")


# ---------------- 成績 ----------------
def _pick(s: dict, q: Question, checks: list[Check], runs: list[dict]) -> float | None:
    """依題組計分政策，從該生該題的檢查紀錄（依 id 排序、status=done）算出得分"""
    from .sets import entry
    policy = s["score_policy"]
    if policy == "final":
        if entry(s["id"], q.qid)["time_limit_min"] > 0:
            # 限時題目時間到就重置環境，沒有「測驗結束後最終校驗」，改取作答時間內的最高分
            policy = "best"
        else:
            runs = [r for r in runs if r["is_final"]]
    if not runs:
        return None

    # 配分可能事後調整：用 results_json 依目前配分重新計算
    def rescore(r):
        items = json.loads(r["results_json"] or "[]")
        return score_of(q, {i["id"]: i["passed"] for i in items}, checks)[0]

    scores = [rescore(r) for r in runs]
    return max(scores) if policy == "best" else scores[-1]


def question_score(account_id: int, s: dict, q: Question) -> tuple[float | None, float]:
    """依題組的計分政策回傳 (得分, 滿分)。未檢查過回傳 (None, 滿分)。"""
    from .sets import effective_checks
    checks = effective_checks(q, s["id"])
    runs = db.query(
        "SELECT * FROM check_runs WHERE account_id=? AND set_id=? AND qid=? AND status='done' ORDER BY id",
        (account_id, s["id"], q.qid))
    return _pick(s, q, checks, runs), sum(c.points for c in checks)


def set_scores(s: dict, qs: list[Question]) -> tuple[dict[tuple[int, str], float | None], dict[str, float]]:
    """整個題組一次算：回傳 ({(account_id, qid): 得分}, {qid: 滿分})"""
    from collections import defaultdict
    from .sets import effective_checks
    checks = {q.qid: effective_checks(q, s["id"]) for q in qs}
    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in db.query("SELECT * FROM check_runs WHERE set_id=? AND status='done' ORDER BY id", (s["id"],)):
        grouped[(r["account_id"], r["qid"])].append(r)
    qmap = {q.qid: q for q in qs}
    scores = {k: _pick(s, qmap[k[1]], checks[k[1]], v) for k, v in grouped.items() if k[1] in qmap}
    return scores, {qid: sum(c.points for c in cs) for qid, cs in checks.items()}
