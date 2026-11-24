"""Web shell：瀏覽器 xterm.js <-websocket-> 後端 <-SSH-> 作答者實例。
SSH 連不上時改走 PVE 主控台（app/console.py），作答者把網路或 sshd 改壞了也還能登入修好。"""
from __future__ import annotations

import asyncio
import json
import logging

import asyncssh
from fastapi import WebSocket, WebSocketDisconnect

from . import instances
from .console import Console
from .pve import client
from .recording import Recorder
from .config import cfg
from .i18n import _

log = logging.getLogger("shell")


async def _send_msg(ws: WebSocket, text: str, rec: Recorder | None = None) -> None:
    data = f"\r\n\x1b[33m{text}\x1b[0m\r\n".encode()
    if rec is not None:
        rec.output(data)          # 系統訊息也錄進去，重播時看得到
    await ws.send_bytes(data)


def looks_like_paste(data: bytes) -> bool:
    """一般按鍵每次只送 1 個字元或一個 ESC 控制序列；貼上會一次送多個字元。
    bash 開啟 bracketed paste 時，貼上內容會被 ESC[200~ ... ESC[201~ 包住。"""
    if b"\x1b[200~" in data:
        return True
    if data.startswith(b"\x1b"):
        return False
    return len(data.decode("utf-8", "replace")) > 3


class _SSH:
    """一般連線：lsa-web 透過管理網路 SSH 進作答者環境"""

    def __init__(self, conn, proc) -> None:
        self.conn, self.proc = conn, proc

    @classmethod
    async def open(cls, inst: dict, cols: int, rows: int) -> "_SSH":
        conn = await asyncssh.connect(
            inst["ip"], username=cfg.ssh_user, client_keys=[cfg.ssh_key], known_hosts=None,
            connect_timeout=10, keepalive_interval=10, keepalive_count_max=3)
        try:
            proc = await conn.create_process(term_type="xterm-256color", term_size=(cols, rows),
                                             encoding=None, stderr=asyncssh.STDOUT)
        except Exception:
            conn.close()
            raise
        return cls(conn, proc)

    async def read(self) -> bytes:
        return await self.proc.stdout.read(65536)

    async def write(self, data: bytes) -> None:
        self.proc.stdin.write(data)

    async def resize(self, cols: int, rows: int) -> None:
        self.proc.change_terminal_size(cols, rows)

    async def close(self) -> None:
        self.conn.close()


async def _open_terminal(ws: WebSocket, rec: Recorder, inst: dict, cols: int, rows: int,
                         force_console: bool = False, allow_console: bool = True, switchable: bool = True):
    """先用 SSH；連不上（作答者改壞了網路、防火牆或 sshd）就改用 PVE 主控台，不經過網路。
    force_console：直接用主控台（作答者按了「改用主控台」，或題目 terminal 是 console／console-only）。
    allow_console=False（terminal: ssh-only）：SSH 連不上也不改用主控台。
    switchable=False（-only）：訊息不提「改用 SSH」。回傳 None 表示連不上（訊息已經送出）。"""
    err = ""
    if not force_console:
        try:
            return await _SSH.open(inst, cols, rows)
        except Exception as e:  # noqa: BLE001
            err = str(e) or type(e).__name__
        if not allow_console:
            await _send_msg(ws, _("無法連線到你的環境（{e}）。若改過 sshd / 帳號 {user} / 網路，請重置本題。",
                                  e=err, user=cfg.ssh_user), rec)
            return None
        log.info("ssh to %s failed (%s), waiting before console fallback", inst["vmid"], err)
        # 剛重開機（例如檢查時重啟）的環境 SSH 還沒起來：先等一下，不要馬上改用主控台
        await _send_msg(ws, _("SSH 連不上（{e}），等待環境回應…", e=err), rec)
        if await instances.wait_ssh(inst["ip"], 30 if inst["kind"] == "qemu" else 10):
            try:
                return await _SSH.open(inst, cols, rows)
            except Exception as e:  # noqa: BLE001
                err = str(e) or type(e).__name__
    try:
        info = await asyncio.to_thread(client().termproxy, inst["kind"], inst["vmid"])
        term = Console(info)
        await term.open(cols, rows)
    except Exception as e:  # noqa: BLE001
        log.warning("console for %s failed: %r", inst["vmid"], e)
        if force_console:
            await _send_msg(ws, _("主控台連線失敗（{e}）。請按「重新連線」，或重置本題。", e=e), rec)
        else:
            await _send_msg(ws, _("無法連線到你的環境（{e}）。若改過 sshd / 帳號 {user} / 網路，請重置本題。",
                                  e=err, user=cfg.ssh_user), rec)
        return None
    log.info("console for %s (%s)", inst["vmid"], "requested" if force_console else "fallback")
    rec.mark("CONSOLE: requested" if force_console else "CONSOLE: SSH unreachable")
    if force_console and not switchable:
        msg = _("本題使用主控台連線（不經過網路）。登入帳號 {user}，預設密碼 user。", user=cfg.ssh_user)
    elif force_console:
        msg = _("主控台連線（不經過網路）。登入帳號 {user}，預設密碼 user。按「改用 SSH」可以回到一般連線。",
                user=cfg.ssh_user)
    else:
        msg = _("SSH 連不上，已改用主控台連線（不經過網路）。登入帳號 {user}，預設密碼 user。把網路或 sshd 修好後，按「改用 SSH」就會回到一般連線。",
                user=cfg.ssh_user)
    if inst["kind"] == "qemu":
        msg += "\r\n" + _("序列主控台不會自動調整畫面大小，登入後可以執行：stty rows {rows} cols {cols}",
                          rows=rows, cols=cols)
    await _send_msg(ws, msg, rec)
    await ws.send_text(json.dumps({"type": "console", "forced": force_console}))
    await term.write(b"\r")          # 讓主控台重新顯示登入提示（或已登入的 shell 提示字元）
    return term


