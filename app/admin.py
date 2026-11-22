"""管理者後台。"""
from __future__ import annotations

import asyncio
import csv
import hmac
import io
import json
import logging
import re
import shutil
from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from . import attempts, build, db, grader, instances, questions, recording, sets
from .config import cfg
from .i18n import _
from .security import hash_password, random_password

log = logging.getLogger("admin")
router = APIRouter(prefix="/admin")
_tasks: set[asyncio.Task] = set()
job_status: dict[str, str] = {}   # 長時間批次工作的進度訊息


def _raw(msg: str, **kw) -> str:
    """背景工作進度：存原文（中文），顯示時才依觀看者語言翻譯（tr_stored）"""
    return msg.format(**kw) if kw else msg


def _tpl():
    from .main import templates
    return templates


def _render(request: Request, name: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("jobs", job_status)
    return _tpl().TemplateResponse(request, name, ctx, status_code=status_code)


def _back(url: str, msg: str = "") -> RedirectResponse:
    from urllib.parse import quote
    url, _sep, anchor = url.partition("#")
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (f"{sep}msg={quote(msg)}" if msg else "") + (f"#{anchor}" if anchor else ""),
                            status_code=303)


def _all_tags(qs) -> list[str]:
    """題庫裡所有標籤（不分大小寫去重複，依字母排序）"""
    seen: dict[str, str] = {}
    for q in qs:
        for t in q.tags:
            seen.setdefault(t.lower(), t)
    return sorted(seen.values(), key=str.lower)


def _spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def require_admin(request: Request) -> None:
    from .main import Redirect
    if not request.session.get("admin"):
        raise Redirect("/admin/login")


# ---------------- 登入 ----------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _render(request, "admin/login.html", {"error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == cfg.admin_user and hmac.compare_digest(password, cfg.admin_password):
        request.session.clear()
        request.session["admin"] = True
        return RedirectResponse("/admin", status_code=303)
    return _render(request, "admin/login.html", {"error": _("帳號或密碼錯誤")}, 401)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# ---------------- 總覽 ----------------
def _pick_set(request: Request) -> dict | None:
    sid = request.query_params.get("set")
    if sid and sid.isdigit() and (st := sets.get(int(sid))):
        return st
    all_ = sets.all_sets()
    return sets.open_assessment() or next((x for x in all_ if x["mode"] == "assessment"), None) or (all_[0] if all_ else None)


def _score_matrix(st: dict):
    qs = sets.questions_of(st["id"])
    accs = db.query("SELECT * FROM accounts ORDER BY participant_code IS NULL, participant_code, username")
    inst_map = {(r["account_id"], r["qid"]): r
                for r in db.query("SELECT * FROM instances WHERE set_id=?", (st["id"],))}
    scores, totals = grader.set_scores(st, qs)
    att = sets.attendance(st)
    rows = []
    for a in accs:
        cells, got = [], 0.0
        for q in qs:
            sc = scores.get((a["id"], q.qid))
            got += sc or 0
            inst = inst_map.get((a["id"], q.qid))
            cells.append({"score": sc, "total": totals[q.qid], "inst": inst["status"] if inst else None})
        at = att.get(a["id"])
        if at:
            at["late"] = sets.late_minutes(st, at["first"])
        rows.append({"acc": a, "cells": cells, "got": got, "att": at})
    total = sum(totals.values())
    return qs, rows, total


@router.get("", response_class=HTMLResponse)
async def overview(request: Request):
    require_admin(request)
    st = _pick_set(request)
    if st is None:   # 還沒有任何題組（例如剛安裝好）：顯示開始使用的步驟
        qs, _bad = questions.load_all()
        ready = {r["qid"]: r for r in db.query("SELECT qid, selftest_json FROM templates WHERE status='ready'")}
        return _render(request, "admin/getting_started.html", {
            "n_questions": len(qs), "n_ready": sum(1 for q in qs if q in ready),
            "n_accounts": db.one("SELECT COUNT(*) n FROM accounts")["n"],
            "n_bound": db.one("SELECT COUNT(*) n FROM accounts WHERE participant_code IS NOT NULL")["n"]})
    qs, rows, total = _score_matrix(st)
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) n FROM instances WHERE set_id=? GROUP BY status", (st["id"],))}
    queue = db.one("SELECT COUNT(*) n FROM check_runs WHERE status IN ('queued','running')")["n"]
    start, end = sets.window(st)
    return _render(request, "admin/overview.html", {
        "st": st, "all_sets": sets.all_sets(), "qs": qs, "rows": rows, "total": total, "counts": counts,
        "queue": queue, "state": sets.state(st), "start": start, "end": end,
        "bound": sum(1 for r in rows if r["acc"]["participant_code"]),
        "present": sum(1 for r in rows if r["att"]),
    })


