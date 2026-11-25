"""多節點題（環境組）：資料庫升級、生命週期（MockPVE）、VLAN 配置、grader 開關、PVE 網卡設定"""
import asyncio
import shutil
import sqlite3
import subprocess

import pytest

from conftest import EXAMPLE, ROOT

NET = EXAMPLE.parent / "example-net"


def test_db_upgrade_v1_to_v2(appdb, tmp_path, monkeypatch):
    """舊版（第 1 版）資料庫：instances 沒有 vlans 欄位，也沒有 instance_nodes／template_nodes"""
    old = tmp_path / "v1.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            init_password TEXT, participant_code TEXT UNIQUE, bound_at TEXT, note TEXT, created_at TEXT);
        CREATE TABLE instances (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, set_id INTEGER NOT NULL,
            qid TEXT NOT NULL, vmid INTEGER UNIQUE NOT NULL, kind TEXT NOT NULL, ip TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'stopped', template_version TEXT, created_at TEXT, last_access TEXT,
            UNIQUE(account_id, set_id, qid));
        INSERT INTO accounts(id, username, password_hash) VALUES (1, 'u', 'x');
        INSERT INTO instances(account_id, set_id, qid, vmid, kind, ip) VALUES (1, 1, 'q', 10000, 'lxc', '10.10.1.2');
        PRAGMA user_version=1;
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(appdb, "DB_PATH", old)
    appdb.init()
    conn = sqlite3.connect(old)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == appdb.SCHEMA_VERSION == 2
    assert "vlans" in {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"instance_nodes", "template_nodes"} <= tables
    assert conn.execute("SELECT vmid, vlans FROM instances").fetchall() == [(10000, None)]   # 資料保留


# ---------------- 生命週期（MockPVE） ----------------
@pytest.fixture
def group_env(appdb, unit_dir):
    """單元測試的題庫加上 example-net，並建一個假的範本（每個節點一台）"""
    from app import instances, questions
    from app.config import cfg
    from app.pve import client
    dest = cfg.questions_path / NET.name
    if not dest.exists():
        shutil.copytree(NET, dest)
    q = questions.get(NET.name)
    assert q and not q.errors
    db = appdb
    for t in ("instance_nodes", "instances", "template_nodes", "templates"):
        db.execute(f"DELETE FROM {t}")
    pve = client()
    pve.vms.clear()
    db.execute("INSERT INTO templates(qid, vmid, kind, version, status) VALUES (?,?,?,?, 'ready')",
               (q.qid, 9500, "lxc", q.build_version))
    for i, n in enumerate(q.nodes):
        db.execute("INSERT INTO template_nodes(qid, name, vmid, kind) VALUES (?,?,?,?)", (q.qid, n.name, 9500 + i, n.kind))
        pve.vms[9500 + i] = {"status": "stopped"}
    accs = []
    for i in (1, 2):
        db.execute("INSERT OR IGNORE INTO accounts(id, username, password_hash, participant_code) VALUES (?,?,?,?)",
                   (900 + i, f"grp{i}", "x", f"G{i}"))
        accs.append(db.one("SELECT * FROM accounts WHERE id=?", (900 + i,)))
    db.set_setting("max_running", 5)
    yield q, accs, instances, pve
    db.set_setting("max_running", 1)


