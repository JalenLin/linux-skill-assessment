"""單題限時作答（類似 sadservers）。

- 後台每題可設定 time_limit_min（0 = 不限時）與 max_attempts（0 = 不限次數）
- 作答者按「開始作答」、環境就緒後才開始計時；截止時間不超過測驗結束時間
- 作答中「重置本題」不會重新計時
- 時間到：終端機斷線、等進行中的檢查跑完，然後刪除環境（重置）；時間內檢查過的成績保留
- 作答者離開：改開別題立即中斷；終端機全部斷線超過 ABANDON_GRACE_SEC（關掉頁面）也中斷。
  下次打開就是新的一次作答（重新計時、全新環境）
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from . import db, instances, questions, sets
from .config import cfg
from .i18n import _

log = logging.getLogger("attempts")
_expiring: set[int] = set()

ABANDON_GRACE_SEC = 60          # 終端機斷線多久視為離開（容許重新整理、短暫斷網）
Key = tuple[int, int, str]      # (account_id, set_id, qid)
_shells: dict[Key, int] = {}        # 連線中的終端機數
_last_seen: dict[Key, float] = {}   # 最後有連線的時間


def touch(aid: int, sid: int, qid: str) -> None:
    _last_seen[(aid, sid, qid)] = time.time()


def shell_opened(aid: int, sid: int, qid: str) -> None:
    k = (aid, sid, qid)
    _shells[k] = _shells.get(k, 0) + 1
    touch(*k)


def shell_closed(aid: int, sid: int, qid: str) -> None:
    k = (aid, sid, qid)
    _shells[k] = max(0, _shells.get(k, 0) - 1)
    touch(*k)


async def abandon_others(aid: int, sid: int, keep_qid: str) -> None:
    """作答者開了別題（含別的題組）：其他進行中的限時作答立即中斷"""
    for att in db.query("SELECT * FROM attempts WHERE account_id=? AND ended_at IS NULL "
                        "AND NOT (set_id=? AND qid=?)", (aid, sid, keep_qid)):
        asyncio.get_running_loop().create_task(expire(att, "switched"))


def _now() -> datetime:
    return datetime.now()


def limits(sid: int, qid: str) -> tuple[int, int]:
    e = sets.entry(sid, qid)
    return int(e.get("time_limit_min") or 0), int(e.get("max_attempts") or 0)


def is_timed(sid: int, qid: str) -> bool:
    return limits(sid, qid)[0] > 0


def active(aid: int, sid: int, qid: str) -> dict | None:
    return db.one("SELECT * FROM attempts WHERE account_id=? AND set_id=? AND qid=? AND ended_at IS NULL "
                  "ORDER BY id DESC LIMIT 1", (aid, sid, qid))


def used(aid: int, sid: int, qid: str) -> int:
    return db.one("SELECT COUNT(*) n FROM attempts WHERE account_id=? AND set_id=? AND qid=?",
                  (aid, sid, qid))["n"]


def seconds_left(att: dict) -> int:
    left = (datetime.fromisoformat(att["deadline"]) - _now()).total_seconds()
    s = sets.get(att["set_id"])
    set_left = sets.seconds_left(s) if s else 0
    if set_left is not None:
        left = min(left, set_left)
    return max(0, int(left))


def status(aid: int, sid: int, qid: str) -> dict:
    """給頁面用的計時狀態"""
    limit, max_att = limits(sid, qid)
    if limit <= 0:
        return {"timed": False}
    att = active(aid, sid, qid)
    n = used(aid, sid, qid)
    st = {"timed": True, "limit_min": limit, "max_attempts": max_att, "used": n,
          "active": False, "seconds_left": None, "can_start": True, "reason": ""}
    if att and seconds_left(att) > 0:
        st.update(active=True, seconds_left=seconds_left(att))
    elif max_att and n >= max_att:
        st.update(can_start=False, reason=_("已用完作答次數（{n}/{max}）", n=n, max=max_att))
    return st


def begin(aid: int, sid: int, qid: str) -> dict:
    limit, _ = limits(sid, qid)
    now = _now()
    deadline = now + timedelta(minutes=limit)
    att_id = db.execute(
        "INSERT INTO attempts(account_id,set_id,qid,started_at,deadline) VALUES(?,?,?,?,?)",
        (aid, sid, qid, now.isoformat(timespec="seconds"), deadline.isoformat(timespec="seconds")))
    log.info("attempt %s start account=%s set=%s q=%s deadline=%s", att_id, aid, sid, qid, deadline)
    touch(aid, sid, qid)
    return db.one("SELECT * FROM attempts WHERE id=?", (att_id,))


async def expire(att: dict, reason: str, reset: bool = True) -> None:
    """結束一次作答。reset=True 時等檢查跑完後刪除環境。"""
    if att["id"] in _expiring:
        return
    _expiring.add(att["id"])
    try:
        db.execute("UPDATE attempts SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL",
                   (_now().isoformat(timespec="seconds"), reason, att["id"]))
        log.info("attempt %s ended (%s)", att["id"], reason)
        if not reset:
            return
        # 等待在截止前送出的檢查跑完
        for _ in range(cfg.check_timeout + 60):
            busy = db.one("SELECT 1 FROM check_runs WHERE account_id=? AND set_id=? AND qid=? "
                          "AND status IN ('queued','running')", (att["account_id"], att["set_id"], att["qid"]))
            if not busy:
                break
            await asyncio.sleep(1)
        acc = db.one("SELECT * FROM accounts WHERE id=?", (att["account_id"],))
        q = questions.get(att["qid"])
        if acc and q:
            await instances.reset(acc, att["set_id"], q)
    finally:
        _expiring.discard(att["id"])


async def sweep() -> None:
    """背景定期呼叫：處理到期的作答"""
    loop = asyncio.get_running_loop()
    for att in db.query("SELECT * FROM attempts WHERE ended_at IS NULL"):
        s = sets.get(att["set_id"])
        st = sets.state(s) if s else "closed"
        if st == "closed" and s and s["mode"] == "assessment":
            # 測驗結束：只結束作答，實例由測驗結束流程統一停止（保留給管理者查看）
            await expire(att, "set_end", reset=False)
        elif st != "open" or not sets.can_work(s):
            loop.create_task(expire(att, "closed"))
        elif seconds_left(att) <= 0:
            loop.create_task(expire(att, "timeout"))
        else:
            k = (att["account_id"], att["set_id"], att["qid"])
            if _shells.get(k, 0) == 0:
                # 服務重啟後沒有記錄：從現在開始算寬限時間
                last = _last_seen.setdefault(k, time.time())
                if time.time() - last > ABANDON_GRACE_SEC:
                    loop.create_task(expire(att, "left"))
