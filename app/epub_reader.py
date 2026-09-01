"""Renders a downloaded work's own epub content directly in this app -- an
in-browser reading fallback for whenever an external reader (e.g.
Audiobookshelf) isn't working, using AO3's own already-exported chapter
HTML rather than re-typesetting anything.

An AO3 epub's `<spine>` (in content.opf) lists every content document in
reading order; index 0 is always the preface page this app's own scanner
already reads Words/Chapters stats from (see epub_meta.parse_epub_stats)
-- everything after it is real story content, in however many pieces AO3's
own export split it into. A chapter's title is whatever heading (h1/h2/h3)
its own HTML uses (the author's real chapter title, where AO3 puts it) --
"Chapter N" is only a fallback for one with no heading at all.

Chapter HTML is fan-authored content AO3 itself already sanitized when the
author originally posted it, but this app strips <script>/<style> and any
"on*" event-handler attribute from it anyway before embedding it directly
in a page here, rather than trusting a mechanical export to necessarily be
inert forever. Embedded images (fanart some fics bundle in the epub itself)
are served through get_asset_bytes rather than as data: URIs, so a large
image doesn't bloat every chapter's own HTML.
"""

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

from .epub_meta import OPF_NS, XHTML_NS, EpubParseError, find_opf_path

# ElementTree's serializer has a built-in default namespace-to-prefix table
# (xml.etree.ElementTree._namespace_map) that maps the XHTML namespace to
# the prefix "html" when writing out a detached element with no prefix of
# its own recorded from parsing -- without this, get_chapter_html's own
# ET.tostring calls would emit <html:div>/<html:p>/<html:img> instead of
# plain tags, since the source XHTML declares this as its *default* (no
# prefix) namespace. Registering "" as the prefix for it here makes
# ElementTree serialize a bare `xmlns="..."` instead, matching the source
# and letting _XMLNS_ATTR_RE strip it below like any other namespace decl.
ET.register_namespace("", XHTML_NS["xhtml"])

_HEADING_TAGS = {f"{{{XHTML_NS['xhtml']}}}h1", f"{{{XHTML_NS['xhtml']}}}h2", f"{{{XHTML_NS['xhtml']}}}h3"}
_SCRIPT_STYLE_TAGS = {f"{{{XHTML_NS['xhtml']}}}script", f"{{{XHTML_NS['xhtml']}}}style"}
_IMG_TAGS = {f"{{{XHTML_NS['xhtml']}}}img"}
_XMLNS_ATTR_RE = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')
_JAVASCRIPT_HREF_RE = re.compile(r"^\s*javascript:", re.IGNORECASE)

_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


@dataclass
class Chapter:
    index: int
    href: str
    title: str


def _manifest_and_spine(opf_root: ET.Element) -> tuple[dict[str, str], list[str]]:
    manifest = {
        item.attrib["id"]: item.attrib["href"]
        for item in opf_root.findall("opf:manifest/opf:item", OPF_NS)
        if "id" in item.attrib and "href" in item.attrib
    }
    spine_el = opf_root.find("opf:spine", OPF_NS)
    idrefs = [ref.attrib["idref"] for ref in spine_el.findall("opf:itemref", OPF_NS)] if spine_el is not None else []
    return manifest, idrefs


def _heading_text(body: ET.Element) -> str | None:
    for el in body.iter():
        if el.tag in _HEADING_TAGS:
            text = "".join(el.itertext()).strip()
            if text:
                return text
    return None


def list_chapters(path: str) -> list[Chapter]:
    """Every readable content document in the epub's own spine, in order.
    Returns [] for anything unreadable/malformed rather than raising --
    the reader page shows "not available" the same way a parse_error
    already surfaces elsewhere in this app, instead of a 500.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = find_opf_path(zf)
            opf_root = ET.fromstring(zf.read(opf_path))
            manifest, idrefs = _manifest_and_spine(opf_root)
            base_dir = posixpath.dirname(opf_path)
            chapters = []
            for i, idref in enumerate(idrefs):
                href = manifest.get(idref)
                if not href:
                    continue
                full_href = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else href
                title = "Preface" if i == 0 else f"Chapter {i}"
                try:
                    doc = ET.fromstring(zf.read(full_href))
                    body = doc.find("xhtml:body", XHTML_NS)
                    if body is not None and i > 0:
                        heading = _heading_text(body)
                        if heading:
                            title = heading
                except (KeyError, ET.ParseError):
                    pass
                chapters.append(Chapter(index=i, href=full_href, title=title))
            return chapters
    except (zipfile.BadZipFile, KeyError, OSError, ET.ParseError, EpubParseError):
        return []


def _strip_unsafe(el: ET.Element) -> None:
    """Removes script/style children and any "on*" event-handler attribute,
    recursively, in place.
    """
    for child in list(el):
        if child.tag in _SCRIPT_STYLE_TAGS:
            el.remove(child)
            continue
        _strip_unsafe(child)
    for attr in list(el.attrib):
        if attr.lower().startswith("on"):
            del el.attrib[attr]
    href = el.attrib.get("href")
    if href and _JAVASCRIPT_HREF_RE.match(href):
        del el.attrib["href"]


def _rewrite_image_srcs(el: ET.Element, work_id: str, chapter_index: int, base_dir: str) -> None:
    """Points an embedded image at this app's own asset route instead of
    its epub-relative path, so the browser can actually fetch it -- the
    raw path only means anything inside the zip, not on the web.
    """
    for child in el.iter():
        if child.tag in _IMG_TAGS and child.attrib.get("src"):
            asset_href = posixpath.normpath(posixpath.join(base_dir, child.attrib["src"])) if base_dir else child.attrib["src"]
            child.attrib["src"] = f"/reader/{work_id}/{chapter_index}/asset/{asset_href}"


def get_chapter_html(path: str, work_id: str, chapter: Chapter) -> str | None:
    """The sanitized inner-body HTML of one chapter, ready to embed
    directly in the reader page template (marked `| safe` there, since
    it's been sanitized here) -- None if the document can't be read/parsed.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            doc = ET.fromstring(zf.read(chapter.href))
    except (zipfile.BadZipFile, KeyError, OSError, ET.ParseError):
        return None

    body = doc.find("xhtml:body", XHTML_NS)
    if body is None:
        return None

    base_dir = posixpath.dirname(chapter.href)
    _strip_unsafe(body)
    _rewrite_image_srcs(body, work_id, chapter.index, base_dir)

    pieces = [ET.tostring(child, encoding="unicode") for child in body]
    return _XMLNS_ATTR_RE.sub("", "".join(pieces))


def get_asset_bytes(path: str, asset_href: str) -> tuple[bytes, str] | None:
    """Raw bytes + content-type for an embedded image, by its path inside
    the epub zip (as rewritten into a chapter's own HTML by
    _rewrite_image_srcs) -- None if it's missing, not an image, or the
    path tries to escape the zip.
    """
    normalized = posixpath.normpath(asset_href)
    if normalized.startswith("..") or normalized.startswith("/"):
        return None
    ext = posixpath.splitext(normalized)[1].lower()
    content_type = _IMAGE_CONTENT_TYPES.get(ext)
    if not content_type:
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.read(normalized), content_type
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