@router.get("/scores.csv")
async def scores_csv(request: Request):
    require_admin(request)
    st = _pick_set(request)
    if st is None:
        return _back("/admin/sets", _("請先建立題組"))
    qs, rows, total = _score_matrix(st)
    buf = io.StringIO()
    buf.write("\ufeff")  # Excel 開啟中文不亂碼
    w = csv.writer(buf)
    w.writerow([_("編號"), _("帳號"), _("出席"), _("首次進入"), _("最後活動"), _("晚到(分)"), "IP", _("檢查次數")]
               + [f"{q.qid} {q.title_in()}" for q in qs] + [_("總分(/{total})", total=f"{total:g}")])
    for r in rows:
        if not r["acc"]["participant_code"]:
            continue
        at = r["att"] or {}
        w.writerow([r["acc"]["participant_code"], r["acc"]["username"], _("出席") if at else _("缺"),
                    at.get("first", ""), at.get("last", ""), "" if at.get("late") is None else at["late"],
                    at.get("ip") or "", at.get("n_checks", "")]
                   + [("" if c["score"] is None else f"{c['score']:g}") for c in r["cells"]]
                   + [f"{r['got']:g}"])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=scores-set{st['id']}-{datetime.now():%Y%m%d-%H%M}.csv"})


# ---------------- 練習總表（每次練習的成績 + 出席） ----------------
def _summary(request: Request):
    chosen = request.query_params.getlist("sets")
    all_ = sorted(sets.all_sets(), key=lambda x: (x["start_at"] or "9999", x["id"]))
    cols = [x for x in all_ if (str(x["id"]) in chosen if chosen else x["mode"] == "practice")]
    accs = db.query("SELECT * FROM accounts WHERE participant_code IS NOT NULL ORDER BY participant_code")
    data = []
    for st in cols:
        qs = sets.questions_of(st["id"])
        scores, totals = grader.set_scores(st, qs)
        att = sets.attendance(st)
        per = {}
        for a in accs:
            vals = [scores.get((a["id"], q.qid)) for q in qs]
            at = att.get(a["id"])
            per[a["id"]] = {"score": sum(v or 0 for v in vals), "present": bool(at),
                            "late": sets.late_minutes(st, at["first"]) if at else None}
        data.append({"s": st, "total": sum(totals.values()), "per": per,
                     "n_present": sum(1 for v in per.values() if v["present"])})
    rows = []
    for a in accs:
        cells = [d["per"][a["id"]] for d in data]
        rows.append({"acc": a, "cells": cells, "n_present": sum(c["present"] for c in cells),
                     "got": sum(c["score"] for c in cells)})
    return all_, cols, data, rows


@router.get("/summary", response_class=HTMLResponse)
async def summary_page(request: Request):
    require_admin(request)
    all_, cols, data, rows = _summary(request)
    return _render(request, "admin/summary.html", {
        "all_sets": all_, "cols": cols, "data": data, "rows": rows,
        "late_min": int(request.query_params.get("late") or 15),
        "grand": sum(d["total"] for d in data), "qs": request.url.query})


@router.get("/summary.csv")
async def summary_csv(request: Request):
    require_admin(request)
    _all, cols, data, rows = _summary(request)
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    head = [_("編號"), _("帳號")]
    for d in data:
        head += [_("{name} 出席", name=d['s']['name']), _("{name} 晚到(分)", name=d['s']['name']),
                 _("{name} 得分(/{total})", name=d['s']['name'], total=f"{d['total']:g}")]
    w.writerow(head + [_("出席次數"), _("總分")])
    for r in rows:
        line = [r["acc"]["participant_code"], r["acc"]["username"]]
        for c in r["cells"]:
            line += [_("出席") if c["present"] else _("缺"), "" if c["late"] is None else c["late"], f"{c['score']:g}"]
        w.writerow(line + [r["n_present"], f"{r['got']:g}"])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=summary-{datetime.now():%Y%m%d}.csv"})


# ---------------- 系統設定 ----------------
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    require_admin(request)
    return _render(request, "admin/settings.html", {"s": db.all_settings()})


@router.post("/settings")
async def settings_save(request: Request):
    require_admin(request)
    f = await request.form()
    db.set_setting("site_title", str(f.get("site_title", "")).strip())
    for k in ("max_running", "idle_stop_min", "max_instances"):
        db.set_setting(k, max(1, int(f.get(k) or 1)))
    pat = str(f.get("code_pattern") or "").strip()
    try:
        re.compile(pat)
        db.set_setting("code_pattern", pat)
    except re.error:
        return _back("/admin/settings", _("編號格式正規表示式錯誤"))
    db.set_setting("notice", str(f.get("notice", "")))
    from . import i18n
    lang = str(f.get("default_lang") or "zh-TW")
    if lang == "auto" or lang in i18n.languages():
        db.set_setting("default_lang", lang)
    return _back("/admin/settings", _("已儲存"))


# ---------------- 題組 ----------------
@router.get("/sets", response_class=HTMLResponse)
async def sets_page(request: Request):
    require_admin(request)
    items = []
    for st in sets.all_sets():
        start, end = sets.window(st)
        items.append({"s": st, "state": sets.state(st), "participant_state": sets.participant_state(st),
                      "start": start, "end": end, "n_q": len(sets.questions_of(st["id"])),
                      "n_inst": db.one("SELECT COUNT(*) n FROM instances WHERE set_id=?", (st["id"],))["n"]})
    return _render(request, "admin/sets.html", {"items": items,
                   "n_total": db.one("SELECT COUNT(*) n FROM instances")["n"],
                   "limit": int(db.get_setting("max_instances") or 1000)})


