"""整合測試：mock 模式的完整網站（不連 PVE）。

mock 終端機輸入 `mock-pass [檢查id ...]` 會讓之後的檢查通過那些項目，用來模擬「作答」。
"""
from __future__ import annotations

import csv
import io
import re
import time

import pytest

from conftest import ADMIN_PASSWORD
from helpers import Admin, Participant, flash, t

Q = "example-hello"
Q2 = "hello-two"
CJK = re.compile(r"[一-鿿]")


def visible_text(html: str) -> str:
    """頁面上看得到的文字；語言選單裡的語言名稱（例如「繁體中文」）本來就用該語言顯示，不算"""
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html).replace("繁體中文", "")


@pytest.fixture(scope="module")
def admin(server):
    a = Admin(server, ADMIN_PASSWORD)
    for qid in (Q, Q2):
        j = a.build(qid, timeout=60)
        assert j["template"]["status"] == "ready" and j["selftest"]["ok"], j
    return a


_n = [0]


@pytest.fixture
def person(server, admin):
    """產生一個已綁定編號的作答者"""
    def make(prefix=None):
        _n[0] += 1
        prefix = prefix or f"t{_n[0]:02d}x"
        u, pw = next(iter(admin.generate(prefix, 1).items()))
        p = Participant(server, u, pw)
        p.username = u
        p.code = f"C{_n[0]:04d}"
        r = p.bind(p.code)
        assert r.status_code == 303, r.text
        return p
    return make


def _scores(admin, sid) -> dict[str, list[str]]:
    rows = list(csv.reader(io.StringIO(admin.get(f"/admin/scores.csv?set={sid}").text.lstrip("﻿"))))
    return {r[0]: r for r in rows[1:]}


# ---------------- 後台 ----------------
def test_admin_requires_login(server):
    import requests
    r = requests.get(server + "/admin", allow_redirects=False)
    assert r.status_code in (302, 303) and "/admin/login" in r.headers["location"]
    assert requests.post(server + "/admin/login", data={"username": "admin", "password": "nope"},
                         allow_redirects=False).status_code != 303


def test_overview_getting_started_before_any_set(admin):
    """還沒有題組時，成績總覽顯示「開始使用」步驟（不跳到題組頁）。必須在其他測試建立題組之前執行。"""
    r = admin.get("/admin", allow_redirects=False)
    assert r.status_code == 200
    assert "開始使用" in r.text and "已建置 2 / 2 題" in r.text and 'href="/admin/sets"' in r.text
    admin.s.cookies.set("lang", "en")
    try:
        r = admin.get("/admin")
        assert "Getting started" in r.text and "(2 / 2 questions built)" in r.text
        assert not CJK.findall(visible_text(r.text))
    finally:
        admin.s.cookies.set("lang", "zh-TW")


def test_admin_pages_render(admin):
    sid = admin.create_set("Pages", "practice")
    admin.add_question(sid, Q)
    pages = ["/admin", f"/admin?set={sid}", "/admin/sets", f"/admin/sets/{sid}", "/admin/summary",
             "/admin/accounts", "/admin/accounts/print", "/admin/instances", "/admin/settings",
             "/admin/questions", f"/admin/questions/{Q}", f"/admin/scores.csv?set={sid}", "/admin/summary.csv"]
    for lang in ("zh-TW", "en"):
        admin.s.cookies.set("lang", lang)
        for path in pages:
            r = admin.get(path)
            assert r.status_code == 200, (lang, path, r.status_code)
    admin.s.cookies.set("lang", "zh-TW")


def test_english_admin_has_no_untranslated_text(admin):
    """英文介面不應出現中文（使用者資料除外；題目頁會顯示原始檔案，不檢查）"""
    admin.s.cookies.set("lang", "en")
    try:
        for path in ["/admin", "/admin/sets", "/admin/summary", "/admin/accounts", "/admin/instances",
                     "/admin/settings", "/admin/questions"]:
            text = visible_text(admin.get(path).text)
            assert not CJK.findall(text), (path, sorted(set(re.findall(r"\S*[一-鿿]+\S*", text)))[:10])
    finally:
        admin.s.cookies.set("lang", "zh-TW")


