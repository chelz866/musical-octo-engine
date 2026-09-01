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
from .epub_meta import EpubParseError, classify_subjects, looks_like_relationship, parse_epub_metadata, parse_epub_stats
from .log_reader import LogRecord, parse_log

FILENAME_RE = re.compile(r"^(\d+)[ _].*\.epub$", re.IGNORECASE)


def find_files_for_work_id(download_dir: str, work_id: str) -> list[str]:
    """Every file in download_dir whose name matches the "<id> or <id>_"
    convention for this specific work_id, as full paths -- an empty list
    if the folder can't be listed or nothing matches. Shared by
    ao3_client's pre-download "already on disk" check and its
    post-download stale-duplicate cleanup, both of which need the exact
    same filename matching this app's own scanner already uses.
    """
    try:
        names = os.listdir(download_dir)
    except OSError:
        return []
    matches = []
    for name in names:
        match = FILENAME_RE.match(name)
        if match and match.group(1) == work_id:
            matches.append(os.path.join(download_dir, name))
    return matches


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
    characters: list[str] = field(default_factory=list)
    freeform_tags: list[str] = field(default_factory=list)
    series: str | None = None
    series_index: str | None = None
    published_date: str | None = None
    language: str | None = None
    summary: str | None = None  # AO3 work summary, only populated for Audiobookshelf-matched works
    word_count: int | None = None
    chapters_have: int | None = None
    chapters_total: int | None = None  # None means the preface showed "?" (WIP, total not committed)
    file_path: str | None = None
    size_bytes: int | None = None
    mtime: datetime | None = None
    on_disk: bool = False
    log_success: bool | None = None
    log_timestamp: str | None = None
    log_error: str | None = None  # ao3downloader's own exception message, only set when log_success is False
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
                entry.language = abs_match.language
                classification = classify_subjects(abs_match.genres)
                entry.rating = classification.rating
                entry.warnings = classification.warnings
                entry.categories = classification.categories
                entry.fandoms = classification.fandoms
                entry.fandom_candidates = classification.fandom_candidates
                entry.series = abs_match.series
                entry.series_index = abs_match.series_index
            else:
                try:
                    meta = parse_epub_metadata(full_path)
                    entry.title = meta.title
                    entry.author = meta.author
                    entry.rating = meta.rating
                    entry.warnings = meta.warnings
                    entry.categories = meta.categories
                    entry.fandoms = meta.fandoms
                    entry.fandom_candidates = meta.fandom_candidates
                    entry.series = meta.series
                    entry.series_index = meta.series_index
                    entry.published_date = meta.published_date
                    entry.language = meta.language
                except EpubParseError as exc:
                    entry.parse_error = str(exc)

            # Word count/chapter progress live on the epub's own preface page
            # (AO3's own embedded "Stats:" line), not in Audiobookshelf's
            # schema or content.opf -- read regardless of whether an ABS
            # match already supplied the rest of the metadata above.
            stats = parse_epub_stats(full_path)
            entry.word_count = stats.word_count
            entry.chapters_have = stats.chapters_have
            entry.chapters_total = stats.chapters_total

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
            entry.log_error = record.error

        if entry.parse_error:
            entry.issue_type = "parse_error"
        elif not entry.on_disk and record and record.success:
            entry.issue_type = "missing"
        elif record and not record.success:
            entry.issue_type = "failed"

        entries.append(entry)

    return entries


