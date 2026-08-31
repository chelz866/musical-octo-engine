"""Matches downloaded AO3 works to Audiobookshelf library items, and -- for
matched works -- uses Audiobookshelf's own already-scanned metadata instead
of re-parsing the local epub file.

Audiobookshelf's `libraryItems.path` for anything ao3downloader put there
carries the same `<work_id> Title - Author.epub` filename ao3downloader
itself uses (confirmed against a real export of the table), so matching
reuses the same filename convention rather than fuzzy title/author matching
-- reliable for those, though older/renamed imports without the id in the
filename won't match.

`libraryItems.mediaId` is the foreign key into `books.id` (confirmed
against a real row pair, not just inferred from the column name), whose
`title`/`description`/`genres` carry the exact same title/summary/tag data
this app would otherwise get by unzipping and parsing the epub itself --
Audiobookshelf's own scan already read the same embedded metadata. `genres`
in particular is the same flat AO3 tag list `epub_meta.classify_subjects`
expects (confirmed content and ordering against a real row), so scanner.py
uses it in place of an epub parse when a work has a match here.

Series membership (AO3's "Part N of <series>") isn't in `books` at all --
Audiobookshelf models it as a separate many-to-many join, `bookSeries`
(`bookId`, `seriesId`, `sequence`) against a `series` table (`id`, `name`)
(confirmed against a real export of both tables). A book with more than one
series row picks whichever was added first (`ORDER BY ... createdAt LIMIT 1`)
rather than surfacing all of them -- AO3 fics are effectively always in at
most one series in practice, so this is a deliberate simplification, not a
bug if it ever isn't.

This is optional and read-only: if the db file isn't mounted, isn't a
valid Audiobookshelf database, or the query otherwise fails, matching
degrades to no matches rather than breaking the (unrelated) downloads/log
refresh it runs alongside.

Read status (has this work been finished) is handled separately, in
load_read_work_ids below -- it's per-person (Audiobookshelf's own
`mediaProgresses` table is keyed by its `users.id`), unlike everything
above, which is shared metadata about the book itself. An app account
only gets Audiobookshelf-synced read status once someone pairs it with
an Audiobookshelf username (see db.get_user_abs_username); otherwise
read/unread stays purely the manual per-user toggle (db.read_marks).
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field

# Mirrors scanner.FILENAME_RE -- duplicated rather than imported to avoid a
# circular import (scanner needs AbsBookMatch from here to fold ABS metadata
# into a WorkEntry).
FILENAME_RE = re.compile(r"^(\d+)[ _].*\.epub$", re.IGNORECASE)


@dataclass
class AbsBookMatch:
    item_id: str  # libraryItems.id -- what the "open in Audiobookshelf" link needs
    title: str | None = None
    author: str | None = None
    description: str | None = None
    language: str | None = None
    genres: list[str] = field(default_factory=list)  # same flat AO3 tag list an epub's dc:subject would have
    series: str | None = None  # series.name, via the bookSeries join table
    series_index: str | None = None  # bookSeries.sequence -- AO3's "Part N of" position


def load_matches(abs_db_path: str, library_id: str) -> dict[str, AbsBookMatch]:
    """AO3 work_id -> AbsBookMatch, restricted to one library (Audiobookshelf
    instances commonly hold other libraries too -- comics, ebooks, podcasts
    -- that shouldn't be matched against).
    """
    if not abs_db_path or not os.path.isfile(abs_db_path):
        return {}

    try:
        conn = sqlite3.connect(f"file:{abs_db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT li.id, li.path, li.authorNamesFirstLast, b.title, b.description, b.language, b.genres,
                    (SELECT s.name FROM bookSeries bs JOIN series s ON s.id = bs.seriesId
                     WHERE bs.bookId = b.id ORDER BY bs.createdAt LIMIT 1) AS series_name,
                    (SELECT bs.sequence FROM bookSeries bs
                     WHERE bs.bookId = b.id ORDER BY bs.createdAt LIMIT 1) AS series_index
                FROM libraryItems li
                JOIN books b ON b.id = li.mediaId
                WHERE li.libraryId = ?
                """,
                (library_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}

    matches: dict[str, AbsBookMatch] = {}
    for item_id, path, author, title, description, language, genres_json, series_name, series_index in rows:
        if not path:
            continue
        filename_match = FILENAME_RE.match(os.path.basename(path))
        if not filename_match:
            continue
        try:
            genres = json.loads(genres_json) if genres_json else []
        except (TypeError, ValueError):
            genres = []
        matches[filename_match.group(1)] = AbsBookMatch(
            item_id=item_id, title=title, author=author, description=description, language=language, genres=genres,
            series=series_name, series_index=str(series_index) if series_index is not None else None,
        )
    return matches


def item_url(base_url: str, item_id: str) -> str:
    return f"{base_url.rstrip('/')}/item/{item_id}"


def load_read_work_ids(abs_db_path: str, library_id: str, username: str) -> dict[str, str | None]:
    """AO3 work_id -> finishedAt (may be None) for every work `username` has
    marked finished in Audiobookshelf's `mediaProgresses` table, restricted
    to one library the same way load_matches is.

    Unlike load_matches (shared metadata, same for every app user),
    "read" is inherently per-person -- Audiobookshelf itself tracks
    mediaProgresses.userId per its own user, so the caller supplies the
    Audiobookshelf *username* an app account has been paired with (see
    db.get_user_abs_username) and this resolves it to that user's
    internal id itself, rather than the caller needing to know it.
    Reusing `mediaProgresses.mediaItemId = books.id = libraryItems.mediaId`
    is the same join load_matches already uses.
    """
    if not abs_db_path or not os.path.isfile(abs_db_path) or not username:
        return {}

    try:
        conn = sqlite3.connect(f"file:{abs_db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT li.path, mp.finishedAt
                FROM users u
                JOIN mediaProgresses mp ON mp.userId = u.id
                JOIN books b ON b.id = mp.mediaItemId
                JOIN libraryItems li ON li.mediaId = b.id
                WHERE u.username = ? AND li.libraryId = ? AND mp.isFinished = 1
                """,
                (username, library_id),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}

    finished: dict[str, str | None] = {}
    for path, finished_at in rows:
        if not path:
            continue
        filename_match = FILENAME_RE.match(os.path.basename(path))
        if not filename_match:
            continue
        finished[filename_match.group(1)] = finished_at
    return finished