def test_group_lifecycle(group_env):
    q, (a1, a2), instances, pve = group_env

    async def go():
        i1 = await instances.ensure_running(a1, 1, q, need_ssh=False)
        i2 = await instances.ensure_running(a2, 1, q, need_ssh=False)
        n1, n2 = instances.nodes_of(i1), instances.nodes_of(i2)
        assert [n["name"] for n in n1] == ["pc1", "r1", "web", "grader"]
        assert len({n["vmid"] for n in n1 + n2}) == 8                    # 每台一個 VMID
        assert i1["vmid"] == n1[0]["vmid"] and i1["ip"] == n1[0]["ip"]   # instances 列 = 第一台
        v1, v2 = (instances.json.loads(i["vlans"]) for i in (i1, i2))
        assert set(v1) == {"lan", "dmz"} and not set(v1.values()) & set(v2.values())   # 組與組 VLAN 不重複
        st = {n["name"]: pve.status("lxc", n["vmid"]) for n in n1}
        assert st == {"pc1": "running", "r1": "running", "web": "running", "grader": "stopped"}   # grader 平常關機
        assert instances.machine_count() == 8

        t = instances.node_target(i1, "web")
        assert t["vmid"] == n1[2]["vmid"] and t["node"] == "web" and t["id"] == i1["id"]
        assert instances.node_target(i1, "nope") is None
        target = instances.playbook_target(i1, q)
        assert [h["name"] for h in target] == ["pc1", "r1", "web", "grader"]
        assert target[1]["nics"] == {"lan": "192.168.10.1/24", "dmz": "172.16.0.1/24"}

        gs = await instances.start_graders(i1, q)                         # 按「檢查」
        assert [g["name"] for g in gs] == ["grader"] and pve.status("lxc", gs[0]["vmid"]) == "running"
        await instances.stop_graders(gs)
        assert pve.status("lxc", gs[0]["vmid"]) == "stopped"

        instances.shell_kicks.clear()
        await instances.pause(i1)                                         # 暫停：整組關機
        assert all(pve.status("lxc", n["vmid"]) == "stopped" for n in n1)
        await instances.start(i1)                                         # 後台「啟動」：grader 不開
        assert [pve.status("lxc", n["vmid"]) for n in n1] == ["running"] * 3 + ["stopped"]

        # PVE 上少了一台：整組重建
        pve.vms.pop(n1[1]["vmid"])
        i1b = await instances.ensure_running(a1, 1, q, need_ssh=False)
        assert i1b["id"] != i1["id"] and len(instances.nodes_of(i1b)) == 4
        assert len(pve.vms) == 4 + 4 + 4                                   # 範本＋第二組＋重建的這組（舊的整組刪了）
        assert instances.db.one("SELECT COUNT(*) n FROM instance_nodes")["n"] == 8

        await instances.reset(a2, 1, q)                                   # 重置：刪除整組、歸還 VLAN
        assert instances.get(a2["id"], 1, q.qid) is None
        assert len(pve.vms) == 4 + 4
        assert instances.db.one("SELECT COUNT(*) n FROM instance_nodes WHERE instance_id=?", (i2["id"],))["n"] == 0
        i2b = await instances.ensure_running(a2, 1, q, need_ssh=False)
        assert set(instances.json.loads(i2b["vlans"]).values()) == set(v2.values())   # 放回去的 VLAN 再用
        await instances.reset(a1, 1, q)
        await instances.reset(a2, 1, q)
        assert instances.machine_count() == 0
    asyncio.run(go())


def test_sync_status_uses_any_node(group_env):
    q, (a1, _), instances, pve = group_env

    async def go():
        i = await instances.ensure_running(a1, 1, q, need_ssh=False)
        instances.db.execute("UPDATE instances SET last_access='2000-01-01T00:00:00' WHERE id=?", (i["id"],))
        await instances.sync_status()
        assert instances.get(a1["id"], 1, q.qid)["status"] == "running"
        for n in instances.nodes_of(i)[1:]:                               # 只剩第一台以外的停了：仍算運作中
            pve.stop("lxc", n["vmid"])
        await instances.sync_status()
        assert instances.get(a1["id"], 1, q.qid)["status"] == "running"
        pve.stop("lxc", i["vmid"])
        await instances.sync_status()
        assert instances.get(a1["id"], 1, q.qid)["status"] == "stopped"
        await instances.reset(a1, 1, q)
    asyncio.run(go())


def test_vlan_exhaustion(group_env, monkeypatch):
    q, (a1, a2), instances, _ = group_env
    monkeypatch.setattr(instances.cfg, "vlan_max", instances.cfg.vlan_min + 2)   # 只夠一組（每組 2 個網路）+1

    async def go():
        await instances.ensure_running(a1, 1, q, need_ssh=False)
        with pytest.raises(instances.InstanceError, match="VLAN"):
            await instances.ensure_running(a2, 1, q, need_ssh=False)
        assert instances.get(a2["id"], 1, q.qid) is None                  # 沒留下半成品
        await instances.reset(a1, 1, q)
    asyncio.run(go())


# ---------------- PVE 網卡設定 ----------------
def _fake_pve(config=None):
    from app.pve import PVE
    p = PVE.__new__(PVE)
    p.calls = []
    p.set_config = lambda kind, vmid, **kw: p.calls.append(kw)
    p.get_config = lambda kind, vmid: config or {}
    return p


def test_set_nics_strings():
    p = _fake_pve()
    p.set_nics("lxc", 1, [(1, 100, "192.168.10.1/24", None), (2, 101, None, None), (3, 102, "10.0.0.5/24", "10.0.0.1")])
    assert p.calls[-1] == {
        "net1": "name=eth1,bridge=vmbr1,tag=100,ip=192.168.10.1/24,firewall=0",
        "net2": "name=eth2,bridge=vmbr1,tag=101,ip=manual,firewall=0",
        "net3": "name=eth3,bridge=vmbr1,tag=102,ip=10.0.0.5/24,firewall=0,gw=10.0.0.1"}
    p.set_nics("qemu", 1, [(1, 100, "192.168.50.11/24", "192.168.50.1"), (2, 101, None, None)])
    assert p.calls[-1] == {"net1": "virtio,bridge=vmbr1,tag=100,firewall=0", "ipconfig1": "ip=192.168.50.11/24,gw=192.168.50.1",
                           "net2": "virtio,bridge=vmbr1,tag=101,firewall=0"}          # 不設 IP：沒有 ipconfig2
    n = len(p.calls)
    p.set_nics("lxc", 1, [])
    assert len(p.calls) == n                                               # 沒有網卡就不呼叫


