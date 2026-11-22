"""題目標籤：解析與改寫 question.yaml"""
import shutil

import pytest
import yaml

from app import questions
from app.config import cfg
from conftest import EXAMPLE


def test_parse_tags():
    assert questions.parse_tags(None) == ([], None)
    assert questions.parse_tags(["systemd", " web ", "Web", "", "入門"]) == (["systemd", "web", "入門"], None)
    assert questions.parse_tags("a, b ,c") == (["a", "b", "c"], None)
    assert questions.parse_tags({"a": 1})[1]
    assert questions.parse_tags(["x" * 31])[1]


@pytest.fixture
def qdir():
    d = cfg.questions_path / "tag-test"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(EXAMPLE, d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


CASES = {
    "inline": "title: T  # 標題\ntags: [a, b]  # 註解\nlang: en\n",
    "block": "title: T\ntags:\n  - a\n  - b\n\n# 下一段\nlang: en\n",
    "block0": "title: T\ntags:\n- a\n- b\nlang: en\n",
    "none": "# 開頭註解\ntitle: T\nlang: en\n",
    "notitle": "lang: en\n",
    "nested": "title: T\ni18n:\n  en:\n    tags: nope\nlang: en\n",
}


@pytest.mark.parametrize("name", CASES)
def test_set_tags_rewrites_only_tags(qdir, name):
    src = CASES[name]
    p = qdir / "question.yaml"
    meta = yaml.safe_load(p.read_text(encoding="utf-8"))
    meta.pop("tags", None)
    body = yaml.safe_dump({k: v for k, v in meta.items() if k not in ("title", "lang")}, allow_unicode=True)
    p.write_text(src + body, encoding="utf-8")
    before = yaml.safe_load(p.read_text(encoding="utf-8"))

    assert questions.set_tags("tag-test", ["systemd", "網路", "systemd"]) == ["systemd", "網路"]
    text = p.read_text(encoding="utf-8")
    after = yaml.safe_load(text)
    assert after["tags"] == ["systemd", "網路"]
    assert {k: v for k, v in after.items() if k != "tags"} == {k: v for k, v in before.items() if k != "tags"}
    for comment in ("# 標題", "# 下一段", "# 開頭註解"):
        if comment in src:
            assert comment in text
    assert questions.load_one(qdir).tags == ["systemd", "網路"]

    questions.set_tags("tag-test", [])                     # 清空 → 拿掉 tags
    after = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "tags" not in after
    assert {k: v for k, v in after.items()} == {k: v for k, v in before.items() if k != "tags"}


def test_set_tags_rejects_bad(qdir):
    with pytest.raises(questions.QuestionError):
        questions.set_tags("tag-test", ["a,b"])
    with pytest.raises(questions.QuestionError):
        questions.set_tags("../x", ["a"])
