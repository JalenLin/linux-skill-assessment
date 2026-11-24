"""SSH 連不上時改用 PVE 主控台（用一個假的 termproxy websocket 伺服器）"""
import asyncio
import json

import websockets

from app import console, shell
from app.recording import read


class FakeTermproxy:
    """模仿 PVE vncwebsocket：先驗證 "user:ticket\\n"，回 OK，之後把輸入回顯"""

    def __init__(self, ticket=b"lsa@pve!web:TICKET\n"):
        self.ticket, self.got = ticket, []

    async def handler(self, ws):
        assert ws.request.headers["Authorization"].startswith("PVEAPIToken=")
        if await ws.recv() != self.ticket:
            await ws.send(b"NO")
            return
        await ws.send(b"OKlogin: ")
        async for m in ws:
            self.got.append(m)
            if m.startswith(b"0:"):
                _, n, rest = m.split(b":", 2)
                assert int(n) == len(rest)
                await ws.send(rest)

    async def __aenter__(self):
        self.server = await websockets.serve(self.handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.info = {"url": f"ws://127.0.0.1:{port}/", "headers": {"Authorization": "PVEAPIToken=x"},
                     "hello": b"lsa@pve!web:TICKET\n", "verify_ssl": False}
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()


def test_console_protocol():
    async def go():
        async with FakeTermproxy() as fake:
            c = console.Console(fake.info)
            await c.open(120, 40)
            assert await c.read() == b"login: "          # "OK" 後面跟著的輸出不能掉
            await c.write("中".encode())
            assert await c.read() == "中".encode()
            await c.close()
            assert fake.got[0] == b"1:120:40:"
            assert fake.got[1] == b"0:3:" + "中".encode()

            bad = console.Console(dict(fake.info, hello=b"wrong\n"))
            try:
                await bad.open(80, 24)
                raise AssertionError("should fail")
            except console.ConsoleError:
                pass
    asyncio.run(go())


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_bytes(self, b):
        self.sent.append(b)

    async def send_text(self, t):
        self.sent.append(json.loads(t))


def test_fallback_to_console(monkeypatch):
    async def go():
        async with FakeTermproxy() as fake:
            async def no_ssh(*a, **kw):
                raise ConnectionRefusedError("refused")

            async def wait_ssh(ip, timeout):
                return False

            class P:
                def termproxy(self, kind, vmid):
                    return fake.info
            monkeypatch.setattr(shell._SSH, "open", no_ssh)
            monkeypatch.setattr(shell.instances, "wait_ssh", wait_ssh)
            monkeypatch.setattr(shell, "client", lambda: P())
            ws = FakeWS()
            rec = shell.Recorder(950, 3, "example-hello", 100, 30)
            term = await shell._open_terminal(ws, rec, {"vmid": 10001, "kind": "qemu", "ip": "10.10.0.9"}, 100, 30)
            assert isinstance(term, console.Console)
            assert {"type": "console", "forced": False} in ws.sent
            text = b"".join(x for x in ws.sent if isinstance(x, bytes)).decode()
            assert "stty rows 30 cols 100" in text                 # VM 序列主控台的提示
            await term.close()
            rec.close()
            _, events = read(rec.path)
            assert ["m", "CONSOLE: SSH unreachable"] in [e[1:] for e in events]

            # 主控台也連不上：顯示原本的錯誤訊息，回傳 None
            class Broken:
                def termproxy(self, kind, vmid):
                    raise RuntimeError("403 Forbidden")
            monkeypatch.setattr(shell, "client", lambda: Broken())
            ws2 = FakeWS()
            rec2 = shell.Recorder(950, 3, "example-hello", 100, 30)
            assert await shell._open_terminal(ws2, rec2, {"vmid": 10001, "kind": "lxc", "ip": "10.10.0.9"}, 100, 30) is None
            assert "refused" in b"".join(x for x in ws2.sent if isinstance(x, bytes)).decode()
            rec2.close()
    asyncio.run(go())


def test_forced_console_skips_ssh(monkeypatch):
    """作答者按「改用主控台」：不試 SSH、不等待，直接連主控台"""
    async def go():
        async with FakeTermproxy() as fake:
            async def must_not_call(*a, **kw):
                raise AssertionError("SSH should not be tried")

            class P:
                def termproxy(self, kind, vmid):
                    return fake.info
            monkeypatch.setattr(shell._SSH, "open", must_not_call)
            monkeypatch.setattr(shell.instances, "wait_ssh", must_not_call)
            monkeypatch.setattr(shell, "client", lambda: P())
            ws = FakeWS()
            rec = shell.Recorder(951, 3, "example-hello", 100, 30)
            term = await shell._open_terminal(ws, rec, {"vmid": 10002, "kind": "lxc", "ip": "10.10.0.9"},
                                              100, 30, force_console=True)
            assert isinstance(term, console.Console)
            assert {"type": "console", "forced": True} in ws.sent
            await term.close()
            rec.close()
            _, events = read(rec.path)
            assert ["m", "CONSOLE: requested"] in [e[1:] for e in events]
    asyncio.run(go())


def test_terminal_mode_enforced_on_server():
    """-only 的題目：作答者改網址（console=0/1）也換不了方式"""
    from types import SimpleNamespace
    from app.main import _wants_console
    from app.questions import Question

    def q(mode):
        return Question(qid="x", title="x", kind="lxc", hostname="h", resources={}, checks=[], description_md="",
                        path=None, has_solution=False, build_version="", terminal=mode)

    def ws(v=None):
        return SimpleNamespace(query_params={} if v is None else {"console": v})
    assert [_wants_console(ws(v), q("ssh")) for v in (None, "0", "1")] == [False, False, True]
    assert [_wants_console(ws(v), q("console")) for v in (None, "0", "1")] == [True, False, True]
    assert [_wants_console(ws(v), q("console-only")) for v in (None, "0", "1")] == [True, True, True]
    assert [_wants_console(ws(v), q("ssh-only")) for v in (None, "0", "1")] == [False, False, False]


def test_ssh_only_has_no_console_fallback(monkeypatch):
    async def go():
        async def no_ssh(*a, **kw):
            raise ConnectionRefusedError("refused")

        class P:
            def termproxy(self, kind, vmid):
                raise AssertionError("console must not be used for ssh-only")
        monkeypatch.setattr(shell._SSH, "open", no_ssh)
        monkeypatch.setattr(shell, "client", lambda: P())
        ws = FakeWS()
        rec = shell.Recorder(952, 3, "example-hello", 100, 30)
        assert await shell._open_terminal(ws, rec, {"vmid": 10003, "kind": "lxc", "ip": "10.10.0.9"}, 100, 30,
                                          allow_console=False, switchable=False) is None
        assert "refused" in b"".join(x for x in ws.sent if isinstance(x, bytes)).decode()
        rec.close()
    asyncio.run(go())
