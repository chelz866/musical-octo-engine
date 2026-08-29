import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

import pytest

from app.rss import (
    FeedEntry,
    FeedRefreshError,
    add_tracked_feed,
    assess_status,
    delete_tracked_feed,
    get_feed_entries,
    list_tracked_feeds,
    refresh_all_tracked_feeds,
    refresh_auto_feeds,
    refresh_feed,
    set_feed_auto_refresh,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_feed.atom")

SMALL_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>AO3 works tagged 'Test'</title>
  <updated>{updated}</updated>
  <entry>
    <id>tag:archiveofourown.org,2005:Work/{work_id}</id>
    <updated>{updated}</updated>
    <link rel="alternate" type="text/html" href="https://archiveofourown.org/works/{work_id}"/>
    <title>{title}</title>
    <summary type="html">&lt;p&gt;Words: 1, Chapters: {chapters}, Language: English&lt;/p&gt;</summary>
    <author><name>NewAuthor</name></author>
  </entry>
</feed>
"""

# reader does not support file:// URLs (no retriever registered for them),
# so exercising real fetch/parse/caching behavior needs an actual HTTP
# round-trip -- these tests spin up a throwaway local server per test, as a
# real subprocess (an in-process socketserver was seen to trip feedparser's
# "bozo"/NonXMLContentType path in a way a plain `http.server` subprocess
# does not; matched here to what was verified working manually).


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def feed_server(tmp_path):
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        (serve_dir / "feed.atom").write_text(f.read(), encoding="utf-8")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(serve_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/feed.atom"
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=0.2)
            break
        except Exception:
            time.sleep(0.1)

    def write_feed(work_id: str, title: str, chapters: str, updated: str | None = None):
        xml = SMALL_FEED.format(
            work_id=work_id,
            title=title,
            chapters=chapters,
            updated=updated or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        (serve_dir / "feed.atom").write_text(xml, encoding="utf-8")

    yield url, write_feed

    proc.terminate()
    proc.wait(timeout=5)


def _feeds_db(tmp_path) -> str:
    return str(tmp_path / "feeds.sqlite")


def test_add_and_list_tracked_feeds(feed_server, tmp_path):
    url, _ = feed_server
    db_path = _feeds_db(tmp_path)
    add_tracked_feed(db_path, url, "My Label")

    feeds = list_tracked_feeds(db_path)
    assert len(feeds) == 1
    assert feeds[0].url == url
    assert feeds[0].user_title == "My Label"


def test_add_tracked_feed_is_idempotent(feed_server, tmp_path):
    url, _ = feed_server
    db_path = _feeds_db(tmp_path)
    add_tracked_feed(db_path, url, "First")
    add_tracked_feed(db_path, url, None)

    assert len(list_tracked_feeds(db_path)) == 1


def test_get_feed_entries_after_refresh_parses_real_sample(feed_server, tmp_path):
    url, _ = feed_server
    db_path = _feeds_db(tmp_path)
    add_tracked_feed(db_path, url, None)
    refresh_feed(db_path, url)

    entries = {e.work_id: e for e in get_feed_entries(db_path, url)}
    assert set(entries) == {"91535741", "91535026", "91534106"}
    assert entries["91535026"].title == "Too Tired"
    assert entries["91535026"].author == "Anonymous"
    assert entries["91535026"].chapters_have == 1
    assert entries["91535026"].chapters_total == 1
    assert entries["91535026"].is_complete is True
    assert entries["91535741"].chapters_total is None  # "1/?" in the real sample


def test_entries_persist_after_scrolling_out_of_the_feed_window(feed_server, tmp_path):
    # The whole reason for switching to reader: a work that ages out of
    # AO3's recent-works window on a later refresh must stay tracked.
    url, write_feed = feed_server
    db_path = _feeds_db(tmp_path)
    add_tracked_feed(db_path, url, None)
    refresh_feed(db_path, url)
    original_ids = {e.work_id for e in get_feed_entries(db_path, url)}
    assert len(original_ids) == 3

    # http.server sends a Last-Modified header (file mtime) and honors
    # If-Modified-Since, and reader replays it on the next fetch -- if the
    # file's mtime doesn't advance to a new whole second before we overwrite
    # it, the server 304s the second request and reader never sees the new
    # content. Sleep before writing, not after, since it's the *file's*
    # mtime that needs to move, not the timing of the request itself.
    time.sleep(1.1)
    write_feed(work_id="55555555", title="Brand New Work", chapters="1/1")
    refresh_feed(db_path, url)

    after_ids = {e.work_id for e in get_feed_entries(db_path, url)}
    assert original_ids <= after_ids
    assert "55555555" in after_ids


def test_refresh_feed_raises_on_broken_url(tmp_path):
    db_path = _feeds_db(tmp_path)
    bad_url = "http://127.0.0.1:1/does-not-exist.atom"  # nothing listens on port 1
    add_tracked_feed(db_path, bad_url, None)

    with pytest.raises(FeedRefreshError):
        refresh_feed(db_path, bad_url)


def test_refresh_all_tracked_feeds_collects_errors_without_raising(feed_server, tmp_path):
    good_url, _ = feed_server
    bad_url = "http://127.0.0.1:1/does-not-exist.atom"
    db_path = _feeds_db(tmp_path)
    add_tracked_feed(db_path, good_url, "Good Feed")
    add_tracked_feed(db_path, bad_url, "Bad Feed")

    errors = refresh_all_tracked_feeds(db_path)

    assert len(errors) == 1
    assert "Bad Feed" in errors[0]
    assert len(get_feed_entries(db_path, good_url)) == 3


def test_disabled_feed_is_skipped_by_refresh_auto_feeds_but_not_by_refresh_all(feed_server, tmp_path):
    url, _ = feed_server
    db_path = _feeds_db(tmp_path)
    add_tracked_feed(db_path, url, None)
    set_feed_auto_refresh(db_path, url, False)

    refresh_auto_feeds(db_path)
    assert get_feed_entries(db_path, url) == []

    refresh_all_tracked_feeds(db_path)
    assert len(get_feed_entries(db_path, url)) == 3


def test_delete_tracked_feed_removes_it(feed_server, tmp_path):
    url, _ = feed_server
    db_path = _feeds_db(tmp_path)
    add_tracked_feed(db_path, url, None)

    delete_tracked_feed(db_path, url)

    assert list_tracked_feeds(db_path) == []


def test_assess_status_not_downloaded():
    entry = FeedEntry(work_id="1", feed_updated=datetime(2026, 8, 28, 19, 28, 16, tzinfo=timezone.utc))
    assert assess_status(entry, on_disk=False, local_timestamp=datetime(2026, 8, 29)) == "not_downloaded"


def test_assess_status_up_to_date():
    entry = FeedEntry(work_id="1", feed_updated=datetime(2026, 8, 28, 19, 28, 16, tzinfo=timezone.utc))
    local = datetime(2026, 8, 29, 0, 0, 0)
    assert assess_status(entry, on_disk=True, local_timestamp=local) == "up_to_date"


def test_assess_status_may_need_update():
    entry = FeedEntry(work_id="1", feed_updated=datetime(2026, 8, 28, 19, 28, 16, tzinfo=timezone.utc))
    local = datetime(2026, 8, 27, 0, 0, 0)
    assert assess_status(entry, on_disk=True, local_timestamp=local) == "may_need_update"


def test_assess_status_unknown_without_timestamps():
    entry = FeedEntry(work_id="1", feed_updated=None)
    assert assess_status(entry, on_disk=True, local_timestamp=None) == "unknown"
