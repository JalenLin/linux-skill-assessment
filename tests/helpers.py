"""整合測試／端對端測試共用的 HTTP 與終端機操作"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

import requests
import websockets

FMT = "%Y-%m-%dT%H:%M"


def t(**kw) -> str:
    """現在 + 時間差，題組時間欄位的格式"""
    return (datetime.now() + timedelta(**kw)).strftime(FMT)


def flash(r: requests.Response) -> str:
    """後台操作後 redirect 帶的訊息"""
    m = re.search(r"msg=([^&#]+)", r.headers.get("location", ""))
    return unquote(m.group(1)) if m else ""


class Admin:
    def __init__(self, base: str, password: str, user: str = "admin"):
        self.base = base
        self.s = requests.Session()
        r = self.s.post(base + "/admin/login", data={"username": user, "password": password}, allow_redirects=False)
        assert r.status_code == 303, "admin login failed"

    def get(self, path, **kw):
        return self.s.get(self.base + path, **kw)

    def post(self, path, **kw):
        kw.setdefault("allow_redirects", False)
        return self.s.post(self.base + path, **kw)

    # ---- 帳號 ----
    def accounts(self) -> dict[str, dict]:
        rows = self.get("/admin/accounts.csv").text.lstrip("﻿").splitlines()[1:]
        out = {}
        for line in rows:
            u, pw, code, note = (line.split(",") + ["", "", ""])[:4]
            out[u] = {"password": pw, "code": code, "note": note}
        return out

    def generate(self, prefix: str, n: int) -> dict[str, str]:
        self.post("/admin/accounts/generate", data={"prefix": prefix, "count": str(n), "start": "1"})
        return {u: a["password"] for u, a in self.accounts().items() if u.startswith(prefix)}

    def account_id(self, username: str) -> int:
        html = self.get("/admin/accounts").text
        for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
            if f">{username}<" in row:
                ids = re.findall(r"/admin/accounts/(\d+)/", row)
                if ids:
                    return int(ids[0])
        raise KeyError(username)

    # ---- 題組 ----
    def create_set(self, name: str, mode: str = "practice", **settings) -> int:
        r = self.post("/admin/sets/create", data={"name": name, "mode": mode, "copy_from": ""})
        sid = int(re.search(r"/admin/sets/(\d+)", r.headers["location"]).group(1))
        self.save_set(sid, name=name, mode=mode, **settings)
        return sid

    def save_set(self, sid: int, **kw):
        data = {"name": kw.pop("name", f"set{sid}"), "mode": kw.pop("mode", "practice"), "visible": "on",
                "start_at": "", "end_at": "", "score_policy": "best", "show_detail": "on", "cooldown_sec": "0",
                "notice": "", "sort_order": "0"}
        data.update(kw)
        data = {k: v for k, v in data.items() if v is not None}
        return self.post(f"/admin/sets/{sid}", data=data)

    def add_question(self, sid: int, qid: str, time_limit_min: int = 0, max_attempts: int = 0, **points):
        data = {"enabled": "on", "sort_order": "0", "time_limit_min": str(time_limit_min),
                "max_attempts": str(max_attempts)}
        data.update({f"points_{k}": str(v) for k, v in points.items()})
        return self.post(f"/admin/sets/{sid}/q/{qid}", data=data)

    def delete_set(self, sid: int, name: str):
        self.post(f"/admin/sets/{sid}/destroy_instances", data={"confirm": "DELETE"})
        return self.post(f"/admin/sets/{sid}/delete", data={"confirm": name})

    # ---- 題庫 ----
    def build(self, qid: str, timeout: float = 900) -> dict:
        """建置範本＋自我測試，回傳自我測試結果"""
        self.post(f"/admin/questions/{qid}/build")
        end = time.time() + timeout
        time.sleep(1)
        while time.time() < end:
            j = self.get(f"/admin/questions/{qid}/status.json").json()
            if not j["building"] and j["template"] and j["template"]["status"] in ("ready", "failed"):
                if j["template"]["status"] == "failed" or j["selftest"]:
                    return j
            time.sleep(2)
        raise TimeoutError(qid)


class Participant:
    def __init__(self, base: str, username: str, password: str):
        self.base = base
        self.s = requests.Session()
        r = self.s.post(base + "/login", data={"username": username, "password": password}, allow_redirects=False)
        assert r.status_code == 303, f"login failed for {username}"

    def get(self, path, **kw):
        return self.s.get(self.base + path, **kw)

    def post(self, path, **kw):
        kw.setdefault("allow_redirects", False)
        return self.s.post(self.base + path, **kw)

    def bind(self, code: str, code2: str | None = None):
        return self.post("/bind", data={"participant_code": code, "participant_code2": code2 or code})

    def api(self, sid: int, qid: str, action: str, **kw) -> dict:
        r = self.s.post(f"{self.base}/api/s/{sid}/q/{qid}/{action}", timeout=900, **kw)
        return r.json()

    def runs(self, sid: int, qid: str) -> list[dict]:
        return self.s.get(f"{self.base}/api/s/{sid}/q/{qid}/runs").json()["runs"]

    def check(self, sid: int, qid: str, timeout: float = 600) -> dict:
        """按檢查並等結果"""
        j = self.api(sid, qid, "check")
        assert j.get("ok"), j
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1)
            r = self.runs(sid, qid)[0]
            if r["status"] in ("done", "error"):
                return r
        raise TimeoutError("check")

    def terminal(self, sid: int, qid: str) -> "Terminal":
        return Terminal(self, sid, qid)


class Terminal:
    """同步操作 web 終端機（底下用 websockets，在自己的 event loop 跑）"""

    def __init__(self, p: Participant, sid: int, qid: str):
        self.loop = asyncio.new_event_loop()
        ck = "; ".join(f"{k}={v}" for k, v in p.s.cookies.items())
        url = p.base.replace("http", "ws", 1) + f"/ws/shell/{sid}/{qid}?cols=120&rows=30"
        self.ws = self.loop.run_until_complete(
            websockets.connect(url, additional_headers={"Cookie": ck}, open_timeout=120, max_size=None))
        self.texts: list[str] = []     # 伺服器的控制訊息（JSON 字串）

    def read(self, until: str | None = None, timeout: float = 10) -> str:
        async def go():
            buf = ""
            try:
                while until is None or until not in buf:
                    m = await asyncio.wait_for(self.ws.recv(), timeout)
                    if isinstance(m, str):
                        self.texts.append(m)
                        buf += m
                    else:
                        buf += m.decode("utf-8", "replace")
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                if until is not None and until not in buf:
                    raise AssertionError(f"did not see {until!r} in terminal output: {buf[-500:]!r}")
            return buf
        return self.loop.run_until_complete(go())

    def run(self, cmd: str, timeout: float = 60) -> str:
        """送一行指令，等到結束標記出現；回傳輸出"""
        mark = f"__END{int(time.time() * 1000) % 100000}__"
        self.send(f"{cmd}; echo {mark[:4]}''{mark[4:]}\r")      # 回顯的指令本身不含完整標記
        out = self.read(until=mark, timeout=timeout)
        return out.split(mark)[0]

    def send(self, data: str):
        self.loop.run_until_complete(self.ws.send(data.encode()))

    @property
    def closed(self) -> bool:
        return self.ws.close_code is not None

    def close(self):
        try:
            self.loop.run_until_complete(self.ws.close())
        finally:
            self.loop.close()
