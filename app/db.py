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

tag_wranglings is AO3-style tag wrangling, also global, and now split into
two genuinely different mechanisms rather than one generic graph:

- 'synonym' rows merge one tag's spelling into a canonical tag everywhere
  (display, counts, classification) -- category-blind, since two spellings
  of "the same tag" aren't a category question.
- 'child' rows are a same-*category*-only parent/child hierarchy (a
  Fandom's parent is always a Fandom, a Character's a Character, etc --
  enforced by the caller in main.py, since checking a tag's effective
  category needs the scanned library, not just this table). Chains are
  allowed within one category (e.g. Freeform -> Freeform -> Freeform),
  cycle-checked by set_tag_wrangling.

Fandom/Character/Relationship association is a *separate* concept from
the same-category hierarchy above, matching how real AO3 wrangling
splits "Parent Tag" (same-type) from a tag's "Fandom" (cross-type):

- tag_fandoms: every Character/Relationship/Freeform tag has at most one
  Fandom association (or the explicit sentinel "No Fandom"), inherited
  down its own same-category 'child' chain when a tag itself has no
  explicit row -- see scanner._resolve_tag_fandom. Fandom tags don't get
  a row here; they don't have a Fandom of their own.
- relationship_characters: each of a Relationship's "/"-or-"&"-separated
  name parts (part_index, in split order) maps to one Character tag --
  the character's own spelling can differ from the literal substring.
