"""Combine a scan of the downloads folder with ao3downloader's log.jsonl.

Two ways to get a result: `scan()` does it live (always accurate, but walks
the filesystem and re-parses every epub on every call), and `load_cached()`
reads the snapshot `refresh_cache()` last saved to SQLite (fast, but only as
fresh as the last manual refresh). Manual overrides are applied at read time
in both paths, never baked into the cache, so editing a work on the Issues
page shows up immediately without needing a refresh.

Reconciliation is keyed by AO3 work id, extracted both from the
`<id>_...epub` / `<id> ...epub` filename convention and from log.jsonl's
work URLs. ao3downloader's settings.ini can customize the filename pattern,
so the separator after the leading id may be an underscore or a space.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import db
from .audiobookshelf import AbsBookMatch
from .epub_meta import EpubParseError, classify_subjects, parse_epub_metadata
from .log_reader import LogRecord, parse_log

FILENAME_RE = re.compile(r"^(\d+)[ _].*\.epub$", re.IGNORECASE)


@dataclass
class WorkEntry:
    work_id: str
    title: str | None = None
    author: str | None = None
    rating: str | None = None
    warnings: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    fandoms: list[str] = field(default_factory=list)
    fandom_candidates: list[str] = field(default_factory=list)
    series: str | None = None
    series_index: str | None = None
    published_date: str | None = None
    summary: str | None = None  # AO3 work summary, only populated for Audiobookshelf-matched works
    file_path: str | None = None
    size_bytes: int | None = None
    mtime: datetime | None = None
    on_disk: bool = False
    log_success: bool | None = None
    log_timestamp: str | None = None
    parse_error: str | None = None
    dismissed: bool = False
    issue_type: str | None = None  # "parse_error" | "missing" | "failed" | None


@dataclass
class ScanStats:
    total_on_disk: int = 0
    total_size_bytes: int = 0
    missing_but_logged_success: int = 0
    on_disk_no_log_entry: int = 0
    logged_failure_count: int = 0


@dataclass
class ScanResult:
    entries: list[WorkEntry]
    stats: ScanStats


def _scan_disk(download_dir: str, abs_matches: dict[str, AbsBookMatch] | None = None) -> dict[str, WorkEntry]:
    abs_matches = abs_matches or {}
    entries: dict[str, WorkEntry] = {}
    if not os.path.isdir(download_dir):
        return entries

    for root, _dirs, files in os.walk(download_dir):
        for name in files:
            match = FILENAME_RE.match(name)
            if not match:
                continue
            work_id = match.group(1)
            full_path = os.path.join(root, name)
            stat = os.stat(full_path)
            entry = WorkEntry(
                work_id=work_id,
                file_path=full_path,
                size_bytes=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime),
                on_disk=True,
            )

            abs_match = abs_matches.get(work_id)
            if abs_match is not None:
                # Audiobookshelf already scanned this file's embedded
                # metadata -- reuse it instead of unzipping/parsing the epub
                # ourselves again.
                entry.title = abs_match.title
                entry.author = abs_match.author
                entry.summary = abs_match.description
                classification = classify_subjects(abs_match.genres)
                entry.rating = classification.rating
                entry.warnings = classification.warnings
                entry.categories = classification.categories
                entry.relationships = classification.relationships
                entry.fandoms = classification.fandoms
                entry.fandom_candidates = classification.fandom_candidates
            else:
                try:
                    meta = parse_epub_metadata(full_path)
                    entry.title = meta.title
                    entry.author = meta.author
                    entry.rating = meta.rating
                    entry.warnings = meta.warnings
                    entry.categories = meta.categories
                    entry.relationships = meta.relationships
                    entry.fandoms = meta.fandoms
                    entry.fandom_candidates = meta.fandom_candidates
                    entry.series = meta.series
                    entry.series_index = meta.series_index
                    entry.published_date = meta.published_date
                except EpubParseError as exc:
                    entry.parse_error = str(exc)
            entries[work_id] = entry

    return entries


def scan_raw(download_dir: str, log_path: str | None, abs_matches: dict[str, AbsBookMatch] | None = None) -> list[WorkEntry]:
    """Live disk+log merge with no overrides applied -- the expensive part
    (filesystem walk + epub parsing) that `refresh_cache` snapshots to SQLite.
    """
    disk_entries = _scan_disk(download_dir, abs_matches)

    log_records: dict[str, LogRecord] = {}
    if log_path and os.path.isfile(log_path):
        log_records = parse_log(log_path)

    entries: list[WorkEntry] = []
    for work_id in set(disk_entries) | set(log_records):
        entry = disk_entries.get(work_id)
        record = log_records.get(work_id)

        if entry is None:
            entry = WorkEntry(work_id=work_id, on_disk=False)
            if record:
                entry.title = record.title
                entry.author = record.author

        if record:
            entry.log_success = record.success
            entry.log_timestamp = record.timestamp

        if entry.parse_error:
            entry.issue_type = "parse_error"
        elif not entry.on_disk and record and record.success:
            entry.issue_type = "missing"
        elif record and not record.success:
            entry.issue_type = "failed"

        entries.append(entry)

    return entries


def _resolve_fandoms(entry: WorkEntry, tag_flags: dict[str, bool]) -> list[str]:
    """A tag counts as fandom if it's been explicitly classified that way
    (globally, via the Tags page -- see db.set_tag_flags), else falls back
    to this work's own heuristic guess (already sitting in entry.fandoms).
    """
    if not entry.fandom_candidates:
        return entry.fandoms
    guessed = set(entry.fandoms)
    return [tag for tag in entry.fandom_candidates if tag_flags.get(tag, tag in guessed)]


def _finalize(
    entries: list[WorkEntry],
    overrides: dict[str, db.Override],
    tag_flags: dict[str, bool],
) -> ScanResult:
    stats = ScanStats()
    result_entries: list[WorkEntry] = []

    all_ids = {e.work_id for e in entries} | set(overrides)
    by_id = {e.work_id: e for e in entries}

    for work_id in all_ids:
        entry = by_id.get(work_id)
        if entry is None:
            entry = WorkEntry(work_id=work_id, on_disk=False)

        entry.fandoms = _resolve_fandoms(entry, tag_flags)

        override = overrides.get(work_id)
        if override:
            if override.title:
                entry.title = override.title
            if override.author:
                entry.author = override.author
            entry.dismissed = override.dismissed

        if entry.on_disk:
            stats.total_on_disk += 1
            stats.total_size_bytes += entry.size_bytes or 0
            if entry.log_success is None:
                stats.on_disk_no_log_entry += 1
        elif entry.log_success:
            stats.missing_but_logged_success += 1

        if entry.log_success is False:
            stats.logged_failure_count += 1

        result_entries.append(entry)

    result_entries.sort(key=lambda e: (e.title or "").lower())
    return ScanResult(entries=result_entries, stats=stats)


def scan(
    download_dir: str,
    log_path: str | None,
    db_path: str | None = None,
    abs_matches: dict[str, AbsBookMatch] | None = None,
) -> ScanResult:
    overrides = db.get_all_overrides(db_path) if db_path else {}
    tag_flags = db.get_all_tag_flags(db_path) if db_path else {}
    return _finalize(scan_raw(download_dir, log_path, abs_matches), overrides, tag_flags)


def refresh_cache(
    download_dir: str,
    log_path: str | None,
    db_path: str,
    abs_matches: dict[str, AbsBookMatch] | None = None,
) -> ScanResult:
    entries = scan_raw(download_dir, log_path, abs_matches)
    db.save_works_cache(db_path, [_entry_to_row(e) for e in entries])
    return _finalize(entries, db.get_all_overrides(db_path), db.get_all_tag_flags(db_path))


def load_cached(db_path: str) -> ScanResult:
    rows = db.load_works_cache(db_path)
    entries = [_row_to_entry(row) for row in rows]
    return _finalize(entries, db.get_all_overrides(db_path), db.get_all_tag_flags(db_path))


def _join(values: list[str]) -> str | None:
    return "\x1f".join(values) if values else None


def _split(value: str | None) -> list[str]:
    return [v for v in value.split("\x1f") if v] if value else []


def _entry_to_row(entry: WorkEntry) -> dict:
    return {
        "work_id": entry.work_id,
        "title": entry.title,
        "author": entry.author,
        "rating": entry.rating,
        "warnings": _join(entry.warnings),
        "categories": _join(entry.categories),
        "relationships": _join(entry.relationships),
        "fandoms": _join(entry.fandoms),
        "fandom_candidates": _join(entry.fandom_candidates),
        "series": entry.series,
        "series_index": entry.series_index,
        "published_date": entry.published_date,
        "summary": entry.summary,
        "file_path": entry.file_path,
        "size_bytes": entry.size_bytes,
        "mtime": entry.mtime.isoformat() if entry.mtime else None,
        "on_disk": int(entry.on_disk),
        "log_success": None if entry.log_success is None else int(entry.log_success),
        "log_timestamp": entry.log_timestamp,
        "parse_error": entry.parse_error,
        "issue_type": entry.issue_type,
    }


def _row_to_entry(row: dict) -> WorkEntry:
    return WorkEntry(
        work_id=row["work_id"],
        title=row["title"],
        author=row["author"],
        rating=row["rating"],
        warnings=_split(row["warnings"]),
        categories=_split(row["categories"]),
        relationships=_split(row["relationships"]),
        fandoms=_split(row["fandoms"]),
        fandom_candidates=_split(row["fandom_candidates"]),
        series=row["series"],
        series_index=row["series_index"],
        published_date=row["published_date"],
        summary=row["summary"],
        file_path=row["file_path"],
        size_bytes=row["size_bytes"],
        mtime=datetime.fromisoformat(row["mtime"]) if row["mtime"] else None,
        on_disk=bool(row["on_disk"]),
        log_success=None if row["log_success"] is None else bool(row["log_success"]),
        log_timestamp=row["log_timestamp"],
        parse_error=row["parse_error"],
        issue_type=row["issue_type"],
    )


def effective_timestamp(entry: WorkEntry) -> datetime | None:
    """Best guess at "when did we last get this work": the file's mtime if
    we have one, else the log's timestamp (parsed from ao3downloader's
    MM/DD/YYYY, HH:MM:SS format). Both are naive/local-clock, not UTC.
    """
    if entry.mtime:
        return entry.mtime
    if entry.log_timestamp:
        try:
            return datetime.strptime(entry.log_timestamp, "%m/%d/%Y, %H:%M:%S")
        except ValueError:
            return None
    return None
