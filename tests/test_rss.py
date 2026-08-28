import os
import tempfile
from datetime import datetime
from unittest.mock import patch

from app import db
from app.rss import (
    CHAPTERS_RE,
    FeedEntry,
    FeedFetchError,
    FeedResult,
    assess_status,
    load_cached_feed,
    parse_feed_timestamp,
    parse_feed_xml,
    refresh_feed_cache,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_feed.atom")


def _load_fixture() -> str:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return f.read()


def test_parses_real_sample_feed():
    result = parse_feed_xml(_load_fixture())

    assert result.title == "AO3 works tagged 'Game Changers | Heated Rivalry - All Media Types'"
    assert len(result.entries) == 3
    assert [e.work_id for e in result.entries] == ["91535741", "91535026", "91534106"]


def test_extracts_ongoing_chapters():
    result = parse_feed_xml(_load_fixture())
    entry = next(e for e in result.entries if e.work_id == "91535741")

    assert entry.title == "Follow your heart, wherever it takes you"
    assert entry.author == "the_summer_before"
    assert entry.chapters_have == 1
    assert entry.chapters_total is None
    assert entry.is_complete is False


def test_extracts_complete_chapters():
    result = parse_feed_xml(_load_fixture())
    entry = next(e for e in result.entries if e.work_id == "91535026")

    assert entry.chapters_have == 1
    assert entry.chapters_total == 1
    assert entry.is_complete is True


def test_anonymous_author_has_no_uri_but_name_still_parsed():
    result = parse_feed_xml(_load_fixture())
    entry = next(e for e in result.entries if e.work_id == "91535026")
    assert entry.author == "Anonymous"


def test_chapters_regex_handles_multi_digit_and_unknown_total():
    assert CHAPTERS_RE.search("Words: 100, Chapters: 12/30, Language: English").groups() == ("12", "30")
    assert CHAPTERS_RE.search("Words: 100, Chapters: 3/?, Language: English").groups() == ("3", "?")


def test_parse_feed_timestamp_valid_and_invalid():
    assert parse_feed_timestamp("2026-08-28T19:28:16Z") == datetime(2026, 8, 28, 19, 28, 16)
    assert parse_feed_timestamp("not a timestamp") is None
    assert parse_feed_timestamp(None) is None


def test_assess_status_not_downloaded():
    entry = FeedEntry(work_id="1", feed_updated="2026-08-28T19:28:16Z")
    assert assess_status(entry, on_disk=False, local_timestamp=datetime(2026, 8, 29)) == "not_downloaded"


def test_assess_status_up_to_date():
    entry = FeedEntry(work_id="1", feed_updated="2026-08-28T19:28:16Z")
    local = datetime(2026, 8, 29, 0, 0, 0)
    assert assess_status(entry, on_disk=True, local_timestamp=local) == "up_to_date"


def test_assess_status_may_need_update():
    entry = FeedEntry(work_id="1", feed_updated="2026-08-28T19:28:16Z")
    local = datetime(2026, 8, 27, 0, 0, 0)
    assert assess_status(entry, on_disk=True, local_timestamp=local) == "may_need_update"


def test_assess_status_unknown_without_timestamps():
    entry = FeedEntry(work_id="1", feed_updated=None)
    assert assess_status(entry, on_disk=True, local_timestamp=None) == "unknown"


def _make_feed(tmp) -> "db.TrackedFeed":
    db_path = os.path.join(tmp, "app.db")
    db.init_db(db_path)
    db.add_tracked_feed(db_path, "https://example.com/feed.atom", "Label")
    feed = db.list_tracked_feeds(db_path)[0]
    return db_path, feed


def test_refresh_feed_cache_saves_entries_and_title():
    with tempfile.TemporaryDirectory() as tmp:
        db_path, feed = _make_feed(tmp)
        fake_result = FeedResult(title="Fetched Title", entries=[FeedEntry(work_id="1", title="Work One")])

        with patch("app.rss.fetch_feed", return_value=fake_result):
            result = refresh_feed_cache(db_path, feed)

        assert result is fake_result
        cached = load_cached_feed(db_path, db.list_tracked_feeds(db_path)[0])
        assert cached.title == "Fetched Title"
        assert [e.work_id for e in cached.entries] == ["1"]


def test_load_cached_feed_empty_before_any_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        db_path, feed = _make_feed(tmp)
        result = load_cached_feed(db_path, feed)
    assert result.entries == []
    assert result.title == feed.title  # None -- hasn't been fetched yet


def test_failed_refresh_leaves_previous_cache_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        db_path, feed = _make_feed(tmp)
        good_result = FeedResult(title="Good Title", entries=[FeedEntry(work_id="1")])
        with patch("app.rss.fetch_feed", return_value=good_result):
            refresh_feed_cache(db_path, feed)

        with patch("app.rss.fetch_feed", side_effect=FeedFetchError("network down")):
            try:
                refresh_feed_cache(db_path, feed)
            except FeedFetchError:
                pass

        cached = load_cached_feed(db_path, db.list_tracked_feeds(db_path)[0])
        assert cached.title == "Good Title"
        assert [e.work_id for e in cached.entries] == ["1"]
