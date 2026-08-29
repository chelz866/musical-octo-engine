import os
import tempfile

from app import db


def test_set_title_author_and_get():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "123", "Fixed Title", "Fixed Author")

        override = db.get_override(path, "123")
        assert override.title == "Fixed Title"
        assert override.author == "Fixed Author"
        assert override.dismissed is False


def test_set_title_author_overwrites_previous_values():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "123", "First", None)
        db.set_title_author(path, "123", "Second", "Someone")

        override = db.get_override(path, "123")
        assert override.title == "Second"
        assert override.author == "Someone"


def test_set_dismissed_preserves_other_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "123", "Fixed Title", None)
        db.set_dismissed(path, "123", True)

        override = db.get_override(path, "123")
        assert override.title == "Fixed Title"
        assert override.dismissed is True

        db.set_dismissed(path, "123", False)
        override = db.get_override(path, "123")
        assert override.dismissed is False
        assert override.title == "Fixed Title"


def test_get_override_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_override(path, "does-not-exist") is None


def test_get_all_overrides():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "1", "A", None)
        db.set_dismissed(path, "2", True)

        all_overrides = db.get_all_overrides(path)
        assert set(all_overrides) == {"1", "2"}
        assert all_overrides["1"].title == "A"
        assert all_overrides["2"].dismissed is True


def test_set_and_get_all_tag_flags():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_flags(path, {"Torchwood": True, "Ianto Jones": False})

        flags = db.get_all_tag_flags(path)
        assert flags == {"Torchwood": True, "Ianto Jones": False}


def test_set_tag_flags_overwrites_existing_entries():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_flags(path, {"Torchwood": False})
        db.set_tag_flags(path, {"Torchwood": True, "Doctor Who": True})

        flags = db.get_all_tag_flags(path)
        assert flags == {"Torchwood": True, "Doctor Who": True}


def test_get_all_tag_flags_empty_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_all_tag_flags(path) == {}


def _sample_work_row(work_id="1", **overrides):
    row = {c: None for c in db.WORKS_CACHE_COLUMNS}
    row.update({
        "work_id": work_id, "title": "A Title", "author": "An Author",
        "size_bytes": 123, "on_disk": 1, "log_success": 1,
    })
    row.update(overrides)
    return row


def test_save_and_load_works_cache_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_works_cache(path, [_sample_work_row("1"), _sample_work_row("2", title="Other")])

        rows = db.load_works_cache(path)
        assert {r["work_id"] for r in rows} == {"1", "2"}
        assert next(r for r in rows if r["work_id"] == "1")["title"] == "A Title"


def test_save_works_cache_replaces_previous_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_works_cache(path, [_sample_work_row("1")])
        db.save_works_cache(path, [_sample_work_row("2")])

        rows = db.load_works_cache(path)
        assert {r["work_id"] for r in rows} == {"2"}


def test_pop_legacy_tracked_feeds_returns_empty_when_no_legacy_table(tmp_path):
    path = str(tmp_path / "app.db")
    db.init_db(path)
    assert db.pop_legacy_tracked_feeds(path) == []


def test_pop_legacy_tracked_feeds_migrates_and_drops_old_tables(tmp_path):
    import sqlite3

    path = str(tmp_path / "app.db")
    db.init_db(path)

    # simulate a database from before the switch to the `reader` library,
    # which stored tracked feeds in this same app.db
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tracked_feeds (id INTEGER PRIMARY KEY, url TEXT, label TEXT, title TEXT)")
    conn.execute("CREATE TABLE feed_entries (feed_id INTEGER, work_id TEXT)")
    conn.execute("INSERT INTO tracked_feeds (url, label) VALUES (?, ?)", ("https://example.com/a.atom", "A"))
    conn.execute("INSERT INTO tracked_feeds (url, label) VALUES (?, ?)", ("https://example.com/b.atom", None))
    conn.commit()
    conn.close()

    migrated = db.pop_legacy_tracked_feeds(path)
    assert set(migrated) == {("https://example.com/a.atom", "A"), ("https://example.com/b.atom", None)}

    # idempotent: tables are gone, so a second call finds nothing to migrate
    assert db.pop_legacy_tracked_feeds(path) == []

    conn = sqlite3.connect(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "tracked_feeds" not in tables
    assert "feed_entries" not in tables


def test_set_title_author_preserves_dismissed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_dismissed(path, "1", True)

        db.set_title_author(path, "1", "New Title", "New Author")

        override = db.get_override(path, "1")
        assert override.dismissed is True
        assert override.title == "New Title"
        assert override.author == "New Author"


def test_init_db_adds_missing_column_to_existing_table_without_losing_data():
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")

        # Simulate a database created by an older version of the app, before
        # fandom_candidates existed on works_cache.
        old_columns = [c for c in db.WORKS_CACHE_COLUMNS if c != "fandom_candidates"]
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE works_cache ({', '.join(f'{c} TEXT' for c in old_columns)}, PRIMARY KEY (work_id))")
        conn.execute(
            f"INSERT INTO works_cache ({', '.join(old_columns)}) VALUES ({', '.join('?' for _ in old_columns)})",
            tuple("1" if c == "work_id" else None for c in old_columns),
        )
        conn.commit()
        conn.close()

        db.init_db(path)  # should migrate in place, not wipe existing rows

        rows = db.load_works_cache(path)
        assert len(rows) == 1
        assert rows[0]["work_id"] == "1"
        assert rows[0]["fandom_candidates"] is None

        # and the newly-added column is now usable
        db.save_works_cache(path, [{**{c: None for c in db.WORKS_CACHE_COLUMNS}, "work_id": "2", "fandom_candidates": "Torchwood"}])
        assert db.load_works_cache(path)[0]["fandom_candidates"] == "Torchwood"


def test_save_and_get_all_abs_matches():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_abs_matches(path, {"1": "item-a", "2": "item-b"})

        assert db.get_all_abs_matches(path) == {"1": "item-a", "2": "item-b"}


def test_save_abs_matches_replaces_previous_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_abs_matches(path, {"1": "item-a"})
        db.save_abs_matches(path, {"2": "item-b"})

        assert db.get_all_abs_matches(path) == {"2": "item-b"}


def test_get_all_abs_matches_empty_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_all_abs_matches(path) == {}


def test_meta_get_and_set():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_meta(path, "last_refreshed_at") is None

        db.set_meta(path, "last_refreshed_at", "2026-01-01T00:00:00")
        assert db.get_meta(path, "last_refreshed_at") == "2026-01-01T00:00:00"

        db.set_meta(path, "last_refreshed_at", "2026-01-02T00:00:00")
        assert db.get_meta(path, "last_refreshed_at") == "2026-01-02T00:00:00"
