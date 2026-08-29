"""Parse AO3-generated .epub files for embedded Dublin Core / calibre metadata.

AO3's epub export embeds title/author/tags in content.opf as flat, untyped
dc:subject entries. Rating, Warnings, and Category come from small fixed AO3
vocabularies and can be exact-matched reliably. Relationships are guessable
via the "/" or "&" convention AO3 uses between character names.

Fandom, Character, and Freeform tags have no type label and their relative
order isn't consistent across works, so there's no fully reliable way to
split them. `_guess_fandoms` uses a best-effort heuristic instead: take the
leading run of leftover subjects, stopping at the first one that "looks
like a character name" (2-4 Title Case words, no digits or parentheses).
The first leftover subject is always kept even if it looks name-shaped,
since a short fandom name (e.g. "The Authority") can otherwise be wrongly
excluded when it's the only leftover subject. This is deliberately a guess,
not a reliable classification -- callers should present it as such.
"""

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

CONTAINER_NS = {"container": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

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


def _guess_fandoms(leftover_subjects: list[str]) -> list[str]:
    fandoms = []
    for i, subject in enumerate(leftover_subjects):
        if i > 0 and _looks_character_shaped(subject):
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
    series: str | None = None
    series_index: str | None = None
    rating: str | None = None
    warnings: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    fandoms: list[str] = field(default_factory=list)
    fandom_candidates: list[str] = field(default_factory=list)  # every untyped tag, for manual correction


@dataclass
class SubjectClassification:
    rating: str | None = None
    warnings: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    fandoms: list[str] = field(default_factory=list)
    fandom_candidates: list[str] = field(default_factory=list)


def classify_subjects(subjects: list[str]) -> SubjectClassification:
    """Buckets a flat list of AO3 tag strings into rating/warnings/category/
    relationships/fandom, purely by content -- independent of where the list
    came from. Originally written for an epub's own `dc:subject` entries, but
    Audiobookshelf's own library scan stores the identical tag list (it reads
    the same embedded epub metadata) in its `books.genres` column, so this
    also runs directly against that when a work has an Audiobookshelf match
    (see app/audiobookshelf.py) -- confirmed against a real export to bucket
    identically either way.
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
        elif "/" in subject or "&" in subject:
            result.relationships.append(subject)
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
    meta.relationships = classification.relationships
    meta.fandoms = classification.fandoms
    meta.fandom_candidates = classification.fandom_candidates

    return meta