- freeform_characters / freeform_relationships: a Freeform tag can be
  associated with any number of Characters and/or Relationships, with no
  slot structure (unlike a Relationship's fixed per-part Characters).

Users/sessions/bookmarks are the one place this file departs from "global,
shared data": bookmarks are scoped per user_id, everything else in this
module stays shared across every account, same as before login existed.

catalog_works holds metadata for works this app has never scanned a file
for -- imported in bulk from an external export (see app/catalog_import.py)
rather than produced by this app's own scanning. scanner.py folds these in
as synthetic on_disk=False entries alongside real scanned/logged works, so
they flow through the exact same classification/filtering pipeline.

Everything else is computed live from the filesystem/log/feed on each
request -- this is deliberately the minimum needed to make those pages useful.
"""

import sqlite3
from collections import defaultdict
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
    "log_success", "log_timestamp", "log_error", "parse_error", "issue_type",
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
            CREATE TABLE IF NOT EXISTS tag_wranglings (
                tag TEXT PRIMARY KEY,
                relation TEXT NOT NULL CHECK (relation IN ('synonym', 'child')),
                target TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS catalog_works (
                work_id TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                rating TEXT,
                warnings TEXT,
                categories TEXT,
                fandoms TEXT,
                relationships TEXT,
                freeform TEXT,
                language TEXT,
                summary TEXT,
                word_count INTEGER,
                chapters_have INTEGER,
                chapters_total INTEGER,
                published_date TEXT,
                series TEXT,
                story_url TEXT,
                source_path TEXT,
                imported_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS work_tags (
                work_id TEXT NOT NULL,
                category TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (work_id, category, tag)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_descendants (
                ancestor TEXT NOT NULL,
                descendant TEXT NOT NULL,
                PRIMARY KEY (ancestor, descendant)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_fandoms (
                tag TEXT PRIMARY KEY,
                fandom TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_media_types (
                tag TEXT NOT NULL,
                media_type TEXT NOT NULL,
                PRIMARY KEY (tag, media_type)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metatags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                parent_id INTEGER REFERENCES metatags(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metatag_tags (
                metatag_id INTEGER NOT NULL REFERENCES metatags(id),
                tag TEXT NOT NULL,
                PRIMARY KEY (metatag_id, tag)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_verified (
                tag TEXT PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS relationship_characters (
                relationship_tag TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                character_tag TEXT NOT NULL,
                PRIMARY KEY (relationship_tag, part_index)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS freeform_characters (
                freeform_tag TEXT NOT NULL,
                character_tag TEXT NOT NULL,
                PRIMARY KEY (freeform_tag, character_tag)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS freeform_relationships (
                freeform_tag TEXT NOT NULL,
                relationship_tag TEXT NOT NULL,
                PRIMARY KEY (freeform_tag, relationship_tag)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS themes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                css TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                added_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_links (
                work_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS view_history (
                user_id INTEGER NOT NULL REFERENCES users(id),
                work_id TEXT NOT NULL,
                viewed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, work_id)
            )
            """
        )
        # A 'downloading' row means the background worker died mid-item on
        # a previous run of the app (nothing can still be in flight right
        # after a fresh start) -- put it back to pending so it's picked up
        # again instead of sitting stuck forever. Idempotent: a no-op once
        # nothing is actually stuck.
        conn.execute("UPDATE download_queue SET status = 'pending' WHERE status = 'downloading'")
        _ensure_column(conn, "works_cache", "fandom_candidates")
        _ensure_column(conn, "works_cache", "summary")
        _ensure_column(conn, "works_cache", "language")
        _ensure_column(conn, "works_cache", "word_count", "INTEGER")
        _ensure_column(conn, "works_cache", "chapters_have", "INTEGER")
        _ensure_column(conn, "works_cache", "chapters_total", "INTEGER")
        _ensure_column(conn, "works_cache", "log_error")
        _ensure_column(conn, "tag_flags", "category")
        _ensure_column(conn, "users", "theme_css")
        _ensure_column(conn, "users", "abs_username")
        _ensure_column(conn, "users", "active_theme_id", "INTEGER")
        _ensure_column(conn, "users", "home_edit_source", "INTEGER")
        _ensure_column(conn, "users", "timezone")
        _ensure_column(conn, "bookmarks", "note")
        # One-time, idempotent: users.theme_css used to hold a single unnamed
        # theme per account. Existing values become a named theme ("My
        # Theme") in the new themes table and are set active, so nobody's
        # current look changes; theme_css is cleared in the same pass so
        # this can't re-fire after someone later switches back to no active
        # theme (which also leaves active_theme_id NULL).
        legacy_themes = conn.execute(
            "SELECT id, theme_css FROM users WHERE theme_css IS NOT NULL AND active_theme_id IS NULL"
        ).fetchall()
        for user_id, css in legacy_themes:
            cursor = conn.execute(
                "INSERT INTO themes (user_id, name, css, created_at) VALUES (?, 'My Theme', ?, datetime('now'))",
                (user_id, css),
            )
            conn.execute(
                "UPDATE users SET active_theme_id = ?, theme_css = NULL WHERE id = ?",
                (cursor.lastrowid, user_id),
            )
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
        # One-time, idempotent: tag_media_types used to allow only one media
        # type per Fandom tag (tag TEXT PRIMARY KEY). A Fandom can genuinely
        # belong to more than one AO3-style category, so the table is now
        # keyed on (tag, media_type) instead -- detected by checking whether
        # media_type is still part of the primary key (pk=0 means it isn't,
        # i.e. this is still the old single-value table); existing rows
        # carry over unchanged; already-migrated installs (or a fresh one,
        # created with the new schema above) see media_type already in the
        # primary key and skip this entirely.
        media_type_cols = {row[1]: row[5] for row in conn.execute("PRAGMA table_info(tag_media_types)")}
        if media_type_cols.get("media_type") == 0:
            old_rows = conn.execute("SELECT tag, media_type FROM tag_media_types").fetchall()
            conn.execute("ALTER TABLE tag_media_types RENAME TO tag_media_types_old")
            conn.execute(
                """
                CREATE TABLE tag_media_types (
                    tag TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    PRIMARY KEY (tag, media_type)
                )
                """
            )
            conn.executemany("INSERT INTO tag_media_types (tag, media_type) VALUES (?, ?)", old_rows)
            conn.execute("DROP TABLE tag_media_types_old")
        # Rebuilt unconditionally on every startup, not just when empty --
        # cheap at this app's scale and self-healing if tag_descendants
        # ever falls out of sync with tag_wranglings (e.g. an install
        # upgrading from before this table existed, where 'child' edges
        # were already there but never flattened).
        _rebuild_tag_descendants(conn)


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


def remove_tag_categories(path: str, tags: list[str]) -> None:
    """Reverts each of `tags` back to Unclassified -- the bulk "Unclassify"
    action on Classify Tags. tag_flags has no other data worth keeping once
    its category is gone (see set_tag_categories), so this deletes the row
    outright rather than setting category back to NULL in place; any
    Fandom/Media Type/Character association the tag already had is left
    alone, same as switching a tag from one category to another already does.
    """
    if not tags:
        return
    with _connect(path) as conn:
        conn.executemany("DELETE FROM tag_flags WHERE tag = ?", [(tag,) for tag in tags])


