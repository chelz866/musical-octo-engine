import os
import tempfile

from app import db


def test_set_and_get_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_fields(path, "123", title="Fixed Title", author="Fixed Author", fandoms=["Torchwood", "Doctor Who"])

        override = db.get_override(path, "123")
        assert override.title == "Fixed Title"
        assert override.author == "Fixed Author"
        assert override.fandoms == ["Torchwood", "Doctor Who"]
        assert override.dismissed is False


def test_set_fields_overwrites_previous_values():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_fields(path, "123", title="First", author=None, fandoms=None)
        db.set_fields(path, "123", title="Second", author="Someone", fandoms=["X"])

        override = db.get_override(path, "123")
        assert override.title == "Second"
        assert override.author == "Someone"
        assert override.fandoms == ["X"]


def test_set_dismissed_preserves_other_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_fields(path, "123", title="Fixed Title", author=None, fandoms=None)
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
        db.set_fields(path, "1", title="A", author=None, fandoms=None)
        db.set_dismissed(path, "2", True)

        all_overrides = db.get_all_overrides(path)
        assert set(all_overrides) == {"1", "2"}
        assert all_overrides["1"].title == "A"
        assert all_overrides["2"].dismissed is True


def test_add_and_list_tracked_feeds():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.add_tracked_feed(path, "https://archiveofourown.org/tags/161642381/feed.atom", "Heated Rivalry")
        db.add_tracked_feed(path, "https://archiveofourown.org/tags/999/feed.atom", None)

        feeds = db.list_tracked_feeds(path)
        assert [f.url for f in feeds] == [
            "https://archiveofourown.org/tags/161642381/feed.atom",
            "https://archiveofourown.org/tags/999/feed.atom",
        ]
        assert feeds[0].label == "Heated Rivalry"
        assert feeds[1].label is None


def test_add_tracked_feed_ignores_duplicate_url():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.add_tracked_feed(path, "https://example.com/feed.atom", "First")
        db.add_tracked_feed(path, "https://example.com/feed.atom", "Second")

        feeds = db.list_tracked_feeds(path)
        assert len(feeds) == 1


def test_delete_tracked_feed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.add_tracked_feed(path, "https://example.com/feed.atom", None)
        feed_id = db.list_tracked_feeds(path)[0].id

        db.delete_tracked_feed(path, feed_id)
        assert db.list_tracked_feeds(path) == []


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


def _sample_feed_row(work_id="1", **overrides):
    row = {"feed_id": 1, "work_id": work_id, "title": "T", "author": "A",
           "chapters_have": 1, "chapters_total": 1, "feed_updated": "2026-01-01T00:00:00Z"}
    row.update(overrides)
    return row


def test_save_and_load_feed_entries_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_feed_entries(path, 1, [_sample_feed_row("1"), _sample_feed_row("2")])

        rows = db.load_feed_entries(path, 1)
        assert {r["work_id"] for r in rows} == {"1", "2"}


def test_save_feed_entries_only_replaces_that_feed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_feed_entries(path, 1, [_sample_feed_row("1", feed_id=1)])
        db.save_feed_entries(path, 2, [_sample_feed_row("2", feed_id=2)])

        db.save_feed_entries(path, 1, [_sample_feed_row("3", feed_id=1)])

        assert {r["work_id"] for r in db.load_feed_entries(path, 1)} == {"3"}
        assert {r["work_id"] for r in db.load_feed_entries(path, 2)} == {"2"}


def test_set_tracked_feed_title():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.add_tracked_feed(path, "https://example.com/feed.atom", None)
        feed_id = db.list_tracked_feeds(path)[0].id

        db.set_tracked_feed_title(path, feed_id, "Fetched Feed Title")
        assert db.list_tracked_feeds(path)[0].title == "Fetched Feed Title"


def test_delete_tracked_feed_also_clears_its_cached_entries():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.add_tracked_feed(path, "https://example.com/feed.atom", None)
        feed_id = db.list_tracked_feeds(path)[0].id
        db.save_feed_entries(path, feed_id, [_sample_feed_row("1", feed_id=feed_id)])

        db.delete_tracked_feed(path, feed_id)
        assert db.load_feed_entries(path, feed_id) == []


def test_meta_get_and_set():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_meta(path, "last_refreshed_at") is None

        db.set_meta(path, "last_refreshed_at", "2026-01-01T00:00:00")
        assert db.get_meta(path, "last_refreshed_at") == "2026-01-01T00:00:00"

        db.set_meta(path, "last_refreshed_at", "2026-01-02T00:00:00")
        assert db.get_meta(path, "last_refreshed_at") == "2026-01-02T00:00:00"
