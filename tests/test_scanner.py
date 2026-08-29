import json
import os
import tempfile
import zipfile

from app import db
from app.scanner import load_cached, refresh_cache, scan

_CONTAINER_XML = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_OPF_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:opf="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Title</dc:title>
    <dc:creator>Author</dc:creator>
    {subjects}
  </metadata>
</package>"""


def _build_epub(path, subjects):
    subject_xml = "\n".join(f"<dc:subject>{s}</dc:subject>" for s in subjects)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("content.opf", _OPF_TEMPLATE.format(subjects=subject_xml))

# Real content doesn't matter here -- these exercise the filename matching,
# not epub parsing (a bad zip still produces a WorkEntry with parse_error set,
# it's just not skipped).


def _write_log(tmp, lines):
    path = os.path.join(tmp, "log.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def test_matches_underscore_separated_filenames():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "7773_The_Business__Basingstoke.epub"), "w").close()
        result = scan(tmp, None)
    assert {e.work_id for e in result.entries} == {"7773"}
    assert result.stats.total_on_disk == 1


def test_matches_space_separated_filenames():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "1011406 Messy - artist_artists.epub"), "w").close()
        result = scan(tmp, None)
    assert {e.work_id for e in result.entries} == {"1011406"}
    assert result.stats.total_on_disk == 1


def test_ignores_non_epub_and_non_matching_files():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "readme.txt"), "w").close()
        open(os.path.join(tmp, "not_an_id.epub"), "w").close()
        result = scan(tmp, None)
    assert result.entries == []
    assert result.stats.total_on_disk == 0


def test_bad_epub_on_disk_is_flagged_as_parse_error_issue():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "123_Bad_Epub.epub"), "w") as f:
            f.write("not actually a zip")
        result = scan(tmp, None)

    entry = result.entries[0]
    assert entry.on_disk is True
    assert entry.issue_type == "parse_error"


def test_logged_success_with_no_file_is_flagged_missing():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as logs:
        log_path = _write_log(logs, [
            {"link": "https://archiveofourown.org/works/999", "title": ["999 Ghost Fic - Nobody"], "success": True, "timestamp": "01/01/2026, 00:00:00"},
        ])
        result = scan(downloads, log_path)

    entry = result.entries[0]
    assert entry.on_disk is False
    assert entry.issue_type == "missing"
    assert result.stats.missing_but_logged_success == 1


def test_logged_failure_with_no_file_is_flagged_failed():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as logs:
        log_path = _write_log(logs, [
            {"link": "https://archiveofourown.org/works/999", "title": ["999 Ghost Fic - Nobody"], "success": False, "timestamp": "01/01/2026, 00:00:00"},
        ])
        result = scan(downloads, log_path)

    entry = result.entries[0]
    assert entry.issue_type == "failed"
    assert result.stats.logged_failure_count == 1


def test_override_applies_title_author_and_dismissed():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)
        db.set_title_author(db_path, "999", "Manually Fixed", "Real Author")
        db.set_dismissed(db_path, "999", True)

        result = scan(tmp, None, db_path)

    entry = result.entries[0]
    assert entry.work_id == "999"
    assert entry.title == "Manually Fixed"
    assert entry.author == "Real Author"
    assert entry.dismissed is True


def test_load_cached_matches_empty_before_any_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        result = load_cached(db_path)
    assert result.entries == []
    assert result.stats.total_on_disk == 0


def test_refresh_cache_populates_cache_and_load_cached_reads_it_back():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        open(os.path.join(downloads, "7773_The_Business__Basingstoke.epub"), "w").close()

        live_result = refresh_cache(downloads, None, db_path)
        assert live_result.stats.total_on_disk == 1

        cached_result = load_cached(db_path)
    assert {e.work_id for e in cached_result.entries} == {"7773"}
    assert cached_result.stats.total_on_disk == 1
    assert cached_result.entries[0].issue_type == "parse_error"  # empty file isn't a valid epub


def test_load_cached_does_not_reflect_filesystem_changes_until_refreshed_again():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        path = os.path.join(downloads, "7773_The_Business__Basingstoke.epub")
        open(path, "w").close()
        refresh_cache(downloads, None, db_path)

        os.remove(path)

        # the cache still shows the file as present -- this is the whole
        # point of caching, it's only as fresh as the last refresh
        assert load_cached(db_path).stats.total_on_disk == 1


def test_load_cached_applies_overrides_same_as_live_scan():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        refresh_cache(downloads, None, db_path)
        db.set_title_author(db_path, "42", "Manual Only", None)

        result = load_cached(db_path)
    assert result.entries[0].work_id == "42"
    assert result.entries[0].title == "Manual Only"


def test_fandom_candidates_survive_refresh_cache_round_trip():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(
            os.path.join(downloads, "1_Title_Author.epub"),
            ["Fanworks", "Torchwood", "Ianto Jones"],
        )

        refresh_cache(downloads, None, db_path)
        entry = load_cached(db_path).entries[0]

    assert entry.fandoms == ["Torchwood"]
    assert entry.fandom_candidates == ["Torchwood", "Ianto Jones"]


def test_tag_flag_overrides_heuristic_guess_for_one_work():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(
            os.path.join(downloads, "1_Title_Author.epub"),
            ["Fanworks", "Torchwood", "Ianto Jones"],
        )
        refresh_cache(downloads, None, db_path)
        db.set_tag_flags(db_path, {"Torchwood": False, "Ianto Jones": True})  # user corrects the guess

        entry = load_cached(db_path).entries[0]
    assert entry.fandoms == ["Ianto Jones"]
    assert entry.fandom_candidates == ["Torchwood", "Ianto Jones"]  # picker options unchanged


def test_tag_flag_applies_to_every_work_sharing_that_tag():
    # The whole point: classifying one tag fixes every work with that tag
    # in one action, instead of a per-work correction on each of them.
    # In both works "Ianto Jones" sits second in the candidate list behind
    # a parenthesized fandom name, so the heuristic guess excludes it from
    # both (it looks character-shaped and isn't the kept first item).
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(os.path.join(downloads, "1_A.epub"), ["Fanworks", "Some Fandom (2020)", "Ianto Jones"])
        _build_epub(os.path.join(downloads, "2_B.epub"), ["Fanworks", "Other Fandom (2021)", "Ianto Jones"])
        refresh_cache(downloads, None, db_path)

        before = {e.work_id: e for e in load_cached(db_path).entries}
        assert "Ianto Jones" not in before["1"].fandoms
        assert "Ianto Jones" not in before["2"].fandoms

        db.set_tag_flags(db_path, {"Ianto Jones": True})

        entries = {e.work_id: e for e in load_cached(db_path).entries}
    assert "Ianto Jones" in entries["1"].fandoms
    assert "Ianto Jones" in entries["2"].fandoms
