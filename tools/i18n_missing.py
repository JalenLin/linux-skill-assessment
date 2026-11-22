"""列出某個語言還沒翻譯的介面字串。

    python tools/i18n_missing.py en          # 列出缺少的
    python tools/i18n_missing.py en --json   # 輸出成可以貼進翻譯檔的 JSON 骨架

只找得到程式與樣板裡直接寫成 _("…")、t("…")、_raw("…") 的字串；
存在資料庫裡、事後才翻譯的訊息（translate_stored）請另外確認。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "app"
PAT = re.compile(r'''(?<![A-Za-z0-9_])(?:_|t|_raw|gettext)\(\s*(["'])((?:\\.|(?!\1).)+)\1''')


def source_strings() -> set[str]:
    out = set()
    for p in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.html")) + list(ROOT.glob("static/*.js")):
        for m in PAT.finditer(p.read_text(encoding="utf-8")):
            s = m.group(2).encode().decode("unicode_escape").encode("latin-1").decode("utf-8") if "\\" in m.group(2) else m.group(2)
            if re.search(r"[一-鿿]", s):
                out.add(s)
    return out


def catalog(lang: str) -> dict[str, str]:
    cat = {}
    for f in sorted((ROOT / "locales" / lang).glob("*.json")):
        cat.update(json.loads(f.read_text(encoding="utf-8")))
    return cat


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    lang = sys.argv[1]
    missing = sorted(s for s in source_strings() if s not in catalog(lang))
    if "--json" in sys.argv:
        print(json.dumps({s: "" for s in missing}, ensure_ascii=False, indent=1))
    else:
        for s in missing:
            print(s)
        print(f"\n{len(missing)} 個字串沒有 {lang} 翻譯", file=sys.stderr)