def get_tag_synonyms(path: str) -> dict[str, str]:
    """tag -> canonical target, for relation='synonym' rows only. See
    scanner._resolve_tag_categories, which folds a synonym's tag into its
    target's name (and, since classification is looked up by name,
    effectively its category too) at read time, before a work's tags are
    ever counted or classified -- the merge is never baked into the
    on-disk works_cache, so retargeting a synonym later takes effect on
    the next page load, the same way correcting a tag's category already
    does today.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, target FROM tag_wranglings WHERE relation = 'synonym'").fetchall()
    return dict(rows)


def _rebuild_tag_descendants(conn) -> None:
    """Recomputes tag_descendants from scratch: every (ancestor, descendant)
    pair reachable by following 'child' wrangling edges downward, at any
    depth -- not just direct parent/child. Called after every
    tag_wranglings write (set_tag_wrangling/remove_tag_wrangling) and once
    at startup (init_db), so get_all_tag_descendants never has to walk the
    edge graph itself. A full rebuild rather than an incremental diff is
    fine here -- wrangling edits are rare admin actions, not per-request
    work, so even a from-scratch pass over the whole table is cheap.
    """
    rows = conn.execute("SELECT tag, target FROM tag_wranglings WHERE relation = 'child'").fetchall()
    children: dict[str, set[str]] = defaultdict(set)
    for tag, target in rows:
        children[target].add(tag)

    pairs = []
    for ancestor in children:
        descendants: set[str] = set()
        stack = list(children[ancestor])
        while stack:
            node = stack.pop()
            if node in descendants:
                continue
            descendants.add(node)
            stack.extend(children.get(node, ()))
        pairs.extend((ancestor, descendant) for descendant in descendants)

    conn.execute("DELETE FROM tag_descendants")
    conn.executemany("INSERT INTO tag_descendants (ancestor, descendant) VALUES (?, ?)", pairs)


def get_all_tag_descendants(path: str) -> dict[str, set[str]]:
    """ancestor -> every descendant tag at any depth, precomputed by
    _rebuild_tag_descendants whenever tag_wranglings changes. This is the
    transitive closure of get_tag_children's direct edges -- read this
    instead of walking get_tag_children yourself when what you need is
    "does selecting X also match Y, however many wrangling hops apart."
    """
    descendants: dict[str, set[str]] = defaultdict(set)
    with _connect(path) as conn:
        rows = conn.execute("SELECT ancestor, descendant FROM tag_descendants").fetchall()
    for ancestor, descendant in rows:
        descendants[ancestor].add(descendant)
    return dict(descendants)


def get_tag_children(path: str) -> dict[str, set[str]]:
    """parent tag -> set of its *direct* child tags only (relation='child'
    rows only) -- one edge per row, not the transitive closure. Since
    'child' rows are same-category-only (enforced by the caller in
    main.py before it ever calls set_tag_wrangling), this is a
    same-category hierarchy: used for nesting on the Tags/Fandoms pages
    and for Downloads filter expansion within one category (selecting a
    parent also matches its descendants). scanner._resolve_tag_fandom
    walks this same edge list for a different purpose -- finding a
    Character/Relationship/Freeform tag's nearest explicit Fandom
    association up its own same-category chain.
    """
    children: dict[str, set[str]] = defaultdict(set)
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, target FROM tag_wranglings WHERE relation = 'child'").fetchall()
    for tag, target in rows:
        children[target].add(tag)
    return dict(children)


def get_all_tag_wranglings(path: str) -> dict[str, tuple[str, str]]:
    """tag -> (relation, target) for every wrangled tag -- for the Classify
    Tags admin page to show current state and offer to undo it.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, relation, target FROM tag_wranglings").fetchall()
    return {tag: (relation, target) for tag, relation, target in rows}


def set_tag_wrangling(path: str, tag: str, relation: str, target: str) -> None:
    """Points `tag` at `target` as either a 'synonym' (merges tag into
    target's canonical name and category everywhere -- display, counts,
    classification) or a 'child' (a same-category parent/child hierarchy
    edge -- tag keeps its own identity, but filtering by target also
    matches works tagged with `tag`). This function doesn't itself check
    that tag/target share a category for 'child' -- main.py's
    wrangle_tags route does that using the scanned library before ever
    calling this, since this module has no access to resolved categories.

    Chains are allowed and expected -- `target` can itself already be
    wrangled to something else, and other tags can already point at
    `tag`, forming a real multi-level hierarchy (e.g. a Freeform tag
    several levels under another Freeform tag). The only thing refused is
    a cycle: wrangling `tag` into `target` when `target`'s own chain of
    targets eventually leads back to `tag`, which would make every
    read-time chain-walk loop forever.
    """
    if tag == target:
        raise ValueError("a tag can't be wrangled into itself")
    with _connect(path) as conn:
        seen = {tag}
        current = target
        while current is not None:
            if current in seen:
                raise ValueError(f"wrangling {tag!r} into {target!r} would create a cycle")
            seen.add(current)
            row = conn.execute("SELECT target FROM tag_wranglings WHERE tag = ?", (current,)).fetchone()
            current = row[0] if row else None
        conn.execute(
            """
            INSERT INTO tag_wranglings (tag, relation, target) VALUES (?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET relation = excluded.relation, target = excluded.target
            """,
            (tag, relation, target),
        )
        _rebuild_tag_descendants(conn)


