import json
import os
import tempfile
import zipfile

from app import db
from app.audiobookshelf import AbsBookMatch
from app.scanner import (
    WorkEntry,
    _resolve_associated_fandoms,
    _resolve_tag_categories,
    child_parent_map,
    find_files_for_work_id,
    load_cached,
    rebuild_work_tags,
    refresh_cache,
    resolve_tag_fandom,
    resolve_tag_fandom_explicit,
    resolve_tag_media_type,
    resolve_tag_media_type_explicit,
    scan,
)

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


def test_find_files_for_work_id_matches_underscore_and_space_separated():
    with tempfile.TemporaryDirectory() as tmp:
        underscore_path = os.path.join(tmp, "7773_The_Business - Author.epub")
        space_path = os.path.join(tmp, "7773 The Business - Author.epub")
        open(underscore_path, "w").close()
        open(space_path, "w").close()

        found = find_files_for_work_id(tmp, "7773")

        assert sorted(found) == sorted([underscore_path, space_path])


def test_find_files_for_work_id_only_matches_the_given_id():
    with tempfile.TemporaryDirectory() as tmp:
        wanted_path = os.path.join(tmp, "7773_The_Business - Author.epub")
        other_path = os.path.join(tmp, "9999_Unrelated - Author.epub")
        open(wanted_path, "w").close()
        open(other_path, "w").close()

        assert find_files_for_work_id(tmp, "7773") == [wanted_path]


def test_find_files_for_work_id_empty_when_nothing_matches():
    with tempfile.TemporaryDirectory() as tmp:
        assert find_files_for_work_id(tmp, "7773") == []


def test_find_files_for_work_id_empty_for_a_nonexistent_directory():
    assert find_files_for_work_id("/no/such/directory", "7773") == []


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


def test_scan_finds_files_in_an_extra_dir():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as manual:
        _build_epub(os.path.join(downloads, "1_A.epub"), ["Fanworks"])
        _build_epub(os.path.join(manual, "2_B.epub"), ["Fanworks"])
        result = scan(downloads, None, extra_dirs=[manual])

    work_ids = {e.work_id for e in result.entries}
    assert work_ids == {"1", "2"}
    assert all(e.on_disk for e in result.entries)


def test_scan_extra_dir_does_not_override_the_primary_dir_on_collision():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as manual:
        _build_epub(os.path.join(downloads, "1_Primary.epub"), ["Fanworks"])
        _build_epub(os.path.join(manual, "1_Manual.epub"), ["Fanworks"])
        result = scan(downloads, None, extra_dirs=[manual])

    assert len(result.entries) == 1
    assert result.entries[0].file_path == os.path.join(downloads, "1_Primary.epub")


def test_scan_with_no_extra_dirs_behaves_as_before():
    with tempfile.TemporaryDirectory() as downloads:
        _build_epub(os.path.join(downloads, "1_A.epub"), ["Fanworks"])
        result = scan(downloads, None)

    assert result.entries[0].work_id == "1"


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


def test_logged_failure_carries_the_error_message_through():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as logs:
        log_path = _write_log(logs, [
            {
                "link": "https://archiveofourown.org/works/999",
                "title": ["999 Ghost Fic - Nobody"],
                "success": False,
                "error": "Work is only available to registered users of the Archive.",
                "timestamp": "01/01/2026, 00:00:00",
            },
        ])
        result = scan(downloads, log_path)

    entry = result.entries[0]
    assert entry.log_error == "Work is only available to registered users of the Archive."


def test_log_error_survives_refresh_cache_round_trip():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as logs, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        log_path = _write_log(logs, [
            {
                "link": "https://archiveofourown.org/works/999",
                "title": ["999 Ghost Fic - Nobody"],
                "success": False,
                "error": "Work is only available to registered users of the Archive.",
                "timestamp": "01/01/2026, 00:00:00",
            },
        ])

        refresh_cache(downloads, log_path, db_path)
        entry = load_cached(db_path).entries[0]

    assert entry.log_error == "Work is only available to registered users of the Archive."


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


def test_load_cached_does_not_reflect_a_classification_change_until_rebuilt():
    # load_cached reads the work_tags precompute, not a live resolution --
    # a tag_flags write alone doesn't retroactively change what it returns.
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(os.path.join(downloads, "1_A.epub"), ["Fanworks", "Ianto Jones"])
        refresh_cache(downloads, None, db_path)

        db.set_tag_categories(db_path, {"Ianto Jones": "character"})
        assert "Ianto Jones" not in load_cached(db_path).entries[0].characters

        rebuild_work_tags(db_path)
        assert "Ianto Jones" in load_cached(db_path).entries[0].characters


