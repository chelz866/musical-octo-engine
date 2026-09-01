"""Parse ao3downloader's log.jsonl into the latest record per AO3 work id.

Lines come in three shapes: per-work success/failure entries with a `link`,
and bookkeeping/error entries with no `link` at all (can't be attributed to
a specific work, so they're skipped). The file is append-only and roughly
chronological, so the last matching line for a given work id in file order
is treated as the most recent -- no timestamp parsing/sorting needed.
"""

import json
import re
from dataclasses import dataclass

WORK_ID_RE = re.compile(r"/works/(\d+)")


@dataclass
class LogRecord:
    work_id: str
    title: str | None
    author: str | None
    success: bool
    timestamp: str | None
    error: str | None = None


def _parse_title_author(raw_title: str, work_id: str) -> tuple[str | None, str | None]:
    text = raw_title.strip()
    prefix = f"{work_id} "
    if text.startswith(prefix):
        text = text[len(prefix):]
    if " - " in text:
        title, author = text.rsplit(" - ", 1)
        return title.strip() or None, author.strip() or None
    return text.strip() or None, None


def parse_log(path: str) -> dict[str, LogRecord]:
    records: dict[str, LogRecord] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            link = entry.get("link")
            if not link:
                continue
            match = WORK_ID_RE.search(link)
            if not match:
                continue
            work_id = match.group(1)

            title = author = None
            raw_titles = entry.get("title")
            if raw_titles:
                title, author = _parse_title_author(raw_titles[0], work_id)

            records[work_id] = LogRecord(
                work_id=work_id,
                title=title,
                author=author,
                success=bool(entry.get("success", False)),
                timestamp=entry.get("timestamp"),
                error=entry.get("error"),
            )
    return records