def remove_tag_wrangling(path: str, tag: str) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM tag_wranglings WHERE tag = ?", (tag,))
        _rebuild_tag_descendants(conn)


def get_all_tag_fandoms(path: str) -> dict[str, str]:
    """tag -> its explicit Fandom association ('No Fandom' or a real
    fandom tag name). A tag absent from this dict has never had one set
    explicitly -- see scanner._resolve_tag_fandom, which then walks the
    tag's own same-category 'child' chain (get_tag_children) looking for
    the nearest ancestor that does, defaulting to 'No Fandom' if the whole
    chain comes up empty.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, fandom FROM tag_fandoms").fetchall()
    return dict(rows)


def set_tag_fandom(path: str, tag: str, fandom: str) -> None:
    """Sets tag's own explicit Fandom association -- 'No Fandom' is a
    real, terminal choice here (it stops inheritance from an ancestor
    just as much as a real fandom name would), not the same as never
    having set anything at all.
    """
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO tag_fandoms (tag, fandom) VALUES (?, ?)
            ON CONFLICT(tag) DO UPDATE SET fandom = excluded.fandom
            """,
            (tag, fandom),
        )


def remove_tag_fandom(path: str, tag: str) -> None:
    """Clears tag's own explicit Fandom association, reverting it to
    whatever it would inherit from its same-category parent chain (or
    'No Fandom' if nothing in the chain has one set either).
    """
    with _connect(path) as conn:
        conn.execute("DELETE FROM tag_fandoms WHERE tag = ?", (tag,))


def get_all_tag_media_types(path: str) -> dict[str, set[str]]:
    """tag -> its explicit AO3-style media types (one or more of, e.g.
    {'TV Shows', 'Anime & Manga'} for a crossover-friendly fandom, or
    {'Uncategorized Fandoms'} as a real, terminal choice) -- only
    meaningful for a Fandom-category tag, though nothing here enforces
    that (the Classify Tags UI only ever offers this control for one). A
    tag absent from this dict has never had one set explicitly -- see
    scanner.resolve_tag_media_type_explicit, which then walks the tag's
    own same-category 'child' chain (get_tag_children) looking for the
    nearest ancestor that does, defaulting to {'Uncategorized Fandoms'} if
    the whole chain comes up empty -- same inheritance shape as
    get_all_tag_fandoms/resolve_tag_fandom_explicit, just a set of values
    at each link instead of one.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag, media_type FROM tag_media_types").fetchall()
    result: dict[str, set[str]] = defaultdict(set)
    for tag, media_type in rows:
        result[tag].add(media_type)
    return dict(result)


def set_tag_media_types(path: str, tag: str, media_types: set[str]) -> None:
    """Replaces tag's own explicit media types wholesale with exactly
    `media_types` -- the per-row checkbox group on Classify Tags submits
    its whole checked set every time, so this is a full replace rather
    than an add/remove pair. An empty set clears tag's own explicit
    choice entirely, reverting it to whatever it would inherit from its
    same-category parent chain (or {'Uncategorized Fandoms'} if nothing
    in the chain has one set either) -- same as the old remove_tag_media_type.
    Passing {'Uncategorized Fandoms'} explicitly is still a real, terminal
    choice that stops inheritance, not the same as clearing it.
    """
    with _connect(path) as conn:
        conn.execute("DELETE FROM tag_media_types WHERE tag = ?", (tag,))
        if media_types:
            conn.executemany(
                "INSERT INTO tag_media_types (tag, media_type) VALUES (?, ?)",
                [(tag, media_type) for media_type in media_types],
            )


def get_all_verified_tags(path: str) -> set[str]:
    """Every tag someone has manually reviewed and confirmed correct on
    Classify Tags -- purely a personal checklist over classification
    that's otherwise already saved (Fandom/Character/Relationship/
    Freeform, its associations, its Fandom Category); nothing else in the
    app reads this, and re-classifying a tag doesn't clear it, since
    "verified" tracks whether *someone looked*, not what the answer was.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT tag FROM tag_verified").fetchall()
    return {row[0] for row in rows}


def set_tag_verified(path: str, tag: str, verified: bool) -> None:
    with _connect(path) as conn:
        if verified:
            conn.execute("INSERT INTO tag_verified (tag) VALUES (?) ON CONFLICT(tag) DO NOTHING", (tag,))
        else:
            conn.execute("DELETE FROM tag_verified WHERE tag = ?", (tag,))


