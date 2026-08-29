"""Small local SQLite store for the only persistent state this app has:
per-work manual overrides (title/author), issue dismissals, a global tag ->
is-fandom classification, and the list of AO3 feed URLs the user wants
tracked on the Tracked Feeds page.

Fandom is classified per *tag*, not per work: correcting one tag (e.g.
marking "Torchwood" as a fandom) retroactively fixes every work that has
that tag, instead of requiring a correction on each of what could be
thousands of individual works. See scanner._resolve_fandoms.

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
    dismissed: bool = False


@dataclass
class TrackedFeed:
    id: int
    url: str
    label: str | None = None
    title: str | None = None


WORKS_CACHE_COLUMNS = [
    "work_id", "title", "author", "rating", "warnings", "categories",
    "relationships", "fandoms", "fandom_candidates", "series", "series_index",
    "published_date", "file_path", "size_bytes", "mtime", "on_disk",
    "log_success", "log_timestamp", "parse_error", "issue_type",
]

FEED_ENTRY_COLUMNS = [
    "feed_id", "work_id", "title", "author", "chapters_have",
    "chapters_total", "feed_updated",
]


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
                label TEXT,
                title TEXT
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS works_cache (
                {", ".join(f"{c} TEXT" if c not in ("size_bytes", "on_disk", "log_success") else f"{c} INTEGER" for c in WORKS_CACHE_COLUMNS)},
                PRIMARY KEY (work_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_entries (
                feed_id INTEGER NOT NULL,
                work_id TEXT NOT NULL,
                title TEXT,
                author TEXT,
                chapters_have INTEGER,
                chapters_total INTEGER,
                feed_updated TEXT,
                PRIMARY KEY (feed_id, work_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_flags (
                tag TEXT PRIMARY KEY,
                is_fandom INTEGER NOT NULL
            )
            """
        )
        _ensure_column(conn, "works_cache", "fandom_candidates")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str = "TEXT") -> None:
    """Adds a column to an existing table if it's missing, for upgrading a
    database created by an older version of the app (CREATE TABLE IF NOT
    EXISTS is a no-op once the table already exists, so new columns added
    to WORKS_CACHE_COLUMNS etc. need this to reach existing installs).
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


@contextmanager
def _connect(path: str):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_override(row) -> Override:
    work_id, title, author, dismissed = row
    return Override(work_id=work_id, title=title, author=author, dismissed=bool(dismissed))


def get_all_overrides(path: str) -> dict[str, Override]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT work_id, title, author, dismissed FROM overrides").fetchall()
    return {row[0]: _row_to_override(row) for row in rows}


def get_override(path: str, work_id: str) -> Override | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT work_id, title, author, dismissed FROM overrides WHERE work_id = ?",
            (work_id,),
        ).fetchone()
    return _row_to_override(row) if row else None


def set_title_author(path: str, work_id: str, title: str | None, author: str | None) -> None:
    """Updates only title/author, leaving the dismissed flag untouched."""
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO overrides (work_id, title, author)
            VALUES (?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET title = excluded.title, author = excluded.author
            """,
            (work_id, title, author),
        )


def get_all_tag_flags(path: str) -> dict[str, bool]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, is_fandom FROM tag_flags").fetchall()
    return {row[0]: bool(row[1]) for row in rows}


def set_tag_flags(path: str, flags: dict[str, bool]) -> None:
    """Bulk-sets tag -> is-fandom classifications. Explicitly classifying a
    tag applies everywhere that tag appears, across every work -- this is
    the mechanism for correcting fandom at scale instead of per work.
    """
    with _connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO tag_flags (tag, is_fandom) VALUES (?, ?)
            ON CONFLICT(tag) DO UPDATE SET is_fandom = excluded.is_fandom
            """,
            [(tag, int(is_fandom)) for tag, is_fandom in flags.items()],
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
        rows = conn.execute("SELECT id, url, label, title FROM tracked_feeds ORDER BY id").fetchall()
    return [TrackedFeed(id=row[0], url=row[1], label=row[2], title=row[3]) for row in rows]


def delete_tracked_feed(path: str, feed_id: int) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM tracked_feeds WHERE id = ?", (feed_id,))
        conn.execute("DELETE FROM feed_entries WHERE feed_id = ?", (feed_id,))


def set_tracked_feed_title(path: str, feed_id: int, title: str | None) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE tracked_feeds SET title = ? WHERE id = ?", (title, feed_id))


def save_works_cache(path: str, rows: list[dict]) -> None:
    cols = WORKS_CACHE_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    with _connect(path) as conn:
        conn.execute("DELETE FROM works_cache")
        conn.executemany(
            f"INSERT INTO works_cache ({', '.join(cols)}) VALUES ({placeholders})",
            [tuple(row[c] for c in cols) for row in rows],
        )


def load_works_cache(path: str) -> list[dict]:
    cols = WORKS_CACHE_COLUMNS
    with _connect(path) as conn:
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM works_cache").fetchall()
    return [dict(zip(cols, row)) for row in rows]


def save_feed_entries(path: str, feed_id: int, rows: list[dict]) -> None:
    cols = FEED_ENTRY_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    with _connect(path) as conn:
        conn.execute("DELETE FROM feed_entries WHERE feed_id = ?", (feed_id,))
        conn.executemany(
            f"INSERT INTO feed_entries ({', '.join(cols)}) VALUES ({placeholders})",
            [tuple(row[c] for c in cols) for row in rows],
        )


def load_feed_entries(path: str, feed_id: int) -> list[dict]:
    cols = FEED_ENTRY_COLUMNS
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM feed_entries WHERE feed_id = ?", (feed_id,)
        ).fetchall()
    return [dict(zip(cols, row)) for row in rows]


def get_meta(path: str, key: str) -> str | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(path: str, key: str, value: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
