"""Covers the Classify Tags associations that touch app.main's module-level
DB_PATH directly (_auto_link_relationship_characters, _tag_has_no_fandom,
and the route guards built on them) -- unlike most of test_main.py, these
need a real database, so each test points DB_PATH at a fresh temp file for
its own duration via _temp_db, matching the SERVER_TZ_NAME save/restore
pattern already used in test_main.py for a module-level constant.
"""

import os
import tempfile
from contextlib import contextmanager

from app import db, main as main_module, scanner
from app.main import (
    _auto_link_relationship_characters,
    _tag_has_no_fandom,
    _tag_rows,
    _unverified,
    apply_associations,
    mark_all_unclassified_freeform,
    mark_page_freeform,
    remove_freeform_character_route,
    remove_freeform_relationship_route,
    set_relationship_character_route,
    set_selected_tags,
    set_tag_fandom_route,
    set_tag_media_type_route,
    set_tag_verified_route,
    unwrangle_tag,
    wrangle_tags,
)
from app.scanner import WorkEntry, _entry_to_row


@contextmanager
def _temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        original = main_module.DB_PATH
        main_module.DB_PATH = path
        try:
            yield path
        finally:
            main_module.DB_PATH = original


def _seed_candidate_tag(path: str, tag: str) -> None:
    """Puts `tag` into the works_cache as one work's sole fandom_candidate,
    so scanner.load_cached (which apply_associations reads via
    _effective_tag_category) actually resolves it to whatever category
    db.set_tag_categories gives it -- an empty works_cache would otherwise
    make every tag resolve to None regardless of its explicit category.
    """
    _seed_candidate_tags(path, [tag])


def _seed_candidate_tags(path: str, tags: list[str]) -> None:
    entry = WorkEntry(work_id="1", fandom_candidates=tags)
    db.save_works_cache(path, [_entry_to_row(entry)])
    scanner.rebuild_work_tags(path)


def _reclassify(path: str, categories: dict[str, str]) -> None:
    """db.set_tag_categories, then refreshes the work_tags precompute so
    scanner.load_cached (which every route under test here reads) actually
    reflects it -- see scanner.rebuild_work_tags's own docstring.
    """
    db.set_tag_categories(path, categories)
    scanner.rebuild_work_tags(path)


_WRANGLE_DEFAULTS = dict(
    filter="all", page=1, sort="count_desc", work_id="", q="",
    show_guessed=False, show_set=False, incomplete_only=False,
)


def test_tag_has_no_fandom_false_when_never_set():
    with _temp_db():
        assert _tag_has_no_fandom("Coffee Shop AU") is False


def test_tag_has_no_fandom_true_only_for_the_explicit_terminal_choice():
    with _temp_db() as path:
        db.set_tag_fandom(path, "Coffee Shop AU", "No Fandom")
        assert _tag_has_no_fandom("Coffee Shop AU") is True

        db.set_tag_fandom(path, "Some Freeform Tag", "Harry Potter")
        assert _tag_has_no_fandom("Some Freeform Tag") is False


def test_auto_link_relationship_characters_links_an_exact_match():
    with _temp_db() as path:
        db.set_tag_categories(path, {
            "Harry Potter": "character",
            "Harry Potter/Draco Malfoy": "relationship",
        })

        _auto_link_relationship_characters()

        assert db.get_all_relationship_characters(path) == {
            "Harry Potter/Draco Malfoy": {0: "Harry Potter"},
        }


def test_auto_link_relationship_characters_works_regardless_of_classification_order():
    # A Character classified after its Relationship already exists should
    # get picked up on the next run just the same -- this recomputes from
    # scratch rather than reacting to only one side of a match.
    with _temp_db() as path:
        db.set_tag_categories(path, {"Harry Potter/Draco Malfoy": "relationship"})
        _auto_link_relationship_characters()
        assert db.get_all_relationship_characters(path) == {}

        db.set_tag_categories(path, {"Harry Potter": "character"})
        _auto_link_relationship_characters()
        assert db.get_all_relationship_characters(path) == {
            "Harry Potter/Draco Malfoy": {0: "Harry Potter"},
        }


