"""測試共用設定。

- 單元測試：直接 import app 模組，使用暫存資料目錄（mock 模式，不連 PVE）
- 整合測試（tests/integration）：另外啟動一個 mock 模式的網站（uvicorn 子行程）
- 端對端測試（tests/e2e）：連真實部署，需設定環境變數，見 tests/e2e/README.md
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "questions" / "example-hello"
EXAMPLE_K8S = ROOT / "questions" / "example-k8s"       # 多節點範例題（VM）
EXAMPLE_NET = ROOT / "questions" / "example-net"       # 多節點範例題（LXC）
ADMIN_PASSWORD = "test-admin-pw"


def make_config(base: Path, copies: tuple[str, ...] = ()) -> Path:
    """在 base 底下建立 mock 模式的設定檔、資料目錄與題庫：範例題，另外可複製成其他題目 id"""
    (base / "data").mkdir(parents=True, exist_ok=True)
    qdir = base / "questions"
    for qid in ("example-hello",) + copies:
        if not (qdir / qid).exists():
            shutil.copytree(EXAMPLE, qdir / qid)
    cfg = base / "config.yaml"
    cfg.write_text(
        "mock: true\n"
        "secret_key: test-secret\n"
        f"admin_password: {ADMIN_PASSWORD}\n"
        f"data_dir: {base / 'data'}\n"
        f"questions_dir: {qdir}\n",
        encoding="utf-8")
    return cfg


# app 模組在 import 時就讀設定檔：一定要在任何 `import app` 之前設定好
_UNIT_DIR = Path(tempfile.mkdtemp(prefix="lsa-unit-"))
os.environ["LSA_CONFIG"] = str(make_config(_UNIT_DIR))
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def unit_dir() -> Path:
    return _UNIT_DIR


@pytest.fixture(scope="session")
def appdb():
    """單元測試用的資料庫（每個 session 一份）"""
    from app import db
    db.init()
    return db


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """啟動 mock 模式的網站，回傳 base URL"""
    base = tmp_path_factory.mktemp("lsa-server")
    cfg = make_config(base, copies=("hello-two",))
    for ex in (EXAMPLE_K8S, EXAMPLE_NET):
        shutil.copytree(ex, base / "questions" / ex.name)
    port = _free_port()
    env = dict(os.environ, LSA_CONFIG=str(cfg))
    log = open(base / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
                             "--log-level", "warning"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    import requests
    for _ in range(100):
        try:
            if requests.get(url + "/login", timeout=1).status_code == 200:
                break
        except requests.ConnectionError:
            pass
        if proc.poll() is not None:
            raise RuntimeError("server exited:\n" + (base / "server.log").read_text())
        time.sleep(0.2)
    else:
        raise RuntimeError("server did not start:\n" + (base / "server.log").read_text())
    yield url
    proc.terminate()
    proc.wait(10)
    log.close()
    text = (base / "server.log").read_text()
    assert "Traceback" not in text, "server log has errors:\n" + text
