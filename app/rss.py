"""Fetch and parse an AO3 Atom feed (tag/series/user feed) to cross-reference
tracked works against what's already downloaded.

AO3's Atom feeds are standard Atom 1.0. Each <entry>'s work id is embedded in
both <id> (tag:archiveofourown.org,2005:Work/12345) and the alternate <link>.
Chapter progress ("Chapters: 3/7", or "3/?" for an author who hasn't
committed to a final count) is embedded as plain text inside the escaped
HTML <summary>, not as a separate structured field -- there's no chapter
count anywhere else in the feed, so this regex is the only source for it.
"""

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

WORK_ID_RE = re.compile(r"Work/(\d+)")
CHAPTERS_RE = re.compile(r"Chapters:\s*(\d+)\s*/\s*(\d+|\?)")

USER_AGENT = "Mozilla/5.0 (compatible; ao3-downloads-viewer)"


class FeedFetchError(Exception):
    pass


@dataclass
class FeedEntry:
    work_id: str
    title: str | None = None
    author: str | None = None
    chapters_have: int | None = None
    chapters_total: int | None = None  # None means the feed showed "?" (author hasn't committed to a total)
    feed_updated: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.chapters_total is not None and self.chapters_have == self.chapters_total


@dataclass
class FeedResult:
    title: str | None = None
    entries: list[FeedEntry] = field(default_factory=list)


def _entry_work_id(entry_el) -> str | None:
    id_el = entry_el.find("atom:id", ATOM_NS)
    if id_el is not None and id_el.text:
        match = WORK_ID_RE.search(id_el.text)
        if match:
            return match.group(1)

    link_el = entry_el.find("atom:link", ATOM_NS)
    if link_el is not None:
        match = WORK_ID_RE.search(link_el.attrib.get("href", ""))
        if match:
            return match.group(1)

    return None


def parse_feed_xml(xml_text: str) -> FeedResult:
    root = ET.fromstring(xml_text)

    title_el = root.find("atom:title", ATOM_NS)
    result = FeedResult(title=title_el.text.strip() if title_el is not None and title_el.text else None)

    for entry_el in root.findall("atom:entry", ATOM_NS):
        work_id = _entry_work_id(entry_el)
        if not work_id:
            continue

        entry = FeedEntry(work_id=work_id)

        title_el = entry_el.find("atom:title", ATOM_NS)
        if title_el is not None and title_el.text:
            entry.title = title_el.text.strip()

        author_el = entry_el.find("atom:author/atom:name", ATOM_NS)
        if author_el is not None and author_el.text:
            entry.author = author_el.text.strip()

        updated_el = entry_el.find("atom:updated", ATOM_NS)
        if updated_el is not None and updated_el.text:
            entry.feed_updated = updated_el.text.strip()

        summary_el = entry_el.find("atom:summary", ATOM_NS)
        if summary_el is not None and summary_el.text:
            match = CHAPTERS_RE.search(summary_el.text)
            if match:
                entry.chapters_have = int(match.group(1))
                entry.chapters_total = None if match.group(2) == "?" else int(match.group(2))

        result.entries.append(entry)

    return result


def parse_feed_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def assess_status(entry: FeedEntry, on_disk: bool, local_timestamp: datetime | None) -> str:
    """Best-effort only: compares the feed's <updated> (UTC) against a local
    filesystem mtime or log timestamp (assumed to be roughly the same clock,
    since ao3downloader doesn't record a timezone). Can be off near the
    boundary if the server's clock isn't UTC -- treat as a hint, not proof.
    """
    if not on_disk:
        return "not_downloaded"
    feed_updated = parse_feed_timestamp(entry.feed_updated) if entry.feed_updated else None
    if feed_updated is None or local_timestamp is None:
        return "unknown"
    if local_timestamp >= feed_updated:
        return "up_to_date"
    return "may_need_update"


def fetch_feed(url: str, timeout: int = 15) -> FeedResult:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            xml_text = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FeedFetchError(f"could not fetch {url}: {exc}") from exc

    try:
        return parse_feed_xml(xml_text)
    except ET.ParseError as exc:
        raise FeedFetchError(f"could not parse feed from {url}: {exc}") from exc