def test_rebuild_work_tags_does_not_require_a_disk_rescan():
    # Unlike refresh_cache, rebuild_work_tags only re-derives from the
    # already-cached works_cache rows -- no filesystem/epub work needed.
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        path = os.path.join(downloads, "1_A.epub")
        _build_epub(path, ["Fanworks", "Ianto Jones"])
        refresh_cache(downloads, None, db_path)
        os.remove(path)

        db.set_tag_categories(db_path, {"Ianto Jones": "character"})
        rebuild_work_tags(db_path)

        entry = load_cached(db_path).entries[0]
    assert "Ianto Jones" in entry.characters
    assert entry.on_disk  # untouched by rebuild_work_tags -- still reflects the last refresh_cache


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
        rebuild_work_tags(db_path)

        entry = load_cached(db_path).entries[0]
    assert entry.fandoms == ["Ianto Jones"]
    assert entry.fandom_candidates == ["Torchwood", "Ianto Jones"]  # picker options unchanged


def test_resolve_tag_categories_explicit_classification_wins():
    entry = WorkEntry(work_id="1", fandom_candidates=["Torchwood", "Ianto Jones", "Angst"], fandoms=["Torchwood"])
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(
        entry, {"Ianto Jones": "character", "Angst": "freeform"}, {},
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
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {}, {})
    assert fandoms == ["Torchwood"]
    assert characters == []
    assert relationships == []
    assert freeform == ["Ianto Jones"]


def test_resolve_tag_categories_unclassified_non_guessed_defaults_to_freeform():
    entry = WorkEntry(work_id="1", fandom_candidates=["Angst", "Fluff"], fandoms=[])
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {}, {})
    assert fandoms == []
    assert characters == []
    assert relationships == []
    assert freeform == ["Angst", "Fluff"]


def test_resolve_tag_categories_explicit_character_overrides_fandom_guess():
    # Even if the heuristic guessed this tag as a fandom, an explicit
    # 'character' classification must win.
    entry = WorkEntry(work_id="1", fandom_candidates=["The Authority"], fandoms=["The Authority"])
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(
        entry, {"The Authority": "character"}, {},
    )
    assert fandoms == []
    assert characters == ["The Authority"]
    assert relationships == []
    assert freeform == []


def test_resolve_tag_categories_no_candidates_returns_existing_fandoms_only():
    entry = WorkEntry(work_id="1", fandom_candidates=[], fandoms=["Torchwood"])
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {}, {})
    assert fandoms == ["Torchwood"]
    assert characters == []
    assert relationships == []
    assert freeform == []


def test_resolve_tag_categories_unclassified_relationship_shaped_tag_defaults_to_relationship():
    # Not explicitly classified, not a guessed fandom, but "/" between two
    # names -- the heuristic default is relationship, same as the fandom
    # guess already gets a heuristic default.
    entry = WorkEntry(work_id="1", fandom_candidates=["Torchwood", "Ianto Jones/Jack Harkness"], fandoms=["Torchwood"])
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(entry, {}, {})
    assert fandoms == ["Torchwood"]
    assert relationships == ["Ianto Jones/Jack Harkness"]
    assert freeform == []


def test_resolve_tag_categories_explicit_freeform_fixes_a_mis_guessed_relationship():
    # "Hurt/Comfort" looks relationship-shaped (it has a "/") but isn't one
    # -- this is exactly the false positive an explicit classification has
    # to be able to fix.
    entry = WorkEntry(work_id="1", fandom_candidates=["Hurt/Comfort"], fandoms=[])
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(
        entry, {"Hurt/Comfort": "freeform"}, {},
    )
    assert relationships == []
    assert freeform == ["Hurt/Comfort"]


def test_resolve_tag_categories_explicit_relationship_classification():
    entry = WorkEntry(work_id="1", fandom_candidates=["Established Relationship"], fandoms=[])
    _candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(
        entry, {"Established Relationship": "relationship"}, {},
    )
    assert relationships == ["Established Relationship"]
    assert freeform == []


