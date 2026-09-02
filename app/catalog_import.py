"""Bulk-imports work metadata from an external SQLite export into this
app's own catalog_works table (see app/db.py) -- for works this app has
never scanned a file for, brought in from wherever the user already tracks
their library (a personal database, a Calibre-style export, etc.) instead
of re-derived from an epub this app doesn't have.

Built against one concrete export shape: one row per work, with columns
named (case-insensitively) Path, Title, Author, Category (the work's
Fandom -- confusingly named in the source export, not to be confused with
AO3's own Rating/Warning/Category vocabulary), Genre (Additional/Freeform
tags, AO3's own Category token sometimes mixed in alongside them -- see
epub_meta.classify_subjects, reused here to pull it back out), Language,
Rating, Warnings, Chapters, Words, Summary, Relationships, Series,
Published, and "Story URL" (work_id is parsed from this -- there's no
dedicated id column). A source table missing some of these just leaves
those fields blank rather than failing the whole import.

Reads the source database read-only and in batches (`fetchmany`, never the
whole table at once) so this is safe to run against an export with
millions of rows without loading it all into memory -- see
import_from_sqlite's own docstring for how a caller should run this off
the request thread.

Each row's list-valued fields are also flattened into db.catalog_work_tags
(see _tag_rows_for_record) -- a normalized (work_id, category, tag) index
kept in sync on every import, since catalog_works' own \x1f-joined columns
can't be searched by an individual tag value without a full table scan.
"""

import re
import sqlite3
from datetime import datetime
from urllib.parse import quote

from . import db
from .epub_meta import classify_subjects

_WORK_ID_RE = re.compile(r"/works/(\d+)")


class CatalogImportError(Exception):
    pass


def _open_readonly(source_db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(source_db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(source_db_path: str) -> list[str]:
    """Every user table in the source database -- lets an admin pick the
    right one when import_from_sqlite can't auto-detect a single table.
    """
    conn = _open_readonly(source_db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def _work_id_from_story_url(url: str | None) -> str | None:
    if not url:
        return None
    match = _WORK_ID_RE.search(url)
    return match.group(1) if match else None


def _split_tags(value) -> list[str]:
    if not value:
        return []
    return [t.strip() for t in str(value).split(",") if t.strip()]


def _parse_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").strip()
    return int(text) if text.isdigit() else None


def _parse_chapters(value) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    parts = str(value).split("/", 1)
    have = _parse_int(parts[0])
    total = _parse_int(parts[1]) if len(parts) > 1 and parts[1].strip() != "?" else None
    return have, total


def _build_column_map(column_names: list[str]) -> dict[str, str]:
    return {name.strip().lower(): name for name in column_names}


def _get(row: sqlite3.Row, colmap: dict[str, str], *aliases: str):
    for alias in aliases:
        actual = colmap.get(alias)
        if actual is not None:
            return row[actual]
    return None


def _transform_row(row: sqlite3.Row, colmap: dict[str, str]) -> dict | None:
    """One source row -> one db.save_catalog_works row, or None if no
    work_id could be parsed (nothing to key the catalog entry on).
    """
    story_url = _get(row, colmap, "story url", "storyurl", "url")
    work_id = _work_id_from_story_url(story_url)
    if not work_id:
        return None

    genre_tags = _split_tags(_get(row, colmap, "genre", "tags"))
    classified = classify_subjects(genre_tags)
    chapters_have, chapters_total = _parse_chapters(_get(row, colmap, "chapters"))

    return {
        "work_id": work_id,
        "title": _get(row, colmap, "title"),
        "author": _get(row, colmap, "author"),
        "rating": _get(row, colmap, "rating"),
        "warnings": _split_tags(_get(row, colmap, "warnings")),
        "categories": classified.categories,
        "fandoms": _split_tags(_get(row, colmap, "category", "fandom", "fandoms")),
        "relationships": _split_tags(_get(row, colmap, "relationships", "relationship")),
        "freeform": classified.fandom_candidates,
        "language": _get(row, colmap, "language"),
        "summary": _get(row, colmap, "summary"),
        "word_count": _parse_int(_get(row, colmap, "words", "word_count", "word count")),
        "chapters_have": chapters_have,
        "chapters_total": chapters_total,
        "published_date": _get(row, colmap, "published", "published_date"),
        "series": _get(row, colmap, "series"),
        "story_url": story_url,
        "source_path": _get(row, colmap, "path"),
        "imported_at": datetime.now().isoformat(),
    }


CATALOG_TAG_KINDS = ("fandom", "relationship", "freeform", "warning", "category")


def _tag_rows_for_record(record: dict) -> list[tuple[str, str, str]]:
    """Flattens one _transform_row record's list-valued fields into
    (work_id, category, tag) triples for db.save_catalog_work_tags -- the
    normalized index the Catalog Browse page queries, kept in sync with
    catalog_works on every import.
    """
    rows = []
    for kind, tags in zip(
        CATALOG_TAG_KINDS,
        (record["fandoms"], record["relationships"], record["freeform"], record["warnings"], record["categories"]),
    ):
        rows.extend((record["work_id"], kind, tag) for tag in tags)
    return rows


def import_from_sqlite(
    db_path: str,
    source_db_path: str,
    table_name: str | None = None,
    batch_size: int = 2000,
    progress_cb=None,
) -> tuple[int, int]:
    """Reads every row of `table_name` (auto-detected if the source has
    exactly one user table; CatalogImportError naming the choices
    otherwise) from `source_db_path` and upserts it into this app's own
    catalog_works. Returns (imported_count, skipped_count) -- a row is
    skipped only when no work_id could be parsed from it.

    This runs entirely synchronously and can take a while against a
    multi-million-row export -- callers on an async request path should
    run it via asyncio.to_thread (see main.py's own background-worker
    pattern for the download queue) rather than blocking the event loop.
    `progress_cb(imported, skipped)`, if given, is called after each
    batch commits, so a caller can surface live progress (see db.set_meta).
    """
    conn = _open_readonly(source_db_path)
    try:
        if not table_name:
            tables = list_tables(source_db_path)
            if len(tables) != 1:
                choices = ", ".join(tables) or "(none found)"
                raise CatalogImportError(f"Specify which table to import -- found: {choices}")
            table_name = tables[0]

        try:
            cursor = conn.execute(f'SELECT * FROM "{table_name}"')
        except sqlite3.OperationalError as exc:
            raise CatalogImportError(str(exc)) from exc

        colmap = _build_column_map([d[0] for d in cursor.description])
        imported = 0
        skipped = 0
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            transformed = []
            for row in batch:
                record = _transform_row(row, colmap)
                if record is None:
                    skipped += 1
                else:
                    transformed.append(record)
            db.save_catalog_works(db_path, transformed)
            db.save_catalog_work_tags(
                db_path,
                [record["work_id"] for record in transformed],
                [tag_row for record in transformed for tag_row in _tag_rows_for_record(record)],
            )
            imported += len(transformed)
            if progress_cb:
                progress_cb(imported, skipped)
        return imported, skipped
    finally:
        conn.close()
