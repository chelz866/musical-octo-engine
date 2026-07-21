"""Parse AO3-generated .epub files for embedded Dublin Core / calibre metadata.

AO3's epub export embeds title/author/tags in content.opf as flat, untyped
dc:subject entries. Rating, Warnings, and Category come from small fixed AO3
vocabularies and can be exact-matched reliably. Relationships are guessable
via the "/" or "&" convention AO3 uses between character names. Fandom,
Character, and Freeform tags are NOT reliably separable from each other here
(no consistent ordering across real samples) and are intentionally skipped.
"""

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

    for subject_el in metadata_el.findall("dc:subject", OPF_NS):
        subject = (subject_el.text or "").strip()
        if not subject or subject in IGNORED_SUBJECTS:
            continue
        if subject in RATINGS:
            meta.rating = subject
        elif subject in WARNINGS:
            meta.warnings.append(subject)
        elif subject in CATEGORIES:
            meta.categories.append(subject)
        elif "/" in subject or "&" in subject:
            meta.relationships.append(subject)
        # else: ambiguous fandom/character/freeform tag -- skipped in v1

    return meta
