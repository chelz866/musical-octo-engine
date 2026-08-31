import os
import tempfile
import zipfile

import pytest

from app.epub_meta import EpubParseError, looks_like_relationship, parse_epub_metadata, parse_epub_stats

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
    {language_meta}
    {series_meta}
    {subjects}
  </metadata>
  {manifest_spine}
</package>
"""

# Shaped after a real ao3downloader epub's preface page (see
# Never_Stop_Looking_At_Me_split_000.xhtml): a <dl class="tags"> with a
# "Stats:" <dt> followed by a <dd> holding Published/Words/Chapters as
# plain text.
PREFACE_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body class="calibre">
<div id="preface" class="calibre1">
<dl class="tags">
<dt class="calibre3">Stats:</dt>
<dd class="calibre5">
Published: 2020-01-01
Words: {words}
Chapters: {chapters_have}/{chapters_total}
</dd>
</dl>
</div>
</body></html>
"""


def _build_epub(
    tmp_path, title, author, subjects, series=None, series_index=None,
    language=None, words=None, chapters_have=None, chapters_total="?",
):
    subject_xml = "\n".join(f"<dc:subject>{s}</dc:subject>" for s in subjects)
    series_meta = ""
    if series:
        series_meta = f'<meta name="calibre:series" content="{series}"/>'
        if series_index:
            series_meta += f'<meta name="calibre:series_index" content="{series_index}"/>'
    language_meta = f"<dc:language>{language}</dc:language>" if language else ""

    manifest_spine = ""
    extra_files = {}
    if words is not None or chapters_have is not None:
        manifest_spine = """
  <manifest>
    <item id="preface" href="preface.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="preface"/>
  </spine>
"""
        extra_files["preface.xhtml"] = PREFACE_TEMPLATE.format(
            words=words if words is not None else "",
            chapters_have=chapters_have if chapters_have is not None else "",
            chapters_total=chapters_total,
        )

    opf = OPF_TEMPLATE.format(
        title=title, author=author, subjects=subject_xml,
        series_meta=series_meta, language_meta=language_meta, manifest_spine=manifest_spine,
    )

    path = os.path.join(tmp_path, "test.epub")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("content.opf", opf)
        for name, content in extra_files.items():
            zf.writestr(name, content)
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


def test_relationship_shaped_tag_is_a_fandom_candidate_not_a_category():
    # Relationships are no longer hard-classified here -- they flow into
    # fandom_candidates like everything else, to be resolved (with a
    # "/"/"&" guess as the default, always overridable) by
    # scanner._resolve_tag_categories instead. See test_scanner.py for the
    # actual relationship-guess behavior.
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

    assert meta.categories == ["Multi"]
    assert "Jack Harkness/Ianto Jones" not in meta.categories
    assert "Jack Harkness/Ianto Jones" in meta.fandom_candidates


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


def test_fandom_guess_stops_at_relationship_shaped_tag():
    # A relationship tag right after the fandom(s) shouldn't get swallowed
    # into the fandom guess just because it isn't "character-shaped" --
    # looks_like_relationship has to stop the walk too.
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="Bluebird",
            author="Basingstoke",
            subjects=["Fanworks", "Torchwood", "Jack Harkness/Ianto Jones", "Angst"],
        )
        meta = parse_epub_metadata(path)

    assert meta.fandoms == ["Torchwood"]
    assert meta.fandom_candidates == ["Torchwood", "Jack Harkness/Ianto Jones", "Angst"]


def test_looks_like_relationship_matches_slash_and_ampersand():
    assert looks_like_relationship("Jack Harkness/Ianto Jones") is True
    assert looks_like_relationship("Steve Harrington & The Party") is True
    assert looks_like_relationship("Angst") is False
    assert looks_like_relationship("Fluff") is False


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


def test_fandom_candidates_include_every_leftover_tag_not_just_the_guess():
    # Bluebird's guess stops at the first character- or relationship-shaped
    # tag, but the full candidate list (for manual correction) should still
    # include everything left over -- relationships, characters, and
    # freeform tags included.
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp,
            title="Bluebird",
            author="Basingstoke",
            subjects=[
                "Fanworks",
                "Explicit",
                "Torchwood",
                "Addams Family (1991)",
                "Jack Harkness/Ianto Jones",
                "Ianto Jones",
                "Crossover",
                "Multi",
            ],
        )
        meta = parse_epub_metadata(path)

    assert meta.fandoms == ["Torchwood", "Addams Family (1991)"]
    assert meta.fandom_candidates == [
        "Torchwood", "Addams Family (1991)", "Jack Harkness/Ianto Jones", "Ianto Jones", "Crossover",
    ]


def test_bad_zip_raises_parse_error():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "not_an_epub.epub")
        with open(path, "w") as f:
            f.write("not a zip file")

        with pytest.raises(EpubParseError):
            parse_epub_metadata(path)


def test_language_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(tmp, title="Bluebird", author="Basingstoke", subjects=["Fanworks"], language="en")
        meta = parse_epub_metadata(path)

    assert meta.language == "en"


def test_language_absent_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(tmp, title="Bluebird", author="Basingstoke", subjects=["Fanworks"])
        meta = parse_epub_metadata(path)

    assert meta.language is None


def test_parse_epub_stats_reads_words_and_chapters_from_real_shaped_preface():
    # Real sample values (Never_Stop_Looking_At_Me_split_000.xhtml): a
    # complete one-shot.
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp, title="T", author="A", subjects=["Fanworks"],
            words=22513, chapters_have=1, chapters_total=1,
        )
        stats = parse_epub_stats(path)

    assert stats.word_count == 22513
    assert stats.chapters_have == 1
    assert stats.chapters_total == 1


def test_parse_epub_stats_handles_comma_formatted_word_counts():
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp, title="T", author="A", subjects=["Fanworks"],
            words="123,456", chapters_have=3, chapters_total=10,
        )
        stats = parse_epub_stats(path)

    assert stats.word_count == 123456


def test_parse_epub_stats_wip_with_unknown_total():
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(
            tmp, title="T", author="A", subjects=["Fanworks"],
            words=5000, chapters_have=3, chapters_total="?",
        )
        stats = parse_epub_stats(path)

    assert stats.chapters_have == 3
    assert stats.chapters_total is None


def test_parse_epub_stats_missing_preface_returns_blank():
    # Built without words/chapters_have -- no manifest/spine/preface file at
    # all, same shape as every other test in this file.
    with tempfile.TemporaryDirectory() as tmp:
        path = _build_epub(tmp, title="T", author="A", subjects=["Fanworks"])
        stats = parse_epub_stats(path)

    assert stats.word_count is None
    assert stats.chapters_have is None
    assert stats.chapters_total is None


def test_parse_epub_stats_bad_zip_returns_blank_not_raises():
    # Unlike parse_epub_metadata, this must never raise -- a missing/broken
    # stats page shouldn't turn into a parse_error Issue.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "not_an_epub.epub")
        with open(path, "w") as f:
            f.write("not a zip file")

        stats = parse_epub_stats(path)

    assert stats.word_count is None