def test_settings_save(admin):
    r = admin.post("/admin/settings", data={"site_title": "My Lab", "notice": "hi", "max_running": "1",
                                            "idle_stop_min": "30", "max_instances": "1000",
                                            "code_pattern": "^[A-Za-z0-9]{4,20}$", "default_lang": "zh-TW"})
    assert r.status_code == 303
    assert "My Lab" in admin.get("/admin/settings").text
    bad = admin.post("/admin/settings", data={"site_title": "My Lab", "code_pattern": "([", "max_running": "1",
                                              "idle_stop_min": "30", "max_instances": "1000"})
    assert flash(bad) == "編號格式正規表示式錯誤"
    admin.post("/admin/settings", data={"site_title": "Linux 實作能力檢驗系統", "notice": "", "max_running": "1",
                                        "idle_stop_min": "30", "max_instances": "1000",
                                        "code_pattern": "^[A-Za-z0-9]{4,20}$", "default_lang": "zh-TW"})


# ---------------- 帳號 ----------------
def test_csv_import_validates_codes(admin):
    data = ("username,password,code,note\n"
            "imp01,,IMP0001,ok\n"
            "imp02,,../evil,bad path\n"
            "imp03,,a/b,bad path\n"
            "imp04,,IMP0001,duplicate code\n"
            "imp01,,IMP0009,duplicate user\n"
            "imp05,secret12,,no code\n")
    r = admin.post("/admin/accounts/import", files={"file": ("a.csv", data.encode())})
    msg = flash(r)
    assert msg.startswith("已匯入 2 個帳號"), msg
    assert "../evil" in msg and "a/b" in msg and "IMP0001" in msg
    accs = admin.accounts()
    assert accs["imp01"]["code"] == "IMP0001"
    assert accs["imp05"]["password"] == "secret12" and accs["imp05"]["code"] == ""


def test_bind_flow(server, admin):
    u, pw = next(iter(admin.generate("bindt", 1).items()))
    p = Participant(server, u, pw)
    assert p.get("/", allow_redirects=False).headers["location"].endswith("/bind")
    assert "不一致" in p.post("/bind", data={"participant_code": "B0001", "participant_code2": "B0002"}).text
    assert "格式不正確" in p.bind("../x").text
    assert "格式不正確" in p.bind("ab").text                             # 太短
    admin.post("/admin/accounts/import", files={"file": ("a.csv", b"taken1,,TAKEN01,\n")})
    assert "已被其他帳號使用" in p.bind("TAKEN01").text
    assert p.bind("B0001").status_code == 303
    assert admin.accounts()[u]["code"] == "B0001"


def test_login_rejects_wrong_password(server, admin):
    import requests
    u = next(iter(admin.generate("wrongpw", 1)))
    r = requests.post(server + "/login", data={"username": u, "password": "nope"}, allow_redirects=False)
    assert r.status_code != 303


# ---------------- 練習 ----------------
def test_practice_flow(admin, person):
    sid = admin.create_set("Practice A", "practice")
    admin.add_question(sid, Q)
    p = person()
    page = p.get(f"/s/{sid}").text
    assert f"/s/{sid}/q/{Q}" in page and "練習不計入測驗成績" in page
    j = p.api(sid, Q, "start")
    assert j["ok"], j
    assert p.get(f"/s/{sid}/q/{Q}").status_code == 200
    assert p.get(f"/s/{sid}/q/{Q}/term").status_code == 200

    r = p.check(sid, Q)                                     # 還沒作答：0 分
    assert r["status"] == "done" and r["score"] == 0 and len(r["items"]) == 3

    term = p.terminal(sid, Q)
    assert "MOCK" in term.read(until="$ ")
    term.send("mock-pass page\r")
    term.read(until="$ ")
    r = p.check(sid, Q)
    assert r["score"] == 6 and [i["passed"] for i in r["items"]] == [True, False, False]
    term.send("mock-pass\r")
    term.read(until="$ ")
    term.close()
    assert p.check(sid, Q)["score"] == 10

    assert admin.get(f"/admin?set={sid}").status_code == 200   # 後台成績
    assert _scores(admin, sid)[p.code][-1] == "10"


