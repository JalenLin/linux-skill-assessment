"""SQLite 資料存取（輕量包裝）與資料庫結構。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

from .config import cfg

DB_PATH = cfg.data_path / "lsa.db"

# 資料庫結構版本（PRAGMA user_version）。
# 改結構時：SCHEMA 改成最新版（給新安裝），SCHEMA_VERSION + 1，並在 MIGRATIONS 加上從上一版升級的 SQL。
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    init_password TEXT,                      -- 發給作答者的初始密碼（方便列印）
    participant_code TEXT UNIQUE,            -- 編號（編號、員工編號…），第一次登入時綁定
    bound_at TEXT,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL                      -- JSON
);
CREATE TABLE IF NOT EXISTS qsets (            -- 題組：測驗 / 練習
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'practice',   -- assessment / practice
    visible INTEGER NOT NULL DEFAULT 0,      -- 作答者看得到
    start_at TEXT NOT NULL DEFAULT '',       -- "YYYY-MM-DDTHH:MM"；練習可留空 = 立即/不限
    end_at TEXT NOT NULL DEFAULT '',
    score_policy TEXT NOT NULL DEFAULT 'best',   -- best / latest / final
    show_detail INTEGER NOT NULL DEFAULT 1,
    block_paste INTEGER NOT NULL DEFAULT 0,
    cooldown_sec INTEGER NOT NULL DEFAULT 30,
    notice TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS set_questions (
    set_id INTEGER NOT NULL REFERENCES qsets(id) ON DELETE CASCADE,
    qid TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    points_json TEXT NOT NULL DEFAULT '{}',  -- {check_id: points} 覆寫 question.yaml
    time_limit_min INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (set_id, qid)
);
CREATE TABLE IF NOT EXISTS templates (
    qid TEXT PRIMARY KEY,
    vmid INTEGER,
    kind TEXT,
    version TEXT,
    status TEXT,                             -- building / ready / failed
    built_at TEXT,
    selftest_json TEXT,
    log TEXT
);
CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    set_id INTEGER NOT NULL,
    qid TEXT NOT NULL,
    vmid INTEGER UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    ip TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stopped',
    template_version TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    last_access TEXT,
    UNIQUE(account_id, set_id, qid)
);
CREATE TABLE IF NOT EXISTS check_runs (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    set_id INTEGER NOT NULL,
    qid TEXT NOT NULL,
    status TEXT NOT NULL,                    -- queued / running / done / error
    is_final INTEGER NOT NULL DEFAULT 0,
    score REAL,
    max_score REAL,
    results_json TEXT,
    message TEXT,
    log TEXT,
    started_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS attempts (         -- 限時題目的每次作答
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    set_id INTEGER NOT NULL,
    qid TEXT NOT NULL,
    started_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT                          -- timeout / set_end / closed / switched / left / admin
);
CREATE TABLE IF NOT EXISTS activity (         -- 作答者活動紀錄（出席判定用）
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    set_id INTEGER,                          -- 登入時為 NULL
    kind TEXT NOT NULL,                      -- login / view / start / shell
    qid TEXT,
    ip TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_asq ON check_runs(account_id, set_id, qid);
CREATE INDEX IF NOT EXISTS idx_attempts_asq ON attempts(account_id, set_id, qid);
CREATE INDEX IF NOT EXISTS idx_activity_sa ON activity(set_id, account_id);
"""

# {版本: 從上一版升級到這一版的 SQL}；例：{2: "ALTER TABLE qsets ADD COLUMN foo TEXT"}
MIGRATIONS: dict[int, str] = {}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def query(sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def one(sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple | list = ()) -> int:
    conn = _connect()
    try:
        cur = conn.execute(sql, args)
        return cur.lastrowid
    finally:
        conn.close()


def init() -> None:
    conn = _connect()
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "exam_questions" in tables or ("accounts" in tables and "student_id" in   # 舊版的欄位名稱
                                          {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}):
            raise SystemExit(f"{DB_PATH} 是舊版（exam-judge）的資料庫，結構不相容；請改用新的資料目錄，"
                             "或先備份後刪除此檔案")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(SCHEMA)
        if version == 0:                     # 新資料庫：SCHEMA 已經是最新版
            version = SCHEMA_VERSION
        for v in sorted(MIGRATIONS):
            if v > version:
                conn.executescript(MIGRATIONS[v])
                version = v
        conn.execute(f"PRAGMA user_version={version}")
    finally:
        conn.close()


# ---------------- settings ----------------
# 全系統設定（測驗時間、計分等由各題組設定，見 qsets）
DEFAULT_SETTINGS: dict[str, Any] = {
    "site_title": "Linux 實作能力檢驗系統",   # 網站標題（預設值會依介面語言翻譯；自訂的標題照原樣顯示）
    "max_running": 1,          # 每位作答者同時運作的實例上限（1 = 一次只開一題；VM 題多時記憶體才夠，見 docs/OPERATIONS.md）
    "idle_stop_min": 30,       # 無連線多久自動停止
    "max_instances": 1000,     # 全系統實例總數上限（預先建立時檢查）；受 PVE 儲存（LVM metadata）限制
    "code_pattern": r"^[A-Za-z0-9]{4,20}$",   # 編號格式
    "notice": "",              # 首頁公告
    "default_lang": "zh-TW",   # 預設介面語言；auto = 依瀏覽器語言
}


def get_setting(key: str) -> Any:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return json.loads(row["value"])


def all_settings() -> dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    for r in query("SELECT key, value FROM settings"):
        s[r["key"]] = json.loads(r["value"])
    return s


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
