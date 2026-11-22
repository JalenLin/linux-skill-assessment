"""終端機紀錄（asciicast v2）"""
import json

from app import recording


def _record(aid=900, sid=7, qid="example-hello"):
    rec = recording.Recorder(aid, sid, qid, 100, 30, title="C001 · example-hello")
    rec.output("中文".encode()[:4])           # UTF-8 被切在字中間
    rec.output("中文".encode()[4:] + b"\r\n$ ")
    for ch in "ls\x7f\x7fpwd\r":
        rec.input(ch.encode())
    rec.input(b"\x1b[A\r")
    rec.paste_blocked(b"\x1b[200~rm -rf /\x1b[201~")
    rec.resize(120, 40)
    rec.close()
    return rec.path


def test_cast_format():
    p = _record()
    assert p.parent.name == "a900" and recording.NAME_RE.match(p.name)
    lines = p.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2 and (header["width"], header["height"]) == (100, 30)
    events = [json.loads(x) for x in lines[1:]]
    assert "".join(e[2] for e in events if e[1] == "o").startswith("中文")
    assert "�" not in "".join(e[2] for e in events if e[1] == "o")      # 切開的 UTF-8 有接回來
    assert ["r", "120x40"] in [e[1:] for e in events]


def test_input_lines_and_views():
    p = _record(sid=8)
    header, events = recording.read(p)
    lines = recording.input_lines(header, events)
    assert [r["text"] for r in lines] == ["ls⌫⌫pwd ⏎", "↑ ⏎", "PASTE BLOCKED: rm -rf /"]
    assert [r["kind"] for r in lines] == ["input", "input", "blocked"]
    assert all(r["at"].count(":") == 2 for r in lines)
    cast = [json.loads(x) for x in recording.player_cast(header, events, lines).splitlines()]
    assert [e[2] for e in cast[1:] if e[1] == "m"] == [r["text"] for r in lines]
    assert recording.output_text([[0, "o", "\x1b[31mred\x1b[0m\r\nabc\rxy\n"]]) == "red\nxy\n"


def test_list_and_delete():
    a = _record(aid=901, sid=1)
    _record(aid=901, sid=12)                  # s12 不能被「刪除題組 1」誤刪
    rows = recording.list_for(901, {1: "Set one"})
    assert {r["set_id"] for r in rows} == {1, 12}
    one = next(r for r in rows if r["set_id"] == 1)
    assert one["set_name"] == "Set one" and one["qid"] == "example-hello" and one["blocked"] == 1
    assert recording.safe_path(901, a.name) == a
    assert recording.safe_path(901, "../a900/" + a.name) is None
    assert recording.delete_set(1) == 1
    assert [r["set_id"] for r in recording.list_for(901)] == [12]
    recording.delete_account(901)
    assert recording.list_for(901) == [] and not recording.account_dir(901).exists()


def test_truncated_last_line_is_ignored():
    p = _record(aid=902)
    with open(p, "a", encoding="utf-8") as f:
        f.write('[99.0, "o", "unfinish')
    header, events = recording.read(p)
    assert header["version"] == 2 and events[-1][0] < 99