def test_resolve_tag_categories_synonym_merges_and_dedupes_candidates():
    # Two spellings of the same fandom in one work's raw tag list collapse
    # into a single canonical candidate, and the merged tag's category
    # comes from the canonical name.
    entry = WorkEntry(work_id="1", fandom_candidates=["MCU", "Marvel Cinematic Universe", "Angst"], fandoms=["MCU"])
    candidates, fandoms, characters, relationships, freeform = _resolve_tag_categories(
        entry, {"Marvel Cinematic Universe": "fandom"}, {"MCU": "Marvel Cinematic Universe"},
    )
    assert candidates == ["Marvel Cinematic Universe", "Angst"]
    assert fandoms == ["Marvel Cinematic Universe"]
    assert freeform == ["Angst"]


def test_resolve_tag_categories_synonym_resolves_guessed_fandom_too():
    # entry.fandoms (the heuristic guess) is itself canonicalized before
    # being used as the guessed-fandom fallback set, so a synonym of a
    # guessed fandom still resolves as fandom instead of freeform.
    entry = WorkEntry(work_id="1", fandom_candidates=["MCU"], fandoms=["Marvel Cinematic Universe"])
    _candidates, fandoms, _characters, _relationships, freeform = _resolve_tag_categories(
        entry, {}, {"MCU": "Marvel Cinematic Universe"},
    )
    assert fandoms == ["Marvel Cinematic Universe"]
    assert freeform == []


def test_resolve_tag_categories_child_wrangling_is_a_no_op_here():
    # Parent/child wrangling only affects Downloads filter matching (see
    # main._entry_matches) -- a child tag keeps its own name and
    # classification unchanged.
    entry = WorkEntry(work_id="1", fandom_candidates=["Alternate Reality - Canon Divergence"], fandoms=[])
    _candidates, _fandoms, _characters, _relationships, freeform = _resolve_tag_categories(
        entry, {"Alternate Reality - Canon Divergence": "freeform"}, {},
    )
    assert freeform == ["Alternate Reality - Canon Divergence"]


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
        rebuild_work_tags(db_path)

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
        rebuild_work_tags(db_path)

        entries = {e.work_id: e for e in load_cached(db_path).entries}
    assert "Ianto Jones" in entries["1"].characters
    assert "Ianto Jones" in entries["2"].characters
    assert "Ianto Jones" not in entries["1"].freeform_tags
    assert "Ianto Jones" not in entries["2"].freeform_tags


def test_child_parent_map_inverts_children_map():
    children = {"Ron Weasley": {"Ron Weasley (Auror)"}, "Angst": {"Hurt/Comfort Angst"}}
    assert child_parent_map(children) == {
        "Ron Weasley (Auror)": "Ron Weasley",
        "Hurt/Comfort Angst": "Angst",
    }


def test_resolve_tag_fandom_uses_own_explicit_association():
    assert resolve_tag_fandom("The Doctor", {}, {"The Doctor": "Doctor Who"}) == "Doctor Who"


def test_resolve_tag_fandom_defaults_to_no_fandom_when_never_set():
    assert resolve_tag_fandom("Coffee Shops", {}, {}) == "No Fandom"


def test_resolve_tag_fandom_inherits_from_same_category_parent():
    parent_of = {"Ron Weasley (Auror)": "Ron Weasley"}
    assert resolve_tag_fandom("Ron Weasley (Auror)", parent_of, {"Ron Weasley": "Harry Potter"}) == "Harry Potter"


def test_resolve_tag_fandom_own_explicit_association_overrides_inherited():
    parent_of = {"Anxious Shane Hollander": "Anxious Character"}
    explicit = {"Anxious Character": "No Fandom", "Anxious Shane Hollander": "Heated Rivalry"}
    assert resolve_tag_fandom("Anxious Shane Hollander", parent_of, explicit) == "Heated Rivalry"


def test_resolve_tag_fandom_walks_multiple_levels():
    parent_of = {"Ron Weasley (Auror, Injured)": "Ron Weasley (Auror)", "Ron Weasley (Auror)": "Ron Weasley"}
    explicit = {"Ron Weasley": "Harry Potter"}
    assert resolve_tag_fandom("Ron Weasley (Auror, Injured)", parent_of, explicit) == "Harry Potter"


def test_resolve_tag_fandom_explicit_true_when_own_association_set():
    assert resolve_tag_fandom_explicit("The Doctor", {}, {"The Doctor": "Doctor Who"}) == ("Doctor Who", True)


def test_resolve_tag_fandom_explicit_false_when_never_set_anywhere():
    assert resolve_tag_fandom_explicit("Coffee Shops", {}, {}) == ("No Fandom", False)


