import json
import os
import tempfile
import zipfile

from app import db
from app.audiobookshelf import AbsBookMatch
from app.scanner import WorkEntry, _resolve_tag_categories, load_cached, refresh_cache, scan

_CONTAINER_XML = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_OPF_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:opf="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Title</dc:title>
    <dc:creator>Author</dc:creator>
    {language_meta}
    {subjects}
  </metadata>
  {manifest_spine}
</package>"""

# Shaped after a real ao3downloader epub's preface page.
_PREFACE_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
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


def _build_epub(path, subjects, language=None, words=None, chapters_have=None, chapters_total="?"):
    subject_xml = "\n".join(f"<dc:subject>{s}</dc:subject>" for s in subjects)
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
        extra_files["preface.xhtml"] = _PREFACE_TEMPLATE.format(
            words=words if words is not None else "",
            chapters_have=chapters_have if chapters_have is not None else "",
            chapters_total=chapters_total,
        )

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("content.opf", _OPF_TEMPLATE.format(
            subjects=subject_xml, language_meta=language_meta, manifest_spine=manifest_spine,
        ))
        for name, content in extra_files.items():
            zf.writestr(name, content)

# Real content doesn't matter here -- these exercise the filename matching,
# not epub parsing (a bad zip still produces a WorkEntry with parse_error set,
# it's just not skipped).


def _write_log(tmp, lines):
    path = os.path.join(tmp, "log.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def test_matches_underscore_separated_filenames():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "7773_The_Business__Basingstoke.epub"), "w").close()
        result = scan(tmp, None)
    assert {e.work_id for e in result.entries} == {"7773"}
    assert result.stats.total_on_disk == 1


def test_matches_space_separated_filenames():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "1011406 Messy - artist_artists.epub"), "w").close()
        result = scan(tmp, None)
    assert {e.work_id for e in result.entries} == {"1011406"}
    assert result.stats.total_on_disk == 1


def test_ignores_non_epub_and_non_matching_files():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "readme.txt"), "w").close()
        open(os.path.join(tmp, "not_an_id.epub"), "w").close()
        result = scan(tmp, None)
    assert result.entries == []
    assert result.stats.total_on_disk == 0


def test_bad_epub_on_disk_is_flagged_as_parse_error_issue():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "123_Bad_Epub.epub"), "w") as f:
            f.write("not actually a zip")
        result = scan(tmp, None)

    entry = result.entries[0]
    assert entry.on_disk is True
    assert entry.issue_type == "parse_error"


def test_logged_success_with_no_file_is_flagged_missing():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as logs:
        log_path = _write_log(logs, [
            {"link": "https://archiveofourown.org/works/999", "title": ["999 Ghost Fic - Nobody"], "success": True, "timestamp": "01/01/2026, 00:00:00"},
        ])
        result = scan(downloads, log_path)

    entry = result.entries[0]
    assert entry.on_disk is False
    assert entry.issue_type == "missing"
    assert result.stats.missing_but_logged_success == 1


def test_logged_failure_with_no_file_is_flagged_failed():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as logs:
        log_path = _write_log(logs, [
            {"link": "https://archiveofourown.org/works/999", "title": ["999 Ghost Fic - Nobody"], "success": False, "timestamp": "01/01/2026, 00:00:00"},
        ])
        result = scan(downloads, log_path)

    entry = result.entries[0]
    assert entry.issue_type == "failed"
    assert result.stats.logged_failure_count == 1


def test_override_applies_title_author_and_dismissed():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)
        db.set_title_author(db_path, "999", "Manually Fixed", "Real Author")
        db.set_dismissed(db_path, "999", True)

        result = scan(tmp, None, db_path)

    entry = result.entries[0]
    assert entry.work_id == "999"
    assert entry.title == "Manually Fixed"
    assert entry.author == "Real Author"
    assert entry.dismissed is True


def test_load_cached_matches_empty_before_any_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        result = load_cached(db_path)
    assert result.entries == []
    assert result.stats.total_on_disk == 0


def test_refresh_cache_populates_cache_and_load_cached_reads_it_back():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        open(os.path.join(downloads, "7773_The_Business__Basingstoke.epub"), "w").close()

        live_result = refresh_cache(downloads, None, db_path)
        assert live_result.stats.total_on_disk == 1

        cached_result = load_cached(db_path)
    assert {e.work_id for e in cached_result.entries} == {"7773"}
    assert cached_result.stats.total_on_disk == 1
    assert cached_result.entries[0].issue_type == "parse_error"  # empty file isn't a valid epub


def test_load_cached_does_not_reflect_filesystem_changes_until_refreshed_again():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        path = os.path.join(downloads, "7773_The_Business__Basingstoke.epub")
        open(path, "w").close()
        refresh_cache(downloads, None, db_path)

        os.remove(path)

        # the cache still shows the file as present -- this is the whole
        # point of caching, it's only as fresh as the last refresh
        assert load_cached(db_path).stats.total_on_disk == 1


def test_load_cached_applies_overrides_same_as_live_scan():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        refresh_cache(downloads, None, db_path)
        db.set_title_author(db_path, "42", "Manual Only", None)

        result = load_cached(db_path)
    assert result.entries[0].work_id == "42"
    assert result.entries[0].title == "Manual Only"


def test_fandom_candidates_survive_refresh_cache_round_trip():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(
            os.path.join(downloads, "1_Title_Author.epub"),
            ["Fanworks", "Torchwood", "Ianto Jones"],
        )

        refresh_cache(downloads, None, db_path)
        entry = load_cached(db_path).entries[0]

    assert entry.fandoms == ["Torchwood"]
    assert entry.fandom_candidates == ["Torchwood", "Ianto Jones"]


def test_language_word_count_and_chapters_parsed_from_local_epub():
    # Real sample values (a real ao3downloader epub's own preface page).
    with tempfile.TemporaryDirectory() as downloads:
        _build_epub(
            os.path.join(downloads, "1_Title_Author.epub"),
            ["Fanworks"],
            language="en", words=22513, chapters_have=1, chapters_total=1,
        )
        result = scan(downloads, None)

    entry = result.entries[0]
    assert entry.language == "en"
    assert entry.word_count == 22513
    assert entry.chapters_have == 1
    assert entry.chapters_total == 1


def test_word_count_and_chapters_blank_without_a_preface_page():
    with tempfile.TemporaryDirectory() as downloads:
        _build_epub(os.path.join(downloads, "1_Title_Author.epub"), ["Fanworks"])
        result = scan(downloads, None)

    entry = result.entries[0]
    assert entry.word_count is None
    assert entry.chapters_have is None
    assert entry.chapters_total is None


def test_word_count_and_chapters_survive_refresh_cache_round_trip():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(
            os.path.join(downloads, "1_Title_Author.epub"),
            ["Fanworks"],
            language="en", words=5000, chapters_have=3, chapters_total="?",
        )

        refresh_cache(downloads, None, db_path)
        entry = load_cached(db_path).entries[0]

    assert entry.language == "en"
    assert entry.word_count == 5000
    assert entry.chapters_have == 3
    assert entry.chapters_total is None  # "?" in the preface -- WIP, total not committed


def test_word_count_and_chapters_come_from_local_epub_even_for_abs_matched_works():
    # Audiobookshelf doesn't track word count, and its "chapters" JSON field
    # (audiobook chapter markers) isn't the AO3 X/Y format -- confirmed
    # against a real export. So these still come from the local epub's own
    # preface page even when the rest of the metadata comes from ABS.
    with tempfile.TemporaryDirectory() as downloads:
        path = os.path.join(downloads, "1_Whatever.epub")
        _build_epub(path, ["Fanworks"], words=22513, chapters_have=1, chapters_total=1)
        abs_matches = {"1": AbsBookMatch(item_id="item-1", title="From Audiobookshelf", genres=["Fanworks"])}

        result = scan(downloads, None, None, abs_matches)

    entry = result.entries[0]
    assert entry.title == "From Audiobookshelf"  # confirms the ABS path was taken
    assert entry.word_count == 22513
    assert entry.chapters_have == 1
    assert entry.chapters_total == 1


def test_tag_flag_overrides_heuristic_guess_for_one_work():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(
            os.path.join(downloads, "1_Title_Author.epub"),
            ["Fanworks", "Torchwood", "Ianto Jones"],
        )
        refresh_cache(downloads, None, db_path)
        db.set_tag_categories(db_path, {"Torchwood": "freeform", "Ianto Jones": "fandom"})  # user corrects the guess

        entry = load_cached(db_path).entries[0]
    assert entry.fandoms == ["Ianto Jones"]
    assert entry.fandom_candidates == ["Torchwood", "Ianto Jones"]  # picker options unchanged


def test_resolve_tag_categories_explicit_classification_wins():
    entry = WorkEntry(work_id="1", fandom_candidates=["Torchwood", "Ianto Jones", "Angst"], fandoms=["Torchwood"])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(
        entry, {"Ianto Jones": "character", "Angst": "freeform"},
    )
    assert fandoms == ["Torchwood"]
    assert characters == ["Ianto Jones"]
    assert relationships == []
    assert freeform == ["Angst"]


def test_resolve_tag_categories_unclassified_falls_back_to_heuristic_guess():
    # "Torchwood" was heuristically guessed as a fandom (in entry.fandoms)
    # and has no explicit tag_categories entry -- it should still resolve
    # as fandom, not silently drop to freeform.
    entry = WorkEntry(work_id="1", fandom_candidates=["Torchwood", "Ianto Jones"], fandoms=["Torchwood"])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {})
    assert fandoms == ["Torchwood"]
    assert characters == []
    assert relationships == []
    assert freeform == ["Ianto Jones"]


def test_resolve_tag_categories_unclassified_non_guessed_defaults_to_freeform():
    entry = WorkEntry(work_id="1", fandom_candidates=["Angst", "Fluff"], fandoms=[])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {})
    assert fandoms == []
    assert characters == []
    assert relationships == []
    assert freeform == ["Angst", "Fluff"]


def test_resolve_tag_categories_explicit_character_overrides_fandom_guess():
    # Even if the heuristic guessed this tag as a fandom, an explicit
    # 'character' classification must win.
    entry = WorkEntry(work_id="1", fandom_candidates=["The Authority"], fandoms=["The Authority"])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {"The Authority": "character"})
    assert fandoms == []
    assert characters == ["The Authority"]
    assert relationships == []
    assert freeform == []


def test_resolve_tag_categories_no_candidates_returns_existing_fandoms_only():
    entry = WorkEntry(work_id="1", fandom_candidates=[], fandoms=["Torchwood"])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {})
    assert fandoms == ["Torchwood"]
    assert characters == []
    assert relationships == []
    assert freeform == []


def test_resolve_tag_categories_unclassified_relationship_shaped_tag_defaults_to_relationship():
    # Not explicitly classified, not a guessed fandom, but "/" between two
    # names -- the heuristic default is relationship, same as the fandom
    # guess already gets a heuristic default.
    entry = WorkEntry(work_id="1", fandom_candidates=["Torchwood", "Ianto Jones/Jack Harkness"], fandoms=["Torchwood"])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {})
    assert fandoms == ["Torchwood"]
    assert relationships == ["Ianto Jones/Jack Harkness"]
    assert freeform == []


def test_resolve_tag_categories_explicit_freeform_fixes_a_mis_guessed_relationship():
    # "Hurt/Comfort" looks relationship-shaped (it has a "/") but isn't one
    # -- this is exactly the false positive an explicit classification has
    # to be able to fix.
    entry = WorkEntry(work_id="1", fandom_candidates=["Hurt/Comfort"], fandoms=[])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {"Hurt/Comfort": "freeform"})
    assert relationships == []
    assert freeform == ["Hurt/Comfort"]


def test_resolve_tag_categories_explicit_relationship_classification():
    entry = WorkEntry(work_id="1", fandom_candidates=["Established Relationship"], fandoms=[])
    fandoms, characters, relationships, freeform = _resolve_tag_categories(
        entry, {"Established Relationship": "relationship"},
    )
    assert relationships == ["Established Relationship"]
    assert freeform == []


def test_abs_match_replaces_epub_parsing_for_that_work():
    # The whole point of the Audiobookshelf integration going further than
    # just a link: for a matched work, its title/tags/author come from
    # Audiobookshelf's own already-scanned metadata instead of parsing the
    # local epub -- proven here by using a *broken* epub (would otherwise
    # produce a parse_error) whose real metadata still comes through because
    # of the match.
    with tempfile.TemporaryDirectory() as downloads:
        with open(os.path.join(downloads, "1_Whatever.epub"), "w") as f:
            f.write("not actually a zip")

        abs_matches = {
            "1": AbsBookMatch(
                item_id="item-1",
                title="Real Title From Audiobookshelf",
                author="Real Author",
                description="A summary from Audiobookshelf.",
                genres=["Fanworks", "General Audiences", "Some Fandom", "Gen"],
            )
        }
        result = scan(downloads, None, None, abs_matches)

    entry = result.entries[0]
    assert entry.parse_error is None
    assert entry.title == "Real Title From Audiobookshelf"
    assert entry.author == "Real Author"
    assert entry.summary == "A summary from Audiobookshelf."
    assert entry.rating == "General Audiences"
    assert entry.categories == ["Gen"]
    assert entry.fandoms == ["Some Fandom"]


def test_abs_match_handles_a_real_20_plus_tag_work_without_choking():
    # AO3 fics routinely carry 20-30+ tags -- oddly common. This is the
    # exact real genres array (27 entries) pulled from a matched
    # Audiobookshelf book row earlier, used verbatim to make sure
    # classification doesn't misbucket or choke at real-world scale.
    with tempfile.TemporaryDirectory() as downloads:
        open(os.path.join(downloads, "1_Whatever.epub"), "w").close()
        genres = [
            "Fanworks", "General Audiences", "Stranger Things (TV 2016)",
            "Steve Harrington & The Party", "Steve Harrington & Eddie Munson",
            "Joyce Byers & Steve Harrington", 'Steve Harrington & Jim "Chief" Hopper',
            "Steve Harrington", "Eddie Munson", "Dustin Henderson",
            'Maxine "Max" Mayfield', "Mike Wheeler", "Eleven | Jane Hopper",
            'Jim "Chief" Hopper', "Joyce Byers", "Will Byers", "Nancy Wheeler",
            "Lucas Sinclair", "Hurt Steve Harrington", "Steve Harrington Needs a Hug",
            "Epilepsy", "Seizures", "Steve Harrington Has Self-Esteem Issues",
            "The Party Loves Steve Harrington", "The Party as Family (Stranger Things)",
            "Gen", "Choose Not To Use Archive Warnings",
        ]
        assert len(genres) >= 20

        abs_matches = {
            "1": AbsBookMatch(
                item_id="item-1",
                title="A Member Of The Party",
                author="CartoonCrazy789",
                genres=genres,
                description="Steve didn't want them to know. He didn't want them to know why he couldn't drive",
            )
        }
        result = scan(downloads, None, None, abs_matches)

    entry = result.entries[0]
    assert entry.rating == "General Audiences"
    assert entry.categories == ["Gen"]
    assert entry.warnings == ["Choose Not To Use Archive Warnings"]
    assert entry.relationships == [
        "Steve Harrington & The Party",
        "Steve Harrington & Eddie Munson",
        "Joyce Byers & Steve Harrington",
        'Steve Harrington & Jim "Chief" Hopper',
    ]
    # 23 leftover relationship/character/freeform tags (the 4 "&"-joined
    # relationships now included, since they're no longer pulled out early)
    # -- the fandom-guessing heuristic still correctly picks out just the
    # one real fandom name among them.
    assert entry.fandoms == ["Stranger Things (TV 2016)"]
    assert len(entry.fandom_candidates) == 23
    assert entry.summary == "Steve didn't want them to know. He didn't want them to know why he couldn't drive"


def test_abs_match_includes_series():
    with tempfile.TemporaryDirectory() as downloads:
        with open(os.path.join(downloads, "1_Whatever.epub"), "w") as f:
            f.write("not actually a zip")

        abs_matches = {
            "1": AbsBookMatch(item_id="item-1", title="Sanctuary", series="Hogwarts Stranger Secrets", series_index="5"),
        }
        result = scan(downloads, None, None, abs_matches)

    entry = result.entries[0]
    assert entry.series == "Hogwarts Stranger Secrets"
    assert entry.series_index == "5"


def test_abs_matches_do_not_affect_unmatched_works():
    with tempfile.TemporaryDirectory() as downloads:
        _build_epub(os.path.join(downloads, "2_Title_Author.epub"), ["Fanworks", "Torchwood"])
        abs_matches = {"999": AbsBookMatch(item_id="item-999", title="Unrelated")}

        result = scan(downloads, None, None, abs_matches)

    entry = result.entries[0]
    assert entry.work_id == "2"
    assert entry.title == "Title"  # from the epub, unaffected by an unrelated ABS match
    assert entry.fandoms == ["Torchwood"]


def test_abs_match_metadata_survives_refresh_cache_round_trip():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        open(os.path.join(downloads, "1_Whatever.epub"), "w").close()
        abs_matches = {
            "1": AbsBookMatch(
                item_id="item-1",
                title="From Audiobookshelf",
                description="A summary.",
                genres=["Fanworks", "Some Fandom"],
            )
        }

        refresh_cache(downloads, None, db_path, abs_matches)
        entry = load_cached(db_path).entries[0]

    assert entry.title == "From Audiobookshelf"
    assert entry.summary == "A summary."
    assert entry.fandoms == ["Some Fandom"]


def test_tag_flag_applies_to_every_work_sharing_that_tag():
    # The whole point: classifying one tag fixes every work with that tag
    # in one action, instead of a per-work correction on each of them.
    # In both works "Ianto Jones" sits second in the candidate list behind
    # a parenthesized fandom name, so the heuristic guess excludes it from
    # both (it looks character-shaped and isn't the kept first item).
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(os.path.join(downloads, "1_A.epub"), ["Fanworks", "Some Fandom (2020)", "Ianto Jones"])
        _build_epub(os.path.join(downloads, "2_B.epub"), ["Fanworks", "Other Fandom (2021)", "Ianto Jones"])
        refresh_cache(downloads, None, db_path)

        before = {e.work_id: e for e in load_cached(db_path).entries}
        assert "Ianto Jones" not in before["1"].fandoms
        assert "Ianto Jones" not in before["2"].fandoms

        db.set_tag_categories(db_path, {"Ianto Jones": "fandom"})

        entries = {e.work_id: e for e in load_cached(db_path).entries}
    assert "Ianto Jones" in entries["1"].fandoms
    assert "Ianto Jones" in entries["2"].fandoms


def test_character_classification_applies_to_every_work_sharing_that_tag():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(os.path.join(downloads, "1_A.epub"), ["Fanworks", "Some Fandom (2020)", "Ianto Jones"])
        _build_epub(os.path.join(downloads, "2_B.epub"), ["Fanworks", "Other Fandom (2021)", "Ianto Jones"])
        refresh_cache(downloads, None, db_path)

        # before classifying: heuristic already excludes "Ianto Jones" from
        # fandoms, so today it would just default to freeform
        before = {e.work_id: e for e in load_cached(db_path).entries}
        assert "Ianto Jones" in before["1"].freeform_tags
        assert "Ianto Jones" in before["2"].freeform_tags

        db.set_tag_categories(db_path, {"Ianto Jones": "character"})

        entries = {e.work_id: e for e in load_cached(db_path).entries}
    assert "Ianto Jones" in entries["1"].characters
    assert "Ianto Jones" in entries["2"].characters
    assert "Ianto Jones" not in entries["1"].freeform_tags
    assert "Ianto Jones" not in entries["2"].freeform_tags
