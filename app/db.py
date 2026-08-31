"""Small local SQLite store for the only persistent state this app keeps
itself: per-work manual overrides (title/author), issue dismissals, a
global tag -> category classification, the works_cache snapshot, a
key/value meta table (currently just last-refreshed-at), and now users/
sessions/bookmarks for the login system.

Tracked feeds themselves are no longer stored here -- see app/rss.py,
which uses the `reader` library and its own separate SQLite file for that
(feed URLs, per-feed auto-refresh flag, and entries all live there now).

Fandom/Character/Relationship/Freeform is classified per *tag*, not per
work: correcting one tag (e.g. marking "Torchwood" as a fandom) retroactively
fixes every work that has that tag, instead of requiring a correction on
each of what could be thousands of individual works. See
scanner._resolve_tag_categories.

Users/sessions/bookmarks are the one place this file departs from "global,
shared data": bookmarks are scoped per user_id, everything else in this
module stays shared across every account, same as before login existed.

Everything else is computed live from the filesystem/log/feed on each
request -- this is deliberately the minimum needed to make those pages useful.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from .auth import User


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookmarks (
                user_id INTEGER NOT NULL REFERENCES users(id),
                work_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, work_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS read_marks (
                user_id INTEGER NOT NULL REFERENCES users(id),
                work_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, work_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS abs_read_status (
                user_id INTEGER NOT NULL REFERENCES users(id),
                work_id TEXT NOT NULL,
                finished_at TEXT,
                PRIMARY KEY (user_id, work_id)
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
        _ensure_column(conn, "users", "theme_css")
        _ensure_column(conn, "users", "abs_username")
        _ensure_column(conn, "bookmarks", "note")
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
    """tag -> 'fandom' | 'character' | 'relationship' | 'freeform'. A tag
    with no row at all is simply absent from the dict -- it's unclassified,
    see scanner._resolve_tag_categories for how that falls back.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, category FROM tag_flags WHERE category IS NOT NULL").fetchall()
    return dict(rows)


def set_tag_categories(path: str, categories: dict[str, str]) -> None:
    """Bulk-sets tag -> category ('fandom'/'character'/'relationship'/'freeform').
    Explicitly classifying a tag applies everywhere that tag appears, across
    every work -- this is the mechanism for correcting classification at
    scale instead of per work. is_fandom is kept in sync purely because the
    column is still NOT NULL; nothing reads it anymore.
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


def count_users(path: str) -> int:
    with _connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(path: str, username: str, password_hash: str, role: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, password_hash, role),
        )


def list_users(path: str) -> list[User]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT id, username, role FROM users ORDER BY username").fetchall()
    return [User(id=row[0], username=row[1], role=row[2]) for row in rows]


def get_user_by_id(path: str, user_id: int) -> User | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(id=row[0], username=row[1], role=row[2]) if row else None


def get_user_credentials(path: str, username: str) -> tuple[User, str] | None:
    """Returns (User, password_hash) for login verification -- the only
    place a password hash leaves this module. Every other lookup here
    returns a plain User instead.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return None
    return User(id=row[0], username=row[1], role=row[3]), row[2]


def set_user_password(path: str, user_id: int, password_hash: str) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def create_session(path: str, token: str, user_id: int, created_at: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, created_at),
        )


def get_session_user(path: str, token: str) -> User | None:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT users.id, users.username, users.role
            FROM sessions JOIN users ON sessions.user_id = users.id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
    return User(id=row[0], username=row[1], role=row[2]) if row else None


def delete_session(path: str, token: str) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def add_bookmark(path: str, user_id: int, work_id: str, created_at: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO bookmarks (user_id, work_id, created_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id, work_id) DO NOTHING
            """,
            (user_id, work_id, created_at),
        )


def remove_bookmark(path: str, user_id: int, work_id: str) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM bookmarks WHERE user_id = ? AND work_id = ?", (user_id, work_id))


def get_bookmarked_work_ids(path: str, user_id: int) -> set[str]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT work_id FROM bookmarks WHERE user_id = ?", (user_id,)).fetchall()
    return {row[0] for row in rows}


def set_bookmark_note(path: str, user_id: int, work_id: str, note: str) -> None:
    """No-ops if the work isn't bookmarked (no row to update) -- the note
    editor is only ever shown for a bookmarked work, but this stays safe
    either way rather than assuming the caller got that right.
    """
    with _connect(path) as conn:
        conn.execute(
            "UPDATE bookmarks SET note = ? WHERE user_id = ? AND work_id = ?",
            (note.strip() or None, user_id, work_id),
        )


def get_bookmark_notes(path: str, user_id: int) -> dict[str, str]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT work_id, note FROM bookmarks WHERE user_id = ? AND note IS NOT NULL",
            (user_id,),
        ).fetchall()
    return dict(rows)


def get_user_theme_css(path: str, user_id: int) -> str | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT theme_css FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def set_user_theme_css(path: str, user_id: int, css: str) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE users SET theme_css = ? WHERE id = ?", (css.strip() or None, user_id))


def get_user_abs_username(path: str, user_id: int) -> str | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT abs_username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def set_user_abs_username(path: str, user_id: int, username: str) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE users SET abs_username = ? WHERE id = ?", (username.strip() or None, user_id))


def list_user_abs_usernames(path: str) -> dict[int, str]:
    """user_id -> abs_username, only for users who've actually set one --
    the refresh cycle uses this to know which app users to sync Audiobookshelf
    read status for at all (see audiobookshelf.load_read_work_ids).
    """
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, abs_username FROM users WHERE abs_username IS NOT NULL AND abs_username != ''"
        ).fetchall()
    return dict(rows)


def add_read_mark(path: str, user_id: int, work_id: str, created_at: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO read_marks (user_id, work_id, created_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id, work_id) DO NOTHING
            """,
            (user_id, work_id, created_at),
        )


def remove_read_mark(path: str, user_id: int, work_id: str) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM read_marks WHERE user_id = ? AND work_id = ?", (user_id, work_id))


def get_read_marked_work_ids(path: str, user_id: int) -> set[str]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT work_id FROM read_marks WHERE user_id = ?", (user_id,)).fetchall()
    return {row[0] for row in rows}


def save_abs_read_status(path: str, user_id: int, finished: dict[str, str | None]) -> None:
    """Replaces one user's whole work_id -> finishedAt snapshot (finished_at
    may be None if Audiobookshelf didn't record a timestamp) -- same
    replace-all-on-refresh approach as save_abs_matches, just scoped to a
    single user_id since read status is per-person, unlike the shared
    work_id -> item_id match.
    """
    with _connect(path) as conn:
        conn.execute("DELETE FROM abs_read_status WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO abs_read_status (user_id, work_id, finished_at) VALUES (?, ?, ?)",
            [(user_id, work_id, finished_at) for work_id, finished_at in finished.items()],
        )


def get_abs_read_work_ids(path: str, user_id: int) -> set[str]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT work_id FROM abs_read_status WHERE user_id = ?", (user_id,)).fetchall()
    return {row[0] for row in rows}
