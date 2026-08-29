"""Track AO3 Atom feeds using the `reader` library instead of hand-rolled
fetch/parse/storage.

reader owns its own SQLite file (separate from app.db, see FEEDS_DB_PATH in
main.py) and gives us, for free, exactly the things worth not re-inventing:
entries persist across updates even after they scroll out of AO3's
recent-works window (that's the whole point of "tracking" a feed over
time -- a plain snapshot-per-refresh model, which is what this app used to
do, actively fights that), efficient conditional re-fetching, and a native
per-feed enabled/disabled flag (`updates_enabled`) that IS the auto-refresh
toggle -- `reader.update_feeds()` already only touches enabled feeds by
default, no extra column of our own needed.

AO3-specific parsing (work id, chapter progress) isn't something reader
knows about -- that's still our own regex over each Entry's id/summary,
same as before.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from reader import Entry, Feed, ParseError, make_reader

WORK_ID_RE = re.compile(r"Work/(\d+)")
CHAPTERS_RE = re.compile(r"Chapters:\s*(\d+)\s*/\s*(\d+|\?)")


class FeedRefreshError(Exception):
    pass


@dataclass
class FeedEntry:
    work_id: str
    title: str | None = None
    author: str | None = None
    chapters_have: int | None = None
    chapters_total: int | None = None  # None means the feed showed "?" (author hasn't committed to a total)
    feed_updated: datetime | None = None  # tz-aware, from reader

    @property
    def is_complete(self) -> bool:
        return self.chapters_total is not None and self.chapters_have == self.chapters_total


def add_tracked_feed(feeds_db_path: str, url: str, label: str | None) -> None:
    r = make_reader(feeds_db_path)
    try:
        r.add_feed(url, exist_ok=True)
    except ParseError as exc:
        raise FeedRefreshError(str(exc)) from exc
    if label:
        r.set_feed_user_title(url, label)


def delete_tracked_feed(feeds_db_path: str, url: str) -> None:
    make_reader(feeds_db_path).delete_feed(url, missing_ok=True)


def set_feed_auto_refresh(feeds_db_path: str, url: str, enabled: bool) -> None:
    r = make_reader(feeds_db_path)
    if enabled:
        r.enable_feed_updates(url)
    else:
        r.disable_feed_updates(url)


def list_tracked_feeds(feeds_db_path: str) -> list[Feed]:
    return list(make_reader(feeds_db_path).get_feeds())


def _to_feed_entry(entry: Entry) -> FeedEntry | None:
    match = WORK_ID_RE.search(entry.id)
    if not match:
        return None

    chapters_have = chapters_total = None
    if entry.summary:
        chapter_match = CHAPTERS_RE.search(entry.summary)
        if chapter_match:
            chapters_have = int(chapter_match.group(1))
            chapters_total = None if chapter_match.group(2) == "?" else int(chapter_match.group(2))

    return FeedEntry(
        work_id=match.group(1),
        title=entry.title,
        author=entry.authors[0].name if entry.authors else None,
        chapters_have=chapters_have,
        chapters_total=chapters_total,
        feed_updated=entry.updated,
    )


def get_feed_entries(feeds_db_path: str, feed_url: str) -> list[FeedEntry]:
    r = make_reader(feeds_db_path)
    entries = (_to_feed_entry(e) for e in r.get_entries(feed=feed_url))
    return [e for e in entries if e is not None]


def refresh_feed(feeds_db_path: str, url: str) -> None:
    """Force-refreshes one feed regardless of its auto-refresh setting."""
    try:
        make_reader(feeds_db_path).update_feed(url)
    except ParseError as exc:
        raise FeedRefreshError(str(exc)) from exc


def refresh_all_tracked_feeds(feeds_db_path: str) -> list[str]:
    """Force-refreshes every tracked feed regardless of its auto-refresh
    setting, for the manual Refresh button. Returns an error message per
    feed that failed; a feed with nothing new is not an error.
    """
    r = make_reader(feeds_db_path)
    errors = []
    for feed in r.get_feeds():
        try:
            r.update_feed(feed.url)
        except ParseError as exc:
            errors.append(f"{feed.user_title or feed.title or feed.url}: {exc}")
    return errors


def refresh_auto_feeds(feeds_db_path: str) -> None:
    """Refreshes only feeds with auto-refresh enabled -- used by the
    background poll loop, where there's no user watching to show an
    error to, so failures are silently skipped (same as reader's own
    update_feeds() does internally).
    """
    r = make_reader(feeds_db_path)
    for feed in r.get_feeds(updates_enabled=True):
        try:
            r.update_feed(feed.url)
        except ParseError:
            pass


def assess_status(entry: FeedEntry, on_disk: bool, local_timestamp: datetime | None) -> str:
    """Best-effort only: compares the feed's updated time (tz-aware UTC,
    from reader) against a local filesystem mtime or log timestamp (naive,
    assumed to be roughly the same clock, since ao3downloader doesn't
    record a timezone). Can be off near the boundary if the server's clock
    isn't UTC -- treat as a hint, not proof.
    """
    if not on_disk:
        return "not_downloaded"
    if entry.feed_updated is None or local_timestamp is None:
        return "unknown"
    if local_timestamp >= entry.feed_updated.replace(tzinfo=None):
        return "up_to_date"
    return "may_need_update"
