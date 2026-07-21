import json
import os
import tempfile

from app.log_reader import parse_log

LINES = [
    {"link": "https://archiveofourown.org/users/someone/bookmarks", "message": "starting page", "level": "debug", "timestamp": "06/12/2026, 20:05:48"},
    {"link": "https://archiveofourown.org/works/7773", "title": ["7773 The Business - Basingstoke"], "workskin": False, "success": True, "timestamp": "06/13/2026, 17:42:22"},
    {"error": "Cloudflare challenge or error page detected", "success": False, "timestamp": "07/05/2026, 20:49:58"},
    {"link": "https://archiveofourown.org/works/9999", "title": ["9999 Some Fic - Some Author"], "success": False, "timestamp": "06/13/2026, 18:00:00"},
    # duplicate id, later in file -> should win over an earlier record for the same id
    {"link": "https://archiveofourown.org/works/7773", "title": ["7773 The Business (revised) - Basingstoke"], "success": True, "timestamp": "06/14/2026, 09:00:00"},
]


def _write_log(tmp_path):
    path = os.path.join(tmp_path, "log.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in LINES:
            f.write(json.dumps(line) + "\n")
    return path


def test_skips_lines_without_link():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(tmp)
        records = parse_log(path)
    assert set(records) == {"7773", "9999"}


def test_parses_title_and_author():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(tmp)
        records = parse_log(path)
    rec = records["9999"]
    assert rec.title == "Some Fic"
    assert rec.author == "Some Author"
    assert rec.success is False


def test_later_line_wins_for_duplicate_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_log(tmp)
        records = parse_log(path)
    rec = records["7773"]
    assert rec.title == "The Business (revised)"
    assert rec.timestamp == "06/14/2026, 09:00:00"
    assert rec.success is True
