"""Parse AO3-generated .epub files for embedded Dublin Core / calibre metadata.

AO3's epub export embeds title/author/tags in content.opf as flat, untyped
dc:subject entries. Rating, Warnings, and Category come from small fixed AO3
vocabularies and can be exact-matched reliably.

Fandom, Relationship, Character, and Freeform tags have no type label and
their relative order isn't consistent across works, so there's no fully
reliable way to split them -- everything left over becomes a
`fandom_candidate` instead, and app/scanner.py's _resolve_tag_categories
resolves each one into a real category, always overridable via the Tags
page. Two guesses feed that resolution as defaults, not final answers:
`_guess_fandoms` takes the leading run of leftover subjects, stopping at the
first one that "looks like a character name" (2-4 Title Case words, no
digits or parentheses) or a relationship (see `looks_like_relationship`) --
the first leftover subject is always kept even if it looks name-shaped,
since a short fandom name (e.g. "The Authority") can otherwise be wrongly
excluded when it's the only leftover subject. `looks_like_relationship`
guesses from the "/" or "&" convention AO3 uses between character names --
deliberately just a guess, not a reliable classification, since plenty of
ordinary Additional Tags use the same punctuation (e.g. "Hurt/Comfort").
"""

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

CONTAINER_NS = {"container": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
XHTML_NS = {"xhtml": "http://www.w3.org/1999/xhtml"}

_STATS_WORDS_RE = re.compile(r"Words:\s*([\d,]+)")
_STATS_CHAPTERS_RE = re.compile(r"Chapters:\s*(\d+)\s*/\s*(\d+|\?)")

RATINGS = {
    "Not Rated",
    "General Audiences",
    "Teen And Up Audiences",
    "Mature",
    "Explicit",
}
WARNINGS = {
    "No Archive Warnings Apply",
    "Choose Not To Use Archive Warnings",
    "Graphic Depictions Of Violence",
    "Major Character Death",
    "Rape/Non-Con",
    "Underage",
}
CATEGORIES = {"Gen", "F/M", "M/M", "F/F", "Multi", "Other"}

IGNORED_SUBJECTS = {"Fanworks"}

_CHARACTER_SHAPED_RE = re.compile(r"^[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){1,3}$")


def _looks_character_shaped(subject: str) -> bool:
    if any(ch.isdigit() for ch in subject) or "(" in subject:
        return False
    return bool(_CHARACTER_SHAPED_RE.match(subject))


def looks_like_relationship(subject: str) -> bool:
    """The "/" or "&" convention AO3 uses between character names in a
    relationship tag -- a guess, not a reliable classification, since plenty
    of ordinary Additional Tags use the same punctuation (e.g. "Hurt/Comfort",
    "Fix-It/Episode Related", "Friends & Family"). Used both to keep
    _guess_fandoms from swallowing a relationship tag into the fandom guess,
    and in scanner._resolve_tag_categories as the default guess for an
    unclassified candidate tag -- always overridable there via the Tags page,
    exactly like the fandom/character/freeform guesses already are.
    """
    return "/" in subject or "&" in subject


def _guess_fandoms(leftover_subjects: list[str]) -> list[str]:
    fandoms = []
    for i, subject in enumerate(leftover_subjects):
        if i > 0 and (_looks_character_shaped(subject) or looks_like_relationship(subject)):
            break
        fandoms.append(subject)
    return fandoms


class EpubParseError(Exception):
    pass


@dataclass
class EpubMetadata:
    title: str | None = None
    author: str | None = None
    published_date: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: str | None = None
    rating: str | None = None
    warnings: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    fandoms: list[str] = field(default_factory=list)
    fandom_candidates: list[str] = field(default_factory=list)  # every untyped tag, for manual correction


@dataclass
class EpubStats:
    word_count: int | None = None
    chapters_have: int | None = None
    chapters_total: int | None = None  # None means the preface showed "?" (WIP, total not yet committed)


@dataclass
class SubjectClassification:
    rating: str | None = None
    warnings: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    fandoms: list[str] = field(default_factory=list)
    fandom_candidates: list[str] = field(default_factory=list)


def classify_subjects(subjects: list[str]) -> SubjectClassification:
    """Buckets a flat list of AO3 tag strings into rating/warnings/category,
    purely by content -- independent of where the list came from. Originally
    written for an epub's own `dc:subject` entries, but Audiobookshelf's own
    library scan stores the identical tag list (it reads the same embedded
    epub metadata) in its `books.genres` column, so this also runs directly
    against that when a work has an Audiobookshelf match (see
    app/audiobookshelf.py) -- confirmed against a real export to bucket
    identically either way.

    Fandom, Relationship, Character, and Freeform tags have no type label
    and their relative order isn't consistent across works, so none of them
    get a hard classification here -- everything left over becomes a
    `fandom_candidate`, resolved into one of those four buckets later by
    scanner._resolve_tag_categories (an explicit Tags-page classification
    wins; otherwise it falls back to a guess: _guess_fandoms for fandom,
    looks_like_relationship for relationship, else freeform). Relationships
    used to be hard-classified right here via the same "/"/"&" convention,
    but that had no way to fix a false positive like "Hurt/Comfort" being
    swept in as a relationship -- moving it into the same
    guess-with-override pipeline as everything else fixes that.
    """
    result = SubjectClassification()
    leftover_subjects = []
    for raw in subjects:
        subject = (raw or "").strip()
        if not subject or subject in IGNORED_SUBJECTS:
            continue
        if subject in RATINGS:
            result.rating = subject
        elif subject in WARNINGS:
            result.warnings.append(subject)
        elif subject in CATEGORIES:
            result.categories.append(subject)
        else:
            leftover_subjects.append(subject)

    result.fandoms = _guess_fandoms(leftover_subjects)
    result.fandom_candidates = leftover_subjects
    return result


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    try:
        container_xml = zf.read("META-INF/container.xml")
    except KeyError as exc:
        raise EpubParseError("missing META-INF/container.xml") from exc

    root = ET.fromstring(container_xml)
    rootfile = root.find(".//container:rootfile", CONTAINER_NS)
    if rootfile is None or "full-path" not in rootfile.attrib:
        raise EpubParseError("could not locate OPF rootfile in container.xml")
    return rootfile.attrib["full-path"]


def parse_epub_metadata(path: str) -> EpubMetadata:
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = _find_opf_path(zf)
            opf_xml = zf.read(opf_path)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise EpubParseError(f"could not read epub {path}: {exc}") from exc

    try:
        root = ET.fromstring(opf_xml)
    except ET.ParseError as exc:
        raise EpubParseError(f"malformed OPF in {path}: {exc}") from exc

    metadata_el = root.find("opf:metadata", OPF_NS)
    if metadata_el is None:
        raise EpubParseError(f"no <metadata> element in {path}")

    meta = EpubMetadata()

    title_el = metadata_el.find("dc:title", OPF_NS)
    if title_el is not None and title_el.text:
        meta.title = title_el.text.strip()

    creator_el = metadata_el.find("dc:creator", OPF_NS)
    if creator_el is not None and creator_el.text:
        meta.author = creator_el.text.strip()

    date_el = metadata_el.find("dc:date", OPF_NS)
    if date_el is not None and date_el.text:
        meta.published_date = date_el.text.strip()

    language_el = metadata_el.find("dc:language", OPF_NS)
    if language_el is not None and language_el.text:
        meta.language = language_el.text.strip()

    for meta_tag in metadata_el.findall("opf:meta", OPF_NS):
        name = meta_tag.attrib.get("name")
        if name == "calibre:series":
            meta.series = meta_tag.attrib.get("content")
        elif name == "calibre:series_index":
            meta.series_index = meta_tag.attrib.get("content")

    subjects = [subject_el.text for subject_el in metadata_el.findall("dc:subject", OPF_NS)]
    classification = classify_subjects(subjects)
    meta.rating = classification.rating
    meta.warnings = classification.warnings
    meta.categories = classification.categories
    meta.fandoms = classification.fandoms
    meta.fandom_candidates = classification.fandom_candidates

    return meta


def _first_spine_item_href(opf_root: ET.Element) -> str | None:
    manifest_items = {
        item.attrib["id"]: item.attrib["href"]
        for item in opf_root.findall("opf:manifest/opf:item", OPF_NS)
        if "id" in item.attrib and "href" in item.attrib
    }
    spine = opf_root.find("opf:spine", OPF_NS)
    if spine is None:
        return None
    first_itemref = spine.find("opf:itemref", OPF_NS)
    if first_itemref is None:
        return None
    return manifest_items.get(first_itemref.attrib.get("idref"))


def _extract_stats_from_preface(html_bytes: bytes) -> EpubStats:
    """AO3's own epub export embeds a "Stats:" line (Words/Chapters) as
    plain text in a <dl class="tags"> on the preface page it generates --
    read that directly instead of guessing chapter count from the file's
    own structure (can't tell "complete" from "still posting") or needing
    an external source for word count (confirmed Audiobookshelf doesn't
    track it either).
    """
    stats = EpubStats()
    try:
        root = ET.fromstring(html_bytes)
    except ET.ParseError:
        return stats

    dl = root.find(".//xhtml:dl[@class='tags']", XHTML_NS)
    if dl is None:
        return stats

    children = list(dl)
    for i, child in enumerate(children):
        if child.tag.endswith("}dt") and (child.text or "").strip() == "Stats:":
            if i + 1 >= len(children):
                break
            stats_text = "".join(children[i + 1].itertext())
            words_match = _STATS_WORDS_RE.search(stats_text)
            if words_match:
                stats.word_count = int(words_match.group(1).replace(",", ""))
            chapters_match = _STATS_CHAPTERS_RE.search(stats_text)
            if chapters_match:
                stats.chapters_have = int(chapters_match.group(1))
                stats.chapters_total = None if chapters_match.group(2) == "?" else int(chapters_match.group(2))
            break
    return stats


def parse_epub_stats(path: str) -> EpubStats:
    """Word count / chapter progress, read from the preface page's own
    "Stats:" line rather than content.opf -- a separate, optional pass from
    parse_epub_metadata, since this still applies even to Audiobookshelf-
    matched works (which skip parse_epub_metadata but the epub file itself
    still sits on disk with this same preface page in it). Never raises --
    a missing/malformed page just means no stats, not a parse_error.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = _find_opf_path(zf)
            opf_root = ET.fromstring(zf.read(opf_path))
            href = _first_spine_item_href(opf_root)
            if not href:
                return EpubStats()
            page_path = posixpath.join(posixpath.dirname(opf_path), href)
            page_bytes = zf.read(page_path)
    except (zipfile.BadZipFile, KeyError, OSError, ET.ParseError, EpubParseError):
        return EpubStats()
    return _extract_stats_from_preface(page_bytes)
