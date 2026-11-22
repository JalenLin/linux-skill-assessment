"""端對端測試：連一套真實部署（PVE＋網站），用範例題 example-hello 實際作答。

需要環境變數（沒設就整個略過）：
    LSA_E2E_URL              網站網址，例如 http://192.168.1.20
    LSA_E2E_ADMIN_PASSWORD   後台密碼
會建立 e2e 開頭的題組與帳號，結束後刪除。約 3~6 分鐘。
"""
from __future__ import annotations

import os
import re
import time

import pytest

from helpers import Admin, Participant

URL = os.environ.get("LSA_E2E_URL")
PW = os.environ.get("LSA_E2E_ADMIN_PASSWORD")
Q = "example-hello"
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not (URL and PW), reason="LSA_E2E_URL / LSA_E2E_ADMIN_PASSWORD 未設定")]

# 範例題的解法（作答者 user 可以 sudo）
FIX = ("sudo sed -i 's#/usr/bin/python #/usr/bin/python3 #' /etc/systemd/system/hello.service"
       " && sudo chmod 644 /srv/hello/index.html"
       " && sudo systemctl daemon-reload && sudo systemctl enable --now hello")


@pytest.fixture(scope="module")
def admin():
    a = Admin(URL.rstrip("/"), PW)
    j = a.get(f"/admin/questions/{Q}/status.json").json()
    if not (j["template"] and j["template"]["status"] == "ready" and j["selftest"] and j["selftest"]["ok"]):
        j = a.build(Q, timeout=900)
    assert j["template"]["status"] == "ready", j
    return a


@pytest.fixture(scope="module")
def env(admin):
    """兩個練習題組（都放範例題）＋一個作答者；結束後全部刪掉"""
    tag = f"e2e{int(time.time()) % 100000}"
    s1 = admin.create_set(f"{tag}-a", "practice")
    s2 = admin.create_set(f"{tag}-b", "practice")
    for s in (s1, s2):
        admin.add_question(s, Q)
    u, pw = next(iter(admin.generate(tag, 1).items()))
    p = Participant(admin.base, u, pw)
    p.code = tag.upper()
    assert p.bind(p.code).status_code == 303
    yield {"admin": admin, "p": p, "s1": s1, "s2": s2, "tag": tag, "user": u}
    for s in (s1, s2):
        admin.post(f"/admin/sets/{s}/destroy_instances", data={"confirm": "DELETE"})
    for _ in range(120):                                   # 等背景刪除完成
        if p.code not in admin.get("/admin/instances").text:
            break
        time.sleep(2)
    for s, name in ((s1, f"{tag}-a"), (s2, f"{tag}-b")):
        admin.post(f"/admin/sets/{s}/delete", data={"confirm": name})
    admin.post(f"/admin/accounts/{admin.account_id(u)}/delete")


def test_selftest_passed(admin):
    st = admin.get(f"/admin/questions/{Q}/status.json").json()["selftest"]
    assert st["ok"] and st["broken_score"] == 0 and st["solved_score"] == st["total"]


def test_solve_through_terminal(env):
    p, sid = env["p"], env["s1"]
    j = p.api(sid, Q, "start")
    assert j["ok"], j
    assert p.check(sid, Q)["score"] == 0                    # 還沒修：0 分
    term = p.terminal(sid, Q)
    try:
        assert "hello" in term.run("hostname")
        assert "inactive" in term.run("systemctl is-active hello") or "failed" in term.run("systemctl is-active hello")
        term.run(FIX, timeout=60)
        assert "Hello Lab" in term.run("sleep 1; curl -s http://localhost:8000/")
    finally:
        term.close()
    r = p.check(sid, Q)
    assert r["status"] == "done" and r["score"] == r["max_score"] == 10, r


def test_switch_pauses_and_keeps_files(env):
    """一次只開一題：開另一個題組的題目時，前一個正常關機暫停；切回來檔案還在（不需要 sync）"""
    admin, p, s1, s2 = env["admin"], env["p"], env["s1"], env["s2"]
    assert p.api(s1, Q, "start")["ok"]
    term = p.terminal(s1, Q)
    term.run("echo keep-me > ~/e2e-mark.txt")                # 故意不 sync
    assert p.api(s2, Q, "start")["ok"]
    out = term.read(timeout=30)
    assert '"paused"' in out, out[-300:]
    term.close()
    rows = [r for r in admin.get("/admin/instances").text.split("<tr>") if p.code in r]
    assert sum("running" in r for r in rows) == 1, rows

    assert p.api(s1, Q, "start")["ok"]                       # 切回來
    term = p.terminal(s1, Q)
    try:
        assert "keep-me" in term.run("cat ~/e2e-mark.txt")
        assert "Hello Lab" in term.run("curl -s http://localhost:8000/")   # 開機自動啟動的服務也回來了
    finally:
        term.close()


def test_participant_record_shows_terminal_log(env):
    admin, p = env["admin"], env["p"]
    html = admin.get(f"/admin/participant/{admin.account_id(env['user'])}").text
    assert "e2e-mark.txt" in html or re.search(r"shell_log/\d+/", html), "終端機紀錄應該出現在作答者頁"