def test_check_cooldown(admin, person):
    sid = admin.create_set("Cooldown", "practice", cooldown_sec="60")
    admin.add_question(sid, Q)
    p = person()
    assert p.api(sid, Q, "start")["ok"]
    assert p.api(sid, Q, "check")["ok"]
    time.sleep(1.5)
    j = p.api(sid, Q, "check")
    assert not j["ok"] and "秒" in j["error"]


def test_hidden_and_closed_sets(admin, person):
    hidden = admin.create_set("Hidden", "practice", visible=None)
    admin.add_question(hidden, Q)
    closed = admin.create_set("Closed", "practice", end_at=t(minutes=-1))
    admin.add_question(closed, Q)
    p = person()
    home = p.get("/").text
    assert "Hidden" not in home
    assert not p.api(hidden, Q, "start")["ok"]
    j = p.api(closed, Q, "start")
    assert not j["ok"] and j["error"] == "開放時間已結束"


def test_one_environment_at_a_time(admin, person):
    """每人同時只開 1 題：開新題時舊題暫停，舊終端機收到暫停訊息"""
    sid = admin.create_set("Switch", "practice")
    admin.add_question(sid, Q)
    admin.add_question(sid, Q2)
    p = person()
    assert p.api(sid, Q, "start")["ok"]
    term = p.terminal(sid, Q)
    term.read(until="$ ")
    assert p.api(sid, Q2, "start")["ok"]
    out = term.read(timeout=10)
    assert '"paused"' in out and "本題環境已暫停" in out
    term.close()
    rows = [r for r in admin.get("/admin/instances").text.split("<tr>") if p.code in r]
    assert len(rows) == 2 and sum("running" in r for r in rows) == 1      # 兩個環境，只有一個在跑