@router.post("/sets/create")
async def sets_create(request: Request, name: str = Form(...), mode: str = Form("practice"),
                      copy_from: str = Form("")):
    require_admin(request)
    mode = mode if mode in sets.MODES else "practice"
    src = sets.get(int(copy_from)) if copy_from.isdigit() else None
    practice = mode == "practice"
    sid = db.execute(
        "INSERT INTO qsets(name,mode,visible,score_policy,show_detail,block_paste,cooldown_sec) VALUES(?,?,?,?,?,?,?)",
        (name.strip() or "未命名題組", mode, 0,
         "best", 1 if practice else (src["show_detail"] if src else 1),
         0 if practice else (src["block_paste"] if src else 0),
         10 if practice else (src["cooldown_sec"] if src else 30)))
    if src:
        db.execute("INSERT INTO set_questions(set_id,qid,enabled,sort_order,points_json,time_limit_min,max_attempts) "
                   "SELECT ?,qid,enabled,sort_order,points_json,time_limit_min,max_attempts "
                   "FROM set_questions WHERE set_id=?", (sid, src["id"]))
    return _back(f"/admin/sets/{sid}", _("已建立（目前未公開，設定好後勾選「作答者可見」）"))


@router.get("/sets/{sid}", response_class=HTMLResponse)
async def set_detail(request: Request, sid: int):
    require_admin(request)
    st = sets.get(sid)
    if st is None:
        return _back("/admin/sets", _("題組不存在"))
    qs, bad = questions.load_all()
    items = []
    for q in qs.values():
        e = sets.entry(sid, q.qid)
        t = instances.template_for(q.qid)
        items.append({"q": q, "e": e, "checks": sets.effective_checks(q, sid), "ready": bool(t),
                      "n_inst": db.one("SELECT COUNT(*) n FROM instances WHERE set_id=? AND qid=?",
                                       (sid, q.qid))["n"]})
    items.sort(key=lambda i: (not i["e"]["enabled"], i["e"]["sort_order"], i["q"].qid))
    return _render(request, "admin/set_detail.html", {
        "st": st, "items": items, "all_tags": _all_tags(qs.values()), "state": sets.state(st), "participant_state": sets.participant_state(st),
        "total": sum(sum(c.points for c in i["checks"]) for i in items if i["e"]["enabled"])})


@router.post("/sets/{sid}")
async def set_save(request: Request, sid: int):
    require_admin(request)
    st = sets.get(sid)
    if st is None:
        return _back("/admin/sets", _("題組不存在"))
    f = await request.form()
    mode = f.get("mode") if f.get("mode") in sets.MODES else st["mode"]
    start, end = str(f.get("start_at") or ""), str(f.get("end_at") or "")
    if start and end and end <= start:
        return _back(f"/admin/sets/{sid}", _("結束時間必須晚於開始時間"))
    policy = f.get("score_policy") if f.get("score_policy") in ("best", "latest", "final") else "best"
    db.execute(
        "UPDATE qsets SET name=?, mode=?, visible=?, start_at=?, end_at=?, score_policy=?, show_detail=?, "
        "block_paste=?, cooldown_sec=?, notice=?, sort_order=? WHERE id=?",
        (str(f.get("name") or st["name"]).strip(), mode, int(f.get("visible") == "on"), start, end, policy,
         int(f.get("show_detail") == "on"), int(f.get("block_paste") == "on"),
         max(0, int(f.get("cooldown_sec") or 0)), str(f.get("notice") or ""), int(f.get("sort_order") or 0), sid))
    msg = _("已儲存")
    if mode == "assessment" and (not start or not end):
        msg += _("（測驗題組需要設定開始與結束時間，作答者才能作答）")
    return _back(f"/admin/sets/{sid}", msg)


@router.post("/sets/{sid}/q/{qid}")
async def set_question_save(request: Request, sid: int, qid: str):
    require_admin(request)
    q = questions.get(qid)
    if q is None or sets.get(sid) is None:
        return _back(f"/admin/sets/{sid}", _("題目不存在"))
    f = await request.form()
    pts = {}
    for c in q.checks:
        v = f.get(f"points_{c.id}")
        if v not in (None, "") and float(v) != c.points:
            pts[c.id] = float(v)
    db.execute(
        "INSERT INTO set_questions(set_id,qid,enabled,sort_order,points_json,time_limit_min,max_attempts) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(set_id,qid) DO UPDATE SET enabled=excluded.enabled, sort_order=excluded.sort_order, "
        "points_json=excluded.points_json, time_limit_min=excluded.time_limit_min, "
        "max_attempts=excluded.max_attempts",
        (sid, qid, int(f.get("enabled") == "on"), int(f.get("sort_order") or 0), json.dumps(pts),
         max(0, int(f.get("time_limit_min") or 0)), max(0, int(f.get("max_attempts") or 0))))
    return _back(f"/admin/sets/{sid}", _("{qid} 已更新", qid=qid))


