"""Combine a live scan of the downloads folder with ao3downloader's log.jsonl.

No database: this scans the filesystem on every call so the view is always
accurate. Reconciliation is keyed by AO3 work id, extracted both from the
`<id>_...epub` / `<id> ...epub` filename convention and from log.jsonl's
work URLs. ao3downloader's settings.ini can customize the filename pattern,
so the separator after the leading id may be an underscore or a space.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from .epub_meta import EpubParseError, parse_epub_metadata
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
    series: str | None = None
    series_index: str | None = None
    published_date: str | None = None
    file_path: str | None = None
    size_bytes: int | None = None
    mtime: datetime | None = None
    on_disk: bool = False
    log_success: bool | None = None
    log_timestamp: str | None = None
    parse_error: str | None = None


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


def _scan_disk(download_dir: str) -> dict[str, WorkEntry]:
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
            try:
                meta = parse_epub_metadata(full_path)
                entry.title = meta.title
                entry.author = meta.author
                entry.rating = meta.rating
                entry.warnings = meta.warnings
                entry.categories = meta.categories
                entry.relationships = meta.relationships
                entry.series = meta.series
                entry.series_index = meta.series_index
                entry.published_date = meta.published_date
            except EpubParseError as exc:
                entry.parse_error = str(exc)
            entries[work_id] = entry

    return entries


def scan(download_dir: str, log_path: str | None) -> ScanResult:
    disk_entries = _scan_disk(download_dir)

    log_records: dict[str, LogRecord] = {}
    if log_path and os.path.isfile(log_path):
        log_records = parse_log(log_path)

    stats = ScanStats()
    result_entries: list[WorkEntry] = []

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

        if entry.on_disk:
            stats.total_on_disk += 1
            stats.total_size_bytes += entry.size_bytes or 0
            if record is None:
                stats.on_disk_no_log_entry += 1
        elif record and record.success:
            stats.missing_but_logged_success += 1

        if record and not record.success:
            stats.logged_failure_count += 1

        result_entries.append(entry)

    result_entries.sort(key=lambda e: (e.title or "").lower())
    return ScanResult(entries=result_entries, stats=stats)
