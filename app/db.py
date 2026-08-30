"""Small local SQLite store for the only persistent state this app keeps
itself: per-work manual overrides (title/author), issue dismissals, a
global tag -> category classification, the works_cache snapshot, and a
key/value meta table (currently just last-refreshed-at).

Tracked feeds themselves are no longer stored here -- see app/rss.py,
which uses the `reader` library and its own separate SQLite file for that
(feed URLs, per-feed auto-refresh flag, and entries all live there now).

Fandom/Character/Freeform is classified per *tag*, not per work: correcting
one tag (e.g. marking "Torchwood" as a fandom) retroactively fixes every
work that has that tag, instead of requiring a correction on each of what
could be thousands of individual works. See scanner._resolve_tag_categories.

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


WORKS_CACHE_COLUMNS = [
    "work_id", "title", "author", "rating", "warnings", "categories",
    "relationships", "fandoms", "fandom_candidates", "series", "series_index",
    "published_date", "language", "summary", "word_count", "chapters_have",
    "chapters_total", "file_path", "size_bytes", "mtime", "on_disk",
    "log_success", "log_timestamp", "parse_error", "issue_type",
]
_WORKS_CACHE_INTEGER_COLUMNS = ("size_bytes", "on_disk", "log_success", "word_count", "chapters_have", "chapters_total")


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
            f"""
            CREATE TABLE IF NOT EXISTS works_cache (
                {", ".join(f"{c} TEXT" if c not in _WORKS_CACHE_INTEGER_COLUMNS else f"{c} INTEGER" for c in WORKS_CACHE_COLUMNS)},
                PRIMARY KEY (work_id)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS abs_matches (
                work_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "works_cache", "fandom_candidates")
        _ensure_column(conn, "works_cache", "summary")
        _ensure_column(conn, "works_cache", "language")
        _ensure_column(conn, "works_cache", "word_count", "INTEGER")
        _ensure_column(conn, "works_cache", "chapters_have", "INTEGER")
        _ensure_column(conn, "works_cache", "chapters_total", "INTEGER")
        _ensure_column(conn, "tag_flags", "category")
        # One-time, idempotent: is_fandom used to be the only signal. Existing
        # True rows become an explicit 'fandom' category; existing False rows
        # become 'freeform' (not "unclassified" -- the user already looked at
        # these and said "not a fandom", same bucket every other leftover tag
        # already fell into before categories existed). Tags with no row at
        # all (never explicitly touched) are left alone -- they're genuinely
        # unclassified, which is what the new Tags page filter surfaces.
        conn.execute(
            """
            UPDATE tag_flags SET category = CASE WHEN is_fandom = 1 THEN 'fandom' ELSE 'freeform' END
            WHERE category IS NULL
            """
        )


def pop_legacy_tracked_feeds(path: str) -> list[tuple[str, str | None]]:
    """One-time migration: the app used to store tracked feeds in this
    same database (tracked_feeds/feed_entries tables) before switching to
    `reader`'s own storage. Reads and drops those old tables if present,
    returning (url, label) pairs the caller should re-add via app.rss --
    otherwise a feed added before this migration would silently vanish.
    No-op (returns []) once already migrated, since the tables are gone.
    """
    with _connect(path) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tracked_feeds'"
        ).fetchone()
        if not exists:
            return []
        rows = conn.execute("SELECT url, label FROM tracked_feeds").fetchall()
        conn.execute("DROP TABLE tracked_feeds")
        conn.execute("DROP TABLE IF EXISTS feed_entries")
    return [(row[0], row[1]) for row in rows]


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


def get_all_tag_categories(path: str) -> dict[str, str]:
    """tag -> 'fandom' | 'character' | 'freeform'. A tag with no row at all
    is simply absent from the dict -- it's unclassified, see
    scanner._resolve_tag_categories for how that falls back.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, category FROM tag_flags WHERE category IS NOT NULL").fetchall()
    return dict(rows)


def set_tag_categories(path: str, categories: dict[str, str]) -> None:
    """Bulk-sets tag -> category ('fandom'/'character'/'freeform'). Explicitly
    classifying a tag applies everywhere that tag appears, across every work
    -- this is the mechanism for correcting classification at scale instead
    of per work. is_fandom is kept in sync purely because the column is
    still NOT NULL; nothing reads it anymore.
    """
    with _connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO tag_flags (tag, is_fandom, category) VALUES (?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET is_fandom = excluded.is_fandom, category = excluded.category
            """,
            [(tag, int(category == "fandom"), category) for tag, category in categories.items()],
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


def save_abs_matches(path: str, matches: dict[str, str]) -> None:
    """Replaces the whole work_id -> Audiobookshelf item_id snapshot, same
    replace-all-on-refresh approach as save_works_cache.
    """
    with _connect(path) as conn:
        conn.execute("DELETE FROM abs_matches")
        conn.executemany(
            "INSERT INTO abs_matches (work_id, item_id) VALUES (?, ?)",
            list(matches.items()),
        )


def get_all_abs_matches(path: str) -> dict[str, str]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT work_id, item_id FROM abs_matches").fetchall()
    return dict(rows)


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