@router.post("/sets/{sid}/bulk")
async def set_bulk(request: Request, sid: int):
    """勾選多題一次納入／移出（移出只是取消勾選「納入」，該題的配分、限時等設定保留）"""
    require_admin(request)
    if sets.get(sid) is None:
        return _back("/admin/sets", _("題組不存在"))
    f = await request.form()
    action = f.get("action")
    qids = [q for q in f.getlist("qid") if questions.get(str(q))]
    if action not in ("include", "exclude") or not qids:
        return _back(f"/admin/sets/{sid}", _("請先勾選題目"))
    on = int(action == "include")
    for qid in qids:
        db.execute("INSERT INTO set_questions(set_id,qid,enabled) VALUES(?,?,?) "
                   "ON CONFLICT(set_id,qid) DO UPDATE SET enabled=excluded.enabled", (sid, qid, on))
    return _back(f"/admin/sets/{sid}", _("已納入 {n} 題", n=len(qids)) if on else _("已移出 {n} 題", n=len(qids)))


@router.post("/sets/{sid}/extend")
async def set_extend(request: Request, sid: int, minutes: int = Form(...)):
    require_admin(request)
    st = sets.get(sid)
    _start, end = sets.window(st) if st else (None, None)
    if end:
        from datetime import timedelta
        db.execute("UPDATE qsets SET end_at=? WHERE id=?",
                   ((end + timedelta(minutes=minutes)).isoformat(timespec="minutes"), sid))
    return _back(f"/admin?set={sid}", _("結束時間延長 {n} 分鐘", n=minutes))


@router.post("/sets/{sid}/delete")
async def set_delete(request: Request, sid: int, confirm: str = Form("")):
    require_admin(request)
    st = sets.get(sid)
    if st is None:
        return _back("/admin/sets", _("題組不存在"))
    if db.one("SELECT 1 FROM instances WHERE set_id=?", (sid,)):
        return _back(f"/admin/sets/{sid}", _("此題組還有作答者的實例，請先「刪除此題組所有實例」"))
    if confirm != st["name"]:
        return _back(f"/admin/sets/{sid}", _("請輸入題組名稱確認刪除"))
    db.execute("DELETE FROM attempts WHERE set_id=?", (sid,))
    db.execute("DELETE FROM check_runs WHERE set_id=?", (sid,))
    db.execute("DELETE FROM set_questions WHERE set_id=?", (sid,))
    db.execute("DELETE FROM qsets WHERE id=?", (sid,))
    recording.delete_set(sid)
    return _back("/admin/sets", _("題組「{name}」與其成績紀錄已刪除", name=st['name']))


@router.post("/sets/{sid}/prewarm")
async def set_prewarm(request: Request, sid: int):
    """為所有帳號 × 本題組題目預先建立實例（不啟動），測驗開始時就不用排隊 clone。
    實例綁在帳號上（不是編號），所以可以在作答者登入前、甚至前一天就先建立。"""
    require_admin(request)
    st = sets.get(sid)
    if st is None:
        return _back("/admin/sets", _("題組不存在"))
    form = await request.form()
    if form.get("all_accounts"):
        accs = db.query("SELECT * FROM accounts")
    else:   # 預設只為已綁定編號的帳號建立（沒發出去的帳號不佔儲存空間）
        accs = db.query("SELECT * FROM accounts WHERE participant_code IS NOT NULL")
    qs = [q for q in sets.questions_of(sid) if instances.template_for(q.qid)]
    total = len(accs) * len(qs)
    have = {(r["account_id"], r["qid"]) for r in
            db.query("SELECT account_id, qid FROM instances WHERE set_id=?", (sid,))}
    new = sum(1 for a in accs for q in qs if (a["id"], q.qid) not in have)
    cur = db.one("SELECT COUNT(*) n FROM instances")["n"]
    limit = int(db.get_setting("max_instances") or 1000)
    if cur + new > limit:
        return _back(f"/admin/sets/{sid}", _(
            "實例總數會達到 {n}，超過上限 {limit}（目前 {cur}）。請先刪除已結束題組的實例，或到系統設定調整上限。",
            n=cur + new, limit=limit, cur=cur))
    progress = {"ok": 0, "fail": 0}
    job = _raw("預先建立實例（{name}）", name=st['name'])

    async def per_question(q):  # 每題依序 clone；各題之間平行
        for a in accs:
            try:
                await instances.prepare(a, sid, q)
                progress["ok"] += 1
            except Exception:  # noqa: BLE001
                log.exception("prewarm %s %s", a["username"], q.qid)
                progress["fail"] += 1
            job_status[job] = _raw("{done}/{total}（失敗 {fail}）", done=progress['ok'] + progress['fail'], total=total,
                                fail=progress['fail'])

    async def go():
        await asyncio.gather(*(per_question(q) for q in qs))
        job_status[job] = _raw("完成：成功 {ok}、失敗 {fail}（{time}）", ok=progress['ok'], fail=progress['fail'],
                                time=f"{datetime.now():%H:%M:%S}")
    _spawn(go())
    return _back(f"/admin/sets/{sid}", _("開始預先建立 {n} 個實例（已存在的會略過）", n=new))


@router.post("/sets/{sid}/destroy_instances")
async def set_destroy_instances(request: Request, sid: int, confirm: str = Form("")):
    require_admin(request)
    st = sets.get(sid)
    if st is None:
        return _back("/admin/sets", _("題組不存在"))
    if confirm != "DELETE":
        return _back(f"/admin/sets/{sid}", _("請輸入 DELETE 確認"))
    rows = db.query("SELECT * FROM instances WHERE set_id=?", (sid,))
    _spawn(_batch(_raw("刪除實例（{name}）", name=st['name']), rows, instances.destroy))
    return _back(f"/admin/sets/{sid}", _("開始刪除 {n} 個實例（成績紀錄保留）", n=len(rows)))


