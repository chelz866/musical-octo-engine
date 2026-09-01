import os
import tempfile
import zipfile

from app.epub_reader import get_asset_bytes, get_chapter_html, list_chapters

_CONTAINER_XML = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

# Shaped after a real AO3 epub export: the preface is its own spine item,
# followed by one *_split_NNN.xhtml document per chapter (see
# Never_Stop_Looking_At_Me_split_000.xhtml, referenced in test_epub_meta.py).
_OPF_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Work</dc:title>
  </metadata>
  <manifest>
    <item id="preface" href="text/preface.xhtml" media-type="application/xhtml+xml"/>
    {chapter_manifest}
  </manifest>
  <spine>
    <itemref idref="preface"/>
    {chapter_spine}
  </spine>
</package>
"""

_PREFACE_XHTML = """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body class="calibre">
<div id="preface" class="calibre1">
<dl class="tags">
<dt class="calibre3">Stats:</dt>
<dd class="calibre5">Published: 2020-01-01 Words: 100 Chapters: 2/2</dd>
</dl>
</div>
</body></html>
"""

_CHAPTER_XHTML = """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body class="calibre">
<div class="chapter">
<h3 class="title">{heading}</h3>
<p>Some story text.</p>
{extra}
</div>
</body></html>
"""

_UNTITLED_CHAPTER_XHTML = """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body class="calibre">
<div class="chapter">
<p>A chapter with no heading at all.</p>
</div>
</body></html>
"""


def _build_multi_chapter_epub(path: str, chapter_bodies: list[str]) -> None:
    chapter_manifest = "\n".join(
        f'<item id="ch{i}" href="text/split_{i:03d}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(chapter_bodies))
    )
    chapter_spine = "\n".join(f'<itemref idref="ch{i}"/>' for i in range(len(chapter_bodies)))

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("content.opf", _OPF_TEMPLATE.format(chapter_manifest=chapter_manifest, chapter_spine=chapter_spine))
        zf.writestr("text/preface.xhtml", _PREFACE_XHTML)
        for i, body in enumerate(chapter_bodies):
            zf.writestr(f"text/split_{i:03d}.xhtml", body)


def test_list_chapters_returns_preface_then_each_chapter_in_order():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        _build_multi_chapter_epub(path, [
            _CHAPTER_XHTML.format(heading="1. The Beginning", extra=""),
            _CHAPTER_XHTML.format(heading="2. The End", extra=""),
        ])

        chapters = list_chapters(path)

        assert [c.title for c in chapters] == ["Preface", "1. The Beginning", "2. The End"]
        assert [c.index for c in chapters] == [0, 1, 2]


def test_list_chapters_falls_back_to_chapter_number_with_no_heading():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        _build_multi_chapter_epub(path, [_UNTITLED_CHAPTER_XHTML])

        chapters = list_chapters(path)

        assert chapters[1].title == "Chapter 1"


def test_list_chapters_returns_empty_list_for_a_malformed_epub():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.epub")
        with open(path, "w") as f:
            f.write("not a zip file")

        assert list_chapters(path) == []


def test_get_chapter_html_returns_sanitized_body_content():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        _build_multi_chapter_epub(path, [_CHAPTER_XHTML.format(heading="1. The Beginning", extra="")])
        chapters = list_chapters(path)

        html = get_chapter_html(path, "12345", chapters[1])

        assert "Some story text." in html
        assert "1. The Beginning" in html
        assert "xmlns" not in html
        # ElementTree's serializer has a built-in default that renders a
        # detached XHTML element with an "html:" tag prefix unless the
        # empty-prefix registration in epub_reader's module setup is in
        # place -- <html:p>, not <p>, would otherwise reach the browser.
        assert "<p>" in html
        assert "<html:" not in html


def test_get_chapter_html_strips_script_and_style_tags():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        malicious_extra = '<script>alert("evil")</script><style>body{display:none}</style>'
        _build_multi_chapter_epub(path, [_CHAPTER_XHTML.format(heading="1. Title", extra=malicious_extra)])
        chapters = list_chapters(path)

        html = get_chapter_html(path, "12345", chapters[1])

        assert "<script" not in html
        assert "<style" not in html
        assert "alert" not in html


def test_get_chapter_html_strips_event_handler_attributes():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        malicious_extra = '<p onclick="evil()">click me</p>'
        _build_multi_chapter_epub(path, [_CHAPTER_XHTML.format(heading="1. Title", extra=malicious_extra)])
        chapters = list_chapters(path)

        html = get_chapter_html(path, "12345", chapters[1])

        assert "onclick" not in html
        assert "click me" in html


def test_get_chapter_html_rewrites_image_src_to_the_asset_route():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        image_extra = '<img src="images/art.jpg" alt="fanart"/>'
        _build_multi_chapter_epub(path, [_CHAPTER_XHTML.format(heading="1. Title", extra=image_extra)])
        chapters = list_chapters(path)

        html = get_chapter_html(path, "12345", chapters[1])

        assert 'src="/reader/12345/1/asset/text/images/art.jpg"' in html


def test_get_asset_bytes_reads_an_image_from_inside_the_zip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("images/art.jpg", b"\xff\xd8\xff\xfake-jpeg-bytes")

        result = get_asset_bytes(path, "images/art.jpg")

        assert result == (b"\xff\xd8\xff\xfake-jpeg-bytes", "image/jpeg")


def test_get_asset_bytes_returns_none_for_a_path_traversal_attempt():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")

        assert get_asset_bytes(path, "../../etc/passwd.jpg") is None
        assert get_asset_bytes(path, "/etc/passwd.jpg") is None


def test_get_asset_bytes_returns_none_for_a_non_image_extension():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "work.epub")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("text/split_000.xhtml", "<html></html>")

        assert get_asset_bytes(path, "text/split_000.xhtml") is None
