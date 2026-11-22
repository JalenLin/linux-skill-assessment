"""題組：測驗 / 練習。

每個題組有自己的題目、配分、開放時間與作答規則；作答者的環境、檢查紀錄、限時作答都分題組記錄，
同一題放在練習和測驗裡是兩個獨立的環境與成績。

- 測驗（assessment）：必須設定開始/結束時間。結束後終端機斷線、環境停止，可做「最終校驗」。
- 練習（practice）：開始/結束時間可留空（留空 = 立即開放 / 不結束）。
  任何一場測驗進行中時，練習題組暫停開放（避免測驗中用練習環境或看練習的檢查細項）。
"""
from __future__ import annotations

import json
from datetime import datetime

from . import db, questions
from .i18n import _
from .questions import Check, Question

MODES = {"assessment": "測驗", "practice": "練習"}
STATE_LABEL = {"unset": "未設定時間", "before": "尚未開始", "open": "開放中", "closed": "已結束",
               "locked": "測驗進行中暫停", "hidden": "未公開"}


def _parse(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def get(sid: int) -> dict | None:
    return db.one("SELECT * FROM qsets WHERE id=?", (sid,))


def all_sets() -> list[dict]:
    return db.query("SELECT * FROM qsets ORDER BY mode='practice', sort_order, id")


def window(s: dict) -> tuple[datetime | None, datetime | None]:
    return _parse(s["start_at"]), _parse(s["end_at"])


def state(s: dict, now: datetime | None = None) -> str:
    """unset / before / open / closed（只看時間）"""
    now = now or datetime.now()
    start, end = window(s)
    if s["mode"] == "assessment" and (not start or not end):
        return "unset"
    if start and now < start:
        return "before"
    if end and now >= end:
        return "closed"
    return "open"


def seconds_left(s: dict) -> int | None:
    _, end = window(s)
    if not end:
        return None
    return max(0, int((end - datetime.now()).total_seconds()))


def open_assessment() -> dict | None:
    """目前進行中的測驗（同時有多場時取第一場）"""
    for s in all_sets():
        if s["mode"] == "assessment" and s["visible"] and state(s) == "open":
            return s
    return None


def participant_state(s: dict) -> str:
    """作答者看到的狀態：hidden / locked / unset / before / open / closed"""
    if not s["visible"]:
        return "hidden"
    st = state(s)
    if st == "open" and s["mode"] == "practice" and open_assessment():
        return "locked"
    return st


def can_work(s: dict) -> bool:
    return participant_state(s) == "open"


def blocked_reason(s: dict) -> str | None:
    st = participant_state(s)
    return None if st == "open" else _({
        "hidden": "題組未開放", "locked": "測驗進行中，練習暫停開放", "unset": "題組尚未設定時間",
        "before": "尚未開始", "closed": "開放時間已結束"}[st])


def visible_sets() -> list[dict]:
    return [s for s in all_sets() if s["visible"]]


# ---------------- 題組內的題目 ----------------
def entry(sid: int, qid: str) -> dict:
    row = db.one("SELECT * FROM set_questions WHERE set_id=? AND qid=?", (sid, qid))
    if row is None:
        return {"set_id": sid, "qid": qid, "enabled": 0, "sort_order": 0, "points": {},
                "time_limit_min": 0, "max_attempts": 0}
    row["points"] = json.loads(row.pop("points_json") or "{}")
    return row


def effective_checks(q: Question, sid: int) -> list[Check]:
    pts = entry(sid, q.qid)["points"]
    return [Check(c.id, c.desc, float(pts.get(c.id, c.points))) for c in q.checks]


def questions_of(sid: int) -> list[Question]:
    qs, _ = questions.load_all()
    rows = db.query("SELECT qid FROM set_questions WHERE set_id=? AND enabled=1 ORDER BY sort_order, qid", (sid,))
    return [qs[r["qid"]] for r in rows if r["qid"] in qs]


def has_question(sid: int, qid: str) -> bool:
    return bool(db.one("SELECT 1 FROM set_questions WHERE set_id=? AND qid=? AND enabled=1", (sid, qid)))


# ---------------- 出席 ----------------
def attendance(s: dict) -> dict[int, dict]:
    """題組開放時間內的作答者活動：{account_id: {first, last, ip, n_ip, n_checks}}
    活動 = 進入題組、開題、連終端機、按檢查（登入本身不算，因為不知道是哪個題組）。"""
    start, end = s["start_at"] or "0000", s["end_at"] or "9999"
    rows = db.query(
        "SELECT account_id, at, ip, 'act' src FROM activity WHERE set_id=? AND at>=? AND at<? "
        "UNION ALL SELECT account_id, started_at, NULL, 'check' FROM check_runs WHERE set_id=? AND is_final=0 "
        "AND started_at>=? AND started_at<? ORDER BY 2", (s["id"], start, end, s["id"], start, end))
    out: dict[int, dict] = {}
    for r in rows:
        a = out.setdefault(r["account_id"], {"first": r["at"], "last": r["at"], "ip": None, "ips": set(),
                                             "n_checks": 0})
        a["last"] = r["at"]
        if r["src"] == "check":
            a["n_checks"] += 1
        elif r["ip"]:
            a["ip"] = a["ip"] or r["ip"]
            a["ips"].add(r["ip"])
    for a in out.values():
        a["n_ip"] = len(a.pop("ips"))
    return out


def late_minutes(s: dict, first: str) -> int | None:
    """首次活動比題組開始晚幾分鐘（沒有開始時間回傳 None）"""
    start = _parse(s["start_at"])
    if not start:
        return None
    return max(0, int((datetime.fromisoformat(first) - start).total_seconds() // 60))
