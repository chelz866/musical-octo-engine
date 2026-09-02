"""Covers the unified Home page search (main._search_unified_works/
_search_unified_tags) -- on-disk and catalog-imported works browsed
through one indexed lookup, same as _temp_db's own doc comment in
test_tag_associations.py, this needs a real database (main.DB_PATH
pointed at a temp file) rather than pure in-memory fixtures.
"""

import os
import tempfile
from contextlib import contextmanager

from app import db, main as main_module, scanner
from app.main import _search_unified_tags, _search_unified_works
from app.scanner import WorkEntry, _entry_to_row


@contextmanager
def _temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        original = main_module.DB_PATH
        main_module.DB_PATH = path
        try:
            yield path
        finally:
            main_module.DB_PATH = original


def _seed_on_disk_work(path: str, work_id: str, title: str, fandom: str) -> None:
    entry = WorkEntry(work_id=work_id, title=title, fandom_candidates=[fandom], fandoms=[fandom], on_disk=True)
    db.save_works_cache(path, [_entry_to_row(entry)])
    scanner.rebuild_work_tags(path)


def _seed_catalog_work(path: str, work_id: str, title: str, fandom: str) -> None:
    db.save_catalog_works(path, [{
        "work_id": work_id, "title": title, "author": "Author", "rating": None,
        "warnings": [], "categories": [], "fandoms": [fandom], "relationships": [], "freeform": [],
        "language": None, "summary": "a summary", "word_count": None, "chapters_have": None,
        "chapters_total": None, "published_date": None, "series": None, "story_url": None,
        "source_path": None, "imported_at": "2026-01-01T00:00:00",
    }])
    db.save_catalog_work_tags(path, [work_id], [(work_id, "fandom", fandom)])


def test_search_unified_works_combines_on_disk_and_catalog():
    with _temp_db() as path:
        _seed_on_disk_work(path, "1", "On Disk Fic", "Doctor Who")
        _seed_catalog_work(path, "2", "Catalog Fic", "Doctor Who")

        entries, page, total_pages, total = _search_unified_works("fandom", "Doctor Who", 1, 25)

    assert total == 2
    assert page == 1
    assert total_pages == 1
    by_id = {e.work_id: e for e in entries}
    assert by_id["1"].on_disk is True
    assert by_id["2"].on_disk is False
    assert by_id["2"].summary == "a summary"


def test_search_unified_works_on_disk_fills_a_page_before_catalog():
    with _temp_db() as path:
        _seed_on_disk_work(path, "1", "A On Disk", "Doctor Who")
        _seed_catalog_work(path, "2", "B Catalog", "Doctor Who")
        _seed_catalog_work(path, "3", "C Catalog", "Doctor Who")

        entries, page, total_pages, total = _search_unified_works("fandom", "Doctor Who", 1, 2)
        assert total == 3
        assert total_pages == 2
        assert [e.work_id for e in entries] == ["1", "2"]

        entries2, page2, _, _ = _search_unified_works("fandom", "Doctor Who", 2, 2)
        assert [e.work_id for e in entries2] == ["3"]
        assert page2 == 2


def test_search_unified_works_no_tag_returns_nothing():
    with _temp_db() as path:
        _seed_on_disk_work(path, "1", "On Disk Fic", "Doctor Who")
        entries, page, total_pages, total = _search_unified_works("fandom", "", 1, 25)

    assert entries == []
    assert total == 0
    assert total_pages == 1


def test_search_unified_works_no_match_returns_empty():
    with _temp_db() as path:
        entries, page, total_pages, total = _search_unified_works("fandom", "Nonexistent", 1, 25)

    assert entries == []
    assert total == 0


def test_search_unified_tags_combines_both_sources():
    with _temp_db() as path:
        _seed_on_disk_work(path, "1", "On Disk Fic", "Doctor Who")
        _seed_catalog_work(path, "2", "Catalog Fic", "Doctor Strange")

        results = _search_unified_tags("fandom", "Doctor")

    assert set(results) == {"Doctor Who", "Doctor Strange"}


def test_search_unified_tags_rejects_an_unsupported_category():
    with _temp_db():
        assert _search_unified_tags("warning", "Graphic") == []