# ---------------- 題庫 ----------------
@router.get("/questions", response_class=HTMLResponse)
async def questions_page(request: Request):
    require_admin(request)
    qs, bad = questions.load_all()
    items = []
    for q in qs.values():
        in_sets = db.query("SELECT q.id, q.name, q.mode FROM set_questions sq JOIN qsets q ON q.id=sq.set_id "
                           "WHERE sq.qid=? AND sq.enabled=1 ORDER BY q.id", (q.qid,))
        t = db.one("SELECT qid,vmid,kind,version,status,built_at,selftest_json FROM templates WHERE qid=?", (q.qid,))
        if t and t.get("selftest_json"):
            t["selftest"] = json.loads(t["selftest_json"])
        n_inst = db.one("SELECT COUNT(*) n FROM instances WHERE qid=?", (q.qid,))["n"]
        items.append({"q": q, "in_sets": in_sets, "t": t,
                      "building": q.qid in build.building, "n_inst": n_inst,
                      "outdated": bool(t and t.get("version") and t["version"] != q.build_version)})
    items.sort(key=lambda i: i["q"].qid)
    return _render(request, "admin/questions.html", {"items": items, "bad": bad, "all_tags": _all_tags(qs.values())})


@router.get("/questions/{qid}/status.json")
async def question_status(request: Request, qid: str):
    """範本建置狀態（給自動化／測試用）"""
    require_admin(request)
    t = db.one("SELECT qid,vmid,kind,version,status,built_at,selftest_json FROM templates WHERE qid=?", (qid,))
    st = json.loads(t.pop("selftest_json")) if t and t.get("selftest_json") else None
    return {"qid": qid, "building": qid in build.building, "template": t, "selftest": st}


@router.get("/questions/{qid}", response_class=HTMLResponse)
async def question_detail(request: Request, qid: str):
    require_admin(request)
    q = questions.get(qid)
    if q is None:
        return _back("/admin/questions", _("題目不存在"))
    t = db.one("SELECT * FROM templates WHERE qid=?", (qid,))
    files = {name: (q.path / name).read_text() for name in ("question.yaml", "setup.yml", "check.yml", "solution.yml")
             if (q.path / name).is_file()}
    return _render(request, "admin/question_detail.html", {
        "q": q, "t": t, "files": files, "building": qid in build.building,
        "selftest": json.loads(t["selftest_json"]) if t and t.get("selftest_json") else None})


@router.post("/questions/{qid}/tags")
async def question_tags(request: Request, qid: str, tags: str = Form("")):
    """改寫題目 question.yaml 的 tags（匯出 zip 也會帶著）"""
    require_admin(request)
    if questions.get(qid) is None:
        return _back("/admin/questions", _("題目不存在"))
    try:
        new = questions.set_tags(qid, tags.split(","))
    except questions.QuestionError as e:
        return _back(f"/admin/questions#q-{qid}", _("{qid} 標籤未儲存：{e}", qid=qid, e=str(e)))
    return _back(f"/admin/questions#q-{qid}", _("{qid} 標籤已更新：{tags}", qid=qid, tags=", ".join(new) or "—"))


@router.post("/questions/{qid}/build")
async def question_build(request: Request, qid: str):
    require_admin(request)
    if qid in build.building:
        return _back("/admin/questions", _("建置中"))

    async def go():
        try:
            await build.build(qid)
        except Exception:  # noqa: BLE001
            log.exception("build %s", qid)
    _spawn(go())
    return _back(f"/admin/questions/{qid}", _("開始建置（約 1~5 分鐘），本頁可重新整理查看紀錄"))


@router.post("/questions/{qid}/selftest")
async def question_selftest(request: Request, qid: str):
    require_admin(request)

    async def go():
        try:
            await build.selftest(qid)
        except Exception:  # noqa: BLE001
            log.exception("selftest %s", qid)
    _spawn(go())
    return _back(f"/admin/questions/{qid}", _("開始自我測試"))


@router.post("/questions/upload")
async def question_upload(request: Request, file: UploadFile = File(...)):
    require_admin(request)
    data = await file.read()
    try:
        qid = questions.install_zip(data)
    except questions.QuestionError as e:
        from . import i18n
        return _back("/admin/questions", _("上傳失敗：{e}", e=i18n.translate_stored(str(e))))
    return _back(f"/admin/questions/{qid}", _("上傳成功；若 setup 有變更請重新建置範本"))


