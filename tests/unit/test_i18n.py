"""多國語言"""
import subprocess
import sys

import pytest

from app import i18n


@pytest.fixture
def en():
    tok = i18n.set_lang("en")
    yield
    i18n.reset(tok)


def test_default_is_source_text():
    assert i18n._("檢查完成") == "檢查完成"
    assert i18n._("請等待 {n} 秒後再檢查", n=5) == "請等待 5 秒後再檢查"


def test_gettext_english(en):
    assert i18n._("檢查完成") == "Check complete"
    assert i18n._("請等待 {n} 秒後再檢查", n=5) == "Please wait 5 seconds before checking again"
    assert i18n._("沒有這個字串") == "沒有這個字串"            # 沒翻到顯示原文


def test_translate_stored_patterns(en):
    assert i18n.translate_stored("檢查完成") == "Check complete"
    # 參數本身也是中文訊息：一併翻譯
    assert i18n.translate_stored("建置失敗：題目格式錯誤：沒有定義 checks") == \
        "Build failed: Invalid question package: " + i18n._("沒有定義 checks")
    # 「；」分隔的多則訊息逐一翻譯
    assert i18n.translate_stored("檢查完成；服務重啟，檢查中斷") == "Check complete; Service restarted; check interrupted"
    assert i18n.translate_stored("") == ""
    assert i18n.translate_stored(None) == ""


def test_translate_lines(en):
    log = "[12:00:01] 從 base 9000 完整複製為 9100（lxc）\n不是紀錄格式的行"
    out = i18n.translate_lines(log).split("\n")
    assert out[0] == "[12:00:01] Full clone of base 9000 to 9100 (lxc)"
    assert out[1] == "不是紀錄格式的行"


def test_resolve_language(appdb):
    assert i18n.resolve("en") == "en"
    assert i18n.resolve("xx") == "zh-TW"                 # 不存在的語言 → 預設
    appdb.set_setting("default_lang", "auto")
    try:
        assert i18n.resolve(None, "en-US,en;q=0.9") == "en"
        assert i18n.resolve(None, "fr-FR") == "zh-TW"
    finally:
        appdb.set_setting("default_lang", "zh-TW")


def test_languages_listed():
    langs = i18n.languages()
    assert langs["zh-TW"] == "繁體中文" and langs["en"] == "English"


def test_english_catalog_is_complete():
    """介面上每個字串都要有英文翻譯"""
    r = subprocess.run([sys.executable, "tools/i18n_missing.py", "en"], capture_output=True, text=True,
                       cwd=i18n.LOCALES.parent.parent)
    assert r.stdout.strip() == "", "缺少英文翻譯：\n" + r.stdout


def test_catalog_placeholders_match():
    """譯文的 {參數} 要和原文一樣，否則 format 會出錯"""
    import re
    bad = []
    for k, v in i18n._catalogs["en"].items():
        if set(re.findall(r"\{(\w+)\}", k)) != set(re.findall(r"\{(\w+)\}", v)):
            bad.append((k, v))
    assert not bad, bad
