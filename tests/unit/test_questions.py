"""題目包載入、驗證、zip"""
import io
import re
import shutil
import zipfile

import pytest

from app import questions
from conftest import EXAMPLE


def test_example_question_is_valid():
    q = questions.load_one(EXAMPLE)
    assert q.errors == []
    assert q.kind == "lxc" and q.hostname == "hello"
    assert [c.id for c in q.checks] == ["page", "boot", "restart"]
    assert sum(c.points for c in q.checks) == 10
    assert q.has_solution
    assert q.languages == ["en", "zh-TW"]                       # 主要語言英文，附中文翻譯
    assert q.title_in("en") == "The hello service won't start (example)"
    assert q.title_in("zh-TW") == "Hello 服務無法啟動（範例題）"
    assert q.title_in("ja") == q.title                          # 沒有翻譯 → 原文
    assert "範例題" in q.description_html_in("zh-TW")
    assert "example question" in q.description_html_in("en")
    assert q.check_desc("boot", "zh-TW") == "hello 服務設為開機自動啟動"
    assert q.check_desc("boot", "en") == "The hello service is enabled to start at boot"


def _copy(tmp_path, name="broken-q"):
    d = tmp_path / name
    shutil.copytree(EXAMPLE, d)
    return d


def test_missing_required_file(tmp_path):
    d = _copy(tmp_path)
    (d / "check.yml").unlink()
    with pytest.raises(questions.QuestionError, match="check.yml"):
        questions.load_one(d)


def test_check_without_task_is_reported(tmp_path):
    d = _copy(tmp_path)
    y = (d / "question.yaml").read_text(encoding="utf-8")
    (d / "question.yaml").write_text(y.replace("  - id: restart", "  - id: extra\n    desc: x\n    points: 1\n  - id: restart"),
                                     encoding="utf-8")
    q = questions.load_one(d)
    assert any("check:extra" in e for e in q.errors)


def test_i18n_with_unknown_check(tmp_path):
    d = _copy(tmp_path)
    y = (d / "question.yaml").read_text(encoding="utf-8")
    (d / "question.yaml").write_text(y.replace("      boot:", "      nosuch: x\n      boot:"), encoding="utf-8")
    q = questions.load_one(d)
    assert any("nosuch" in e for e in q.errors)


def test_language_code_underscore_accepted(tmp_path):
    """zh_TW（Linux locale 寫法）與 zh-TW 視為同一語言"""
    d = _copy(tmp_path)
    y = (d / "question.yaml").read_text(encoding="utf-8")
    (d / "question.yaml").write_text(y.replace("  zh-TW:", "  zh_TW:").replace("lang: en ", "lang: EN "), encoding="utf-8")
    (d / "description.zh-TW.md").rename(d / "description.zh_tw.md")
    q = questions.load_one(d)
    assert q.errors == []
    assert q.languages == ["en", "zh-TW"]
    assert q.title_in("zh-TW") == "Hello 服務無法啟動（範例題）"
    assert "範例題" in q.description_html_in("zh-TW")


def test_duplicate_description_language(tmp_path):
    d = _copy(tmp_path)
    (d / "description.zh_TW.md").write_text("x", encoding="utf-8")
    q = questions.load_one(d)
    assert any("重複" in e for e in q.errors)


def test_bad_type(tmp_path):
    d = _copy(tmp_path)
    y = (d / "question.yaml").read_text(encoding="utf-8")
    (d / "question.yaml").write_text(y.replace("type: lxc", "type: docker"), encoding="utf-8")
    assert questions.load_one(d).errors


def test_build_version_changes_only_with_setup(tmp_path):
    d = _copy(tmp_path)
    v0 = questions.load_one(d).build_version
    (d / "description.md").write_text("changed", encoding="utf-8")
    (d / "check.yml").write_text((d / "check.yml").read_text(encoding="utf-8") + "\n# c\n", encoding="utf-8")
    assert questions.load_one(d).build_version == v0           # 敘述、校驗不影響範本
    (d / "files" / "hello.service").write_text("x", encoding="utf-8")
    assert questions.load_one(d).build_version != v0


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, data in entries.items():
            z.writestr(n, data)
    return buf.getvalue()


def test_install_zip_rejects_unsafe_paths():
    with pytest.raises(questions.QuestionError, match="不安全"):
        questions.install_zip(_zip({"../evil/question.yaml": b"x"}))
    with pytest.raises(questions.QuestionError):
        questions.install_zip(b"not a zip")
    with pytest.raises(questions.QuestionError, match="question.yaml"):
        questions.install_zip(_zip({"a/readme.txt": b"x"}))