def create_metatag(path: str, name: str, parent_id: int | None) -> int:
    """Creates a new metatag node and returns its id. Raises ValueError on
    a duplicate name -- metatags are names the user invents from scratch
    (unlike every other tag-shaped table here, which is keyed on a real
    tag's own text), so there's no epub-derived spelling to fall back on
    if two collide. No cycle check is needed here (contrast
    set_tag_wrangling's): with no re-parent operation in this version,
    parent_id can only ever point at a node that already existed before
    this one, so a cycle is structurally impossible.
    """
    with _connect(path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO metatags (name, parent_id) VALUES (?, ?)", (name, parent_id)
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"a metatag named {name!r} already exists")
        return cursor.lastrowid


def delete_metatag(path: str, metatag_id: int) -> None:
    """Refuses (ValueError) to delete a metatag that still has children or
    still has any tag directly linked to it -- deleting is only ever safe
    on a true, empty leaf, so a subtree or an association is never
    silently dropped along with it.
    """
    with _connect(path) as conn:
        has_child = conn.execute(
            "SELECT 1 FROM metatags WHERE parent_id = ? LIMIT 1", (metatag_id,)
        ).fetchone()
        if has_child:
            raise ValueError("can't delete a metatag that still has children")
        has_tag = conn.execute(
            "SELECT 1 FROM metatag_tags WHERE metatag_id = ? LIMIT 1", (metatag_id,)
        ).fetchone()
        if has_tag:
            raise ValueError("can't delete a metatag that still has tags linked to it")
        conn.execute("DELETE FROM metatags WHERE id = ?", (metatag_id,))


def get_all_metatags(path: str) -> dict[int, tuple[str, int | None]]:
    """metatag id -> (name, parent_id) for every metatag, parent_id None
    for a top-level one.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT id, name, parent_id FROM metatags").fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def get_all_metatag_tags(path: str) -> dict[int, set[str]]:
    """metatag id -> the tags directly linked to it -- not its
    descendants' own links, see main.py's _tags_for_metatag for the
    aggregated view a metatag's own page actually shows.
    """
    with _connect(path) as conn:
        rows = conn.execute("SELECT metatag_id, tag FROM metatag_tags").fetchall()
    result: dict[int, set[str]] = defaultdict(set)
    for metatag_id, tag in rows:
        result[metatag_id].add(tag)
    return dict(result)


def add_tag_to_metatag(path: str, metatag_id: int, tag: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO metatag_tags (metatag_id, tag) VALUES (?, ?) ON CONFLICT(metatag_id, tag) DO NOTHING",
            (metatag_id, tag),
        )


def remove_tag_from_metatag(path: str, metatag_id: int, tag: str) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM metatag_tags WHERE metatag_id = ? AND tag = ?", (metatag_id, tag))


def get_all_relationship_characters(path: str) -> dict[str, dict[int, str]]:
    """relationship tag -> {part_index: character_tag}, one entry per
    "/"-or-"&"-separated name part that's been explicitly linked to an
    actual Character tag (a part with no row yet just has no linked
    Character -- see main._relationship_name_parts for how the parts
    themselves are derived from the relationship's own tag text).
    """
    result: dict[str, dict[int, str]] = defaultdict(dict)
    with _connect(path) as conn:
        rows = conn.execute("SELECT relationship_tag, part_index, character_tag FROM relationship_characters").fetchall()
    for relationship_tag, part_index, character_tag in rows:
        result[relationship_tag][part_index] = character_tag
    return dict(result)


def set_relationship_character(path: str, relationship_tag: str, part_index: int, character_tag: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO relationship_characters (relationship_tag, part_index, character_tag) VALUES (?, ?, ?)
            ON CONFLICT(relationship_tag, part_index) DO UPDATE SET character_tag = excluded.character_tag
            """,
            (relationship_tag, part_index, character_tag),
        )


def remove_relationship_character(path: str, relationship_tag: str, part_index: int) -> None:
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM relationship_characters WHERE relationship_tag = ? AND part_index = ?",
            (relationship_tag, part_index),
        )


def get_all_freeform_characters(path: str) -> dict[str, set[str]]:
    """freeform tag -> set of Characters associated with it -- unlike a
    Relationship's per-part Characters, this is just an unstructured set,
    since a Freeform tag has no name-parts to match slots against.
    """
    result: dict[str, set[str]] = defaultdict(set)
    with _connect(path) as conn:
        rows = conn.execute("SELECT freeform_tag, character_tag FROM freeform_characters").fetchall()
    for freeform_tag, character_tag in rows:
        result[freeform_tag].add(character_tag)
    return dict(result)