def test_terminal_recording_replay_and_cleanup(admin, person):
    """終端機紀錄：每次連線一個 asciicast 檔、後台重播與文字檢視、刪題組／刪帳號時一併刪除"""
    import json
    sid = admin.create_set("Recording", "practice", block_paste="on")
    admin.add_question(sid, Q)
    p = person()
    aid = admin.account_id(p.username)
    assert p.api(sid, Q, "start")["ok"]
    term = p.terminal(sid, Q)
    term.read(until="$ ")
    for line in ("ls -l /srv\r", "echo 這是測試\r"):            # 一個字一個字打（一次送很多字會被當成貼上）
        for ch in line:
            term.send(ch)
        term.read(until="$ ")
    term.send("cat /etc/passwd | grep root")                   # 一次送很多字 → 視為貼上，被擋
    term.read(timeout=2)
    term.close()
    time.sleep(0.5)

    page = admin.get(f"/admin/participant/{aid}").text
    names = re.findall(rf'/admin/recording/{aid}/(s{sid}-{Q}-[0-9-]+\.cast)', page)
    assert len(names) == 1 and "Recording" in page and "未檢查" in page
    name = names[0]

    time.sleep(1.1)                                             # 同一題再連一次＋檢查 → 同一組裡兩筆
    p.check(sid, Q)
    term = p.terminal(sid, Q)
    term.read(until="$ ")
    term.close()
    time.sleep(0.5)
    page = admin.get(f"/admin/participant/{aid}").text
    assert len(re.findall(rf'/admin/recording/{aid}/s{sid}-{Q}-', page)) == 2
    assert "2 次" in page and "0/10" in page and "檢查 1 次" in page

    cast = admin.get(f"/admin/shell_log/{aid}/{name}?view=cast")
    assert cast.status_code == 200 and "attachment" in cast.headers["content-disposition"]
    lines = cast.text.splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2 and header["width"] == 120 and header["height"] == 30
    events = [json.loads(x) for x in lines[1:]]
    kinds = {e[1] for e in events}
    assert {"o", "i", "m"} <= kinds
    assert all(events[i][0] <= events[i + 1][0] for i in range(len(events) - 1))
    assert "echo 這是測試\r" in "".join(e[2] for e in events if e[1] == "i")

    inp = admin.get(f"/admin/shell_log/{aid}/{name}?view=input").text
    assert "ls -l /srv ⏎" in inp and "echo 這是測試 ⏎" in inp and "PASTE BLOCKED: cat /etc/passwd" in inp
    assert "MOCK" in admin.get(f"/admin/shell_log/{aid}/{name}?view=output").text
    player = [json.loads(x) for x in admin.get(f"/admin/shell_log/{aid}/{name}?view=player").text.splitlines()[1:]]
    assert [e[2] for e in player if e[1] == "m"][:2] == ["ls -l /srv ⏎", "echo 這是測試 ⏎"]

    rp = admin.get(f"/admin/recording/{aid}/{name}")
    assert rp.status_code == 200 and "asciinema-player.min.js" in rp.text and "ls -l /srv" in rp.text
    assert admin.get("/static/vendor/asciinema-player.min.js").status_code == 200
    # 不合法的檔名
    assert admin.get(f"/admin/shell_log/{aid}/..%2F..%2Flsa.db").status_code == 404
    assert admin.get(f"/admin/shell_log/{aid}/x.log").status_code == 404

    admin.delete_set(sid, "Recording")                          # 刪題組 → 紀錄一起刪
    assert f"/admin/recording/{aid}/" not in admin.get(f"/admin/participant/{aid}").text
    assert admin.get(f"/admin/shell_log/{aid}/{name}?view=cast").status_code == 404

    sid2 = admin.create_set("Recording 2", "practice")          # 刪帳號 → 紀錄一起刪
    admin.add_question(sid2, Q)
    assert p.api(sid2, Q, "start")["ok"]
    term = p.terminal(sid2, Q)
    term.read(until="$ ")
    term.close()
    time.sleep(0.5)
    name2 = re.findall(rf'/admin/recording/{aid}/([^"]+\.cast)', admin.get(f"/admin/participant/{aid}").text)[0]
    admin.delete_set(sid2, "Recording 2")
    sid3 = admin.create_set("Recording 3", "practice")
    admin.add_question(sid3, Q)
    assert p.api(sid3, Q, "start")["ok"]
    term = p.terminal(sid3, Q)
    term.read(until="$ ")
    term.close()
    time.sleep(0.5)
    name3 = re.findall(rf'/admin/recording/{aid}/([^"]+\.cast)', admin.get(f"/admin/participant/{aid}").text)[0]
    assert name3 != name2
    admin.post(f"/admin/sets/{sid3}/destroy_instances", data={"confirm": "DELETE"})
    r = admin.post(f"/admin/accounts/{aid}/delete")
    assert r.status_code == 303
    assert admin.get(f"/admin/shell_log/{aid}/{name3}?view=cast").status_code == 404


# ---------------- 測驗 ----------------
def test_assessment_locks_practice_and_final_check(admin, person):
    prac = admin.create_set("Practice B", "practice")
    admin.add_question(prac, Q)
    asm = admin.create_set("Assessment X", "assessment", start_at=t(minutes=-5), end_at=t(minutes=30),
                           score_policy="final", show_detail=None)
    admin.add_question(asm, Q)
    p = person()
    try:
        assert p.get("/", allow_redirects=False).headers["location"] == f"/s/{asm}"     # 首頁直接進測驗
        j = p.api(prac, Q, "start")
        assert not j["ok"] and "測驗進行中" in j["error"]
        term = p.terminal(prac, Q)
        assert "測驗進行中" in term.read(timeout=5)
        term.close()

        assert p.api(asm, Q, "start")["ok"]
        term = p.terminal(asm, Q)
        term.read(until="$ ")
        term.send("mock-pass\r")
        term.read(until="$ ")
        term.close()
        r = p.check(asm, Q)
        assert r["score"] == 10
        assert "items" not in r or not r["items"]                                     # 關閉檢查細項
        assert "尚未結束" in flash(admin.post(f"/admin/sets/{asm}/final"))

        # 結束測驗 → 練習恢復；最終校驗以結束時的狀態計分
        admin.save_set(asm, name="Assessment X", mode="assessment", start_at=t(hours=-1), end_at=t(minutes=-1),
                       score_policy="final", show_detail=None)
        assert p.api(prac, Q, "start")["ok"]
        r = admin.post(f"/admin/sets/{asm}/final")
        assert "最終校驗" in flash(r)
        for _ in range(30):
            time.sleep(1)
            if _scores(admin, asm)[p.code][-1] == "10":
                break
        assert _scores(admin, asm)[p.code][-1] == "10"
    finally:
        admin.save_set(asm, name="Assessment X", mode="assessment", start_at=t(hours=-2), end_at=t(hours=-1))