@router.get("/questions/{qid}/export.zip")
async def question_export(request: Request, qid: str):
    require_admin(request)
    return Response(questions.export_zip(qid), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename={qid}.zip"})


@router.post("/questions/{qid}/delete")
async def question_delete(request: Request, qid: str):
    require_admin(request)
    q = questions.get(qid)
    if q is None:
        return _back("/admin/questions", _("題目不存在"))
    if db.one("SELECT 1 FROM instances WHERE qid=?", (qid,)):
        return _back("/admin/questions", _("仍有作答者的實例，請先到「實例」頁刪除"))
    t = db.one("SELECT * FROM templates WHERE qid=?", (qid,))
    if t and t.get("vmid"):
        from .pve import client
        await asyncio.to_thread(client().destroy, t["kind"], t["vmid"])
    db.execute("DELETE FROM templates WHERE qid=?", (qid,))
    db.execute("DELETE FROM set_questions WHERE qid=?", (qid,))
    backup = cfg.data_path / "question_backups"
    backup.mkdir(exist_ok=True)
    shutil.move(str(q.path), backup / f"{qid}-deleted-{datetime.now():%Y%m%d%H%M%S}")
    return _back("/admin/questions", _("{qid} 已刪除（檔案移至 data/question_backups）", qid=qid))


# ---------------- 帳號 ----------------
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    require_admin(request)
    accs = db.query("SELECT * FROM accounts ORDER BY username")
    return _render(request, "admin/accounts.html", {"accs": accs})


@router.post("/accounts/generate")
async def accounts_generate(request: Request, prefix: str = Form("p"), count: int = Form(...),
                            start: int = Form(1)):
    require_admin(request)
    made = 0
    for i in range(start, start + count):
        u = f"{prefix}{i:03d}"
        if db.one("SELECT 1 FROM accounts WHERE username=?", (u,)):
            continue
        pw = random_password()
        db.execute("INSERT INTO accounts(username,password_hash,init_password) VALUES(?,?,?)",
                   (u, hash_password(pw), pw))
        made += 1
    return _back("/admin/accounts", _("已產生 {n} 個帳號", n=made))


@router.post("/accounts/import")
async def accounts_import(request: Request, file: UploadFile = File(...)):
    """CSV 欄位依序：帳號, 密碼, 編號, 備註（第一列若為標題會自動略過）"""
    require_admin(request)
    text = (await file.read()).decode("utf-8-sig")
    made = 0
    skipped: list[str] = []
    pat = db.get_setting("code_pattern")
    for n, row in enumerate(csv.reader(io.StringIO(text)), 1):
        if not row or row[0].strip().lower() in ("username", "帳號"):
            continue
        u = row[0].strip()
        pw = (row[1].strip() if len(row) > 1 else "") or random_password()
        sid = row[2].strip() if len(row) > 2 and row[2].strip() else None
        note = row[3].strip() if len(row) > 3 else None
        if not u or db.one("SELECT 1 FROM accounts WHERE username=?", (u,)):
            skipped.append(_("第 {n} 列（帳號已存在或空白）", n=n))
            continue
        # 除了設定的格式，一律只允許字母、數字、_ . -（避免空白、逗號等字元在 CSV 匯出、網址中出問題）
        if sid and (not re.match(pat, sid) or not re.fullmatch(r"[\w.-]+", sid) or sid.startswith(".")):
            skipped.append(_("第 {n} 列（編號格式不符：{sid}）", n=n, sid=sid))
            continue
        if sid and db.one("SELECT 1 FROM accounts WHERE participant_code=?", (sid,)):
            skipped.append(_("第 {n} 列（編號已被綁定：{sid}）", n=n, sid=sid))
            continue
        db.execute("INSERT INTO accounts(username,password_hash,init_password,participant_code,bound_at,note) "
                   "VALUES(?,?,?,?,?,?)",
                   (u, hash_password(pw), pw, sid, datetime.now().isoformat(timespec="seconds") if sid else None,
                    note))
        made += 1
    msg = _("已匯入 {n} 個帳號", n=made)
    if skipped:
        msg += _("；略過 {m} 列：", m=len(skipped)) + _("、").join(skipped[:10]) + ("…" if len(skipped) > 10 else "")
    return _back("/admin/accounts", msg)


@router.get("/accounts.csv")
async def accounts_csv(request: Request):
    require_admin(request)
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow([_("帳號"), _("初始密碼"), _("編號"), _("備註")])
    for a in db.query("SELECT * FROM accounts ORDER BY username"):
        w.writerow([a["username"], a["init_password"] or "", a["participant_code"] or "", a["note"] or ""])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=accounts.csv"})


@router.get("/accounts/print", response_class=HTMLResponse)
async def accounts_print(request: Request):
    require_admin(request)
    accs = db.query("SELECT * FROM accounts WHERE participant_code IS NULL ORDER BY username")
    return _render(request, "admin/accounts_print.html", {"accs": accs})


@router.post("/accounts/{aid}/{action}")
async def account_action(request: Request, aid: int, action: str):
    require_admin(request)
    acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
    if acc is None:
        return _back("/admin/accounts", _("帳號不存在"))
    if action == "unbind":
        db.execute("UPDATE accounts SET participant_code=NULL, bound_at=NULL WHERE id=?", (aid,))
        return _back("/admin/accounts", _("{u} 已解除編號綁定", u=acc['username']))
    if action == "resetpw":
        pw = random_password()
        db.execute("UPDATE accounts SET password_hash=?, init_password=? WHERE id=?", (hash_password(pw), pw, aid))
        return _back("/admin/accounts", _("{u} 新密碼：{pw}", u=acc['username'], pw=pw))
    if action == "delete":
        if db.one("SELECT 1 FROM instances WHERE account_id=?", (aid,)):
            return _back("/admin/accounts", _("此帳號還有實例，請先刪除實例"))
        db.execute("DELETE FROM check_runs WHERE account_id=?", (aid,))
        db.execute("DELETE FROM attempts WHERE account_id=?", (aid,))
        db.execute("DELETE FROM activity WHERE account_id=?", (aid,))
        db.execute("DELETE FROM accounts WHERE id=?", (aid,))
        recording.delete_account(aid)
        return _back("/admin/accounts", _("{u} 已刪除", u=acc['username']))
    return _back("/admin/accounts", _("未知操作"))


# ---------------- 作答者詳細 / 檢查紀錄 ----------------
@router.get("/participant/{aid}", response_class=HTMLResponse)
async def participant_detail(request: Request, aid: int):
    require_admin(request)
    acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
    if acc is None:
        return _back("/admin", _("帳號不存在"))
    names = {x["id"]: x["name"] for x in sets.all_sets()}
    runs = db.query("SELECT id,set_id,qid,status,is_final,score,max_score,message,started_at,finished_at "
                    "FROM check_runs WHERE account_id=? ORDER BY id DESC", (aid,))
    insts = db.query("SELECT * FROM instances WHERE account_id=? ORDER BY set_id, qid", (aid,))
    atts = db.query("SELECT * FROM attempts WHERE account_id=? ORDER BY id DESC", (aid,))
    acts = db.query("SELECT * FROM activity WHERE account_id=? ORDER BY id DESC LIMIT 200", (aid,))
    for r in runs + insts + atts + acts:
        r["set_name"] = names.get(r["set_id"], f"#{r['set_id']}") if r["set_id"] else ""
    logs = recording.group(recording.list_for(aid, names), runs)
    return _render(request, "admin/participant.html", {"acc": acc, "runs": runs, "insts": insts, "logs": logs,
                                                    "atts": atts, "acts": acts})


@router.post("/participant/{aid}/attempts/{att_id}/{action}")
async def participant_attempts(request: Request, aid: int, att_id: int, action: str):
    """end：立即結束進行中的作答；clear：清除該題組該題已結束的作答紀錄（等於多給次數）"""
    require_admin(request)
    att = db.one("SELECT * FROM attempts WHERE id=? AND account_id=?", (att_id, aid))
    if att is None:
        return _back(f"/admin/participant/{aid}", _("紀錄不存在"))
    if action == "end":
        if not att["ended_at"]:
            await attempts.expire(att, "admin")
        return _back(f"/admin/participant/{aid}", _("{qid} 作答已結束並重置", qid=att['qid']))
    if action == "clear":
        db.execute("DELETE FROM attempts WHERE account_id=? AND set_id=? AND qid=? AND ended_at IS NOT NULL",
                   (aid, att["set_id"], att["qid"]))
        return _back(f"/admin/participant/{aid}", _("{qid} 作答次數已重設", qid=att['qid']))
    return _back(f"/admin/participant/{aid}", _("未知操作"))


@router.get("/runs/{rid}", response_class=PlainTextResponse)
async def run_log(request: Request, rid: int):
    require_admin(request)
    r = db.one("SELECT * FROM check_runs WHERE id=?", (rid,))
    if r is None:
        return PlainTextResponse("not found", 404)
    from . import i18n
    items = json.loads(r["results_json"] or "[]")
    q = questions.get(r["qid"])
    desc = (lambda i: q.check_desc(i["id"])) if q else (lambda i: i["desc"])
    head = "\n".join(f"{'PASS' if i['passed'] else 'FAIL'}  {i['points']:g}  {i['id']}  {desc(i)}" for i in items)
    return (f"{r['qid']}  {r['status']}  {r['score']}/{r['max_score']}  {i18n.translate_stored(r['message'])}"
            f"\n\n{head}\n\n{r['log'] or ''}")


@router.get("/recording/{aid}/{name}", response_class=HTMLResponse)
async def recording_page(request: Request, aid: int, name: str):
    """終端機重播：asciinema-player＋輸入列表（點一下跳到該時間）"""
    require_admin(request)
    acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
    p = recording.safe_path(aid, name) if acc else None
    if p is None:
        return _back(f"/admin/participant/{aid}" if acc else "/admin", _("紀錄不存在"))
    header, events = recording.read(p)
    lines = recording.input_lines(header, events)
    info = next((r for r in recording.list_for(aid, {x["id"]: x["name"] for x in sets.all_sets()}) if r["name"] == name), {})
    return _render(request, "admin/recording.html", {"acc": acc, "name": name, "lines": lines, "info": info})


@router.get("/shell_log/{aid}/{name}")
async def shell_log(request: Request, aid: int, name: str, view: str = "cast"):
    """view=cast 原始 asciicast（下載）；player 播放器用（含每行輸入的標記）；input 輸入文字；output 畫面文字"""
    require_admin(request)
    p = recording.safe_path(aid, name)
    if p is None:
        return PlainTextResponse("not found", 404)
    if view == "cast":
        return Response(p.read_bytes(), media_type="application/x-asciicast",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    header, events = recording.read(p)
    if view == "player":
        return Response(recording.player_cast(header, events, recording.input_lines(header, events)),
                        media_type="application/x-asciicast")
    if view == "input":
        return PlainTextResponse("\n".join(f"[{r['at']}] {r['text']}" for r in recording.input_lines(header, events)))
    if view == "output":
        return PlainTextResponse(recording.output_text(events))
    return PlainTextResponse("unknown view", 400)


# ---------------- 實例管理 ----------------
@router.get("/instances", response_class=HTMLResponse)
async def instances_page(request: Request):
    require_admin(request)
    rows = db.query("SELECT i.*, a.username, a.participant_code, s.name set_name, s.mode set_mode FROM instances i "
                    "JOIN accounts a ON a.id=i.account_id LEFT JOIN qsets s ON s.id=i.set_id "
                    "ORDER BY i.set_id, a.participant_code, i.qid")
    for r in rows:
        r["shells"] = instances.active_shells.get(r["vmid"], 0)
    return _render(request, "admin/instances.html", {"rows": rows})


@router.post("/instances/{iid}/{action}")
async def instance_action(request: Request, iid: int, action: str):
    require_admin(request)
    inst = db.one("SELECT * FROM instances WHERE id=?", (iid,))
    if inst is None:
        return _back("/admin/instances", _("實例不存在"))
    if action == "stop":
        await instances.stop(inst)
    elif action == "start":
        from .pve import client
        await asyncio.to_thread(client().start, inst["kind"], inst["vmid"])
        instances.set_status(inst["id"], "running")
    elif action == "destroy":
        await instances.destroy(inst)
    return _back("/admin/instances", _("{vmid} {action} 完成", vmid=inst['vmid'], action=action))


async def _batch(name: str, items, fn) -> None:
    items = list(items)
    ok = fail = 0
    for i, it in enumerate(items, 1):
        job_status[name] = _raw("{i}/{n}（成功 {ok}、失敗 {fail}）", i=i, n=len(items), ok=ok, fail=fail)
        try:
            await fn(it)
            ok += 1
        except Exception:  # noqa: BLE001
            log.exception("%s failed", name)
            fail += 1
    job_status[name] = _raw("完成：成功 {ok}、失敗 {fail}（{time}）", ok=ok, fail=fail, time=f"{datetime.now():%H:%M:%S}")


@router.post("/instances/stop_all")
async def instances_stop_all(request: Request):
    require_admin(request)
    rows = db.query("SELECT * FROM instances WHERE status='running'")
    _spawn(_batch(_raw("停止全部實例"), rows, instances.stop))
    return _back("/admin/instances", _("開始停止 {n} 個實例", n=len(rows)))


@router.post("/instances/destroy_all")
async def instances_destroy_all(request: Request, confirm: str = Form("")):
    require_admin(request)
    if confirm != "DELETE":
        return _back("/admin/instances", _("請輸入 DELETE 確認"))
    rows = db.query("SELECT * FROM instances")
    _spawn(_batch(_raw("刪除全部實例"), rows, instances.destroy))
    return _back("/admin/instances", _("開始刪除 {n} 個實例", n=len(rows)))


# ---------------- 最終校驗 ----------------
@router.post("/sets/{sid}/final")
async def final_grading(request: Request, sid: int):
    """測驗結束後對該題組所有實例統一跑一次校驗（標記 is_final）。"""
    require_admin(request)
    st = sets.get(sid)
    if st is None:
        return _back("/admin", _("題組不存在"))
    if sets.state(st) == "open":
        return _back(f"/admin?set={sid}", _("題組尚未結束；請先結束再做最終校驗"))
    # 限時題目時間到就重置了，不做最終校驗（成績取作答時間內的檢查）
    qs = {q.qid: q for q in sets.questions_of(sid) if not attempts.is_timed(sid, q.qid)}
    # 只校驗作答者真的開過（或檢查過）的環境；預先建立但沒碰過的不用啟動，該題本來就是 0 分
    rows = db.query(
        "SELECT i.qid, a.* FROM instances i JOIN accounts a ON a.id=i.account_id WHERE i.set_id=? AND ("
        " EXISTS (SELECT 1 FROM activity x WHERE x.account_id=i.account_id AND x.set_id=i.set_id AND x.qid=i.qid"
        "         AND x.kind IN ('start','shell'))"
        " OR EXISTS (SELECT 1 FROM check_runs c WHERE c.account_id=i.account_id AND c.set_id=i.set_id"
        "         AND c.qid=i.qid AND c.is_final=0))", (sid,))
    pairs = [(r, qs[r["qid"]]) for r in rows if r["qid"] in qs]
    job = _raw("最終校驗（{name}）", name=st['name'])

    async def one(p):
        acc, q = p
        run_id = grader.enqueue(acc, sid, q, final=True)
        while db.one("SELECT status FROM check_runs WHERE id=?", (run_id,))["status"] in ("queued", "running"):
            await asyncio.sleep(1)

    async def go():
        # 以 grader_concurrency 平行執行
        sem = asyncio.Semaphore(cfg.grader_concurrency)
        done = 0

        async def wrapped(p):
            nonlocal done
            async with sem:
                await one(p)
            done += 1
            job_status[job] = f"{done}/{len(pairs)}"
        await asyncio.gather(*(wrapped(p) for p in pairs))
        job_status[job] = _raw("完成 {n} 筆（{time}）", n=len(pairs), time=f"{datetime.now():%H:%M:%S}")
    _spawn(go())
    return _back(f"/admin?set={sid}", _("開始最終校驗 {n} 個實例", n=len(pairs)))
