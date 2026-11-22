"""終端機操作紀錄：每次連線一個 asciicast v2 檔（https://docs.asciinema.org/manual/asciicast/v2/）。

- 存放：<data>/shell_logs/a<帳號 id>/s<題組 id>-<題目 id>-<YYYYmmdd-HHMMSS>.cast
  （依帳號存放：編號換綁、帳號刪除後重建都不會混在一起）
- 事件："o" 畫面輸出、"i" 鍵盤輸入、"r" 視窗大小改變、"m" 標記（被擋下的貼上）
- 後台用 asciinema-player 重播；另外提供輸入列表（點一下跳到該時間）與純文字檢視
- 刪除帳號 → 刪掉整個 a<id>/；刪除題組 → 刪掉所有 s<題組 id>-*.cast
"""
from __future__ import annotations

import codecs
import json
import re
import shutil
import time
from pathlib import Path

from .config import cfg

NAME_RE = re.compile(r"^s(\d+)-([a-z0-9][a-z0-9-]*)-(\d{8}-\d{6})(?:-\d+)?\.cast$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>78]")
PASTE_LABEL = "PASTE BLOCKED"
_KEYS = {"\x1b[A": "↑", "\x1b[B": "↓", "\x1b[C": "→", "\x1b[D": "←", "\x1bOA": "↑", "\x1bOB": "↓",
         "\x1bOC": "→", "\x1bOD": "←", "\x1b[H": "Home", "\x1b[F": "End", "\x1b[3~": "Del",
         "\x7f": "⌫", "\x08": "⌫", "\t": "⇥", "\x1b": "Esc"}
_KEY_RE = re.compile("|".join(re.escape(k) for k in sorted(_KEYS, key=len, reverse=True)) + r"|[\x00-\x1f]")


def root() -> Path:
    return cfg.data_path / "shell_logs"


def account_dir(account_id: int) -> Path:
    return root() / f"a{int(account_id)}"


class Recorder:
    """一次終端機連線的紀錄"""

    def __init__(self, account_id: int, set_id: int, qid: str, cols: int, rows: int, title: str = "") -> None:
        d = account_dir(account_id)
        d.mkdir(parents=True, exist_ok=True)
        base = f"s{int(set_id)}-{qid}-{time.strftime('%Y%m%d-%H%M%S')}"
        p, n = d / f"{base}.cast", 1
        while p.exists():
            p, n = d / f"{base}-{n}.cast", n + 1
        self.path = p
        self.f = open(p, "w", encoding="utf-8", buffering=1)
        self.t0 = time.monotonic()
        self._out = codecs.getincrementaldecoder("utf-8")("replace")
        self._in = codecs.getincrementaldecoder("utf-8")("replace")
        header = {"version": 2, "width": int(cols), "height": int(rows), "timestamp": int(time.time()),
                  "title": title, "env": {"TERM": "xterm-256color"}}
        self.f.write(json.dumps(header, ensure_ascii=False) + "\n")

    def _ev(self, kind: str, data: str) -> None:
        if data and not self.f.closed:
            self.f.write(json.dumps([round(time.monotonic() - self.t0, 3), kind, data], ensure_ascii=False) + "\n")

    def output(self, b: bytes) -> None:
        self._ev("o", self._out.decode(b))

    def input(self, b: bytes) -> None:
        self._ev("i", self._in.decode(b))

    def resize(self, cols: int, rows: int) -> None:
        self._ev("r", f"{int(cols)}x{int(rows)}")

    def paste_blocked(self, b: bytes) -> None:
        text = b.decode("utf-8", "replace").replace("\x1b[200~", "").replace("\x1b[201~", "")
        self._ev("m", f"{PASTE_LABEL}: {text[:500]}")

    def close(self) -> None:
        if self.f.closed:
            return
        self._ev("o", self._out.decode(b"", final=True))
        self._ev("i", self._in.decode(b"", final=True))
        self.f.close()