def test_install_and_export_zip_roundtrip(unit_dir):
    entries = {f"zip-hello/{p.relative_to(EXAMPLE)}": p.read_bytes() for p in EXAMPLE.rglob("*") if p.is_file()}
    qid = questions.install_zip(_zip(entries))
    assert qid == "zip-hello"
    assert questions.get("zip-hello").errors == []
    with zipfile.ZipFile(io.BytesIO(questions.export_zip("zip-hello"))) as z:
        assert "zip-hello/question.yaml" in z.namelist()


def test_terminal_option(tmp_path):
    import shutil
    from conftest import EXAMPLE
    from app.questions import load_one as load_question
    d = tmp_path / "con-q"
    shutil.copytree(EXAMPLE, d)
    q = load_question(d)
    assert q.terminal == "ssh" and not q.errors
    y = d / "question.yaml"
    y.write_text(y.read_text().replace("# terminal: console", "terminal: Console"))
    q2 = load_question(d)
    assert q2.terminal == "console" and not q2.errors
    assert q2.build_version == q.build_version              # 不影響範本，不用重建
    assert (q2.terminal_default, q2.terminal_switchable) == ("console", True)
    for mode, default, switch in (("console-only", "console", False), ("ssh-only", "ssh", False), ("ssh", "ssh", True)):
        y.write_text(re.sub(r"terminal: \S+", f"terminal: {mode}", y.read_text()))
        q3 = load_question(d)
        assert (q3.terminal, q3.terminal_default, q3.terminal_switchable) == (mode, default, switch), mode
    y.write_text(re.sub(r"terminal: \S+", "terminal: none", y.read_text()))    # none 只給群組題的節點用
    assert any("terminal" in e for e in load_question(d).errors)


# ---------------- 多節點題 ----------------
K8S = EXAMPLE.parent / "example-k8s"


def test_example_k8s_group():
    q = questions.load_one(K8S)
    assert q.errors == [] and q.is_group
    assert [n.name for n in q.nodes] == ["cp1", "w1", "w2"]
    assert all(n.kind == "qemu" for n in q.nodes)
    assert q.networks == {"lab": {"cidr": "192.168.50.0/24", "gateway": "192.168.50.1"}}
    assert q.node("cp1").nics == {"lab": "192.168.50.11/24"}
    assert q.node("cp1").resources["memory"] == 2560 and q.node("w1").resources["cores"] == 2
    assert [t["name"] for t in q.terminal_tabs()] == ["cp1", "w1", "w2"]
    single = questions.load_one(EXAMPLE)
    assert not single.is_group and single.terminal_tabs() == [{"name": "", "default": "ssh", "switch": True}]


def test_example_net_group():
    q = questions.load_one(EXAMPLE.parent / "example-net")
    assert q.errors == [] and [n.kind for n in q.nodes] == ["lxc"] * 4
    assert list(q.node("r1").nics) == ["lan", "dmz"] and q.node("web").resources["memory"] == 256
    g = q.node("grader")
    assert g.is_grader and g.terminal == "none" and "grader" not in [t["name"] for t in q.terminal_tabs()]


def test_grader_role_validation(tmp_path):
    bad = _group(tmp_path, """networks:
  lan: {cidr: 192.168.10.0/24}
nodes:
  a: {nics: {lan: 192.168.10.2}}
  g1: {role: grader, terminal: ssh}
  g2: {role: grader, type: vm}
  g3: {role: judge}
""")
    text = "\n".join(bad.errors)
    for frag in ("g1：grader 節點不提供終端機", "g2：grader 節點必須是 lxc", "nodes.g3.role"):
        assert frag in text, (frag, text)
    only = _group(tmp_path, "networks: {}\nnodes:\n  g: {role: grader, type: lxc}\n")
    assert any("不是 grader" in e for e in only.errors)


def _group(tmp_path, yaml_text: str):
    d = tmp_path / "grp-q"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(K8S, d)
    y = d / "question.yaml"
    head = y.read_text().split("networks:")[0]
    tail = "checks:" + y.read_text().split("checks:", 1)[1]
    y.write_text(head + yaml_text + tail)
    return questions.load_one(d)


