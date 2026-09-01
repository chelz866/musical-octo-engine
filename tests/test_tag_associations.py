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

from app import db, main as main_module
from app.main import (
    _auto_link_relationship_characters,
    _tag_has_no_fandom,
    add_freeform_character_route,
    add_freeform_relationship_route,
    apply_associations,
    set_relationship_character_route,
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


_WRANGLE_DEFAULTS = dict(filter="all", page=1, sort="count_desc", work_id="")


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
            filter="all", page=1, sort="count_desc", work_id="",
        )

        assert db.get_all_relationship_characters(path) == {}


def test_set_relationship_character_route_still_allows_clearing_when_no_fandom():
    with _temp_db() as path:
        db.set_relationship_character(path, "A/B", 0, "A")
        db.set_tag_fandom(path, "A/B", "No Fandom")

        set_relationship_character_route(
            relationship_tag="A/B", part_index=0, character_tag="",
            filter="all", page=1, sort="count_desc", work_id="",
        )

        assert db.get_all_relationship_characters(path) == {}


def test_add_freeform_character_route_refuses_when_no_fandom():
    with _temp_db() as path:
        db.set_tag_fandom(path, "Coffee Shop AU", "No Fandom")

        add_freeform_character_route(
            freeform_tag="Coffee Shop AU", character_tag="Harry Potter",
            filter="all", page=1, sort="count_desc", work_id="",
        )

        assert db.get_all_freeform_characters(path) == {}


def test_add_freeform_character_route_allows_it_without_no_fandom():
    with _temp_db() as path:
        add_freeform_character_route(
            freeform_tag="Coffee Shop AU", character_tag="Harry Potter",
            filter="all", page=1, sort="count_desc", work_id="",
        )
        assert db.get_all_freeform_characters(path) == {"Coffee Shop AU": {"Harry Potter"}}


def test_add_freeform_relationship_route_refuses_when_no_fandom():
    with _temp_db() as path:
        db.set_tag_fandom(path, "Coffee Shop AU", "No Fandom")

        add_freeform_relationship_route(
            freeform_tag="Coffee Shop AU", relationship_tag="A/B",
            filter="all", page=1, sort="count_desc", work_id="",
        )

        assert db.get_all_freeform_relationships(path) == {}


def test_apply_associations_adds_character_and_relationship_when_not_no_fandom():
    # Confirms the seeding helper itself actually resolves the tag to
    # "freeform" -- the no_fandom test below only means something if this
    # positive case adds the associations in the first place.
    with _temp_db() as path:
        _seed_candidate_tag(path, "Coffee Shop AU")
        db.set_tag_categories(path, {"Coffee Shop AU": "freeform"})

        apply_associations(
            tags=["Coffee Shop AU"], fandom="", character="Harry Potter", relationship="A/B",
            filter="all", page=1, sort="count_desc", work_id="",
        )

        assert db.get_all_freeform_characters(path) == {"Coffee Shop AU": {"Harry Potter"}}
        assert db.get_all_freeform_relationships(path) == {"Coffee Shop AU": {"A/B"}}


def test_apply_associations_skips_no_fandom_tags_for_character_and_relationship():
    with _temp_db() as path:
        _seed_candidate_tag(path, "Coffee Shop AU")
        db.set_tag_categories(path, {"Coffee Shop AU": "freeform"})
        db.set_tag_fandom(path, "Coffee Shop AU", "No Fandom")

        apply_associations(
            tags=["Coffee Shop AU"], fandom="", character="Harry Potter", relationship="A/B",
            filter="all", page=1, sort="count_desc", work_id="",
        )

        assert db.get_all_freeform_characters(path) == {}
        assert db.get_all_freeform_relationships(path) == {}


def test_apply_associations_still_allows_fandom_on_a_no_fandom_tag():
    # Setting Fandom is how you'd correct a mistaken "No Fandom" in the
    # first place -- only Character/Relationship association is blocked.
    with _temp_db() as path:
        _seed_candidate_tag(path, "Coffee Shop AU")
        db.set_tag_categories(path, {"Coffee Shop AU": "freeform"})
        db.set_tag_fandom(path, "Coffee Shop AU", "No Fandom")

        apply_associations(
            tags=["Coffee Shop AU"], fandom="Harry Potter", character="", relationship="",
            filter="all", page=1, sort="count_desc", work_id="",
        )

        assert db.get_all_tag_fandoms(path)["Coffee Shop AU"] == "Harry Potter"


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