def test_auto_link_relationship_characters_never_overwrites_an_existing_link():
    with _temp_db() as path:
        db.set_tag_categories(path, {
            "Harry Potter": "character",
            "Harry James Potter": "character",
            "Harry Potter/Draco Malfoy": "relationship",
        })
        db.set_relationship_character(path, "Harry Potter/Draco Malfoy", 0, "Harry James Potter")

        _auto_link_relationship_characters()

        assert db.get_all_relationship_characters(path)["Harry Potter/Draco Malfoy"][0] == "Harry James Potter"


def test_auto_link_relationship_characters_skips_a_no_fandom_relationship():
    with _temp_db() as path:
        db.set_tag_categories(path, {
            "Harry Potter": "character",
            "Harry Potter/Draco Malfoy": "relationship",
        })
        db.set_tag_fandom(path, "Harry Potter/Draco Malfoy", "No Fandom")

        _auto_link_relationship_characters()

        assert db.get_all_relationship_characters(path) == {}


def test_auto_link_relationship_characters_no_op_when_nothing_matches():
    with _temp_db() as path:
        db.set_tag_categories(path, {
            "Some Character": "character",
            "Someone/Someone Else": "relationship",
        })
        _auto_link_relationship_characters()
        assert db.get_all_relationship_characters(path) == {}