def test_assessment_requires_times(admin):
    sid = admin.create_set("No times", "practice")
    r = admin.save_set(sid, name="No times", mode="assessment")
    assert flash(r) == "已儲存（測驗題組需要設定開始與結束時間，作答者才能作答）"
    assert "未設定時間" in admin.get("/admin/sets").text


def test_timed_question_attempts(admin, person):
    sid = admin.create_set("Timed", "practice")
    admin.add_question(sid, Q, time_limit_min=10, max_attempts=1)
    admin.add_question(sid, Q2)
    p = person()
    j = p.api(sid, Q, "start")
    assert not j["ok"] and j["error"].startswith("本題限時")              # 要按「開始作答」
    j = p.api(sid, Q, "start", params={"begin": "1"})
    assert j["ok"] and j["timing"]["active"] and 0 < j["timing"]["seconds_left"] <= 600
    assert p.api(sid, Q2, "start")["ok"]                                  # 改開別題 → 限時作答中斷
    time.sleep(1)
    j = p.api(sid, Q, "start", params={"begin": "1"})
    assert not j["ok"] and "已用完作答次數" in j["error"]


# ---------------- 出席 ----------------
def test_attendance_and_summary(admin, person):
    sid = admin.create_set("Week 1", "practice", start_at=t(minutes=-30), end_at=t(minutes=30))
    admin.add_question(sid, Q)
    present = person()
    person()                                                              # 沒有任何動作 → 缺席
    present.get(f"/s/{sid}")
    assert present.api(sid, Q, "start")["ok"]
    rows = list(csv.reader(io.StringIO(admin.get(f"/admin/summary.csv?sets={sid}").text.lstrip("﻿"))))
    marks = [r for r in rows[1:] if r[0].startswith("C")]
    assert any("出席" in r for r in marks) and any("缺" in r for r in marks)