def test_group_validation(tmp_path):
    ok = _group(tmp_path, """networks:
  lab: {cidr: 192.168.50.0/24}
  dmz: {cidr: 172.16.0.0/24}
nodes:
  r1: {type: lxc, nics: {dmz: 172.16.0.1, lab: 192.168.50.1}, terminal: console-only}
  w1: {nics: {lab: 192.168.50.21}}
  db: {type: lxc, nics: {lab: 192.168.50.30}, terminal: none}
""")
    assert ok.errors == []
    r1 = ok.node("r1")
    assert list(r1.nics) == ["dmz", "lab"]                   # eth1、eth2 依 nics 寫的順序
    assert r1.kind == "lxc" and r1.resources["memory"] == 512 and r1.terminal_default == "console"
    assert [t["name"] for t in ok.terminal_tabs()] == ["r1", "w1"]      # none 沒有分頁

    bad = _group(tmp_path, """networks:
  lab: {cidr: 192.168.50.0/24, gateway: 10.0.0.1}
  mgmt: {cidr: 10.10.5.0/24}
nodes:
  Bad_Name: {}
  a: {nics: {lab: 192.168.60.1}}
  b: {nics: {lab: 192.168.50.5, nope: 1.2.3.4}, terminal: maybe}
  c: {nics: {lab: 192.168.50.5}, type: docker}
""")
    text = "\n".join(bad.errors)
    for frag in ("gateway", "10.10.0.0/16", "Bad_Name", "192.168.60.1", "nope", "nodes.b.terminal", "重複",
                 "nodes.c.type"):
        assert frag in text, (frag, text)

    none = _group(tmp_path, "networks: {}\nnodes:\n  a: {terminal: none}\n")
    assert any("至少要有一台" in e for e in none.errors)

    typo = _group(tmp_path, "hostname: x\nnetworks:\n  lab: {cidr: 192.168.50.0/24, gw: 1}\n"
                            "nodes:\n  a: {nic: {lab: 192.168.50.2}, resources: {cores: 2}}\n")
    text = "\n".join(typo.errors)
    for frag in ("hostname", "networks.lab 只能有", "不認得的設定：nic", "nodes.a.resources：節點直接寫"):
        assert frag in text, (frag, text)


def test_group_router_and_unnumbered(tmp_path):
    """router 節點自己就是 gateway；沒有 cidr 的網路只能接線不設 IP（作答者自己設）"""
    from app.instances import nic_specs
    q = _group(tmp_path, """networks:
  lan: {cidr: 192.168.10.0/24, gateway: 192.168.10.1}
  wire: {}
nodes:
  r1: {nics: {lan: 192.168.10.1, wire: ~}}
  pc1: {nics: {lan: 192.168.10.10}}
""")
    assert q.errors == []
    assert q.node("r1").nics == {"lan": "192.168.10.1/24", "wire": None}
    assert nic_specs(q, q.node("r1"), {"lan": 5, "wire": 6}) == [(1, 5, "192.168.10.1/24", None), (2, 6, None, None)]
    assert nic_specs(q, q.node("pc1"), {"lan": 5, "wire": 6}) == [(1, 5, "192.168.10.10/24", "192.168.10.1")]
    bad = _group(tmp_path, "networks:\n  wire: {}\nnodes:\n  a: {nics: {wire: 10.0.0.1}}\n")
    assert any("沒有 cidr" in e for e in bad.errors)


def test_group_build_version_tracks_topology(tmp_path):
    a = _group(tmp_path, "networks:\n  lab: {cidr: 192.168.50.0/24}\nnodes:\n  a: {nics: {lab: 192.168.50.2}}\n")
    b = _group(tmp_path, "networks:\n  lab: {cidr: 192.168.50.0/24}\nnodes:\n  a: {nics: {lab: 192.168.50.3}}\n")
    assert a.errors == b.errors == [] and a.build_version != b.build_version


def test_inventory_and_nics():
    from app.grader import inventory_for
    from app.instances import nic_specs
    q = questions.load_one(K8S)
    assert inventory_for("10.10.1.2")["all"]["hosts"]["target"]["target_ip"] == "10.10.1.2"
    inv = inventory_for([{"name": n.name, "ip": f"10.10.1.{i}", "nics": n.nics} for i, n in enumerate(q.nodes, 2)])
    hosts = inv["all"]["hosts"]
    assert list(hosts) == ["cp1", "w1", "w2"]
    assert hosts["w1"]["ansible_host"] == "10.10.1.3" and hosts["w1"]["node_ip"] == "192.168.50.21"
    assert inv["all"]["vars"]["lsa_nodes"]["w2"] == {"mgmt_ip": "10.10.1.4", "ips": {"lab": "192.168.50.22"}}
    assert nic_specs(q, q.node("cp1"), {"lab": 123}) == [(1, 123, "192.168.50.11/24", "192.168.50.1")]
    assert nic_specs(q, q.node("cp1"), {"lab": 123}, gateway=False) == [(1, 123, "192.168.50.11/24", None)]
