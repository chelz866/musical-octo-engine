import os
import tempfile
import zipfile

import pytest

from app.epub_meta import EpubParseError, parse_epub_metadata

CONTAINER_XML = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uuid_id">
  <metadata xmlns:opf="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator opf:file-as="author" opf:role="aut">{author}</dc:creator>
    <dc:date>2020-01-01T00:00:00+00:00</dc:date>
    {series_meta}
    {subjects}
  </metadata>
</package>
"""


def _build_epub(tmp_path, title, author, subjects, series=None, series_index=None):
    subject_xml = "\n".join(f"<dc:subject>{s}</dc:subject>" for s in subjects)
    series_meta = ""
    if series:
        series_meta = f'<meta name="calibre:series" content="{series}"/>'
        if series_index:
            series_meta += f'<meta name="calibre:series_index" content="{series_index}"/>'
    opf = OPF_TEMPLATE.format(title=title, author=author, subjects=subject_xml, series_meta=series_meta)

    path = os.path.join(tmp_path, "test.epub")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("content.opf", opf)
    return path


def test_basic_metadata_and_rating_warning():
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="Year of the Scavenger",
            author="Basingstoke",
            subjects=[
                "Fanworks",
                "Choose Not To Use Archive Warnings",
                "Gattaca (1997)",
                "Jerome Eugene Morrow",
                "Mature",
                "M/M",
            ],
        )
        meta = parse_epub_metadata(path)

    assert meta.title == "Year of the Scavenger"
    assert meta.author == "Basingstoke"
    assert meta.rating == "Mature"
    assert meta.warnings == ["Choose Not To Use Archive Warnings"]
    assert meta.categories == ["M/M"]
    assert meta.relationships == []


def test_relationship_detected_and_not_confused_with_category():
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="Bluebird",
            author="Basingstoke",
            subjects=[
                "Fanworks",
                "Explicit",
                "Jack Harkness/Ianto Jones",
                "Multi",
            ],
        )
        meta = parse_epub_metadata(path)

    assert meta.relationships == ["Jack Harkness/Ianto Jones"]
    assert meta.categories == ["Multi"]
    assert "Jack Harkness/Ianto Jones" not in meta.categories


def test_series_metadata_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="The Business",
            author="Basingstoke",
            subjects=["Fanworks"],
            series="Author's Favorites",
            series_index="6",
        )
        meta = parse_epub_metadata(path)

    assert meta.series == "Author's Favorites"
    assert meta.series_index == "6"


def test_fandom_guess_stops_at_character_shaped_tag():
    # Real sample: "Year of the Scavenger" -- fandom has a parenthetical
    # disambiguator so it isn't mistaken for a character name, but the two
    # character tags that follow it should be excluded from the guess.
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="Year of the Scavenger",
            author="Basingstoke",
            subjects=[
                "Fanworks",
                "Choose Not To Use Archive Warnings",
                "Gattaca (1997)",
                "Jerome Eugene Morrow",
                "Vincent Freeman",
                "Mature",
                "M/M",
            ],
        )
        meta = parse_epub_metadata(path)

    assert meta.fandoms == ["Gattaca (1997)"]


def test_fandom_guess_handles_multiple_fandoms_and_many_characters():
    # Real sample: "Bluebird" -- two single-word fandoms, then a relationship
    # (stripped separately) and a long run of character names, then a
    # trailing freeform tag.
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="Bluebird",
            author="Basingstoke",
            subjects=[
                "Fanworks",
                "Explicit",
                "Choose Not To Use Archive Warnings",
                "Torchwood",
                "Addams Family (1991)",
                "Jack Harkness/Ianto Jones",
                "Ianto Jones",
                "Jack Harkness",
                "Gwen Cooper",
                "Crossover",
                "Multi",
            ],
        )
        meta = parse_epub_metadata(path)

    assert meta.fandoms == ["Torchwood", "Addams Family (1991)"]


def test_fandom_guess_keeps_first_item_even_if_name_shaped():
    # Real sample: "The Business" -- the only leftover fandom tag happens to
    # be two Title Case words with no digits/parens (character-shaped by the
    # heuristic), so it must still be kept since it's the first item and
    # there's nothing else it could be.
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="The Business",
            author="Basingstoke",
            subjects=["Fanworks", "The Authority", "Established Relationship"],
        )
        meta = parse_epub_metadata(path)

    assert meta.fandoms == ["The Authority"]
    assert meta.rating is None
    assert meta.warnings == []
    assert meta.categories == []
    assert meta.relationships == []


def test_bad_zip_raises_parse_error():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "not_an_epub.epub")
        with open(path, "w") as f:
            f.write("not a zip file")

        with pytest.raises(EpubParseError):
            parse_epub_metadata(path)
