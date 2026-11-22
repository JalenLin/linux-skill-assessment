"""設定、密碼、資料庫"""
import sqlite3

import pytest


def test_instance_ip_mapping():
    from app.config import build_ip, cfg, instance_ip
    assert instance_ip(cfg.vmid_min) == "10.10.1.2"
    assert instance_ip(cfg.vmid_min + 249) == "10.10.1.251"
    assert instance_ip(cfg.vmid_min + 250) == "10.10.2.2"
    assert build_ip(cfg.template_vmid_min).startswith("10.10.255.")


def test_password_hash_roundtrip():
    from app.security import hash_password, random_password, verify_password
    h = hash_password("s3cret")
    assert h.startswith("scrypt$")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert hash_password("s3cret") != h          # 每次 salt 不同
    pw = random_password(12)
    assert len(pw) == 12 and pw.isalnum()


def test_db_init_sets_schema_version(appdb):
    conn = sqlite3.connect(appdb.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == appdb.SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    assert "participant_code" in cols and "student_id" not in cols
    appdb.init()                                   # 可以重複執行
    assert conn.execute("PRAGMA user_version").fetchone()[0] == appdb.SCHEMA_VERSION


def test_db_refuses_legacy_database(appdb, tmp_path, monkeypatch):
    legacy = tmp_path / "old.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, username TEXT, student_id TEXT)")
    conn.commit()
    monkeypatch.setattr(appdb, "DB_PATH", legacy)
    with pytest.raises(SystemExit, match="舊版"):
        appdb.init()


def test_settings_defaults_and_override(appdb):
    assert appdb.get_setting("max_running") == 1
    assert appdb.get_setting("site_title") == "Linux 實作能力檢驗系統"
    appdb.set_setting("notice", "hello")
    assert appdb.get_setting("notice") == "hello"
    assert appdb.all_settings()["notice"] == "hello"
    appdb.set_setting("notice", "")