# ---------------- 語言 ----------------
def test_language_switch_and_english_participant_pages(server, admin, person):
    sid = admin.create_set("English", "practice")
    admin.add_question(sid, Q)
    p = person()
    r = p.get(f"/lang/en?next=/s/{sid}", allow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/s/{sid}" and "lang=en" in r.headers["set-cookie"]
    assert p.api(sid, Q, "start")["ok"]
    for path in ["/", f"/s/{sid}", f"/s/{sid}/q/{Q}", f"/s/{sid}/q/{Q}/term"]:
        text = visible_text(p.get(path).text)
        assert not CJK.findall(text), (path, sorted(set(re.findall(r"\S*[一-鿿]+\S*", text)))[:10])
    assert "Check complete" in p.check(sid, Q)["message"]
    p.get("/lang/zh-TW?next=/")


# ---------------- 題庫 ----------------
def test_question_status_and_export(admin):
    j = admin.get(f"/admin/questions/{Q}/status.json").json()
    assert j["template"]["status"] == "ready" and j["selftest"]["ok"]
    r = admin.get(f"/admin/questions/{Q}/export.zip")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_tags_edit_filter_and_bulk(admin):
    """網頁改標籤會寫回 question.yaml（匯出 zip 帶著）；題組頁可以勾選批次納入／移出"""
    import zipfile
    page = admin.get("/admin/questions").text
    assert 'data-tag="systemd"' in page and 'class="qf-search"' in page      # 範例題本身的標籤

    r = admin.post(f"/admin/questions/{Q2}/tags", data={"tags": "network, 入門, Network"})
    assert r.status_code == 303 and f"#q-{Q2}" in r.headers["location"]
    assert "標籤已更新" in flash(r)
    page = admin.get("/admin/questions").text
    assert 'data-tag="入門"' in page and page.count('class="qf-tag" data-tag="network"') == 1
    z = zipfile.ZipFile(io.BytesIO(admin.get(f"/admin/questions/{Q2}/export.zip").content))
    yml = next(n for n in z.namelist() if n.endswith("question.yaml"))
    text = z.read(yml).decode()
    assert 'tags: ["network", "入門"]' in text and "# " in text                 # 註解還在
    assert admin.get(f"/admin/questions/{Q2}/status.json").json()["template"]["status"] == "ready"   # 不需要重建

    r = admin.post(f"/admin/questions/{Q2}/tags", data={"tags": "a, " + "z" * 31})
    assert "標籤未儲存" in flash(r)

    sid = admin.create_set("Bulk", "practice")
    r = admin.post(f"/admin/sets/{sid}/bulk", data={"action": "include", "qid": [Q, Q2]})
    assert "已納入 2 題" in flash(r)
    page = admin.get(f"/admin/sets/{sid}").text
    assert page.count('data-in="1"') == 2 and 'name="qid"' in page
    admin.add_question(sid, Q, time_limit_min=15)              # 單題設定
    r = admin.post(f"/admin/sets/{sid}/bulk", data={"action": "exclude", "qid": [Q]})
    assert "已移出 1 題" in flash(r)
    page = admin.get(f"/admin/sets/{sid}").text
    assert page.count('data-in="1"') == 1
    admin.post(f"/admin/sets/{sid}/bulk", data={"action": "include", "qid": [Q]})
    assert f'name="time_{Q}" min="0" value="15"' in admin.get(f"/admin/sets/{sid}").text   # 設定保留
    assert "請先勾選題目" in flash(admin.post(f"/admin/sets/{sid}/bulk", data={"action": "include"}))
    admin.post(f"/admin/questions/{Q2}/tags", data={"tags": ""})
    admin.delete_set(sid, "Bulk")


def test_set_page_progress_and_single_save(admin):
    """題組頁：上方進度清單反映狀態；題目表整張一次儲存（納入、排序、限時、次數、配分）"""
    sid = admin.create_set("Steps", "assessment", visible=None)
    page = admin.get(f"/admin/sets/{sid}").text
    assert "測驗需要設定開始與結束時間" in page and "還沒有納入題目" in page and "還沒勾選「作答者可見」" in page
    assert 'id="questions-form"' in page and page.count(">儲存題目設定</button>") == 1

    data = {"qid": [Q, Q2], f"inc_{Q}": "on", f"sort_{Q}": "2", f"time_{Q}": "20", f"att_{Q}": "3",
            f"pts_{Q}_page": "4", f"pts_{Q}_boot": "2", f"pts_{Q}_restart": "2",
            f"sort_{Q2}": "1", f"time_{Q2}": "0", f"att_{Q2}": "0"}
    r = admin.post(f"/admin/sets/{sid}/questions", data=data)
    assert "納入 1 題" in flash(r) and r.headers["location"].endswith("#questions")
    page = admin.get(f"/admin/sets/{sid}").text
    assert "已納入 1 題，共 8 分" in page
    assert f'name="time_{Q}" min="0" value="20"' in page and f'name="att_{Q}" min="0" value="3"' in page
    assert f'name="pts_{Q}_page" value="4"' in page
    assert f'tr data-q="{Q2}" data-in="0"' in page

    admin.save_set(sid, name="Steps", mode="assessment", start_at=t(minutes=60), end_at=t(minutes=120))
    page = admin.get(f"/admin/sets/{sid}").text
    assert "測驗需要設定開始與結束時間" not in page and "已建立 0 / " in page
    admin.delete_set(sid, "Steps")


def test_upload_bad_zip(admin):
    r = admin.post("/admin/questions/upload", files={"file": ("x.zip", b"not a zip", "application/zip")})
    assert "上傳失敗" in flash(r)