def add_freeform_character(path: str, freeform_tag: str, character_tag: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO freeform_characters (freeform_tag, character_tag) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (freeform_tag, character_tag),
        )


def remove_freeform_character(path: str, freeform_tag: str, character_tag: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM freeform_characters WHERE freeform_tag = ? AND character_tag = ?",
            (freeform_tag, character_tag),
        )


def get_all_freeform_relationships(path: str) -> dict[str, set[str]]:
    """freeform tag -> set of Relationships associated with it, same
    unstructured shape as get_all_freeform_characters.
    """
    result: dict[str, set[str]] = defaultdict(set)
    with _connect(path) as conn:
        rows = conn.execute("SELECT freeform_tag, relationship_tag FROM freeform_relationships").fetchall()
    for freeform_tag, relationship_tag in rows:
        result[freeform_tag].add(relationship_tag)
    return dict(result)


def add_freeform_relationship(path: str, freeform_tag: str, relationship_tag: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO freeform_relationships (freeform_tag, relationship_tag) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (freeform_tag, relationship_tag),
        )


def remove_freeform_relationship(path: str, freeform_tag: str, relationship_tag: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM freeform_relationships WHERE freeform_tag = ? AND relationship_tag = ?",
            (freeform_tag, relationship_tag),
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


def save_work_tags(path: str, rows: list[tuple[str, str, str]]) -> None:
    """Replaces the whole work_tags table with `rows` ((work_id, category,
    tag) triples, category one of 'candidate'/'fandom'/'character'/
    'relationship'/'freeform') -- the precomputed result of
    scanner._resolve_tag_categories/_resolve_associated_fandoms for every
    work, done once by scanner.rebuild_work_tags (and by refresh_cache's
    own scan) instead of live on every scanner.load_cached call. Same
    replace-all-on-write approach as save_works_cache -- cheap at this
    app's scale, and correctness-simple compared to diffing.
    """
    with _connect(path) as conn:
        conn.execute("DELETE FROM work_tags")
        conn.executemany("INSERT INTO work_tags (work_id, category, tag) VALUES (?, ?, ?)", rows)


def load_work_tags(path: str) -> dict[str, dict[str, list[str]]]:
    """work_id -> {category -> [tags]}, as precomputed by save_work_tags.
    A work_id absent here (never scanned, or scanned with no candidate
    tags) simply has no entry -- callers should treat that the same as
    every category being empty.
    """
    result: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    with _connect(path) as conn:
        rows = conn.execute("SELECT work_id, category, tag FROM work_tags").fetchall()
    for work_id, category, tag in rows:
        result[work_id][category].append(tag)
    return {work_id: dict(categories) for work_id, categories in result.items()}


_CATALOG_LIST_COLUMNS = ("warnings", "categories", "fandoms", "relationships", "freeform")
_CATALOG_COLUMNS = [
    "work_id", "title", "author", "rating", *_CATALOG_LIST_COLUMNS, "language", "summary",
    "word_count", "chapters_have", "chapters_total", "published_date", "series", "story_url",
    "source_path", "imported_at",
]


def save_catalog_works(path: str, rows: list[dict]) -> int:
    """Upserts `rows` (each a dict keyed like _CATALOG_COLUMNS, list-valued
    fields as real Python lists) into catalog_works, replacing any existing
    row for the same work_id -- so re-running an import with a fresher
    export just updates in place. See app/catalog_import.py, which builds
    these rows from an external SQLite export; this function only knows
    the storage shape, not where the data came from. Returns len(rows) for
    the caller's own progress bookkeeping.
    """
    if not rows:
        return 0
    encoded = []
    for row in rows:
        encoded.append(tuple(
            "\x1f".join(row[c]) if c in _CATALOG_LIST_COLUMNS else row.get(c)
            for c in _CATALOG_COLUMNS
        ))
    placeholders = ", ".join("?" for _ in _CATALOG_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _CATALOG_COLUMNS if c != "work_id")
    with _connect(path) as conn:
        conn.executemany(
            f"""
            INSERT INTO catalog_works ({', '.join(_CATALOG_COLUMNS)}) VALUES ({placeholders})
            ON CONFLICT(work_id) DO UPDATE SET {updates}
            """,
            encoded,
        )
    return len(rows)


def get_all_catalog_works(path: str) -> dict[str, dict]:
    """work_id -> a dict shaped like a save_catalog_works row, list-valued
    fields decoded back into real lists -- see scanner._catalog_row_to_entry,
    which turns each of these into a synthetic on_disk=False WorkEntry.
    """
    with _connect(path) as conn:
        rows = conn.execute(f"SELECT {', '.join(_CATALOG_COLUMNS)} FROM catalog_works").fetchall()
    result = {}
    for row in rows:
        record = dict(zip(_CATALOG_COLUMNS, row))
        for c in _CATALOG_LIST_COLUMNS:
            record[c] = [v for v in (record[c] or "").split("\x1f") if v]
        result[record["work_id"]] = record
    return result


def count_catalog_works(path: str) -> int:
    with _connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM catalog_works").fetchone()[0]


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
        rows = conn.execute("SELECT id, username, role, timezone FROM users ORDER BY username").fetchall()
    return [User(id=row[0], username=row[1], role=row[2], timezone=row[3]) for row in rows]


def get_user_by_id(path: str, user_id: int) -> User | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT id, username, role, timezone FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(id=row[0], username=row[1], role=row[2], timezone=row[3]) if row else None


def get_user_credentials(path: str, username: str) -> tuple[User, str] | None:
    """Returns (User, password_hash) for login verification -- the only
    place a password hash leaves this module. Every other lookup here
    returns a plain User instead.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, timezone FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return None
    return User(id=row[0], username=row[1], role=row[3], timezone=row[4]), row[2]


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
            SELECT users.id, users.username, users.role, users.timezone
            FROM sessions JOIN users ON sessions.user_id = users.id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
    return User(id=row[0], username=row[1], role=row[2], timezone=row[3]) if row else None


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


def list_user_themes(path: str, user_id: int) -> list[dict]:
    """Every theme this user has saved, most recently created first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, name, css FROM themes WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
    return [{"id": row[0], "name": row[1], "css": row[2]} for row in rows]


def get_user_theme(path: str, user_id: int, theme_id: int) -> dict | None:
    """One theme, scoped to `user_id` so one account can never read or
    edit another's saved theme by guessing its id.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT id, name, css FROM themes WHERE id = ? AND user_id = ?", (theme_id, user_id)
        ).fetchone()
    return {"id": row[0], "name": row[1], "css": row[2]} if row else None


def create_theme(path: str, user_id: int, name: str, css: str, created_at: str) -> int:
    with _connect(path) as conn:
        cursor = conn.execute(
            "INSERT INTO themes (user_id, name, css, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, css, created_at),
        )
        return cursor.lastrowid


def update_theme(path: str, user_id: int, theme_id: int, name: str, css: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE themes SET name = ?, css = ? WHERE id = ? AND user_id = ?",
            (name, css, theme_id, user_id),
        )


def delete_theme(path: str, user_id: int, theme_id: int) -> None:
    """Deletes the theme, and clears it as the active theme if it was --
    otherwise active_theme_id would keep pointing at a row that no longer
    exists.
    """
    with _connect(path) as conn:
        conn.execute("DELETE FROM themes WHERE id = ? AND user_id = ?", (theme_id, user_id))
        conn.execute(
            "UPDATE users SET active_theme_id = NULL WHERE id = ? AND active_theme_id = ?",
            (user_id, theme_id),
        )


def set_active_theme(path: str, user_id: int, theme_id: int | None) -> None:
    """Switches which saved theme (if any) is applied to this user's own
    page loads -- `theme_id=None` means "use the default look." A
    `theme_id` that isn't one of this user's own saved themes is silently
    ignored rather than pointing active_theme_id at someone else's row.
    """
    with _connect(path) as conn:
        if theme_id is None:
            conn.execute("UPDATE users SET active_theme_id = NULL WHERE id = ?", (user_id,))
        else:
            conn.execute(
                """
                UPDATE users SET active_theme_id = ?
                WHERE id = ? AND EXISTS (SELECT 1 FROM themes WHERE id = ? AND user_id = ?)
                """,
                (theme_id, user_id, theme_id, user_id),
            )


def get_active_theme_id(path: str, user_id: int) -> int | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT active_theme_id FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def get_active_theme_css(path: str, user_id: int) -> str | None:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT themes.css FROM users JOIN themes ON themes.id = users.active_theme_id
            WHERE users.id = ?
            """,
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def get_user_abs_username(path: str, user_id: int) -> str | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT abs_username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def set_user_abs_username(path: str, user_id: int, username: str) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE users SET abs_username = ? WHERE id = ?", (username.strip() or None, user_id))


def get_user_home_edit_source(path: str, user_id: int) -> bool:
    """Whether this user has opted into an "Edit" button on each Home
    blurb (see the Admin Dashboard's "Use Home as edit source" checkbox)
    that jumps straight to Classify Tags filtered to just that one work's
    own tags, instead of the whole library. Off by default -- a NULL row
    (never touched, or created before this setting existed) reads as False.
    """
    with _connect(path) as conn:
        row = conn.execute("SELECT home_edit_source FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row[0]) if row else False


def set_user_home_edit_source(path: str, user_id: int, enabled: bool) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE users SET home_edit_source = ? WHERE id = ?", (1 if enabled else 0, user_id))


def set_user_timezone(path: str, user_id: int, tz_name: str | None) -> None:
    """The zone this user's own view of every timestamp in the app is
    converted to for display (see app.main.local_time) -- purely a
    per-account display preference; it never changes what zone the
    underlying recorded times actually are in. None/blank means "no
    conversion, show the server's own recorded time as-is".
    """
    with _connect(path) as conn:
        conn.execute("UPDATE users SET timezone = ? WHERE id = ?", (tz_name or None, user_id))


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


def enqueue_downloads(path: str, items: list[tuple[str, str, str | None]], added_at: str) -> int:
    """Adds (work_id, url, title) rows for the background download worker
    to pick up. A work_id already anywhere in the queue -- pending,
    currently downloading, or already finished -- is left alone instead
    of re-queued, so re-selecting the same Queue rows more than once is a
    harmless no-op rather than a duplicate download. Returns how many were
    actually newly added.
    """
    added = 0
    with _connect(path) as conn:
        for work_id, url, title in items:
            cursor = conn.execute(
                """
                INSERT INTO download_queue (work_id, url, title, status, added_at)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(work_id) DO NOTHING
                """,
                (work_id, url, title, added_at),
            )
            added += cursor.rowcount
    return added


def get_next_pending_download(path: str) -> dict | None:
    """The oldest still-pending item, for the worker to pick up next --
    None once the queue is drained (see main.py's download worker loop,
    which exits when this returns None and restarts on the next enqueue).
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT id, work_id, url, title FROM download_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
    return {"id": row[0], "work_id": row[1], "url": row[2], "title": row[3]} if row else None


def mark_download_status(path: str, item_id: int, status: str, finished_at: str | None = None) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE download_queue SET status = ?, finished_at = ? WHERE id = ?",
            (status, finished_at, item_id),
        )


def get_download_queue_counts(path: str) -> dict[str, int]:
    counts = {"pending": 0, "downloading": 0, "done": 0}
    with _connect(path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM download_queue GROUP BY status").fetchall()
    counts.update(dict(rows))
    return counts


def clear_finished_downloads(path: str) -> None:
    """Drops every 'done' row so the Queue page's count can reset once
    you've confirmed a batch actually landed (via Downloads/Issues) --
    'done' here just means the worker attempted it, not that it
    necessarily succeeded (see app/ao3_client.py's module docstring for
    why per-item success/failure isn't tracked separately here).
    """
    with _connect(path) as conn:
        conn.execute("DELETE FROM download_queue WHERE status = 'done'")


def add_manual_links(path: str, items: list[tuple[str, str]], added_at: str) -> int:
    """Adds (work_id, url) rows to the manually-curated link list -- a
    work_id already present is left alone, so pasting the same link twice
    (or one already tracked through a feed) is a harmless no-op. Returns
    how many were newly added.
    """
    added = 0
    with _connect(path) as conn:
        for work_id, url in items:
            cursor = conn.execute(
                "INSERT INTO manual_links (work_id, url, added_at) VALUES (?, ?, ?) ON CONFLICT(work_id) DO NOTHING",
                (work_id, url, added_at),
            )
            added += cursor.rowcount
    return added


def list_manual_links(path: str) -> list[dict]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT work_id, url, added_at FROM manual_links ORDER BY added_at").fetchall()
    return [{"work_id": r[0], "url": r[1], "added_at": r[2]} for r in rows]


def remove_manual_link(path: str, work_id: str) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM manual_links WHERE work_id = ?", (work_id,))


def record_view(path: str, user_id: int, work_id: str, viewed_at: str) -> None:
    """Upserts one (user, work) row to the given timestamp -- a work only
    ever appears once in a user's History, at whatever time they most
    recently viewed it (see the History page, /go/{work_id}, and the
    reader routes, all of which call this on every view).
    """
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO view_history (user_id, work_id, viewed_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id, work_id) DO UPDATE SET viewed_at = excluded.viewed_at
            """,
            (user_id, work_id, viewed_at),
        )


def get_view_history(path: str, user_id: int) -> dict[str, str]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT work_id, viewed_at FROM view_history WHERE user_id = ?", (user_id,)
        ).fetchall()
    return dict(rows)
