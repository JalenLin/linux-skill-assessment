"""題目包載入。

questions/<qid>/
  question.yaml     題目中繼資料與檢查項目
  description.md    題目敘述（作答者看到）
  setup.yml         埋錯 playbook（建置範本時執行）
  check.yml         校驗 playbook；task 名稱以 "check:<id>" 開頭對應檢查項目
  solution.yml      (選用) 標準解答，供自我測試
  files/            (選用) setup 使用的檔案

多節點題（環境組）：question.yaml 寫 networks（題目網路，每個一個 VLAN）與 nodes（每台機器），
見 docs/QUESTIONS.md「多節點題」。
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
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


# 終端機連線方式：預設 ssh；console 開頭的預設用主控台；-only 不能切換（ssh-only 連不上也不改用主控台）
TERMINAL_MODES = ("ssh", "console", "ssh-only", "console-only")
# 多節點題的節點另外可以用 none：不顯示分頁（只能從別台經題目網路連過去）
NODE_TERMINAL_MODES = TERMINAL_MODES + ("none",)
NODE_RE = re.compile(r"^[a-z][a-z0-9-]{0,14}$")      # 節點名稱 = 主機名稱 = Ansible host
NET_RE = re.compile(r"^[a-z][a-z0-9_]{0,14}$")
KINDS = {"lxc": "lxc", "container": "lxc", "vm": "qemu", "qemu": "qemu"}
MAX_NODES = 8
MAX_NETWORKS = 4                                      # 每組最多幾個題目網路（VLAN）


def terminal_default_of(mode: str) -> str:
    return "console" if mode.startswith("console") else "ssh"


def terminal_switchable_of(mode: str) -> bool:
    return mode in ("ssh", "console")


@dataclass
class Node:
    """多節點題的一台機器：eth0 是管理網路（終端機、評分），eth1… 依 nics 接到題目網路"""
    name: str
    kind: str                      # lxc / qemu
    resources: dict
    nics: dict[str, str | None]    # {網路名稱: "192.168.50.11/24" 或 None（不設 IP）}，寫的順序 → eth1、eth2…
    terminal: str = "ssh"          # ssh / console / ssh-only / console-only / none
    role: str = ""                 # grader：評分用，作答者看不到；平常關機，按「檢查」時才開機

    @property
    def is_grader(self) -> bool:
        return self.role == "grader"

    @property
    def terminal_default(self) -> str:
        return terminal_default_of(self.terminal)

    @property
    def terminal_switchable(self) -> bool:
        return terminal_switchable_of(self.terminal)


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
    terminal: str = "ssh"          # 終端機連線：ssh / console / ssh-only / console-only（見 TERMINAL_MODES）
    # 多節點題：題目網路 {名稱: {"cidr": "192.168.50.0/24", "gateway": "192.168.50.1" 或 None}} 與節點（依 yaml 順序）
    networks: dict[str, dict] = field(default_factory=dict)
    nodes: list[Node] = field(default_factory=list)

    @property
    def terminal_default(self) -> str:
        """一開終端機用哪一種：ssh 或 console"""
        return terminal_default_of(self.terminal)

    @property
    def terminal_switchable(self) -> bool:
        """作答者可以「改用主控台／改用 SSH」（SSH 不通時也會自動改用主控台）"""
        return terminal_switchable_of(self.terminal)

    @property
    def is_group(self) -> bool:
        return bool(self.nodes)

    def node(self, name: str) -> Node | None:
        return next((n for n in self.nodes if n.name == name), None)

    def terminal_tabs(self) -> list[dict]:
        """作答頁的終端機分頁（單機題只有一個、沒有名稱）"""
        if not self.nodes:
            return [{"name": "", "default": self.terminal_default, "switch": self.terminal_switchable}]
        return [{"name": n.name, "default": n.terminal_default, "switch": n.terminal_switchable}
                for n in self.nodes if n.terminal != "none"]

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
    h.update(json.dumps({k: meta.get(k) for k in ("type", "resources", "hostname", "networks", "nodes")},
                        sort_keys=True, default=str).encode())
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
    kind = KINDS.get(meta.get("type", "lxc"))
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
    terminal = str(meta.get("terminal") or "ssh").strip().lower()
    if terminal not in TERMINAL_MODES:
        errors.append("terminal 必須是 ssh、console、ssh-only 或 console-only")
        terminal = "ssh"
    resources = {"cores": 1, "memory": 512 if kind == "lxc" else 1024, "swap": 256, **(meta.get("resources") or {})}
    networks, nodes = _parse_group(meta, kind, resources, terminal, errors)
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
        resources=resources,
        checks=checks,
        description_md=(qdir / "description.md").read_text(),
        path=qdir,
        has_solution=(qdir / "solution.yml").is_file(),
        build_version=_build_version(qdir, meta),
        errors=errors,
        lang=main_lang,
        translations=translations,
        tags=tags,
        terminal=terminal,
        networks=networks,
        nodes=nodes,
    )


NODE_KEYS = {"type", "cores", "memory", "swap", "disk", "nics", "terminal", "role"}
NET_KEYS = {"cidr", "gateway"}


def _parse_group(meta: dict, kind: str, resources: dict, terminal: str,
                 errors: list[str]) -> tuple[dict[str, dict], list[Node]]:
    """networks / nodes（多節點題）。節點沒寫的 type、cores／memory／swap、terminal 用題目頂層的設定"""
    raw_nets, raw_nodes = meta.get("networks"), meta.get("nodes")
    if raw_nets is None and raw_nodes is None:
        return {}, []
    if not isinstance(raw_nodes, dict) or not raw_nodes:
        errors.append("nodes 必須是「節點名稱: 設定」的對應表")
        return {}, []
    if "hostname" in meta:
        errors.append("多節點題不用 hostname：每台的主機名稱就是節點名稱")
    networks: dict[str, dict] = {}
    if raw_nets is not None and not isinstance(raw_nets, dict):
        errors.append("networks 必須是「網路名稱: 設定」的對應表")
        raw_nets = {}
    for name, n in (raw_nets or {}).items():
        name = str(name)
        n = n or {}
        if not NET_RE.match(name):
            errors.append(f"網路名稱不合法：{name!r}（小寫英文開頭，最多 15 字）")
            continue
        if not isinstance(n, dict) or set(n) - NET_KEYS:
            errors.append(f"networks.{name} 只能有 cidr、gateway")
            continue
        cidr = None
        if n.get("cidr") is not None:                  # 不給 cidr：節點在這個網路上不預設 IP（作答者自己設）
            try:
                cidr = ipaddress.ip_network(str(n["cidr"]), strict=True)
                if cidr.version != 4:
                    raise ValueError
            except ValueError:
                errors.append(f"networks.{name}.cidr 必須是 IPv4 網段，例如 192.168.50.0/24")
                continue
            if cidr.overlaps(ipaddress.ip_network("10.10.0.0/16")):
                errors.append(f"networks.{name}.cidr 不能與管理網路 10.10.0.0/16 重疊")
                continue
        gw = n.get("gateway")
        if gw is not None:
            try:
                if cidr is None or ipaddress.ip_address(str(gw)) not in cidr:
                    raise ValueError
            except ValueError:
                errors.append(f"networks.{name}.gateway 必須是 {cidr or 'cidr'} 內的位址")
                gw = None
        networks[name] = {"cidr": str(cidr) if cidr else None, "gateway": str(gw) if gw is not None else None}
    if len(networks) > MAX_NETWORKS:
        errors.append(f"題目網路最多 {MAX_NETWORKS} 個")
    if len(raw_nodes) > MAX_NODES:
        errors.append(f"節點最多 {MAX_NODES} 台")
    nodes: list[Node] = []
    used_ips: set[str] = set()
    for name, n in raw_nodes.items():
        name = str(name)
        n = n or {}
        if not NODE_RE.match(name):
            errors.append(f"節點名稱不合法：{name!r}（小寫英文開頭、英數與 -，最多 15 字）")
            continue
        if not isinstance(n, dict):
            errors.append(f"nodes.{name} 格式錯誤")
            continue
        for k in sorted(set(n) - NODE_KEYS):
            errors.append(f"nodes.{name}.resources：節點直接寫 cores、memory、swap、disk" if k == "resources"
                          else f"nodes.{name} 不認得的設定：{k}")
        nkind = KINDS.get(n.get("type"), None) if n.get("type") else kind
        if nkind is None:
            errors.append(f"nodes.{name}.type 必須是 lxc 或 vm")
            nkind = kind
        res = {**resources, **{k: n[k] for k in ("cores", "memory", "swap", "disk") if k in n}}
        if nkind != kind and "memory" not in n:
            res["memory"] = 512 if nkind == "lxc" else 1024
        role = str(n.get("role") or "").strip().lower()
        if role not in ("", "grader"):
            errors.append(f"nodes.{name}.role 只能是 grader")
            role = ""
        if role == "grader":
            if str(n.get("terminal") or "none").lower() != "none":
                errors.append(f"nodes.{name}：grader 節點不提供終端機（terminal 只能是 none）")
            if nkind != "lxc":
                errors.append(f"nodes.{name}：grader 節點必須是 lxc（每次檢查才開機，VM 太慢）")
        nterm = "none" if role == "grader" else str(n.get("terminal") or terminal).strip().lower()
        if nterm not in NODE_TERMINAL_MODES:
            errors.append(f"nodes.{name}.terminal 必須是 ssh、console、ssh-only、console-only 或 none")
            nterm = "ssh"
        nics: dict[str, str | None] = {}
        raw_nics = n.get("nics") or {}
        if not isinstance(raw_nics, dict):
            errors.append(f"nodes.{name}.nics 必須是「網路名稱: IP」的對應表")
            raw_nics = {}
        for net, ip in raw_nics.items():          # 寫的順序就是 eth1、eth2…
            net = str(net)
            if net not in networks:
                errors.append(f"nodes.{name}.nics：沒有定義網路 {net!r}")
                continue
            if ip is None:                         # 接上這個網路但不設 IP
                nics[net] = None
                continue
            if networks[net]["cidr"] is None:
                errors.append(f"nodes.{name}.nics.{net}：這個網路沒有 cidr，不能指定 IP（要不設 IP 請寫 ~）")
                continue
            cidr = ipaddress.ip_network(networks[net]["cidr"])
            try:
                addr = ipaddress.ip_address(str(ip))
                if addr not in cidr or addr in (cidr.network_address, cidr.broadcast_address):
                    raise ValueError
            except ValueError:
                errors.append(f"nodes.{name}.nics.{net}：{ip!r} 不是 {cidr} 內可用的位址")
                continue
            if str(addr) in used_ips:
                errors.append(f"nodes.{name}.nics.{net}：IP {addr} 重複")
                continue
            used_ips.add(str(addr))
            nics[net] = f"{addr}/{cidr.prefixlen}"
        nodes.append(Node(name=name, kind=nkind, resources=res, nics=nics, terminal=nterm, role=role))
    if nodes and all(n.is_grader for n in nodes):
        errors.append("至少要有一台不是 grader 的節點")
    elif nodes and all(n.terminal == "none" for n in nodes):
        errors.append("至少要有一台節點可以開終端機（terminal 不是 none）")
    return networks, nodes


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
