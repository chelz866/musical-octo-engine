"""Matches downloaded AO3 works to Audiobookshelf library items, so a work
that's also in your Audiobookshelf library can link there instead of (or
alongside) AO3.

Audiobookshelf's `libraryItems.path` for anything ao3downloader put there
carries the same `<work_id> Title - Author.epub` filename ao3downloader
itself uses (confirmed against a real export of the table), so matching
reuses scanner.FILENAME_RE rather than fuzzy title/author matching --
reliable for those, though older/renamed imports without the id in the
filename won't match.

This is optional and read-only: if the db file isn't mounted, isn't a
valid Audiobookshelf database, or the query otherwise fails, matching
degrades to no matches rather than breaking the (unrelated) downloads/log
refresh it runs alongside.
"""

import os
import sqlite3

from .scanner import FILENAME_RE


def load_matches(abs_db_path: str, library_id: str) -> dict[str, str]:
    """AO3 work_id -> Audiobookshelf libraryItems.id, restricted to one
    library (Audiobookshelf instances commonly hold other libraries too --
    comics, ebooks, podcasts -- that shouldn't be matched against).
    """
    if not abs_db_path or not os.path.isfile(abs_db_path):
        return {}

    try:
        conn = sqlite3.connect(f"file:{abs_db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                'SELECT id, path FROM libraryItems WHERE libraryId = ?',
                (library_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}

    matches: dict[str, str] = {}
    for item_id, path in rows:
        if not path:
            continue
        match = FILENAME_RE.match(os.path.basename(path))
        if match:
            matches[match.group(1)] = item_id
    return matches


def item_url(base_url: str, item_id: str) -> str:
    return f"{base_url.rstrip('/')}/item/{item_id}"