def _resolve_tag_categories(
    entry: WorkEntry, tag_categories: dict[str, str], tag_synonyms: dict[str, str]
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Returns (fandom_candidates, fandoms, characters, relationships,
    freeform), all resolved from entry.fandom_candidates.

    Synonyms are resolved first: two spellings of the same tag (e.g. "MCU"
    and "Marvel Cinematic Universe", see db.get_tag_synonyms) collapse into
    one canonical name, with duplicates within a single work's candidate
    list merged away -- the returned fandom_candidates is this canonicalized
    list, which every downstream consumer (Tags page counts, the per-work
    fandom picker, classification below) should use instead of the raw one.

    Each canonical candidate tag then resolves to fandom/character/
    relationship/freeform: an explicit global classification (from the Tags
    page or the per-work picker -- see db.set_tag_categories) wins; an
    unclassified tag falls back to a heuristic guess -- this work's own
    guessed fandom (entry.fandoms, from epub_meta._guess_fandoms, itself
    canonicalized the same way), then epub_meta.looks_like_relationship's
    "/"/"&" convention -- and anything neither explicitly classified nor
    guessed defaults to freeform. This is also where a mis-guessed
    relationship (e.g. "Hurt/Comfort", which looks like one but isn't)
    actually gets fixed: reclassify the tag as freeform on the Tags page
    and every work with it picks that up immediately.

    A 'child' tag wrangling (see db.get_tag_children) isn't handled here --
    a child keeps its own name and classification; only Downloads filter
    matching treats it specially, expanding a selected parent value to also
    match works tagged with any of its children (see main._entry_matches).
    Fandom/Character/Relationship association (see resolve_tag_fandom) is
    also handled separately, in _finalize.
    """
    if not entry.fandom_candidates:
        return entry.fandom_candidates, entry.fandoms, [], [], []

    canonical_candidates: list[str] = []
    seen: set[str] = set()
    for tag in entry.fandom_candidates:
        canonical = tag_synonyms.get(tag, tag)
        if canonical not in seen:
            seen.add(canonical)
            canonical_candidates.append(canonical)

    guessed_fandoms = {tag_synonyms.get(tag, tag) for tag in entry.fandoms}
    fandoms, characters, relationships, freeform = [], [], [], []
    for tag in canonical_candidates:
        category = tag_categories.get(tag)
        if not category:
            if tag in guessed_fandoms:
                category = "fandom"
            elif looks_like_relationship(tag):
                category = "relationship"
            else:
                category = "freeform"
        if category == "fandom":
            fandoms.append(tag)
        elif category == "character":
            characters.append(tag)
        elif category == "relationship":
            relationships.append(tag)
        else:
            freeform.append(tag)
    return canonical_candidates, fandoms, characters, relationships, freeform


def child_parent_map(children: dict[str, set[str]]) -> dict[str, str]:
    """Inverts db.get_tag_children's parent -> {children} map into
    child -> parent, one entry per 'child'-relation edge (each tag has at
    most one, by construction -- see db.set_tag_wrangling).
    """
    return {child: parent for parent, kids in children.items() for child in kids}


def _resolve_explicit_in_chain(
    tag: str, parent_of: dict[str, str], explicit: dict[str, str], default: str
) -> tuple[str, bool]:
    """Walks `tag`'s own same-category 'child' chain (parent_of, from
    child_parent_map/db.get_tag_children) looking for the nearest
    ancestor -- including `tag` itself -- with an explicit value in
    `explicit`. `default` is itself a real, terminal choice that stops
    the walk just as much as any other explicit value would (see
    resolve_tag_fandom_explicit/resolve_tag_media_type_explicit, whose
    own callers rely on being able to set it explicitly too); only a tag
    with no explicit row anywhere in its own chain falls back to it as a
    default. Safe against a cycle even though db.set_tag_wrangling
    already refuses to create one.

    Returns (resolved value, whether an explicit choice -- on `tag`
    itself or an ancestor -- produced it, as opposed to `default` kicking
    in because nothing in the whole chain has ever set one). Callers that
    only need the resolved value can ignore the second element; a caller
    that needs to tell a real "explicitly this default" decision apart
    from a tag nobody's classified yet (e.g. the Organize-by-Fandom
    grouping) needs both.
    """
    current = tag
    seen: set[str] = set()
    while current is not None:
        if current in explicit:
            return explicit[current], True
        if current in seen:
            break
        seen.add(current)
        current = parent_of.get(current)
    return default, False


def resolve_tag_fandom_explicit(
    tag: str, parent_of: dict[str, str], explicit_fandoms: dict[str, str]
) -> tuple[str, bool]:
    """See _resolve_explicit_in_chain -- 'No Fandom' is the default here."""
    return _resolve_explicit_in_chain(tag, parent_of, explicit_fandoms, "No Fandom")


def resolve_tag_fandom(tag: str, parent_of: dict[str, str], explicit_fandoms: dict[str, str]) -> str:
    """The resolved Fandom only -- see resolve_tag_fandom_explicit for the
    full (value, is_explicit) result.
    """
    return resolve_tag_fandom_explicit(tag, parent_of, explicit_fandoms)[0]


def resolve_tag_media_type_explicit(
    tag: str, parent_of: dict[str, str], explicit_media_types: dict[str, set[str]]
) -> tuple[set[str], bool]:
    """See _resolve_explicit_in_chain -- {'Uncategorized Fandoms'} (AO3's
    own real bucket for a Fandom filed under no specific medium) is the
    default here. Only meaningful for a Fandom-category tag, though
    nothing here enforces that. A Fandom can genuinely belong to more than
    one AO3-style category (e.g. a franchise spanning both 'Movies' and
    'Comics'), so the resolved value -- like explicit_media_types itself
    -- is a set, not a single string; _resolve_explicit_in_chain's own
    chain-walk is value-type-agnostic, so the *nearest* ancestor's whole
    set wins outright rather than merging sets across multiple ancestors.
    """
    return _resolve_explicit_in_chain(tag, parent_of, explicit_media_types, {"Uncategorized Fandoms"})


def resolve_tag_media_type(tag: str, parent_of: dict[str, str], explicit_media_types: dict[str, set[str]]) -> set[str]:
    """The resolved media types only -- see resolve_tag_media_type_explicit
    for the full (value, is_explicit) result.
    """
    return resolve_tag_media_type_explicit(tag, parent_of, explicit_media_types)[0]


def _resolve_associated_fandoms(
    characters: list[str],
    relationships: list[str],
    freeform: list[str],
    parent_of: dict[str, str],
    explicit_fandoms: dict[str, str],
) -> list[str]:
    """Every real Fandom that this work's own Characters/Relationships/
    Freeform tags are associated with (see db.set_tag_fandom), in
    first-seen order with no duplicates -- 'No Fandom' contributes
    nothing. Folded into entry.fandoms in _finalize, so a work using a
    fandom-associated Character/Relationship/Freeform tag counts as
    belonging to that Fandom even when none of its own raw tags said so
    directly (e.g. a fic tagged only with the Character "The Doctor",
    associated with the Fandom "Doctor Who").
    """
    found: list[str] = []
    for tag in (*characters, *relationships, *freeform):
        fandom = resolve_tag_fandom(tag, parent_of, explicit_fandoms)
        if fandom != "No Fandom" and fandom not in found:
            found.append(fandom)
    return found


def _finalize(
    entries: list[WorkEntry],
    overrides: dict[str, db.Override],
    tag_categories: dict[str, str],
    tag_synonyms: dict[str, str],
    same_category_parent_of: dict[str, str],
    tag_fandoms: dict[str, str],
) -> ScanResult:
    stats = ScanStats()
    result_entries: list[WorkEntry] = []

    all_ids = {e.work_id for e in entries} | set(overrides)
    by_id = {e.work_id: e for e in entries}

    for work_id in all_ids:
        entry = by_id.get(work_id)
        if entry is None:
            entry = WorkEntry(work_id=work_id, on_disk=False)

        (
            entry.fandom_candidates,
            entry.fandoms,
            entry.characters,
            entry.relationships,
            entry.freeform_tags,
        ) = _resolve_tag_categories(entry, tag_categories, tag_synonyms)

        for fandom in _resolve_associated_fandoms(
            entry.characters, entry.relationships, entry.freeform_tags, same_category_parent_of, tag_fandoms
        ):
            if fandom not in entry.fandoms:
                entry.fandoms.append(fandom)

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
    tag_categories = db.get_all_tag_categories(db_path) if db_path else {}
    tag_synonyms = db.get_tag_synonyms(db_path) if db_path else {}
    same_category_parent_of = child_parent_map(db.get_tag_children(db_path)) if db_path else {}
    tag_fandoms = db.get_all_tag_fandoms(db_path) if db_path else {}
    return _finalize(
        scan_raw(download_dir, log_path, abs_matches), overrides, tag_categories, tag_synonyms,
        same_category_parent_of, tag_fandoms,
    )


def refresh_cache(
    download_dir: str,
    log_path: str | None,
    db_path: str,
    abs_matches: dict[str, AbsBookMatch] | None = None,
) -> ScanResult:
    entries = scan_raw(download_dir, log_path, abs_matches)
    db.save_works_cache(db_path, [_entry_to_row(e) for e in entries])
    return _finalize(
        entries, db.get_all_overrides(db_path), db.get_all_tag_categories(db_path), db.get_tag_synonyms(db_path),
        child_parent_map(db.get_tag_children(db_path)), db.get_all_tag_fandoms(db_path),
    )


def load_cached(db_path: str) -> ScanResult:
    rows = db.load_works_cache(db_path)
    entries = [_row_to_entry(row) for row in rows]
    return _finalize(
        entries, db.get_all_overrides(db_path), db.get_all_tag_categories(db_path), db.get_tag_synonyms(db_path),
        child_parent_map(db.get_tag_children(db_path)), db.get_all_tag_fandoms(db_path),
    )


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
        "language": entry.language,
        "summary": entry.summary,
        "word_count": entry.word_count,
        "chapters_have": entry.chapters_have,
        "chapters_total": entry.chapters_total,
        "file_path": entry.file_path,
        "size_bytes": entry.size_bytes,
        "mtime": entry.mtime.isoformat() if entry.mtime else None,
        "on_disk": int(entry.on_disk),
        "log_success": None if entry.log_success is None else int(entry.log_success),
        "log_timestamp": entry.log_timestamp,
        "log_error": entry.log_error,
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
        language=row["language"],
        summary=row["summary"],
        word_count=row["word_count"],
        chapters_have=row["chapters_have"],
        chapters_total=row["chapters_total"],
        file_path=row["file_path"],
        size_bytes=row["size_bytes"],
        mtime=datetime.fromisoformat(row["mtime"]) if row["mtime"] else None,
        on_disk=bool(row["on_disk"]),
        log_success=None if row["log_success"] is None else bool(row["log_success"]),
        log_timestamp=row["log_timestamp"],
        log_error=row["log_error"],
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