# ---------------- 讀取 ----------------
def safe_path(account_id: int, name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    p = account_dir(account_id) / name
    return p if p.is_file() else None


def read(p: Path) -> tuple[dict, list[list]]:
    """回傳 (header, events)；寫到一半（服務中斷）的最後一行會略過"""
    header: dict = {}
    events: list[list] = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f):
            try:
                v = json.loads(line)
            except ValueError:
                continue
            if n == 0 and isinstance(v, dict):
                header = v
            elif isinstance(v, list) and len(v) == 3:
                events.append(v)
    return header, events


def list_for(account_id: int, set_names: dict[int, str] | None = None) -> list[dict]:
    d = account_dir(account_id)
    out = []
    if not d.is_dir():
        return out
    for p in d.iterdir():
        m = NAME_RE.match(p.name)
        if not m:
            continue
        header, events = read(p)
        sid = int(m.group(1))
        ts = header.get("timestamp")
        out.append({
            "name": p.name, "set_id": sid, "qid": m.group(2),
            "set_name": (set_names or {}).get(sid, f"#{sid}"),
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else m.group(3),
            "duration": events[-1][0] if events else 0.0,
            "inputs": sum(1 for e in events if e[1] == "i"),
            "blocked": sum(1 for e in events if e[1] == "m" and str(e[2]).startswith(PASTE_LABEL)),
            "size": p.stat().st_size,
        })
    out.sort(key=lambda r: r["started"], reverse=True)
    return out


def _keys(s: str) -> str:
    def rep(m: re.Match) -> str:
        k = m.group(0)
        if k in _KEYS:
            return _KEYS[k]
        return "^" + chr(ord(k) + 64)
    return _KEY_RE.sub(rep, s)


def input_lines(header: dict, events: list[list]) -> list[dict]:
    """把鍵盤輸入組成一行一行（按 Enter 為一行），加上被擋的貼上；每筆有 t（秒）與 at（時刻）"""
    t0 = header.get("timestamp") or 0
    out: list[dict] = []
    buf, start = "", None

    def flush(t: float, enter: bool) -> None:
        nonlocal buf, start
        if buf or enter:
            out.append({"t": start if start is not None else t, "kind": "input", "text": _keys(buf) + (" ⏎" if enter else "")})
        buf, start = "", None

    for t, kind, data in events:
        if kind == "i":
            for part in re.split(r"(\r\n|\r|\n)", data):
                if part in ("\r\n", "\r", "\n"):
                    flush(t, True)
                elif part:
                    if start is None:
                        start = t
                    buf += part
        elif kind == "m":
            flush(t, False)
            out.append({"t": t, "kind": "blocked" if str(data).startswith(PASTE_LABEL) else "mark", "text": str(data)})
    if events:
        flush(events[-1][0], False)
    for r in out:
        r["at"] = time.strftime("%H:%M:%S", time.localtime(t0 + r["t"])) if t0 else f"{r['t']:.0f}s"
    return out


def player_cast(header: dict, events: list[list], lines: list[dict]) -> str:
    """給播放器用的版本：每一行輸入都加成標記（進度條上看得到，點列表可以跳過去）。
    原本的 "m" 事件會被這份清單取代，所以標記的順序 = lines 的順序。"""
    marks = [[r["t"], "m", r["text"][:80]] for r in lines]
    evs = [e for e in events if e[1] != "m"] + marks
    evs.sort(key=lambda e: (e[0], e[1] != "m"))
    return "\n".join([json.dumps(header, ensure_ascii=False)] + [json.dumps(e, ensure_ascii=False) for e in evs]) + "\n"


def output_text(events: list[list]) -> str:
    s = "".join(e[2] for e in events if e[1] == "o")
    s = ANSI_RE.sub("", s).replace("\r\n", "\n")
    return re.sub(r"[^\n]*\r", "", s)          # \r 回到行首：只留最後覆寫的內容


# ---------------- 刪除 ----------------
def delete_account(account_id: int) -> None:
    shutil.rmtree(account_dir(account_id), ignore_errors=True)


def delete_set(set_id: int) -> int:
    n = 0
    if root().is_dir():
        for p in root().glob(f"a*/s{int(set_id)}-*.cast"):
            if NAME_RE.match(p.name) and int(NAME_RE.match(p.name).group(1)) == int(set_id):
                p.unlink(missing_ok=True)
                n += 1
    return n