def test_grow_disk_only_grows():
    p = _fake_pve({"scsi0": "local-lvm:vm-1-disk-0,size=8G", "rootfs": "local-lvm:vm-2-disk-0,size=16G"})
    puts = []

    class VM:
        class resize:
            @staticmethod
            def put(**kw):
                puts.append(kw)
                return None
    p._vm = lambda kind, vmid: VM
    p._retry_locked = lambda fn: fn()
    p.grow_disk("qemu", 1, 16)
    p.grow_disk("lxc", 2, 16)                                             # 已經 16G：不動
    p.grow_disk("lxc", 2, 8)                                              # 不會縮小
    assert puts == [{"disk": "scsi0", "size": "16G"}]


def test_node_terminal_mode_enforced():
    """節點各自的 terminal：伺服器端依節點決定（作答者改網址換不了）"""
    from types import SimpleNamespace
    from app.main import _wants_console
    from app.questions import Node

    def ws(v=None):
        return SimpleNamespace(query_params={} if v is None else {"console": v})
    node = Node(name="w2", kind="qemu", resources={}, nics={}, terminal="console-only")
    assert [_wants_console(ws(v), node) for v in (None, "0", "1")] == [True, True, True]
    node.terminal = "ssh"
    assert [_wants_console(ws(v), node) for v in (None, "0", "1")] == [False, False, True]


def test_group_errors_translate_to_english(tmp_path):
    """驗證錯誤是存起來再翻譯的字串（translate_stored）：每一則都要有英文"""
    import re
    from app import i18n, questions
    d = tmp_path / "bad"
    shutil.copytree(NET, d)
    y = d / "question.yaml"
    head = y.read_text().split("networks:")[0]
    tail = "checks:" + y.read_text().split("checks:", 1)[1]
    y.write_text(head + "hostname: x\n" + """networks:
  lan: {cidr: 192.168.10.0/24, gateway: 10.0.0.1, vlan: 3}
  bad: {cidr: 10.10.5.0/24}
  v6: {cidr: "fd00::/64"}
  wire: {}
  Bad: {}
nodes:
  Bad_Name: {}
  a: {nics: {lan: 192.168.60.1, nope: 1.2.3.4, wire: 10.0.0.1}, terminal: maybe, type: docker, ram: 1, resources: {}}
  b: {nics: {lan: 192.168.10.5}}
  c: {nics: {lan: 192.168.10.5}}
  g1: {role: grader, terminal: ssh, type: vm}
  g2: {role: judge}
  e: "x"
""" + tail)
    errs = questions.load_one(d).errors
    assert len(errs) >= 15, errs
    tok = i18n.set_lang("en")
    try:
        for e in errs:
            assert not re.search(r"[一-鿿]", i18n.translate_stored(e)), e
    finally:
        i18n.reset(tok)


def test_pve_setup_makes_old_vmbr1_vlan_aware(tmp_path):
    """舊安裝的 /etc/network/interfaces：pve-setup.sh network 用的 awk／sed 只改 vmbr1 那一段"""
    script = (ROOT / "deploy" / "pve-setup.sh").read_text()
    awk = script.split("elif ! awk '", 1)[1].split("'", 1)[0]
    sed = script.split("sed -i '", 1)[1].split("' \\", 1)[0]
    f = tmp_path / "interfaces"
    f.write_text("auto vmbr0\niface vmbr0 inet static\n\tbridge-ports nic0\n\tbridge-stp off\n\tbridge-fd 0\n\n"
                 "auto vmbr1\niface vmbr1 inet static\n\taddress 10.10.0.1/16\n\tbridge-ports none\n\tbridge-stp off\n"
                 "\tbridge-fd 0\n\tpost-up /usr/local/sbin/lsa-net.sh\n")
    has = lambda: subprocess.run(["awk", awk, str(f)]).returncode == 0          # noqa: E731
    assert not has()
    subprocess.run(["sed", "-i", sed, str(f)], check=True)
    text = f.read_text()
    assert has()
    assert text.count("bridge-vlan-aware yes") == 1 and text.index("bridge-vlan-aware") > text.index("iface vmbr1")
    assert "\tbridge-fd 0\n\tbridge-vlan-aware yes\n\tbridge-vids 2-4094\n\tpost-up" in text


@pytest.mark.skipif(not shutil.which("node"), reason="沒有 node")
def test_frontend_js_syntax():
    for f in (ROOT / "app" / "static").glob("*.js"):
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, (f.name, r.stderr)
