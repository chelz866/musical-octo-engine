"""Small local SQLite store for the only persistent state this app has:
per-work manual overrides (title/author/fandoms), issue dismissals, and the
list of AO3 feed URLs the user wants tracked on the Tracked Feeds page.

Everything else is computed live from the filesystem/log/feed on each
request -- this is deliberately the minimum needed to make those pages useful.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class Override:
    work_id: str
    title: str | None = None
    author: str | None = None
    fandoms: list[str] | None = None
    dismissed: bool = False


@dataclass
class TrackedFeed:
    id: int
    url: str
    label: str | None = None


def init_db(path: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS overrides (
                work_id TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                fandoms TEXT,
                dismissed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                label TEXT
            )
            """
        )


@contextmanager
def _connect(path: str):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_override(row) -> Override:
    work_id, title, author, fandoms, dismissed = row
    return Override(
        work_id=work_id,
        title=title,
        author=author,
        fandoms=[f for f in fandoms.split("\x1f") if f] if fandoms else None,
        dismissed=bool(dismissed),
    )


def get_all_overrides(path: str) -> dict[str, Override]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT work_id, title, author, fandoms, dismissed FROM overrides"
        ).fetchall()
    return {row[0]: _row_to_override(row) for row in rows}


def get_override(path: str, work_id: str) -> Override | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT work_id, title, author, fandoms, dismissed FROM overrides WHERE work_id = ?",
            (work_id,),
        ).fetchone()
    return _row_to_override(row) if row else None


def set_fields(path: str, work_id: str, title: str | None, author: str | None, fandoms: list[str] | None) -> None:
    fandoms_str = "\x1f".join(fandoms) if fandoms else None
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO overrides (work_id, title, author, fandoms, dismissed)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(work_id) DO UPDATE SET title = excluded.title,
                author = excluded.author, fandoms = excluded.fandoms
            """,
            (work_id, title, author, fandoms_str),
        )


def set_dismissed(path: str, work_id: str, dismissed: bool) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO overrides (work_id, dismissed)
            VALUES (?, ?)
            ON CONFLICT(work_id) DO UPDATE SET dismissed = excluded.dismissed
            """,
            (work_id, int(dismissed)),
        )


def add_tracked_feed(path: str, url: str, label: str | None) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tracked_feeds (url, label) VALUES (?, ?)",
            (url, label or None),
        )


def list_tracked_feeds(path: str) -> list[TrackedFeed]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT id, url, label FROM tracked_feeds ORDER BY id").fetchall()
    return [TrackedFeed(id=row[0], url=row[1], label=row[2]) for row in rows]


def delete_tracked_feed(path: str, feed_id: int) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM tracked_feeds WHERE id = ?", (feed_id,))