def test_set_relationship_character_route_refuses_to_add_when_no_fandom():
    with _temp_db() as path:
        db.set_tag_fandom(path, "A/B", "No Fandom")

        set_relationship_character_route(
            relationship_tag="A/B", part_index=0, character_tag="A",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_relationship_characters(path) == {}


def test_set_relationship_character_route_still_allows_clearing_when_no_fandom():
    with _temp_db() as path:
        db.set_relationship_character(path, "A/B", 0, "A")
        db.set_tag_fandom(path, "A/B", "No Fandom")

        set_relationship_character_route(
            relationship_tag="A/B", part_index=0, character_tag="",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_relationship_characters(path) == {}


def test_apply_associations_adds_character_and_relationship_when_not_no_fandom():
    # Confirms the seeding helper itself actually resolves the tag to
    # "freeform" -- the no_fandom test below only means something if this
    # positive case adds the associations in the first place.
    with _temp_db() as path:
        _seed_candidate_tag(path, "Coffee Shop AU")
        _reclassify(path, {"Coffee Shop AU": "freeform"})

        apply_associations(
            tags=["Coffee Shop AU"], fandom="", character="Harry Potter", relationship="A/B", media_type="",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_freeform_characters(path) == {"Coffee Shop AU": {"Harry Potter"}}
        assert db.get_all_freeform_relationships(path) == {"Coffee Shop AU": {"A/B"}}


def test_apply_associations_skips_no_fandom_tags_for_character_and_relationship():
    with _temp_db() as path:
        _seed_candidate_tag(path, "Coffee Shop AU")
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.set_tag_fandom(path, "Coffee Shop AU", "No Fandom")

        apply_associations(
            tags=["Coffee Shop AU"], fandom="", character="Harry Potter", relationship="A/B", media_type="",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_freeform_characters(path) == {}
        assert db.get_all_freeform_relationships(path) == {}


def test_apply_associations_still_allows_fandom_on_a_no_fandom_tag():
    # Setting Fandom is how you'd correct a mistaken "No Fandom" in the
    # first place -- only Character/Relationship association is blocked.
    with _temp_db() as path:
        _seed_candidate_tag(path, "Coffee Shop AU")
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.set_tag_fandom(path, "Coffee Shop AU", "No Fandom")

        apply_associations(
            tags=["Coffee Shop AU"], fandom="Harry Potter", character="", relationship="", media_type="",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_fandoms(path)["Coffee Shop AU"] == "Harry Potter"


def test_apply_associations_bulk_sets_media_type_on_explicitly_classified_fandoms():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Doctor Who", "Harry Potter"])
        db.set_tag_categories(path, {"Doctor Who": "fandom", "Harry Potter": "fandom"})

        apply_associations(
            tags=["Doctor Who", "Harry Potter"], fandom="", character="", relationship="", media_type="TV Shows",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_media_types(path) == {"Doctor Who": {"TV Shows"}, "Harry Potter": {"TV Shows"}}


def test_apply_associations_bulk_adds_media_type_alongside_an_existing_one():
    # A Fandom can genuinely belong to more than one AO3-style category --
    # bulk-applying a second one adds to what's already there rather than
    # replacing it (replacing/clearing is what the per-row checkbox group
    # is for).
    with _temp_db() as path:
        _seed_candidate_tag(path, "Doctor Who")
        db.set_tag_categories(path, {"Doctor Who": "fandom"})
        db.set_tag_media_types(path, "Doctor Who", {"TV Shows"})

        apply_associations(
            tags=["Doctor Who"], fandom="", character="", relationship="", media_type="Books & Literature",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_media_types(path) == {"Doctor Who": {"TV Shows", "Books & Literature"}}


def test_apply_associations_skips_media_type_on_a_merely_guessed_fandom():
    # Only a tag *explicitly* classified Fandom -- not one that just
    # resolves to "fandom" via the heuristic guess -- can get a media type,
    # same restriction as the per-row control (see tags.html's own
    # "(classify to set a type)" hint for a merely-guessed one).
    with _temp_db() as path:
        entry = WorkEntry(work_id="1", fandoms=["Guessed Fandom"], fandom_candidates=["Guessed Fandom"])
        db.save_works_cache(path, [_entry_to_row(entry)])

        apply_associations(
            tags=["Guessed Fandom"], fandom="", character="", relationship="", media_type="TV Shows",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_media_types(path) == {}


def test_apply_associations_media_type_dont_change_leaves_existing_value_alone():
    with _temp_db() as path:
        _seed_candidate_tag(path, "Doctor Who")
        db.set_tag_categories(path, {"Doctor Who": "fandom"})
        db.set_tag_media_types(path, "Doctor Who", {"TV Shows"})

        apply_associations(
            tags=["Doctor Who"], fandom="", character="", relationship="", media_type="",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_media_types(path) == {"Doctor Who": {"TV Shows"}}


def test_wrangle_tags_types_a_brand_new_parent_when_all_children_agree():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Draco Malfoy"])
        db.set_tag_categories(path, {"Harry Potter": "character", "Draco Malfoy": "character"})

        wrangle_tags(tags=["Harry Potter", "Draco Malfoy"], relation="child", target="Hogwarts Students", **_WRANGLE_DEFAULTS)

        assert db.get_all_tag_categories(path)["Hogwarts Students"] == "character"
        assert db.get_tag_children(path)["Hogwarts Students"] == {"Harry Potter", "Draco Malfoy"}


def test_wrangle_tags_leaves_a_brand_new_parent_untyped_when_children_are_mixed():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Coffee Shop AU"])
        db.set_tag_categories(path, {"Harry Potter": "character", "Coffee Shop AU": "freeform"})

        wrangle_tags(tags=["Harry Potter", "Coffee Shop AU"], relation="child", target="Mixed Parent", **_WRANGLE_DEFAULTS)

        assert "Mixed Parent" not in db.get_all_tag_categories(path)


def test_wrangle_tags_recognizes_an_already_typed_virtual_parent_on_a_later_call():
    # The regression this guards: a virtual parent (zero real occurrences
    # of its own) typed by an earlier wrangle call used to look
    # uncategorized again on every later call, since _effective_tag_category
    # couldn't see a purely-virtual tag's own explicit category without
    # this fallback -- letting a different-category tag attach to it
    # after it had already been typed.
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Draco Malfoy", "Coffee Shop AU"])
        db.set_tag_categories(path, {
            "Harry Potter": "character", "Draco Malfoy": "character", "Coffee Shop AU": "freeform",
        })
        wrangle_tags(tags=["Harry Potter", "Draco Malfoy"], relation="child", target="Hogwarts Students", **_WRANGLE_DEFAULTS)
        assert db.get_all_tag_categories(path)["Hogwarts Students"] == "character"

        wrangle_tags(tags=["Coffee Shop AU"], relation="child", target="Hogwarts Students", **_WRANGLE_DEFAULTS)

        assert "Coffee Shop AU" not in db.get_tag_children(path).get("Hogwarts Students", set())


def test_wrangle_tags_typing_a_parent_as_relationship_triggers_auto_link():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "A/Harry Potter", "B/Harry Potter"])
        db.set_tag_categories(path, {
            "Harry Potter": "character",
            "A/Harry Potter": "relationship",
            "B/Harry Potter": "relationship",
        })

        wrangle_tags(
            tags=["A/Harry Potter", "B/Harry Potter"], relation="child", target="Harry Potter Ships",
            **_WRANGLE_DEFAULTS,
        )

        assert db.get_all_tag_categories(path)["Harry Potter Ships"] == "relationship"
        # The freshly-typed parent isn't itself a relationship with name
        # parts to link -- this just confirms typing it as "relationship"
        # didn't error out reaching for _auto_link_relationship_characters,
        # and its actual relationship children still resolve correctly.
        assert db.get_all_relationship_characters(path) == {
            "A/Harry Potter": {1: "Harry Potter"},
            "B/Harry Potter": {1: "Harry Potter"},
        }


def test_tag_rows_q_searches_the_whole_library_not_just_one_page():
    # The bug this guards: the Classify Tags search box used to only
    # filter rows already rendered on the current page (client-side JS),
    # so finding a tag on page 200 of 231 meant paging there by hand
    # first. _tag_rows' own q param instead narrows the whole library
    # before pagination ever happens.
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Draco Malfoy", "Hermione Granger"])
        result = scanner.load_cached(path)

        tags, _, _ = _tag_rows(result, "all", "count_desc", q="potter")

        assert [t for t, _, _ in tags] == ["Harry Potter"]


def test_tag_rows_q_is_case_insensitive():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter"])
        result = scanner.load_cached(path)

        tags, _, _ = _tag_rows(result, "all", "count_desc", q="POTTER")

        assert [t for t, _, _ in tags] == ["Harry Potter"]


def test_tag_rows_q_blank_is_a_no_op():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Draco Malfoy"])
        result = scanner.load_cached(path)

        tags, _, _ = _tag_rows(result, "all", "count_desc", q="")

        assert {t for t, _, _ in tags} == {"Harry Potter", "Draco Malfoy"}


def test_tag_rows_q_does_not_affect_bucket_counts():
    # bucket_counts (the filter-tab counts) stay whole-library totals
    # regardless of the current search, same convention as the Fandoms
    # page's media-type tab counts.
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Draco Malfoy"])
        _reclassify(path, {"Harry Potter": "character", "Draco Malfoy": "character"})
        result = scanner.load_cached(path)

        _, bucket_counts, total_tags = _tag_rows(result, "all", "count_desc", q="potter")

        assert bucket_counts["character"] == 2
        assert total_tags == 2


def test_tag_rows_q_searches_the_whole_library_even_from_a_narrower_filter_tab():
    # The bug this guards: typing into the search box used to search only
    # within whatever filter tab was currently open, so finding a tag of a
    # different category meant switching tabs first. q now overrides the
    # tab entirely -- a search is one library-wide lookup.
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Hermione Granger"])
        _reclassify(path, {"Harry Potter": "fandom", "Hermione Granger": "character"})
        result = scanner.load_cached(path)

        tags, _, _ = _tag_rows(result, "character", "count_desc", q="potter")

        assert [t for t, _, _ in tags] == ["Harry Potter"]


def test_remove_tag_categories_reverts_to_unclassified():
    with _temp_db() as path:
        db.set_tag_categories(path, {"Coffee Shop AU": "freeform", "Harry Potter": "fandom"})

        db.remove_tag_categories(path, ["Coffee Shop AU"])

        assert db.get_all_tag_categories(path) == {"Harry Potter": "fandom"}


def test_remove_tag_categories_empty_list_is_a_no_op():
    with _temp_db() as path:
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.remove_tag_categories(path, [])
        assert db.get_all_tag_categories(path) == {"Coffee Shop AU": "freeform"}


def test_set_selected_tags_unclassify_reverts_selected_tags():
    with _temp_db() as path:
        db.set_tag_categories(path, {"Coffee Shop AU": "freeform", "Harry Potter": "fandom"})

        set_selected_tags(
            tags=["Coffee Shop AU"], category="unclassify", filter="all", page=1, sort="count_desc",
            work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_categories(path) == {"Harry Potter": "fandom"}


def test_set_selected_tags_unclassify_with_no_tags_selected_is_a_no_op():
    with _temp_db() as path:
        _reclassify(path, {"Coffee Shop AU": "freeform"})

        set_selected_tags(
            tags=[], category="unclassify", filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_categories(path) == {"Coffee Shop AU": "freeform"}


def test_set_tag_verified_round_trip():
    with _temp_db() as path:
        assert db.get_all_verified_tags(path) == set()

        db.set_tag_verified(path, "Harry Potter", True)
        assert db.get_all_verified_tags(path) == {"Harry Potter"}

        db.set_tag_verified(path, "Harry Potter", False)
        assert db.get_all_verified_tags(path) == set()


def test_set_tag_verified_is_independent_of_classification():
    # Verified is a personal checklist over classification, not part of it
    # -- reclassifying a tag doesn't clear its verified flag.
    with _temp_db() as path:
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.set_tag_verified(path, "Coffee Shop AU", True)

        db.set_tag_categories(path, {"Coffee Shop AU": "fandom"})

        assert db.get_all_verified_tags(path) == {"Coffee Shop AU"}


def test_set_tag_verified_route_checks_and_unchecks():
    with _temp_db() as path:
        set_tag_verified_route(
            tag="Harry Potter", verified=True, filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )
        assert db.get_all_verified_tags(path) == {"Harry Potter"}

        set_tag_verified_route(
            tag="Harry Potter", verified=False, filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )
        assert db.get_all_verified_tags(path) == set()


def test_unverified_drops_verified_tags_from_a_list():
    assert _unverified(["A", "B", "C"], {"B"}) == ["A", "C"]
    assert _unverified(["A", "B"], set()) == ["A", "B"]
    assert _unverified([], {"A"}) == []


def test_set_tag_fandom_route_refuses_on_a_verified_tag():
    with _temp_db() as path:
        db.set_tag_categories(path, {"Hermione Granger": "character"})
        db.set_tag_verified(path, "Hermione Granger", True)

        set_tag_fandom_route(
            tag="Hermione Granger", fandom="Harry Potter", filter="all", page=1, sort="count_desc",
            work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_fandoms(path) == {}


def test_set_tag_fandom_route_allows_it_once_unverified():
    with _temp_db() as path:
        db.set_tag_categories(path, {"Hermione Granger": "character"})

        set_tag_fandom_route(
            tag="Hermione Granger", fandom="Harry Potter", filter="all", page=1, sort="count_desc",
            work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_fandoms(path) == {"Hermione Granger": "Harry Potter"}


def test_set_tag_media_type_route_refuses_on_a_verified_tag():
    with _temp_db() as path:
        db.set_tag_categories(path, {"Doctor Who": "fandom"})
        db.set_tag_verified(path, "Doctor Who", True)

        set_tag_media_type_route(
            tag="Doctor Who", media_type=["TV Shows"], filter="all", page=1, sort="count_desc",
            work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_media_types(path) == {}


def test_remove_freeform_character_route_refuses_on_a_verified_tag():
    with _temp_db() as path:
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.add_freeform_character(path, "Coffee Shop AU", "Harry Potter")
        db.set_tag_verified(path, "Coffee Shop AU", True)

        remove_freeform_character_route(
            freeform_tag="Coffee Shop AU", character_tag="Harry Potter", filter="all", page=1,
            sort="count_desc", work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_freeform_characters(path) == {"Coffee Shop AU": {"Harry Potter"}}


def test_remove_freeform_relationship_route_refuses_on_a_verified_tag():
    with _temp_db() as path:
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.add_freeform_relationship(path, "Coffee Shop AU", "A/B")
        db.set_tag_verified(path, "Coffee Shop AU", True)

        remove_freeform_relationship_route(
            freeform_tag="Coffee Shop AU", relationship_tag="A/B", filter="all", page=1,
            sort="count_desc", work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_freeform_relationships(path) == {"Coffee Shop AU": {"A/B"}}


def test_set_relationship_character_route_refuses_on_a_verified_relationship_even_to_clear():
    with _temp_db() as path:
        db.set_tag_categories(path, {"A/B": "relationship"})
        db.set_relationship_character(path, "A/B", 0, "A")
        db.set_tag_verified(path, "A/B", True)

        set_relationship_character_route(
            relationship_tag="A/B", part_index=0, character_tag="",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        # Even clearing (normally always allowed) is refused while Verified.
        assert db.get_all_relationship_characters(path) == {"A/B": {0: "A"}}


def test_apply_associations_skips_a_verified_tag_in_the_batch():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Coffee Shop AU", "Angst"])
        db.set_tag_categories(path, {"Coffee Shop AU": "freeform", "Angst": "freeform"})
        db.set_tag_verified(path, "Coffee Shop AU", True)

        apply_associations(
            tags=["Coffee Shop AU", "Angst"], fandom="", character="Harry Potter", relationship="", media_type="",
            filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_freeform_characters(path) == {"Angst": {"Harry Potter"}}


def test_set_selected_tags_skips_a_verified_tag_when_reclassifying():
    with _temp_db() as path:
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.set_tag_verified(path, "Coffee Shop AU", True)

        set_selected_tags(
            tags=["Coffee Shop AU"], category="fandom", filter="all", page=1, sort="count_desc",
            work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_categories(path) == {"Coffee Shop AU": "freeform"}


def test_set_selected_tags_skips_a_verified_tag_when_unclassifying():
    with _temp_db() as path:
        _reclassify(path, {"Coffee Shop AU": "freeform"})
        db.set_tag_verified(path, "Coffee Shop AU", True)

        set_selected_tags(
            tags=["Coffee Shop AU"], category="unclassify", filter="all", page=1, sort="count_desc",
            work_id="", q="", show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_categories(path) == {"Coffee Shop AU": "freeform"}


def test_wrangle_tags_skips_a_verified_tag_being_wrangled():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Draco Malfoy"])
        db.set_tag_categories(path, {"Harry Potter": "character", "Draco Malfoy": "character"})
        db.set_tag_verified(path, "Draco Malfoy", True)

        wrangle_tags(
            tags=["Harry Potter", "Draco Malfoy"], relation="child", target="Hogwarts Students",
            **_WRANGLE_DEFAULTS,
        )

        assert db.get_tag_children(path).get("Hogwarts Students", set()) == {"Harry Potter"}


def test_wrangle_tags_does_not_auto_type_a_verified_target():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Harry Potter", "Draco Malfoy"])
        db.set_tag_categories(path, {"Harry Potter": "character", "Draco Malfoy": "character"})
        db.set_tag_verified(path, "Hogwarts Students", True)

        wrangle_tags(
            tags=["Harry Potter", "Draco Malfoy"], relation="child", target="Hogwarts Students",
            **_WRANGLE_DEFAULTS,
        )

        # The children still attach (they aren't the ones locked)...
        assert db.get_tag_children(path)["Hogwarts Students"] == {"Harry Potter", "Draco Malfoy"}
        # ...but the Verified target itself doesn't get auto-typed.
        assert "Hogwarts Students" not in db.get_all_tag_categories(path)


def test_unwrangle_tag_refuses_on_a_verified_tag():
    with _temp_db() as path:
        db.set_tag_wrangling(path, "Ianto Jones", "child", "Torchwood")
        db.set_tag_verified(path, "Ianto Jones", True)

        unwrangle_tag(tag="Ianto Jones")

        assert db.get_all_tag_wranglings(path) == {"Ianto Jones": ("child", "Torchwood")}


def test_mark_page_freeform_skips_a_verified_unclassified_tag():
    with _temp_db() as path:
        db.set_tag_verified(path, "Weird Tag", True)

        mark_page_freeform(
            tags=["Weird Tag", "Other Tag"], filter="all", page=1, sort="count_desc", work_id="", q="",
            show_guessed=False, show_set=False, incomplete_only=False,
        )

        assert db.get_all_tag_categories(path) == {"Other Tag": "freeform"}


def test_mark_all_unclassified_freeform_skips_a_verified_unclassified_tag():
    with _temp_db() as path:
        _seed_candidate_tags(path, ["Weird Tag", "Other Tag"])
        db.set_tag_verified(path, "Weird Tag", True)

        mark_all_unclassified_freeform(work_id="")

        assert db.get_all_tag_categories(path) == {"Other Tag": "freeform"}
