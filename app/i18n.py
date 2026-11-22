"""多國語言。

- 程式與樣板裡的原文（繁體中文）就是翻譯的 key：`_("檢查作答")`
- 翻譯檔：app/locales/<語言代碼>/*.json，內容為 {"原文": "譯文"}；同一語言可分多個檔案（會合併）
- 帶參數：`_("請等待 {n} 秒後再檢查", n=5)`，譯文中保留 {n}
- 新增語言：建立 app/locales/<代碼>/，放一個 _meta.json：{"_name": "日本語"}，再放翻譯檔。
  沒翻到的字串會顯示原文（中文）。
- 題目內容的多國語言見 docs/QUESTIONS.md（question.yaml 的 i18n 區塊、description.<代碼>.md）
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
from http.cookies import SimpleCookie
from pathlib import Path

log = logging.getLogger("i18n")
DEFAULT = "zh-TW"
COOKIE = "lang"
LOCALES = Path(__file__).parent / "locales"
_current: contextvars.ContextVar[str] = contextvars.ContextVar("lang", default=DEFAULT)
_catalogs: dict[str, dict[str, str]] = {}
_names: dict[str, str] = {DEFAULT: "繁體中文"}
_patterns: dict[str, list[tuple[re.Pattern, str]]] = {}


def normalize_code(code: str) -> str:
    """語言代碼統一成 BCP 47 寫法：zh_TW、zh-tw、ZH_tw → zh-TW；en → en；zh_Hant_TW → zh-Hant-TW"""
    parts = [p for p in re.split(r"[-_]", str(code).strip()) if p]
    if not parts:
        return ""
    out = [parts[0].lower()]
    for p in parts[1:]:
        if len(p) == 2 or p.isdigit():
            out.append(p.upper())          # 地區：TW、US、419
        elif len(p) == 4:
            out.append(p.title())          # 文字：Hant
        else:
            out.append(p.lower())
    return "-".join(out)


def load() -> None:
    _catalogs.clear()
    _patterns.clear()
    if not LOCALES.is_dir():
        return
    for d in sorted(LOCALES.iterdir()):
        if not d.is_dir():
            continue
        code = normalize_code(d.name)
        cat: dict[str, str] = {}
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError as e:
                log.error("翻譯檔格式錯誤 %s: %s", f, e)
                continue
            if "_name" in data:
                _names[code] = str(data.pop("_name"))
            cat.update({k: v for k, v in data.items() if not k.startswith("_") and v})
        _catalogs[code] = cat
        _names.setdefault(code, code)
        # 帶參數的字串：給 translate_stored 比對已存在資料庫的訊息
        pats = []
        for k, v in cat.items():
            if "{" in k:
                rx = re.escape(k)
                rx = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>.*?)", rx)
                try:
                    pats.append((re.compile(f"^{rx}$", re.S), v))
                except re.error:
                    pass
        _patterns[code] = pats


def languages() -> dict[str, str]:
    """可選的介面語言 {代碼: 名稱}"""
    return {DEFAULT: _names[DEFAULT], **{c: _names.get(c, c) for c in _catalogs}}


def current() -> str:
    return _current.get()


def set_lang(code: str):
    return _current.set(code)


def reset(token) -> None:
    _current.reset(token)


def gettext(msg: str, **kw) -> str:
    cat = _catalogs.get(_current.get())
    s = cat.get(msg, msg) if cat else msg
    if kw:
        try:
            return s.format(**kw)
        except (KeyError, IndexError, ValueError):
            return msg.format(**kw)
    return s


_ = gettext


def translate_stored(text: str | None, _depth: int = 0) -> str:
    """翻譯存在資料庫裡的中文訊息（例如檢查結果訊息），含帶參數的訊息；
    參數本身若也是中文訊息會一併翻譯，「；」分隔的多則訊息逐一翻譯"""
    if not text:
        return text or ""
    lang = _current.get()
    cat = _catalogs.get(lang)
    if not cat or _depth > 3:
        return text
    if text in cat:
        return cat[text]
    for rx, tr in _patterns.get(lang, []):
        m = rx.match(text)
        if m:
            try:
                return tr.format(**{k: translate_stored(v, _depth + 1) for k, v in m.groupdict().items()})
            except (KeyError, ValueError):
                return text
    if "；" in text:
        return "; ".join(translate_stored(p, _depth + 1) for p in text.split("；"))
    return text


_LOG_LINE = re.compile(r"^(\[\d\d:\d\d:\d\d\] )(.*)$")


def translate_lines(text: str | None) -> str:
    """逐行翻譯紀錄（建置紀錄每行為「[時:分:秒] 訊息」）"""
    if not text or not _catalogs.get(_current.get()):
        return text or ""
    out = []
    for line in text.split("\n"):
        m = _LOG_LINE.match(line)
        out.append(m.group(1) + translate_stored(m.group(2)) if m else line)
    return "\n".join(out)


def catalog() -> dict[str, str]:
    """目前語言的完整翻譯表（給前端 JS 用）"""
    return _catalogs.get(_current.get(), {})


def resolve(cookie_lang: str | None, accept_language: str | None = None) -> str:
    langs = languages()
    if cookie_lang in langs:
        return cookie_lang
    try:
        from . import db
        default = db.get_setting("default_lang") or DEFAULT
    except Exception:  # noqa: BLE001
        default = DEFAULT
    if default == "auto":
        for part in (accept_language or "").split(","):
            code = part.split(";")[0].strip()
            for c in langs:
                if code and (code.lower() == c.lower() or code.split("-")[0].lower() == c.split("-")[0].lower()):
                    return c
        return DEFAULT
    return default if default in langs else DEFAULT


class LangMiddleware:
    """ASGI middleware：依 cookie / 系統預設決定這個請求（含 WebSocket）的語言"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        ck = SimpleCookie()
        try:
            ck.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001
            pass
        lang = resolve(ck[COOKIE].value if COOKIE in ck else None, headers.get("accept-language"))
        token = _current.set(lang)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)


load()