async def serve(ws: WebSocket, account: dict, inst: dict, still_open, block_paste: bool = False,
                force_console: bool = False, allow_console: bool = True, switchable: bool = True) -> None:
    """still_open(): 回傳 None 表示可繼續；回傳字串則顯示訊息並斷線"""
    cols = int(ws.query_params.get("cols", 100))
    rows = int(ws.query_params.get("rows", 30))
    rec = Recorder(account["id"], inst["set_id"], inst["qid"], cols, rows,
                   title=f"{account.get('participant_code') or account.get('username')} · {inst['qid']}")
    instances.active_shells[inst["vmid"]] += 1
    kicked = asyncio.Event()
    instances.shell_kicks[inst["vmid"]].add(kicked)
    try:
        if cfg.mock:
            await _mock(ws, rec, inst, still_open, kicked, block_paste)
            return
        term = await _open_terminal(ws, rec, inst, cols, rows, force_console, allow_console, switchable)
        if term is None:
            return
        try:
            async def pump_out():
                while True:
                    data = await term.read()
                    if not data:
                        break
                    rec.output(data)
                    await ws.send_bytes(data)

            async def pump_in():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        data = msg["bytes"]
                        if block_paste and looks_like_paste(data):
                            rec.paste_blocked(data)
                            await ws.send_text(json.dumps({"type": "notice", "text": _("本次測驗禁止貼上")}))
                            continue
                        rec.input(data)
                        await term.write(data)
                    elif msg.get("text"):
                        m = json.loads(msg["text"])
                        if m.get("type") == "resize":
                            await term.resize(int(m["cols"]), int(m["rows"]))
                            rec.resize(m["cols"], m["rows"])
                        elif m.get("type") == "ping":
                            instances.touch(inst)

            async def deadline():
                while (msg := still_open()) is None:
                    try:
                        await asyncio.wait_for(kicked.wait(), 2)
                    except asyncio.TimeoutError:
                        continue
                    msg = _("你開啟了其他題目，本題環境已暫停（檔案都會保留）。要繼續作答請按「重新連線」。")
                    try:
                        await ws.send_text(json.dumps({"type": "paused"}))
                    except Exception:  # noqa: BLE001
                        pass
                    break
                await _send_msg(ws, msg, rec)

            tasks = [asyncio.create_task(t()) for t in (pump_out, pump_in, deadline)]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                    log.warning("shell error: %r", t.exception())
        finally:
            await term.close()
    finally:
        instances.active_shells[inst["vmid"]] -= 1
        instances.shell_kicks[inst["vmid"]].discard(kicked)
        instances.touch(inst)
        rec.close()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def _mock(ws: WebSocket, rec: Recorder, inst: dict, still_open, kicked: asyncio.Event,
                block_paste: bool = False) -> None:
    """mock 模式：不連真實環境，輸入會回顯。輸入 `mock-pass [檢查id ...]` 讓之後的檢查通過（測試用）"""
    banner = (_("（MOCK 模式：未連接真實環境，輸入會回顯）") + "\r\n$ ").encode()
    rec.output(banner)
    await ws.send_bytes(banner)
    line = b""
    while True:
        if kicked.is_set():
            await ws.send_text(json.dumps({"type": "paused"}))
            await _send_msg(ws, _("你開啟了其他題目，本題環境已暫停（檔案都會保留）。要繼續作答請按「重新連線」。"), rec)
            return
        if (m := still_open()) is not None:
            await _send_msg(ws, m, rec)
            return
        try:
            msg = await asyncio.wait_for(ws.receive(), 1)
        except asyncio.TimeoutError:
            continue
        if msg["type"] == "websocket.disconnect":
            return
        if msg.get("bytes") is not None:
            data = msg["bytes"]
            if block_paste and looks_like_paste(data):
                rec.paste_blocked(data)
                await ws.send_text(json.dumps({"type": "notice", "text": _("本次測驗禁止貼上")}))
                continue
            rec.input(data)
            line += data
            if b"\r" in line:
                cmd, line = line.split(b"\r", 1)[0].decode("utf-8", "replace").strip(), b""
                if cmd.startswith("mock-pass"):
                    client().mock_set_pass(inst["vmid"], cmd.split()[1:])
            echo = data.replace(b"\r", b"\r\n$ ")
            rec.output(echo)
            await ws.send_bytes(echo)
