"""PVE 主控台（termproxy）的 websocket 用戶端：SSH 連不上時的備援連線，完全不經過作答者環境的網路。

協定（與 PVE 網頁的 xterm.js 主控台相同）：
- 連上後先送 "<user>:<ticket>\\n"，伺服器回 "OK"，之後伺服器送來的都是畫面輸出
- 鍵盤輸入 "0:<位元組數>:<資料>"、改變視窗大小 "1:<cols>:<rows>:"、保持連線 "2"
"""
from __future__ import annotations

import asyncio
import ssl

import websockets


class ConsoleError(Exception):
    pass


class Console:
    def __init__(self, info: dict) -> None:
        self.info = info
        self.ws = None
        self.leftover = b""
        self._ping: asyncio.Task | None = None

    async def open(self, cols: int, rows: int) -> None:
        ctx = None
        if self.info["url"].startswith("wss:"):
            ctx = ssl.create_default_context()
            if not self.info.get("verify_ssl"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        try:
            self.ws = await websockets.connect(self.info["url"], ssl=ctx, additional_headers=self.info["headers"],
                                               subprotocols=["binary"], max_size=None, open_timeout=10)
            await self.ws.send(self.info["hello"])
            first = await asyncio.wait_for(self.ws.recv(), 10)
        except Exception as e:  # noqa: BLE001
            await self.close()
            raise ConsoleError(str(e) or type(e).__name__) from e
        first = first if isinstance(first, bytes) else first.encode()
        if not first.startswith(b"OK"):
            await self.close()
            raise ConsoleError("authentication failed")
        self.leftover = first[2:]
        await self.resize(cols, rows)
        self._ping = asyncio.create_task(self._keepalive())

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self.ws.send(b"2")

    async def read(self) -> bytes:
        """回傳畫面輸出；連線結束回傳 b"" """
        if self.leftover:
            data, self.leftover = self.leftover, b""
            return data
        try:
            m = await self.ws.recv()
        except websockets.ConnectionClosed:
            return b""
        return m if isinstance(m, bytes) else m.encode()

    async def write(self, data: bytes) -> None:
        await self.ws.send(b"0:%d:" % len(data) + data)

    async def resize(self, cols: int, rows: int) -> None:
        await self.ws.send(b"1:%d:%d:" % (int(cols), int(rows)))

    async def close(self) -> None:
        if self._ping:
            self._ping.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