def test_resolve_tag_fandom_explicit_true_for_a_real_no_fandom_choice():
    # "for real for real no fandom" -- someone deliberately chose No Fandom,
    # distinct from a tag nobody's classified either way yet.
    assert resolve_tag_fandom_explicit("Anxious Character", {}, {"Anxious Character": "No Fandom"}) == ("No Fandom", True)


def test_resolve_tag_fandom_explicit_true_when_inherited_no_fandom_choice():
    parent_of = {"Anxious Shane Hollander": "Anxious Character"}
    explicit = {"Anxious Character": "No Fandom"}
    assert resolve_tag_fandom_explicit("Anxious Shane Hollander", parent_of, explicit) == ("No Fandom", True)


def test_resolve_tag_media_type_uses_own_explicit_choice():
    assert resolve_tag_media_type("Doctor Who", {}, {"Doctor Who": {"TV Shows"}}) == {"TV Shows"}


def test_resolve_tag_media_type_can_return_more_than_one():
    # A Fandom can genuinely belong to more than one AO3-style category.
    assert resolve_tag_media_type("Doctor Who", {}, {"Doctor Who": {"TV Shows", "Books & Literature"}}) == {
        "TV Shows", "Books & Literature",
    }


def test_resolve_tag_media_type_defaults_to_uncategorized_when_never_set():
    assert resolve_tag_media_type("Some New Fandom", {}, {}) == {"Uncategorized Fandoms"}


def test_resolve_tag_media_type_inherits_from_same_category_parent():
    parent_of = {"Fantastic Beasts": "Wizarding World"}
    explicit = {"Wizarding World": {"Movies"}}
    assert resolve_tag_media_type("Fantastic Beasts", parent_of, explicit) == {"Movies"}


def test_resolve_tag_media_type_own_explicit_choice_overrides_inherited():
    parent_of = {"Harry Potter": "Wizarding World"}
    explicit = {"Wizarding World": {"Movies"}, "Harry Potter": {"Books & Literature"}}
    assert resolve_tag_media_type("Harry Potter", parent_of, explicit) == {"Books & Literature"}


def test_resolve_tag_media_type_explicit_true_for_a_real_uncategorized_choice():
    # Someone deliberately chose "Uncategorized Fandoms" -- distinct from a
    # tag nobody's classified either way yet, same as "No Fandom" for
    # resolve_tag_fandom_explicit.
    assert resolve_tag_media_type_explicit("Weird Fandom", {}, {"Weird Fandom": {"Uncategorized Fandoms"}}) == (
        {"Uncategorized Fandoms"}, True,
    )


def test_resolve_tag_media_type_explicit_false_when_never_set_anywhere():
    assert resolve_tag_media_type_explicit("Some New Fandom", {}, {}) == ({"Uncategorized Fandoms"}, False)


def test_resolve_associated_fandoms_gathers_from_all_three_lists_deduped():
    parent_of = {}
    explicit = {"Hermione Granger": "Harry Potter", "Harry Potter/Ron Weasley": "Harry Potter", "Coffee Shops": "No Fandom"}
    found = _resolve_associated_fandoms(
        ["Hermione Granger"], ["Harry Potter/Ron Weasley"], ["Coffee Shops"], parent_of, explicit,
    )
    assert found == ["Harry Potter"]


def test_resolve_associated_fandoms_empty_when_nothing_associated():
    assert _resolve_associated_fandoms(["Some Character"], [], ["Some Tag"], {}, {}) == []


def test_end_to_end_fandom_association_folds_into_entry_fandoms():
    # A work tagged only with a Character (no raw Fandom tag at all) --
    # once that Character is associated with a Fandom, the work should
    # count as belonging to it.
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as other:
        db_path = os.path.join(other, "app.db")
        db.init_db(db_path)
        _build_epub(os.path.join(downloads, "1_A.epub"), ["Fanworks", "The Doctor"])
        refresh_cache(downloads, None, db_path)

        db.set_tag_categories(db_path, {"The Doctor": "character"})
        rebuild_work_tags(db_path)
        before = load_cached(db_path).entries[0]
        assert "Doctor Who" not in before.fandoms

        db.set_tag_fandom(db_path, "The Doctor", "Doctor Who")
        rebuild_work_tags(db_path)
        after = load_cached(db_path).entries[0]
    assert "Doctor Who" in after.fandoms
