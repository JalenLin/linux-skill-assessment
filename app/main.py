"""作答者端網頁與 API。啟動：uvicorn app.main:app"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import attempts, db, grader, i18n, instances, questions, sets, shell
from .i18n import _
from .config import cfg
from .security import verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["settings"] = db.all_settings
templates.env.globals["_"] = i18n.gettext
templates.env.globals["tr_stored"] = i18n.translate_stored
templates.env.globals["tr_lines"] = i18n.translate_lines
templates.env.globals["lang"] = i18n.current
templates.env.globals["languages"] = i18n.languages
templates.env.globals["i18n_catalog"] = i18n.catalog
templates.env.globals["open_assessment"] = sets.open_assessment
templates.env.globals["MODES"] = sets.MODES
templates.env.globals["STATE_LABEL"] = sets.STATE_LABEL


async def _background() -> None:
    """定期：同步 PVE 狀態、停止閒置實例、題組結束後停止該題組實例、測驗中停止練習環境"""
    stopped_after_close: set[int] = set()
    while True:
        try:
            await instances.sync_status()
            active = sets.open_assessment()
            for s in sets.all_sets():
                st = sets.state(s)
                if st == "closed" and s["id"] not in stopped_after_close:
                    log.info("題組 %s（%s）結束：停止其作答者實例", s["id"], s["name"])
                    await instances.stop_many(
                        db.query("SELECT * FROM instances WHERE set_id=? AND status='running'", (s["id"],)))
                    stopped_after_close.add(s["id"])
                elif st != "closed":
                    stopped_after_close.discard(s["id"])
                if active and s["mode"] == "practice":
                    # 測驗進行中：練習環境一律停止（釋放資源、避免測驗中使用）
                    await instances.stop_many(
                        db.query("SELECT * FROM instances WHERE set_id=? AND status='running'", (s["id"],)))
            await instances.stop_idle(int(db.get_setting("idle_stop_min") or 30))
        except Exception:  # noqa: BLE001
            log.exception("background loop")
        await asyncio.sleep(30)


async def _attempt_loop() -> None:
    """每 5 秒處理到期的限時作答"""
    while True:
        try:
            await attempts.sweep()
        except Exception:  # noqa: BLE001
            log.exception("attempt loop")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    # 重開服務時，未完成的檢查標記為錯誤
    db.execute("UPDATE check_runs SET status='error', message='服務重啟，檢查中斷' "
               "WHERE status IN ('queued','running')")
    db.execute("UPDATE templates SET status=CASE WHEN vmid IS NULL THEN 'failed' ELSE 'ready' END "
               "WHERE status='building'")
    tasks = [asyncio.create_task(_background()), asyncio.create_task(_attempt_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="lsa_session",
                   max_age=12 * 3600, same_site="lax")
app.add_middleware(i18n.LangMiddleware)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

from . import admin  # noqa: E402

app.include_router(admin.router)


# ---------------- helpers ----------------
class Redirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


@app.exception_handler(Redirect)
async def _redir(_req, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


def current_account(request: Request | WebSocket, require_bound: bool = True) -> dict:
    aid = request.session.get("aid")
    acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,)) if aid else None
    if acc is None:
        raise Redirect("/login")
    if require_bound and not acc["participant_code"]:
        raise Redirect("/bind")
    return acc


def log_activity(request: Request | WebSocket, acc: dict, sid: int | None, kind: str, qid: str | None = None) -> None:
    """出席用活動紀錄"""
    ip = request.client.host if request.client else None
    db.execute("INSERT INTO activity(account_id,set_id,kind,qid,ip,at) VALUES(?,?,?,?,?,?)",
               (acc["id"], sid, kind, qid, ip, datetime.now().isoformat(timespec="seconds")))


def api_error(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _set_or_404(sid: int) -> dict:
    s = sets.get(sid)
    if s is None or not s["visible"]:
        raise Redirect("/")
    return s


def _participant_question(s: dict, qid: str) -> questions.Question | None:
    if not sets.has_question(s["id"], qid):
        return None
    return questions.get(qid)


def _ctx(s: dict) -> dict:
    """樣板共用：題組資訊與網址"""
    return {"s": s, "sbase": f"/s/{s['id']}", "seconds_left": sets.seconds_left(s)}


# ---------------- 語言 ----------------
@app.get("/lang/{code}")
async def set_language(request: Request, code: str):
    nxt = request.query_params.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    resp = RedirectResponse(nxt, status_code=303)
    if code in i18n.languages():
        resp.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, samesite="lax")
    return resp


# ---------------- 登入 / 綁定編號 ----------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    acc = db.one("SELECT * FROM accounts WHERE username=?", (username.strip(),))
    if acc is None or not verify_password(password, acc["password_hash"]):
        return templates.TemplateResponse(request, "login.html", {"error": _("帳號或密碼錯誤")}, status_code=401)
    request.session.clear()
    request.session["aid"] = acc["id"]
    log_activity(request, acc, None, "login")
    return RedirectResponse("/" if acc["participant_code"] else "/bind", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/bind", response_class=HTMLResponse)
async def bind_page(request: Request):
    acc = current_account(request, require_bound=False)
    if acc["participant_code"]:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "bind.html", {"acc": acc, "error": None})


@app.post("/bind")
async def bind(request: Request, participant_code: str = Form(...), participant_code2: str = Form(...)):
    acc = current_account(request, require_bound=False)
    if acc["participant_code"]:
        return RedirectResponse("/", status_code=303)
    sid = participant_code.strip()
    err = None
    if sid != participant_code2.strip():
        err = _("兩次輸入的編號不一致")
    elif (not re.match(db.get_setting("code_pattern"), sid)
          or not re.fullmatch(r"[\w.-]+", sid) or sid.startswith(".")):   # 一律只允許字母、數字、_ . -（見 admin.py 匯入）
        err = _("編號格式不正確")
    elif db.one("SELECT id FROM accounts WHERE participant_code=?", (sid,)):
        err = _("此編號已被其他帳號使用，請洽管理者")
    if err:
        return templates.TemplateResponse(request, "bind.html", {"acc": acc, "error": err}, status_code=400)
    db.execute("UPDATE accounts SET participant_code=?, bound_at=? WHERE id=? AND participant_code IS NULL",
               (sid, datetime.now().isoformat(timespec="seconds"), acc["id"]))
    return RedirectResponse("/", status_code=303)


# ---------------- 題組列表 / 題目列表 / 作答頁 ----------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    acc = current_account(request)
    active = sets.open_assessment()
    if active:   # 測驗進行中直接進測驗
        return RedirectResponse(f"/s/{active['id']}", status_code=303)
    vis = sets.visible_sets()
    if len(vis) == 1:
        return RedirectResponse(f"/s/{vis[0]['id']}", status_code=303)
    items = []
    for s in vis:
        start, end = sets.window(s)
        qs = sets.questions_of(s["id"])
        got = total = 0.0
        for q in qs:
            sc, t = grader.question_score(acc["id"], s, q)
            got += sc or 0
            total += t
        items.append({"s": s, "state": sets.participant_state(s), "start": start, "end": end,
                      "n": len(qs), "got": got, "total": total})
    return templates.TemplateResponse(request, "home.html", {"acc": acc, "items": items})


def _question_rows(acc: dict, s: dict) -> list[dict]:
    rows = []
    for q in sets.questions_of(s["id"]):
        score, total = grader.question_score(acc["id"], s, q)
        inst = instances.get(acc["id"], s["id"], q.qid)
        rows.append({"q": q, "score": score, "total": total,
                     "inst_status": inst["status"] if inst else None,
                     "timing": attempts.status(acc["id"], s["id"], q.qid)})
    return rows


@app.get("/s/{sid}", response_class=HTMLResponse)
async def dashboard(request: Request, sid: int):
    acc = current_account(request)
    s = _set_or_404(sid)
    st = sets.participant_state(s)
    rows = _question_rows(acc, s) if st in ("open", "closed", "locked") else []
    if st == "open":
        log_activity(request, acc, sid, "view")
    got = sum(r["score"] or 0 for r in rows)
    total = sum(r["total"] for r in rows)
    start, end = sets.window(s)
    return templates.TemplateResponse(request, "dashboard.html", {
        **_ctx(s), "acc": acc, "rows": rows, "state": st, "got": got, "total": total,
        "start": start, "end": end, "n_sets": len(sets.visible_sets()),
        "to_start": int((start - datetime.now()).total_seconds()) if start else 0,
    })


@app.get("/s/{sid}/q/{qid}", response_class=HTMLResponse)
async def question_page(request: Request, sid: int, qid: str):
    acc = current_account(request)
    s = _set_or_404(sid)
    q = _participant_question(s, qid) if sets.can_work(s) else None
    if q is None:
        return RedirectResponse(f"/s/{sid}", status_code=303)
    score, total = grader.question_score(acc["id"], s, q)
    return templates.TemplateResponse(request, "question.html", {
        **_ctx(s), "acc": acc, "q": q, "score": score, "total": total,
        "timing": attempts.status(acc["id"], sid, qid),
    })


@app.get("/s/{sid}/q/{qid}/term", response_class=HTMLResponse)
async def term_page(request: Request, sid: int, qid: str):
    """只有終端機的頁面（給「在新視窗開啟」用）"""
    acc = current_account(request)
    s = _set_or_404(sid)
    q = _participant_question(s, qid) if sets.can_work(s) else None
    if q is None:
        return HTMLResponse(f"<p>{_('題組未開放或題目不存在。')}</p>", status_code=404)
    return templates.TemplateResponse(request, "term.html", {
        **_ctx(s), "acc": acc, "q": q, "timing": attempts.status(acc["id"], sid, qid)})


@app.get("/api/state")
async def api_state():
    e = sets.open_assessment()
    return {"assessment": e and {"id": e["id"], "name": e["name"], "seconds_left": sets.seconds_left(e)}}


def _api_target(request: Request, sid: int, qid: str):
    """回傳 (acc, set, question) 或 JSONResponse 錯誤"""
    acc = current_account(request)
    s = sets.get(sid)
    if s is None or not s["visible"]:
        return api_error(_("題組不存在"), 404)
    if (why := sets.blocked_reason(s)):
        return api_error(why)
    q = _participant_question(s, qid)
    if q is None:
        return api_error(_("題目不存在"), 404)
    return acc, s, q


@app.post("/api/s/{sid}/q/{qid}/start")
async def api_start(request: Request, sid: int, qid: str):
    t = _api_target(request, sid, qid)
    if isinstance(t, JSONResponse):
        return t
    acc, s, q = t
    await attempts.abandon_others(acc["id"], sid, qid)   # 改開別題：其他限時作答中斷
    timing = attempts.status(acc["id"], sid, qid)
    new_attempt = timing["timed"] and not timing["active"]
    if new_attempt:
        if request.query_params.get("begin") != "1":
            return api_error(_("本題限時作答，請按「開始作答」"), 409)
        if not timing["can_start"]:
            return api_error(timing["reason"])
        if attempts.used(acc["id"], sid, qid) and instances.get(acc["id"], sid, qid):
            await instances.reset(acc, sid, q)   # 上一次作答留下的環境，確保從頭開始
    attempts.touch(acc["id"], sid, qid)
    log_activity(request, acc, sid, "start", qid)
    try:
        inst = await instances.ensure_running(acc, sid, q, need_ssh=False)
    except instances.InstanceError as e:
        return api_error(i18n.translate_stored(str(e)), 503)
    attempts.touch(acc["id"], sid, qid)
    if new_attempt:   # 環境就緒後才開始計時
        attempts.begin(acc["id"], sid, qid)
    return {"ok": True, "status": inst["status"], "timing": attempts.status(acc["id"], sid, qid)}


def _timed_block(acc: dict, sid: int, qid: str) -> str | None:
    """限時題目沒有進行中的作答時，回傳不能操作的原因"""
    t = attempts.status(acc["id"], sid, qid)
    if t["timed"] and not t["active"]:
        return _("本題作答時間已結束") if attempts.used(acc["id"], sid, qid) else _("請先按「開始作答」")
    return None


@app.post("/api/s/{sid}/q/{qid}/reset")
async def api_reset(request: Request, sid: int, qid: str):
    t = _api_target(request, sid, qid)
    if isinstance(t, JSONResponse):
        return t
    acc, s, q = t
    if (why := _timed_block(acc, sid, qid)):
        return api_error(why)
    last = grader.last_run(acc["id"], sid, qid)
    if last and last["status"] in ("queued", "running"):
        return api_error(_("檢查進行中，請稍後再重置"))
    attempts.touch(acc["id"], sid, qid)
    await instances.reset(acc, sid, q)
    attempts.touch(acc["id"], sid, qid)
    log.info("reset %s set=%s %s", acc["participant_code"], sid, qid)
    return {"ok": True}


@app.post("/api/s/{sid}/q/{qid}/check")
async def api_check(request: Request, sid: int, qid: str):
    t = _api_target(request, sid, qid)
    if isinstance(t, JSONResponse):
        return t
    acc, s, q = t
    if (why := _timed_block(acc, sid, qid)):
        return api_error(why)
    if instances.get(acc["id"], sid, qid) is None:
        return api_error(_("請先開啟本題環境"))
    reason = grader.can_check(acc["id"], s, qid)
    if reason:
        return api_error(reason, 429)
    run_id = grader.enqueue(acc, sid, q)
    return {"ok": True, "run_id": run_id}


@app.get("/api/s/{sid}/q/{qid}/runs")
async def api_runs(request: Request, sid: int, qid: str):
    acc = current_account(request)
    s = sets.get(sid)
    q = _participant_question(s, qid) if s and s["visible"] else None
    if q is None:
        return api_error(_("題目不存在"), 404)
    show = bool(s["show_detail"])
    runs = db.query(
        "SELECT id,status,score,max_score,results_json,message,started_at,finished_at "
        "FROM check_runs WHERE account_id=? AND set_id=? AND qid=? AND is_final=0 ORDER BY id DESC LIMIT 20",
        (acc["id"], sid, qid))
    for r in runs:
        items = json.loads(r.pop("results_json") or "[]")
        for i in items:
            i["desc"] = q.check_desc(i["id"])
        r["items"] = items if show else []
        r["message"] = i18n.translate_stored(r["message"])
    score, total = grader.question_score(acc["id"], s, q)
    return {"ok": True, "runs": runs, "score": score, "total": total,
            "policy": s["score_policy"], "timing": attempts.status(acc["id"], sid, qid)}


@app.websocket("/ws/shell/{sid}/{qid}")
async def ws_shell(ws: WebSocket, sid: int, qid: str):
    try:
        acc = current_account(ws)
    except Redirect:
        await ws.close(code=4401)
        return
    s = sets.get(sid)
    q = _participant_question(s, qid) if s and s["visible"] else None
    inst = instances.get(acc["id"], sid, qid) if q else None
    await ws.accept()
    blocked = (sets.blocked_reason(s) or _timed_block(acc, sid, qid)) if q else _("題目不存在")
    if inst is None or inst["status"] != "running" or blocked:
        await ws.send_bytes(f"\r\n{blocked or _('環境尚未啟動。')}\r\n".encode())
        await ws.close()
        return

    def still_open() -> str | None:
        cur = sets.get(sid)
        why = sets.blocked_reason(cur) if cur else _("題組已刪除")
        if why:
            return (_("測驗時間結束，連線已關閉。") if cur and cur["mode"] == "assessment"
                    else _("{why}，連線已關閉。", why=why))
        if _timed_block(acc, sid, qid):
            return _("本題作答時間到，連線已關閉，環境將重置。")
        return None
    attempts.shell_opened(acc["id"], sid, qid)
    log_activity(ws, acc, sid, "shell", qid)
    try:
        await shell.serve(ws, acc, inst, still_open, block_paste=bool(s["block_paste"]))
    finally:
        attempts.shell_closed(acc["id"], sid, qid)
