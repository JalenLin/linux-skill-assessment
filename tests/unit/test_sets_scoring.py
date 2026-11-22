"""題組狀態、配分、計分政策、出席"""
import json
from datetime import datetime, timedelta

import pytest

from app import grader, questions, sets
from conftest import EXAMPLE

FMT = "%Y-%m-%dT%H:%M"


def _t(**kw):
    return (datetime.now() + timedelta(**kw)).strftime(FMT)


@pytest.fixture
def mkset(appdb):
    made = []

    def make(mode="practice", start="", end="", visible=1, policy="best", **kw):
        sid = appdb.execute("INSERT INTO qsets(name,mode,visible,start_at,end_at,score_policy) VALUES(?,?,?,?,?,?)",
                            (kw.get("name", "t"), mode, visible, start, end, policy))
        made.append(sid)
        return sets.get(sid)
    yield make
    for sid in made:
        appdb.execute("DELETE FROM qsets WHERE id=?", (sid,))
        appdb.execute("DELETE FROM set_questions WHERE set_id=?", (sid,))
        appdb.execute("DELETE FROM activity WHERE set_id=?", (sid,))
        appdb.execute("DELETE FROM check_runs WHERE set_id=?", (sid,))


def test_state_by_time(mkset):
    assert sets.state(mkset("assessment")) == "unset"                     # 測驗一定要有時間
    assert sets.state(mkset("practice")) == "open"                        # 練習不設時間 = 一直開放
    assert sets.state(mkset("assessment", _t(hours=1), _t(hours=2))) == "before"
    assert sets.state(mkset("assessment", _t(hours=-1), _t(hours=1))) == "open"
    assert sets.state(mkset("assessment", _t(hours=-2), _t(hours=-1))) == "closed"
    assert sets.state(mkset("practice", "", _t(minutes=-1))) == "closed"


def test_practice_locked_during_assessment(mkset):
    p = mkset("practice")
    assert sets.participant_state(p) == "open"
    hidden_a = mkset("assessment", _t(hours=-1), _t(hours=1), visible=0)
    assert sets.participant_state(p) == "open"                            # 未公開的測驗不影響
    a = mkset("assessment", _t(hours=-1), _t(hours=1))
    assert sets.open_assessment()["id"] == a["id"]
    assert sets.participant_state(p) == "locked"
    assert not sets.can_work(p)
    assert sets.blocked_reason(p) == "測驗進行中，練習暫停開放"
    assert sets.participant_state(hidden_a) == "hidden"


def test_effective_checks_override_points(appdb, mkset):
    q = questions.load_one(EXAMPLE)
    s = mkset()
    appdb.execute("INSERT INTO set_questions(set_id,qid,points_json) VALUES(?,?,?)",
                  (s["id"], q.qid, json.dumps({"page": 50})))
    pts = {c.id: c.points for c in sets.effective_checks(q, s["id"])}
    assert pts == {"page": 50.0, "boot": 2.0, "restart": 2.0}


def test_score_of():
    q = questions.load_one(EXAMPLE)
    score, total, items = grader.score_of(q, {"page": True, "boot": False})
    assert (score, total) == (6.0, 10.0)
    assert [i["passed"] for i in items] == [True, False, False]


def _run(q, passed: set, final=0):
    _, _, items = grader.score_of(q, {c.id: c.id in passed for c in q.checks})
    return {"results_json": json.dumps(items), "is_final": final}


@pytest.mark.parametrize("policy,expected", [("best", 10.0), ("latest", 6.0), ("final", 2.0)])
def test_score_policies(appdb, mkset, policy, expected):
    q = questions.load_one(EXAMPLE)
    s = mkset(policy=policy)
    appdb.execute("INSERT INTO set_questions(set_id,qid) VALUES(?,?)", (s["id"], q.qid))
    runs = [_run(q, {"page"}), _run(q, {"page", "boot", "restart"}), _run(q, {"boot"}, final=1), _run(q, {"page"})]
    assert grader._pick(s, q, q.checks, runs) == expected


def test_final_policy_without_final_run_is_none(appdb, mkset):
    q = questions.load_one(EXAMPLE)
    s = mkset(policy="final")
    appdb.execute("INSERT INTO set_questions(set_id,qid) VALUES(?,?)", (s["id"], q.qid))
    assert grader._pick(s, q, q.checks, [_run(q, {"page"})]) is None


def test_points_changed_afterwards_are_rescored(appdb, mkset):
    """配分事後調整：舊的檢查紀錄依新配分重算"""
    q = questions.load_one(EXAMPLE)
    s = mkset()
    appdb.execute("INSERT INTO set_questions(set_id,qid,points_json) VALUES(?,?,?)",
                  (s["id"], q.qid, json.dumps({"page": 1, "boot": 1, "restart": 1})))
    checks = sets.effective_checks(q, s["id"])
    assert grader._pick(s, q, checks, [_run(q, {"page", "boot"})]) == 2.0


def test_attendance_counts_activity_in_window(appdb, mkset):
    s = mkset("practice", _t(hours=-1), _t(hours=1))
    aid = appdb.execute("INSERT INTO accounts(username,password_hash,participant_code) VALUES('att1','x','ATT001')")
    try:
        now = datetime.now()
        appdb.execute("INSERT INTO activity(account_id,set_id,kind,ip,at) VALUES(?,?,?,?,?)",
                      (aid, s["id"], "view", "10.0.0.1", (now - timedelta(minutes=30)).isoformat(timespec="seconds")))
        appdb.execute("INSERT INTO activity(account_id,set_id,kind,ip,at) VALUES(?,?,?,?,?)",
                      (aid, s["id"], "start", "10.0.0.2", (now - timedelta(minutes=20)).isoformat(timespec="seconds")))
        att = sets.attendance(s)
        assert aid in att
        assert att[aid]["n_ip"] == 2
        assert sets.late_minutes(s, att[aid]["first"]) in (29, 30)
    finally:
        appdb.execute("DELETE FROM activity WHERE account_id=?", (aid,))
        appdb.execute("DELETE FROM accounts WHERE id=?", (aid,))
