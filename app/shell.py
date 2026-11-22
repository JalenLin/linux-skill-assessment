"""Web shell：瀏覽器 xterm.js <-websocket-> 後端 <-SSH-> 作答者實例。"""
from __future__ import annotations

import asyncio
import json
import logging

import asyncssh
from fastapi import WebSocket, WebSocketDisconnect

from . import instances
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


async def serve(ws: WebSocket, account: dict, inst: dict, still_open, block_paste: bool = False) -> None:
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
        try:
            conn = await asyncssh.connect(
                inst["ip"], username=cfg.ssh_user, client_keys=[cfg.ssh_key],
                known_hosts=None, connect_timeout=15, keepalive_interval=30)
        except Exception as e:  # noqa: BLE001
            await _send_msg(ws, _("無法連線到你的環境（{e}）。若改過 sshd / 帳號 {user} / 網路，請重置本題。",
                                  e=e, user=cfg.ssh_user), rec)
            return
        async with conn:
            proc = await conn.create_process(
                term_type="xterm-256color", term_size=(cols, rows),
                encoding=None, stderr=asyncssh.STDOUT)

            async def pump_out():
                while True:
                    data = await proc.stdout.read(65536)
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
                        proc.stdin.write(data)
                    elif msg.get("text"):
                        m = json.loads(msg["text"])
                        if m.get("type") == "resize":
                            proc.change_terminal_size(int(m["cols"]), int(m["rows"]))
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
    from .pve import client
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
