"""題目包載入。

questions/<qid>/
  question.yaml     題目中繼資料與檢查項目
  description.md    題目敘述（作答者看到）
  setup.yml         埋錯 playbook（建置範本時執行）
  check.yml         校驗 playbook；task 名稱以 "check:<id>" 開頭對應檢查項目
  solution.yml      (選用) 標準解答，供自我測試
  files/            (選用) setup 使用的檔案
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import markdown
import yaml

from . import db
from .config import cfg
from .i18n import normalize_code

QID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
CHECK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REQUIRED_FILES = ("question.yaml", "description.md", "setup.yml", "check.yml")


class QuestionError(Exception):
    pass


@dataclass
class Check:
    id: str
    desc: str
    points: float


@dataclass
class Question:
    qid: str
    title: str
    kind: str                      # lxc / qemu
    hostname: str
    resources: dict
    checks: list[Check]
    description_md: str
    path: Path
    has_solution: bool
    build_version: str             # setup 相關內容雜湊，不同就需重建範本
    errors: list[str] = field(default_factory=list)
    lang: str = "zh-TW"            # 主要內容（title / description.md / checks.desc）的語言
    # 其他語言：{代碼: {"title": str, "checks": {id: desc}, "description_md": str}}
    translations: dict[str, dict] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)   # 分類標籤（只給管理者篩選用，作答者看不到）

    @property
    def description_html(self) -> str:
        return self.description_html_in(None)

    @property
    def languages(self) -> list[str]:
        return [self.lang] + [c for c in self.translations if c != self.lang]

    def _tr(self, lang: str | None) -> dict:
        if lang is None:
            from .i18n import current
            lang = current()
        return self.translations.get(lang, {}) if lang != self.lang else {}

    def title_in(self, lang: str | None = None) -> str:
        return self._tr(lang).get("title") or self.title

    def description_md_in(self, lang: str | None = None) -> str:
        return self._tr(lang).get("description_md") or self.description_md

    def description_html_in(self, lang: str | None = None) -> str:
        return markdown.markdown(self.description_md_in(lang), extensions=["fenced_code", "tables"])

    def check_desc(self, cid: str, lang: str | None = None) -> str:
        t = (self._tr(lang).get("checks") or {}).get(cid)
        if t:
            return t
        return next((c.desc for c in self.checks if c.id == cid), cid)

    @property
    def default_total(self) -> float:
        return sum(c.points for c in self.checks)


def _build_version(qdir: Path, meta: dict) -> str:
    h = hashlib.sha256()
    h.update(json.dumps({k: meta.get(k) for k in ("type", "resources", "hostname")}, sort_keys=True).encode())
    paths = [qdir / "setup.yml"]
    if (qdir / "files").is_dir():
        paths += sorted(p for p in (qdir / "files").rglob("*") if p.is_file())
    for p in paths:
        h.update(str(p.relative_to(qdir)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def load_one(qdir: Path) -> Question:
    qid = qdir.name
    errors: list[str] = []
    for f in REQUIRED_FILES:
        if not (qdir / f).is_file():
            raise QuestionError(f"{qid}: 缺少 {f}")
    meta = yaml.safe_load((qdir / "question.yaml").read_text()) or {}
    kind = {"lxc": "lxc", "container": "lxc", "vm": "qemu", "qemu": "qemu"}.get(meta.get("type", "lxc"))
    if kind is None:
        errors.append("type 必須是 lxc 或 vm")
        kind = "lxc"
    checks = []
    seen = set()
    for c in meta.get("checks") or []:
        cid = str(c.get("id", ""))
        if not CHECK_ID_RE.match(cid) or cid in seen:
            errors.append(f"檢查項目 id 不合法或重複: {cid!r}")
            continue
        seen.add(cid)
        checks.append(Check(cid, str(c.get("desc", cid)), float(c.get("points", 0))))
    if not checks:
        errors.append("沒有定義 checks")
    tags, tag_err = parse_tags(meta.get("tags"))
    if tag_err:
        errors.append(tag_err)
    main_lang = normalize_code(meta.get("lang") or "zh-TW")   # zh_TW 與 zh-TW 都接受
    translations: dict[str, dict] = {}
    for code, tr in (meta.get("i18n") or {}).items():
        if not isinstance(tr, dict):
            errors.append(f"i18n.{code} 格式錯誤")
            continue
        translations[normalize_code(code)] = {"title": str(tr.get("title") or ""),
                                   "checks": {str(k): str(v) for k, v in (tr.get("checks") or {}).items()}}
    for f in qdir.glob("description.*.md"):
        code = normalize_code(f.name[len("description."):-len(".md")])
        if "description_md" in translations.get(code, {}):
            errors.append(f"{f.name}：{code} 的題目敘述重複（zh_TW 與 zh-TW 視為同一語言）")
        translations.setdefault(code, {"title": "", "checks": {}})["description_md"] = f.read_text()
    for code, tr in translations.items():
        for cid in tr["checks"]:
            if cid not in seen:
                errors.append(f"i18n.{code}.checks 有不存在的檢查項目 {cid!r}")
    check_text = (qdir / "check.yml").read_text()
    for c in checks:
        if f"check:{c.id}" not in check_text:
            errors.append(f"check.yml 中找不到 task 'check:{c.id}'")
    return Question(
        qid=qid,
        title=str(meta.get("title", qid)),
        kind=kind,
        hostname=str(meta.get("hostname", "server")),
        resources={"cores": 1, "memory": 512 if kind == "lxc" else 1024, "swap": 256,
                   **(meta.get("resources") or {})},
        checks=checks,
        description_md=(qdir / "description.md").read_text(),
        path=qdir,
        has_solution=(qdir / "solution.yml").is_file(),
        build_version=_build_version(qdir, meta),
        errors=errors,
        lang=main_lang,
        translations=translations,
        tags=tags,
    )


# ---------------- 標籤 ----------------
TAG_MAX = 30


def parse_tags(v) -> tuple[list[str], str | None]:
    """question.yaml 的 tags：list 或逗號分隔字串 → 去空白、去重複（保留順序）"""
    if v is None:
        return [], None
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        return [], "tags 必須是清單，例如 tags: [systemd, network]"
    out: list[str] = []
    for x in v:
        t = re.sub(r"\s+", " ", str(x)).strip()
        if not t:
            continue
        if len(t) > TAG_MAX or "," in t:
            return out, f"標籤不合法（最多 {TAG_MAX} 字、不能有逗號）：{t}"
        if t.lower() not in (o.lower() for o in out):
            out.append(t)
    return out, None


# 頂層的 tags: 加上後面縮排（或以 "- " 開頭）的續行
_TAGS_BLOCK = re.compile(r"^tags[ \t]*:.*(?:\n(?:[ \t]+\S.*|-[ \t].*))*", re.M)


def set_tags(qid: str, tags: list[str]) -> list[str]:
    """改寫題目的 question.yaml：只換掉頂層的 tags（沒有就加在 title 後面），其他內容與註解不動。
    寫入前重新解析確認除了 tags 以外完全一樣，否則不寫。回傳整理後的標籤。"""
    if not QID_RE.match(qid):
        raise QuestionError("題目 id 不合法")
    tags, err = parse_tags(tags)
    if err:
        raise QuestionError(err)
    p = cfg.questions_path / qid / "question.yaml"
    old = p.read_text(encoding="utf-8")
    before = yaml.safe_load(old) or {}
    line = ("tags: " + json.dumps(tags, ensure_ascii=False)) if tags else ""
    m = _TAGS_BLOCK.search(old)
    if m:
        start, end = m.start(), m.end()
        if not line and old[end:end + 1] == "\n":
            end += 1                                # 刪除：連同這一行的換行
        new = old[:start] + line + old[end:]
    elif line:
        t = re.search(r"^title[ \t]*:.*$", old, re.M)
        if t:
            new = old[:t.end()] + "\n" + line + old[t.end():]
        else:
            new = old.rstrip("\n") + "\n" + line + "\n"
    else:
        return tags
    after = yaml.safe_load(new) or {}
    want = {k: v for k, v in before.items() if k != "tags"}
    if {k: v for k, v in after.items() if k != "tags"} != want or (parse_tags(after.get("tags"))[0] != tags):
        raise QuestionError("無法安全地改寫 question.yaml 的 tags，請直接編輯檔案")
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(p)
    return tags


def load_all() -> tuple[dict[str, Question], dict[str, str]]:
    """回傳 (題目, 載入失敗的錯誤訊息)"""
    out, bad = {}, {}
    for qdir in sorted(cfg.questions_path.iterdir()):
        if not qdir.is_dir() or not QID_RE.match(qdir.name):
            continue
        try:
            out[qdir.name] = load_one(qdir)
        except Exception as e:  # noqa: BLE001 - 題目格式錯誤不能讓整站掛掉
            bad[qdir.name] = str(e)
    return out, bad


def get(qid: str) -> Question | None:
    qdir = cfg.questions_path / qid
    if not QID_RE.match(qid) or not qdir.is_dir():
        return None
    try:
        return load_one(qdir)
    except Exception:  # noqa: BLE001
        return None


# ---------------- 上傳 ----------------
def install_zip(data: bytes) -> str:
    """解壓題目包 zip。zip 內可為 <qid>/... 或直接是檔案（此時需 question.yaml 中有 id）。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise QuestionError("不是有效的 zip 檔") from e
    names = [n for n in zf.namelist() if not n.endswith("/") and "__MACOSX" not in n]
    for n in names:
        p = Path(n)
        if p.is_absolute() or ".." in p.parts:
            raise QuestionError(f"zip 內含不安全的路徑: {n}")
    tops = {Path(n).parts[0] for n in names}
    if len(tops) == 1 and (Path(next(iter(tops))) / "question.yaml").as_posix() in names:
        prefix = next(iter(tops)) + "/"
    else:
        prefix = ""
    if prefix + "question.yaml" not in names:
        raise QuestionError("zip 內找不到 question.yaml")
    meta = yaml.safe_load(zf.read(prefix + "question.yaml")) or {}
    qid = str(meta.get("id") or prefix.rstrip("/"))
    if not QID_RE.match(qid):
        raise QuestionError(f"題目 id 不合法: {qid!r}（只能小寫英數與 -，或在 question.yaml 設定 id）")

    staging = cfg.questions_path / f".upload-{qid}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for n in names:
        if not n.startswith(prefix):
            continue
        dest = staging / n[len(prefix):]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zf.read(n))
    try:
        # 先在暫存目錄驗證
        staging_q = load_one(staging)
        if staging_q.errors:
            raise QuestionError("；".join(staging_q.errors))
    except QuestionError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    target = cfg.questions_path / qid
    if target.exists():
        backup = cfg.data_path / "question_backups"
        backup.mkdir(exist_ok=True)
        from datetime import datetime
        shutil.move(str(target), backup / f"{qid}-{datetime.now():%Y%m%d%H%M%S}")
    staging.rename(target)
    return qid


def export_zip(qid: str) -> bytes:
    q = get(qid)
    if q is None:
        raise QuestionError("題目不存在")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(q.path.rglob("*")):
            if p.is_file():
                zf.write(p, f"{qid}/{p.relative_to(q.path)}")
    return buf.getvalue()
