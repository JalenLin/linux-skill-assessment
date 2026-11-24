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
