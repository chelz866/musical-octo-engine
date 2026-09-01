from collections import Counter
from datetime import datetime

from app import main as main_module
from app.main import (
    DESCENDING_SORTS,
    EXCLUDE_FACETS,
    FACETS,
    SORT_OPTIONS,
    _active_chips,
    _build_autocomplete_index,
    _completion_status,
    _effective_tag_category,
    _entry_matches,
    _facet_suggestions,
    _filter_by_media_type,
    _filter_query_string,
    _parse_date,
    _parse_manual_links,
    _selected_with_counts,
    _shared_child_category,
    _add_virtual_parent_counts,
    _all_descendants,
    _association_parents,
    _build_fandom_scope,
    _expand_children_transitively,
    _filter_by_letter,
    _flatten_tag_options,
    _group_tag_rows_by_association,
    _group_tag_rows_by_parent,
    _series_sort_key,
    _value_or_children_present,
    _sort_name_count_rows,
    _static_facet_counts,
    blurb_tag_line,
    local_time,
    paginate,
    sanitize_style_content,
    translate_ao3_skin_selectors,
)
from app.scanner import WorkEntry


def _filters(facets=None, exclude=None, word_min=None, word_max=None, crossover=None,
             date_from=None, date_to=None, bookmarked=False, unread=False, q="", sort="title", children=None,
             fandom_scope=None):
    return {
        "facets": {name: [] for name in FACETS} | (facets or {}),
        "exclude": {name: [] for name in EXCLUDE_FACETS} | (exclude or {}),
        "word_min": word_min,
        "word_max": word_max,
        "crossover": crossover,
        "date_from": date_from,
        "date_to": date_to,
        "bookmarked": bookmarked,
        "unread": unread,
        "fandom_scope": fandom_scope or {},
        "q": q,
        "sort": sort,
        "children": children or {},
    }


def test_paginate_first_page():
    items, page, total_pages = paginate(list(range(60)), page=1, page_size=25)
    assert items == list(range(0, 25))
    assert page == 1
    assert total_pages == 3


def test_paginate_last_partial_page():
    items, page, total_pages = paginate(list(range(60)), page=3, page_size=25)
    assert items == list(range(50, 60))
    assert page == 3
    assert total_pages == 3


def test_paginate_clamps_page_below_one():
    items, page, total_pages = paginate(list(range(60)), page=0, page_size=25)
    assert page == 1
    assert items == list(range(0, 25))


def test_paginate_clamps_page_past_the_end():
    items, page, total_pages = paginate(list(range(60)), page=99, page_size=25)
    assert page == 3
    assert items == list(range(50, 60))


def test_paginate_empty_list_never_divides_by_zero():
    items, page, total_pages = paginate([], page=1, page_size=25)
    assert items == []
    assert page == 1
    assert total_pages == 1


def test_paginate_everything_fits_on_one_page():
    items, page, total_pages = paginate(list(range(10)), page=1, page_size=25)
    assert items == list(range(10))
    assert total_pages == 1


def test_completion_status_unknown_when_no_chapters_have():
    assert _completion_status(WorkEntry(work_id="1")) == "unknown"


def test_completion_status_wip_when_total_not_committed():
    assert _completion_status(WorkEntry(work_id="1", chapters_have=3, chapters_total=None)) == "wip"


def test_completion_status_wip_when_below_total():
    assert _completion_status(WorkEntry(work_id="1", chapters_have=3, chapters_total=12)) == "wip"


def test_completion_status_complete_when_reached_total():
    assert _completion_status(WorkEntry(work_id="1", chapters_have=12, chapters_total=12)) == "complete"


def test_entry_matches_no_filters_is_a_no_op():
    entry = WorkEntry(work_id="1", title="Anything")
    assert _entry_matches(entry, _filters()) is True


def test_entry_matches_ands_across_facets():
    entry = WorkEntry(work_id="1", rating="Explicit", categories=["Gen"])
    matching = _filters(facets={"rating": ["Explicit"], "category": ["Gen"]})
    assert _entry_matches(entry, matching) is True

    non_matching = _filters(facets={"rating": ["Explicit"], "category": ["F/M"]})
    assert _entry_matches(entry, non_matching) is False


def test_entry_matches_include_ands_within_one_facet():
    # Real AO3 semantics: checking two values in the same Include facet
    # requires a work to have BOTH, not either.
    entry = WorkEntry(work_id="1", freeform_tags=["Angst", "Fluff"])
    both = _filters(facets={"freeform": ["Angst", "Fluff"]})
    assert _entry_matches(entry, both) is True

    only_one = WorkEntry(work_id="2", freeform_tags=["Angst"])
    assert _entry_matches(only_one, both) is False


def test_entry_matches_include_and_on_single_valued_facet_always_fails_with_two_selected():
    # The accepted AO3 quirk: Rating is single-valued per work, so AND-ing
    # two selected ratings can never match anything -- same as real AO3.
    entry = WorkEntry(work_id="1", rating="Mature")
    filters = _filters(facets={"rating": ["Explicit", "Mature"]})
    assert _entry_matches(entry, filters) is False


def test_entry_matches_exclude_ors_within_one_facet():
    entry = WorkEntry(work_id="1", characters=["Voldemort"])
    filters = _filters(exclude={"character": ["Voldemort", "Umbridge"]})
    assert _entry_matches(entry, filters) is False

    other = WorkEntry(work_id="2", characters=["Harry Potter"])
    assert _entry_matches(other, filters) is True


def test_entry_matches_skip_include_skips_only_that_facets_include_constraint():
    entry = WorkEntry(work_id="1", rating="Mature")
    filters = _filters(facets={"rating": ["Explicit"]})
    assert _entry_matches(entry, filters, skip_include="rating") is True
    assert _entry_matches(entry, filters) is False


def test_entry_matches_skip_exclude_skips_only_that_facets_exclude_constraint():
    entry = WorkEntry(work_id="1", characters=["Voldemort"])
    filters = _filters(exclude={"character": ["Voldemort"]})
    assert _entry_matches(entry, filters, skip_exclude="character") is True
    assert _entry_matches(entry, filters) is False


def test_entry_matches_skip_include_and_skip_exclude_are_independent():
    # Both an Include and an Exclude can be active on the same facet name
    # at once -- skipping one shouldn't skip the other.
    entry = WorkEntry(work_id="1", fandoms=["Harry Potter"])
    filters = _filters(facets={"fandom": ["Torchwood"]}, exclude={"fandom": ["Harry Potter"]})
    assert _entry_matches(entry, filters, skip_include="fandom") is False  # exclude still applies
    assert _entry_matches(entry, filters, skip_exclude="fandom") is False  # include still applies
    assert _entry_matches(entry, filters, skip_include="fandom", skip_exclude="fandom") is True


def test_value_or_children_present_matches_the_value_itself():
    assert _value_or_children_present("Alternate Reality", {"Alternate Reality"}, {}) is True


def test_value_or_children_present_matches_a_child_tag():
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    assert _value_or_children_present("Alternate Reality", {"Alternate Reality - Canon Divergence"}, children) is True


def test_value_or_children_present_false_when_neither_present():
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    assert _value_or_children_present("Alternate Reality", {"Fluff"}, children) is False


def test_entry_matches_include_expands_parent_to_match_child_tag():
    # Selecting "Alternate Reality" (the parent) should also match a work
    # only tagged with one of its children -- AO3-style tag wrangling.
    entry = WorkEntry(work_id="1", freeform_tags=["Alternate Reality - Canon Divergence"])
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    filters = _filters(facets={"freeform": ["Alternate Reality"]}, children=children)
    assert _entry_matches(entry, filters) is True


def test_entry_matches_exclude_expands_parent_to_drop_child_tag():
    entry = WorkEntry(work_id="1", freeform_tags=["Alternate Reality - Canon Divergence"])
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    filters = _filters(exclude={"freeform": ["Alternate Reality"]}, children=children)
    assert _entry_matches(entry, filters) is False


def test_entry_matches_children_absent_is_a_no_op():
    entry = WorkEntry(work_id="1", freeform_tags=["Fluff"])
    filters = _filters(facets={"freeform": ["Alternate Reality"]})
    assert _entry_matches(entry, filters) is False


def test_entry_matches_crossover_only():
    crossover_entry = WorkEntry(work_id="1", fandoms=["Harry Potter", "Torchwood"])
    single_fandom_entry = WorkEntry(work_id="2", fandoms=["Harry Potter"])
    filters = _filters(crossover="only")
    assert _entry_matches(crossover_entry, filters) is True
    assert _entry_matches(single_fandom_entry, filters) is False


def test_entry_matches_crossover_exclude():
    crossover_entry = WorkEntry(work_id="1", fandoms=["Harry Potter", "Torchwood"])
    single_fandom_entry = WorkEntry(work_id="2", fandoms=["Harry Potter"])
    filters = _filters(crossover="exclude")
    assert _entry_matches(crossover_entry, filters) is False
    assert _entry_matches(single_fandom_entry, filters) is True


def test_entry_matches_date_bounds_are_inclusive():
    entry = WorkEntry(work_id="1", mtime=datetime(2024, 6, 15))
    assert _entry_matches(entry, _filters(date_from=datetime(2024, 6, 15).date(), date_to=datetime(2024, 6, 15).date())) is True
    assert _entry_matches(entry, _filters(date_from=datetime(2024, 6, 16).date())) is False
    assert _entry_matches(entry, _filters(date_to=datetime(2024, 6, 14).date())) is False


def test_entry_matches_date_bound_fails_for_entry_with_no_timestamp():
    entry = WorkEntry(work_id="1")
    assert _entry_matches(entry, _filters(date_from=datetime(2024, 1, 1).date())) is False


def test_parse_date_valid():
    assert _parse_date("2024-06-15") == datetime(2024, 6, 15).date()


def test_parse_date_invalid_or_blank_returns_none():
    assert _parse_date("not-a-date") is None
    assert _parse_date("") is None
    assert _parse_date(None) is None


def test_parse_manual_links_extracts_work_ids_one_per_line():
    text = "https://archiveofourown.org/works/111\nhttps://archiveofourown.org/works/222/chapters/999"

    assert _parse_manual_links(text) == [
        ("111", "https://archiveofourown.org/works/111"),
        ("222", "https://archiveofourown.org/works/222"),
    ]


def test_parse_manual_links_handles_a_comma_separated_or_pasted_paragraph():
    text = "check out https://archiveofourown.org/works/111, and also archiveofourown.org/works/222 !"

    assert [work_id for work_id, _ in _parse_manual_links(text)] == ["111", "222"]


def test_parse_manual_links_dedupes_by_work_id_keeping_first_occurrence():
    text = "https://archiveofourown.org/works/111 https://archiveofourown.org/works/111/chapters/5"

    assert _parse_manual_links(text) == [("111", "https://archiveofourown.org/works/111")]


def test_parse_manual_links_returns_empty_list_for_no_matches():
    assert _parse_manual_links("nothing useful here") == []


def test_local_time_returns_empty_string_for_none_or_blank():
    assert local_time(None, "America/New_York") == ""
    assert local_time("", "America/New_York") == ""


def test_local_time_returns_server_time_unchanged_when_no_zone_chosen():
    assert local_time(datetime(2026, 1, 15, 12, 0), None) == "2026-01-15 12:00"


def test_local_time_accepts_an_iso_string_as_well_as_a_datetime():
    assert local_time("2026-01-15T12:00:00", None) == "2026-01-15 12:00"


def test_local_time_falls_back_to_server_time_for_an_unresolvable_zone_name():
    assert local_time(datetime(2026, 1, 15, 12, 0), "Not/AZone") == "2026-01-15 12:00"


def test_local_time_accepts_a_custom_format():
    assert local_time(datetime(2026, 1, 15, 12, 30), None, "%Y-%m-%d") == "2026-01-15"


def test_local_time_converts_from_server_zone_honoring_winter_and_summer_dst():
    # SERVER_TZ_NAME defaults to "UTC" unless a TZ env var overrides it --
    # pinned here so the test doesn't depend on whatever's ambient.
    original = main_module.SERVER_TZ_NAME
    main_module.SERVER_TZ_NAME = "UTC"
    try:
        # Noon EST (winter, UTC-5) and noon EDT (summer, UTC-4) are
        # different UTC instants -- a fixed-offset conversion would get
        # one of these two wrong, which is exactly what ZoneInfo avoids.
        assert local_time(datetime(2026, 1, 15, 17, 0), "America/New_York") == "2026-01-15 12:00"
        assert local_time(datetime(2026, 7, 15, 16, 0), "America/New_York") == "2026-07-15 12:00"
    finally:
        main_module.SERVER_TZ_NAME = original


def test_entry_matches_word_count_bounds_are_inclusive():
    entry = WorkEntry(work_id="1", word_count=1000)
    assert _entry_matches(entry, _filters(word_min=1000, word_max=1000)) is True
    assert _entry_matches(entry, _filters(word_min=1001)) is False
    assert _entry_matches(entry, _filters(word_max=999)) is False


def test_entry_matches_query_searches_title_author_summary_and_tags():
    entry = WorkEntry(
        work_id="1", title="A Title", author="Some Author", summary="A summary",
        fandoms=["Torchwood"], characters=["Ianto Jones"],
        relationships=["Ianto Jones/Jack Harkness"], freeform_tags=["Angst"],
    )
    assert _entry_matches(entry, _filters(q="ianto jones")) is True
    assert _entry_matches(entry, _filters(q="angst")) is True
    assert _entry_matches(entry, _filters(q="torchwood")) is True
    assert _entry_matches(entry, _filters(q="nonexistent")) is False
    assert _entry_matches(entry, _filters(q="")) is True


def test_facet_suggestions_excludes_already_selected():
    entries = [
        WorkEntry(work_id="1", freeform_tags=["Angst"]),
        WorkEntry(work_id="2", freeform_tags=["Angst"]),
        WorkEntry(work_id="3", freeform_tags=["Fluff"]),
    ]
    filters = _filters(facets={"freeform": ["Angst"]})
    suggestions = _facet_suggestions(entries, filters, "freeform")
    assert suggestions == [("Fluff", 1)]


def test_facet_suggestions_sorts_by_count_desc_then_name():
    entries = [
        WorkEntry(work_id="1", freeform_tags=["Fluff"]),
        WorkEntry(work_id="2", freeform_tags=["Angst"]),
        WorkEntry(work_id="3", freeform_tags=["Angst"]),
    ]
    suggestions = _facet_suggestions(entries, _filters(), "freeform")
    assert suggestions == [("Angst", 2), ("Fluff", 1)]


def test_facet_suggestions_truncates_to_top_n():
    entries = [WorkEntry(work_id=str(i), freeform_tags=[f"Tag {i}"]) for i in range(15)]
    suggestions = _facet_suggestions(entries, _filters(), "freeform", top_n=10)
    assert len(suggestions) == 10


def test_facet_suggestions_selecting_within_facet_does_not_shrink_its_own_pool():
    entries = [
        WorkEntry(work_id="1", freeform_tags=["Angst", "Fluff"]),
        WorkEntry(work_id="2", freeform_tags=["Fluff"]),
    ]
    # Selecting "Angst" narrows the *result* list to just work 1, but the
    # suggestion pool for the freeform facet itself should still consider
    # every entry (both), not just the one that already matches "Angst".
    filters = _filters(facets={"freeform": ["Angst"]})
    suggestions = _facet_suggestions(entries, filters, "freeform")
    assert suggestions == [("Fluff", 2)]


def test_facet_suggestions_narrowed_by_another_active_facet():
    entries = [
        WorkEntry(work_id="1", rating="Explicit", freeform_tags=["Angst"]),
        WorkEntry(work_id="2", rating="General Audiences", freeform_tags=["Fluff"]),
    ]
    filters = _filters(facets={"rating": ["Explicit"]})
    suggestions = _facet_suggestions(entries, filters, "freeform")
    assert suggestions == [("Angst", 1)]


def test_selected_with_counts_reports_zero_for_a_selected_value_with_no_matches():
    entries = [WorkEntry(work_id="1", freeform_tags=["Fluff"])]
    filters = _filters(facets={"freeform": ["Angst"]})
    assert _selected_with_counts(entries, filters, "freeform") == [("Angst", 0)]


def test_static_facet_counts_lists_every_option_with_counts_and_checked_state():
    entries = [WorkEntry(work_id="1", rating="Explicit"), WorkEntry(work_id="2", rating="Explicit")]
    filters = _filters(facets={"rating": ["Explicit"]})
    options = [("Explicit", "Explicit"), ("Mature", "Mature")]
    result = _static_facet_counts(entries, filters, "rating", options)
    assert result == [("Explicit", "Explicit", 2, True), ("Mature", "Mature", 0, False)]


def test_facet_suggestions_exclude_mode_reads_the_exclude_dict():
    entries = [
        WorkEntry(work_id="1", characters=["Voldemort"]),
        WorkEntry(work_id="2", characters=["Voldemort"]),
        WorkEntry(work_id="3", characters=["Umbridge"]),
    ]
    filters = _filters(exclude={"character": ["Voldemort"]})
    suggestions = _facet_suggestions(entries, filters, "character", mode="exclude")
    assert suggestions == [("Umbridge", 1)]


def test_build_fandom_scope_uses_a_tags_own_explicit_association():
    scope = _build_fandom_scope({}, {"The Doctor": "Doctor Who"})
    assert scope == {"The Doctor": "Doctor Who"}


def test_build_fandom_scope_no_fandom_association_is_excluded():
    scope = _build_fandom_scope({}, {"Coffee Shops": "No Fandom"})
    assert scope == {}


def test_build_fandom_scope_inherits_down_a_same_category_chain():
    # "Ron Weasley (Auror)" is a same-category child of "Ron Weasley" --
    # it has no Fandom of its own, so it inherits "Ron Weasley"'s.
    children = {"Ron Weasley": {"Ron Weasley (Auror)"}}
    scope = _build_fandom_scope(children, {"Ron Weasley": "Harry Potter"})
    assert scope["Ron Weasley (Auror)"] == "Harry Potter"
    assert scope["Ron Weasley"] == "Harry Potter"


def test_build_fandom_scope_childs_own_explicit_association_overrides_inherited():
    # "Anxious Character" (parent, no fandom) -> "Anxious Shane Hollander"
    # (child, its own explicit Fandom) -- the child's own choice wins.
    children = {"Anxious Character": {"Anxious Shane Hollander"}}
    explicit = {"Anxious Character": "No Fandom", "Anxious Shane Hollander": "Heated Rivalry"}
    scope = _build_fandom_scope(children, explicit)
    assert scope["Anxious Shane Hollander"] == "Heated Rivalry"
    assert "Anxious Character" not in scope


def test_build_fandom_scope_walks_multiple_levels_up_to_the_fandom():
    # Character -> Character -> Character, a real three-level chain --
    # the great-grandchild's scope should resolve all the way up, not
    # stop at its own direct parent.
    children = {
        "Ron Weasley": {"Ron Weasley (Auror)"},
        "Ron Weasley (Auror)": {"Ron Weasley (Auror, Injured)"},
    }
    scope = _build_fandom_scope(children, {"Ron Weasley": "Harry Potter"})
    assert scope["Ron Weasley (Auror, Injured)"] == "Harry Potter"


def test_build_fandom_scope_chain_that_never_reaches_a_fandom_has_no_scope():
    children = {"Alternate Universe": {"Coffee Shop AU"}, "Coffee Shop AU": {"Barista AU"}}
    scope = _build_fandom_scope(children, {})
    assert "Barista AU" not in scope
    assert "Coffee Shop AU" not in scope


def test_all_descendants_walks_multiple_levels():
    children = {
        "Harry Potter": {"Harry Potter/Hermione Granger"},
        "Harry Potter/Hermione Granger": {"Hermione Granger"},
    }
    assert _all_descendants("Harry Potter", children) == {"Harry Potter/Hermione Granger", "Hermione Granger"}


def test_all_descendants_of_a_leaf_is_empty():
    children = {"Harry Potter": {"Hermione Granger"}}
    assert _all_descendants("Hermione Granger", children) == set()


def test_expand_children_transitively_flattens_the_whole_chain_per_parent():
    children = {
        "Harry Potter": {"Harry Potter/Hermione Granger"},
        "Harry Potter/Hermione Granger": {"Hermione Granger"},
    }
    expanded = _expand_children_transitively(children)
    assert expanded["Harry Potter"] == {"Harry Potter/Hermione Granger", "Hermione Granger"}
    assert expanded["Harry Potter/Hermione Granger"] == {"Hermione Granger"}


def test_value_or_children_present_matches_a_grandchild_via_expanded_map():
    children = _expand_children_transitively({
        "Harry Potter": {"Harry Potter/Hermione Granger"},
        "Harry Potter/Hermione Granger": {"Hermione Granger"},
    })
    assert _value_or_children_present("Harry Potter", {"Hermione Granger"}, children) is True


def test_add_virtual_parent_counts_reaches_grandchildren_via_expanded_map():
    entries = [WorkEntry(work_id="1", fandom_candidates=["Hermione Granger"])]
    counts = Counter({"Hermione Granger": 1})
    expanded = _expand_children_transitively({
        "Sci-Fi Ships": {"Harry Potter/Hermione Granger"},
        "Harry Potter/Hermione Granger": {"Hermione Granger"},
    })
    _add_virtual_parent_counts(counts, entries, lambda e: e.fandom_candidates, expanded)
    assert counts["Sci-Fi Ships"] == 1


def test_facet_suggestions_drops_a_tag_scoped_to_a_different_fandom():
    entries = [
        WorkEntry(work_id="1", fandoms=["Harry Potter"], characters=["Hermione Granger"]),
        WorkEntry(work_id="2", fandoms=["Doctor Who"], characters=["The Doctor"]),
    ]
    filters = _filters(facets={"fandom": ["Harry Potter"]}, fandom_scope={"The Doctor": "Doctor Who"})
    suggestions = _facet_suggestions(entries, filters, "character")
    assert suggestions == [("Hermione Granger", 1)]


def test_facet_suggestions_keeps_a_tag_scoped_to_the_selected_fandom():
    entries = [WorkEntry(work_id="1", fandoms=["Doctor Who"], characters=["The Doctor"])]
    filters = _filters(facets={"fandom": ["Doctor Who"]}, fandom_scope={"The Doctor": "Doctor Who"})
    suggestions = _facet_suggestions(entries, filters, "character")
    assert suggestions == [("The Doctor", 1)]


def test_facet_suggestions_keeps_an_unscoped_tag_regardless_of_selected_fandom():
    entries = [WorkEntry(work_id="1", fandoms=["Harry Potter"], freeform_tags=["Coffee Shops"])]
    filters = _filters(facets={"fandom": ["Harry Potter"]}, fandom_scope={"The Doctor": "Doctor Who"})
    suggestions = _facet_suggestions(entries, filters, "freeform")
    assert suggestions == [("Coffee Shops", 1)]


def test_facet_suggestions_fandom_scope_is_a_no_op_when_no_fandom_is_selected():
    entries = [WorkEntry(work_id="1", fandoms=["Doctor Who"], characters=["The Doctor"])]
    filters = _filters(fandom_scope={"The Doctor": "Doctor Who"})
    suggestions = _facet_suggestions(entries, filters, "character")
    assert suggestions == [("The Doctor", 1)]


def test_facet_suggestions_fandom_scope_does_not_apply_to_the_fandom_facet_itself():
    entries = [WorkEntry(work_id="1", fandoms=["Doctor Who"])]
    filters = _filters(facets={"fandom": ["Harry Potter"]}, fandom_scope={"Doctor Who": "Harry Potter"})
    suggestions = _facet_suggestions(entries, filters, "fandom")
    assert suggestions == [("Doctor Who", 1)]


def test_selected_with_counts_exclude_mode():
    entries = [WorkEntry(work_id="1", characters=["Umbridge"])]
    filters = _filters(exclude={"character": ["Voldemort"]})
    assert _selected_with_counts(entries, filters, "character", mode="exclude") == [("Voldemort", 0)]


def test_static_facet_counts_exclude_mode():
    entries = [WorkEntry(work_id="1", rating="Explicit")]
    filters = _filters(exclude={"rating": ["Explicit"]})
    options = [("Explicit", "Explicit"), ("Mature", "Mature")]
    result = _static_facet_counts(entries, filters, "rating", options, mode="exclude")
    assert result == [("Explicit", "Explicit", 1, True), ("Mature", "Mature", 0, False)]


def test_filter_query_string_round_trips_multiple_facets():
    filters = _filters(
        facets={"rating": ["Explicit"], "freeform": ["Angst", "Fluff"]},
        exclude={"character": ["Voldemort"]},
        crossover="only", q="test",
    )
    qs = _filter_query_string(filters)
    assert qs.startswith("&")
    assert "rating=Explicit" in qs
    assert "freeform=Angst" in qs
    assert "freeform=Fluff" in qs
    assert "x_character=Voldemort" in qs
    assert "crossover=only" in qs
    assert "q=test" in qs


def test_filter_query_string_drops_one_include_value():
    filters = _filters(facets={"freeform": ["Angst", "Fluff"]})
    qs = _filter_query_string(filters, drop_key="freeform", drop_value="Angst")
    assert "freeform=Fluff" in qs
    assert "freeform=Angst" not in qs


def test_filter_query_string_drops_one_exclude_value():
    filters = _filters(exclude={"character": ["Voldemort", "Umbridge"]})
    qs = _filter_query_string(filters, drop_key="x_character", drop_value="Voldemort")
    assert "x_character=Umbridge" in qs
    assert "x_character=Voldemort" not in qs


def test_filter_query_string_empty_when_no_filters_active():
    assert _filter_query_string(_filters()) == ""


def test_active_chips_labels_exclude_values_distinctly():
    filters = _filters(exclude={"character": ["Voldemort"]})
    chips = _active_chips(filters)
    assert any(chip["text"] == "Exclude Character: Voldemort" for chip in chips)


def test_active_chips_include_crossover_and_date_range():
    filters = _filters(crossover="only", date_from=datetime(2024, 1, 1).date())
    chips = _active_chips(filters)
    texts = [chip["text"] for chip in chips]
    assert "Crossovers only" in texts
    assert any(text.startswith("Downloaded on/after") for text in texts)


def test_filter_query_string_includes_bookmarked():
    qs = _filter_query_string(_filters(bookmarked=True))
    assert "bookmarked=true" in qs


def test_filter_query_string_drops_bookmarked():
    qs = _filter_query_string(_filters(bookmarked=True), drop_key="bookmarked")
    assert "bookmarked" not in qs


def test_active_chips_includes_bookmarked_only():
    chips = _active_chips(_filters(bookmarked=True))
    assert any(chip["text"] == "Bookmarked only" for chip in chips)


def test_filter_query_string_includes_unread():
    qs = _filter_query_string(_filters(unread=True))
    assert "unread=true" in qs


def test_filter_query_string_drops_unread():
    qs = _filter_query_string(_filters(unread=True), drop_key="unread")
    assert "unread" not in qs


def test_active_chips_includes_unread_only():
    chips = _active_chips(_filters(unread=True))
    assert any(chip["text"] == "Unread only" for chip in chips)


def _search(autocompleter, word_to_tags, q, limit=20):
    results = autocompleter.search(word=q, max_cost=2, size=limit * 2)
    matched = set()
    for result in results:
        key = " ".join(result) if isinstance(result, list) else result
        matched.update(word_to_tags.get(key, ()))
    return sorted(matched, key=str.lower)[:limit]


def test_autocomplete_index_matches_by_prefix():
    autocompleter, word_to_tags = _build_autocomplete_index({"Torchwood", "Doctor Who"})
    assert _search(autocompleter, word_to_tags, "tor") == ["Torchwood"]


def test_autocomplete_index_finds_relationship_tag_by_either_party():
    autocompleter, word_to_tags = _build_autocomplete_index({"Ianto Jones/Jack Harkness"})
    assert _search(autocompleter, word_to_tags, "ianto") == ["Ianto Jones/Jack Harkness"]
    assert _search(autocompleter, word_to_tags, "jack") == ["Ianto Jones/Jack Harkness"]


def test_autocomplete_index_splits_on_ampersand_too():
    # Matching is by prefix of a full indexed key, not per-word -- "The
    # Doctor" is indexed as its own key from the split, so it's reachable
    # by its own prefix ("the"), not by a word in the middle ("doctor").
    autocompleter, word_to_tags = _build_autocomplete_index({"Rose Tyler & The Doctor"})
    assert _search(autocompleter, word_to_tags, "the") == ["Rose Tyler & The Doctor"]
    assert _search(autocompleter, word_to_tags, "rose") == ["Rose Tyler & The Doctor"]


def test_autocomplete_index_a_shared_party_resolves_to_every_relationship_with_them():
    autocompleter, word_to_tags = _build_autocomplete_index({
        "Ianto Jones/Jack Harkness", "Jack Harkness/Gwen Cooper",
    })
    assert _search(autocompleter, word_to_tags, "jack") == ["Ianto Jones/Jack Harkness", "Jack Harkness/Gwen Cooper"]


def test_autocomplete_index_no_match_returns_empty():
    autocompleter, word_to_tags = _build_autocomplete_index({"Torchwood"})
    assert _search(autocompleter, word_to_tags, "xyz") == []


def test_sort_newest_key_prefers_mtime_then_log_timestamp_then_min():
    # The raw key alone sorts ascending (oldest timestamp first) -- ["newest"]
    # only actually shows newest-first once DESCENDING_SORTS' reverse=True
    # is applied at the dashboard's own sort call, covered separately below.
    with_mtime = WorkEntry(work_id="1", mtime=datetime(2024, 1, 1))
    with_log_timestamp = WorkEntry(work_id="2", log_timestamp="01/01/2023, 00:00:00")
    with_neither = WorkEntry(work_id="3")

    entries = [with_mtime, with_log_timestamp, with_neither]
    entries.sort(key=SORT_OPTIONS["newest"])
    assert [e.work_id for e in entries] == ["3", "2", "1"]


def test_sort_newest_with_descending_reverse_shows_newest_first():
    # This is the actual end-to-end behavior "Date Downloaded (Newest)"
    # promises on Downloads -- see dashboard()'s entries.sort(...,
    # reverse=filters["sort"] in DESCENDING_SORTS). A regression here (e.g.
    # dropping that reverse=True) previously put freshly-downloaded works
    # dead last instead of first, with nothing-downloaded-yet works
    # floating to the top.
    oldest = WorkEntry(work_id="1", mtime=datetime(2024, 1, 1))
    newest = WorkEntry(work_id="2", mtime=datetime(2026, 1, 1))
    no_timestamp = WorkEntry(work_id="3")

    entries = [oldest, newest, no_timestamp]
    entries.sort(key=SORT_OPTIONS["newest"], reverse="newest" in DESCENDING_SORTS)

    assert [e.work_id for e in entries] == ["2", "1", "3"]


def test_shared_child_category_returns_the_common_category():
    entries = [WorkEntry(work_id="1", characters=["Ron Weasley", "Ron Weasley (Auror)"])]
    assert _shared_child_category({"Ron Weasley", "Ron Weasley (Auror)"}, entries) == "character"


def test_shared_child_category_none_when_children_are_mixed():
    entries = [WorkEntry(work_id="1", characters=["Ron Weasley"], freeform_tags=["Coffee Shop AU"])]
    assert _shared_child_category({"Ron Weasley", "Coffee Shop AU"}, entries) is None


def test_shared_child_category_none_when_no_child_resolves_to_anything():
    assert _shared_child_category({"Brand New Tag"}, []) is None


def test_shared_child_category_ignores_unresolved_children_among_resolved_ones():
    # A child that's never actually appeared on any work yet (a purely
    # virtual tag someone typed as a target) shouldn't stop the rest of
    # the children from establishing a shared category.
    entries = [WorkEntry(work_id="1", characters=["Ron Weasley"])]
    assert _shared_child_category({"Ron Weasley", "Never Seen Tag"}, entries) == "character"


def test_effective_tag_category_prefers_explicit_over_entries():
    entries = [WorkEntry(work_id="1", freeform_tags=["Some Tag"])]
    assert _effective_tag_category(entries, "Some Tag", {"Some Tag": "character"}) == "character"


def test_effective_tag_category_explicit_resolves_a_zero_occurrence_virtual_tag():
    # The entries scan alone can never see this -- a purely virtual tag
    # (e.g. a wrangling target nobody's tagged a real work with) never
    # appears in any entry's own resolved tag lists no matter what
    # category it's been explicitly given.
    assert _effective_tag_category([], "Hogwarts Students", {"Hogwarts Students": "character"}) == "character"


def test_effective_tag_category_falls_back_to_entries_when_not_in_explicit():
    entries = [WorkEntry(work_id="1", characters=["Ron Weasley"])]
    assert _effective_tag_category(entries, "Ron Weasley", {"Some Other Tag": "freeform"}) == "character"


def test_effective_tag_category_none_when_omitted_and_tag_never_occurs():
    assert _effective_tag_category([], "Hogwarts Students") is None


def test_sanitize_style_content_neutralizes_closing_style_tag():
    css = "body { color: red; } </style><script>alert(1)</script>"
    result = sanitize_style_content(css)
    assert "</style>" not in result
    assert "&lt;/style" in result


def test_sanitize_style_content_is_case_insensitive():
    css = "a {} </STYLE><script>alert(1)</script>"
    result = sanitize_style_content(css)
    assert "</style" not in result.lower().replace("&lt;/style", "")


def test_sanitize_style_content_leaves_ordinary_css_untouched():
    css = "div > p { color: red; } a.tag { font-weight: bold; }"
    assert sanitize_style_content(css) == css


def test_translate_ao3_skin_selectors_maps_known_fallback_ids():
    # .splash/#stat_chart have no real equivalent on this app's pages, so
    # they're rewritten onto a stand-in.
    css = ".splash { color: blue; } #stat_chart { color: red; }"
    result = translate_ao3_skin_selectors(css)
    assert ".splash" not in result
    assert "#stat_chart" not in result
    assert ".blurb-list { color: blue; }" in result
    assert ".blurb-ao3-stats { color: red; }" in result


def test_translate_ao3_skin_selectors_leaves_dashboard_unmapped():
    # #dashboard was deliberately dropped from the map -- live-testing a
    # real skin showed redirecting its gold-fill background rule onto the
    # entire main content area painted every blurb solid gold instead of
    # just bordered. Left alone, it correctly no-ops instead.
    css = "#dashboard { background: gold; }"
    assert translate_ao3_skin_selectors(css) == css


def test_translate_ao3_skin_selectors_leaves_real_matches_untouched():
    # base.html's own markup now reuses these AO3 ids/classes directly
    # (#header, #outer.wrapper, #inner.wrapper, a.tag), so they should match
    # natively -- rewriting them would be redundant, or worse, wrong (e.g.
    # a.tag also matching .blurb-fandoms a even when a skin didn't intend that).
    css = "#header { background: purple; } #outer.wrapper { background: black; } #inner.wrapper { padding: 0; } a.tag { color: gold; }"
    assert translate_ao3_skin_selectors(css) == css


def test_translate_ao3_skin_selectors_does_not_partial_match_lookalike_selectors():
    # ".splashscreen" is a distinct real-world selector -- a naive substring
    # replace would corrupt it into nonsense.
    css = ".splashscreen { color: blue; }"
    result = translate_ao3_skin_selectors(css)
    assert result == css


def test_translate_ao3_skin_selectors_leaves_unmapped_selectors_untouched():
    css = "li.blurb { background: black; } table, th { border: 1px solid gold; }"
    assert translate_ao3_skin_selectors(css) == css


def test_blurb_tag_line_orders_warnings_relationships_characters_freeforms():
    entry = WorkEntry(
        work_id="1",
        warnings=["Graphic Depictions Of Violence"],
        relationships=["Shane Hollander/Ilya Rozanov"],
        characters=["Shane Hollander"],
        freeform_tags=["Slow Burn"],
    )
    tags = blurb_tag_line(entry)
    assert [t["li_class"] for t in tags] == ["warnings", "relationships", "characters", "freeforms"]


def test_blurb_tag_line_param_matches_the_downloads_filter_facet():
    # Each tag doubles as a working Downloads filter link -- param must be
    # a real FACETS key so "/?{{ tag.param }}=..." actually filters.
    entry = WorkEntry(work_id="1", characters=["Shane Hollander"], freeform_tags=["Slow Burn"])
    tags = blurb_tag_line(entry)
    for tag in tags:
        assert tag["param"] in FACETS


def test_blurb_tag_line_empty_entry_has_no_tags():
    assert blurb_tag_line(WorkEntry(work_id="1")) == []


def test_series_sort_key_orders_numerically_not_lexicographically():
    entries = [
        WorkEntry(work_id="1", series_index="10"),
        WorkEntry(work_id="2", series_index="2"),
        WorkEntry(work_id="3", series_index="1"),
    ]
    entries.sort(key=_series_sort_key)
    assert [e.work_id for e in entries] == ["3", "2", "1"]


def test_series_sort_key_puts_missing_or_non_numeric_index_last():
    entries = [
        WorkEntry(work_id="1", series_index=None),
        WorkEntry(work_id="2", series_index="1"),
        WorkEntry(work_id="3", series_index="not-a-number"),
    ]
    entries.sort(key=_series_sort_key)
    assert entries[0].work_id == "2"
    assert {entries[1].work_id, entries[2].work_id} == {"1", "3"}


def test_sort_name_count_rows_name_asc_is_case_insensitive():
    rows = [("zeta", 1), ("Alpha", 2), ("beta", 3)]
    assert [r[0] for r in _sort_name_count_rows(rows, "name_asc")] == ["Alpha", "beta", "zeta"]


def test_sort_name_count_rows_name_desc():
    rows = [("zeta", 1), ("Alpha", 2), ("beta", 3)]
    assert [r[0] for r in _sort_name_count_rows(rows, "name_desc")] == ["zeta", "beta", "Alpha"]


def test_sort_name_count_rows_count_desc_tiebreaks_ascending_by_name():
    rows = [("zeta", 5), ("alpha", 5), ("beta", 9)]
    assert [r[0] for r in _sort_name_count_rows(rows, "count_desc")] == ["beta", "alpha", "zeta"]


def test_sort_name_count_rows_count_asc_tiebreaks_ascending_by_name_too():
    rows = [("zeta", 1), ("alpha", 1), ("beta", 9)]
    assert [r[0] for r in _sort_name_count_rows(rows, "count_asc")] == ["alpha", "zeta", "beta"]


def test_sort_name_count_rows_defaults_to_name_asc_for_unknown_sort():
    rows = [("zeta", 1), ("alpha", 2)]
    assert [r[0] for r in _sort_name_count_rows(rows, "nonsense")] == ["alpha", "zeta"]


def test_filter_by_letter_matches_case_insensitively():
    rows = [("apple", 1), ("Avocado", 2), ("banana", 3)]
    assert [r[0] for r in _filter_by_letter(rows, "A")] == ["apple", "Avocado"]


def test_filter_by_letter_all_is_a_no_op():
    rows = [("apple", 1), ("banana", 2)]
    assert _filter_by_letter(rows, "all") == rows


def test_filter_by_letter_hash_catches_non_alphabetic_starts():
    rows = [("apple", 1), ("100 Ways", 2), ("(Working Title)", 3)]
    assert [r[0] for r in _filter_by_letter(rows, "#")] == ["100 Ways", "(Working Title)"]


def test_filter_by_media_type_all_is_a_no_op():
    rows = [("Doctor Who", 1), ("Harry Potter", 2)]
    assert _filter_by_media_type(rows, "all", {}, {}) == rows


def test_filter_by_media_type_matches_the_resolved_value():
    rows = [("Doctor Who", 1), ("Harry Potter", 2)]
    explicit = {"Doctor Who": "TV Shows", "Harry Potter": "Books & Literature"}
    assert [r[0] for r in _filter_by_media_type(rows, "TV Shows", {}, explicit)] == ["Doctor Who"]


def test_filter_by_media_type_matches_an_inherited_value():
    rows = [("Fantastic Beasts", 1)]
    parent_of = {"Fantastic Beasts": "Wizarding World"}
    explicit = {"Wizarding World": "Movies"}
    assert _filter_by_media_type(rows, "Movies", parent_of, explicit) == rows


def test_filter_by_media_type_excludes_a_never_classified_row_from_a_real_category():
    rows = [("Some New Fandom", 1)]
    assert _filter_by_media_type(rows, "Movies", {}, {}) == []


def test_filter_by_media_type_uncategorized_matches_a_never_classified_row():
    rows = [("Some New Fandom", 1)]
    assert _filter_by_media_type(rows, "Uncategorized Fandoms", {}, {}) == rows


def test_add_virtual_parent_counts_creates_a_row_for_a_nonexistent_parent():
    # "Sci-Fi Shows" was never an actual tag on any work -- it's a
    # consolidated parent an admin typed in on the Classify Tags page
    # purely to group existing children under.
    entries = [
        WorkEntry(work_id="1", fandom_candidates=["Torchwood"]),
        WorkEntry(work_id="2", fandom_candidates=["Doctor Who"]),
        WorkEntry(work_id="3", fandom_candidates=["Angst"]),
    ]
    counts = Counter({"Torchwood": 1, "Doctor Who": 1, "Angst": 1})
    children = {"Sci-Fi Shows": {"Torchwood", "Doctor Who"}}
    _add_virtual_parent_counts(counts, entries, lambda e: e.fandom_candidates, children)
    assert counts["Sci-Fi Shows"] == 2


def test_add_virtual_parent_counts_does_not_double_count_a_work_with_two_children():
    entries = [WorkEntry(work_id="1", fandom_candidates=["Torchwood", "Doctor Who"])]
    counts = Counter({"Torchwood": 1, "Doctor Who": 1})
    children = {"Sci-Fi Shows": {"Torchwood", "Doctor Who"}}
    _add_virtual_parent_counts(counts, entries, lambda e: e.fandom_candidates, children)
    assert counts["Sci-Fi Shows"] == 1


def test_add_virtual_parent_counts_leaves_a_real_parent_tags_own_count_alone():
    entries = [WorkEntry(work_id="1", fandom_candidates=["Torchwood"]), WorkEntry(work_id="2", fandom_candidates=["Doctor Who"])]
    counts = Counter({"Torchwood": 5, "Doctor Who": 1})  # Torchwood's real count, unrelated to its child
    children = {"Torchwood": {"Doctor Who"}}
    _add_virtual_parent_counts(counts, entries, lambda e: e.fandom_candidates, children)
    assert counts["Torchwood"] == 5


def test_add_virtual_parent_counts_skips_a_parent_whose_children_match_nothing():
    entries = [WorkEntry(work_id="1", fandom_candidates=["Angst"])]
    counts = Counter({"Angst": 1})
    children = {"Sci-Fi Shows": {"Torchwood", "Doctor Who"}}
    _add_virtual_parent_counts(counts, entries, lambda e: e.fandom_candidates, children)
    assert "Sci-Fi Shows" not in counts


def test_group_tag_rows_by_parent_nests_child_under_parent():
    tags = [("Alternate Reality", 5, "freeform"), ("Alternate Reality - Canon Divergence", 2, "freeform")]
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    grouped = _group_tag_rows_by_parent(tags, children)
    assert [row["tag"] for row in grouped] == ["Alternate Reality"]
    assert grouped[0]["children"] == [
        {"tag": "Alternate Reality - Canon Divergence", "count": 2, "category": "freeform", "children": []}
    ]


def test_group_tag_rows_by_parent_nests_multiple_levels_deep():
    # Fandom -> Relationship -> Character, a real three-level chain.
    tags = [
        ("Harry Potter", 5, "fandom"),
        ("Harry Potter/Hermione Granger", 3, "relationship"),
        ("Hermione Granger", 3, "character"),
    ]
    children = {
        "Harry Potter": {"Harry Potter/Hermione Granger"},
        "Harry Potter/Hermione Granger": {"Hermione Granger"},
    }
    grouped = _group_tag_rows_by_parent(tags, children)
    assert [row["tag"] for row in grouped] == ["Harry Potter"]
    relationship_row = grouped[0]["children"][0]
    assert relationship_row["tag"] == "Harry Potter/Hermione Granger"
    assert relationship_row["children"] == [
        {"tag": "Hermione Granger", "count": 3, "category": "character", "children": []}
    ]


def test_group_tag_rows_by_parent_leaves_childless_tags_alone():
    tags = [("Angst", 3, None), ("Fluff", 2, None)]
    grouped = _group_tag_rows_by_parent(tags, {})
    assert [row["tag"] for row in grouped] == ["Angst", "Fluff"]
    assert all(row["children"] == [] for row in grouped)


def test_group_tag_rows_by_parent_orphans_child_when_parent_filtered_out():
    # The parent didn't survive the current filter tab -- the child falls
    # back to its own top-level row instead of disappearing.
    tags = [("Alternate Reality - Canon Divergence", 2, "freeform")]
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    grouped = _group_tag_rows_by_parent(tags, children)
    assert [row["tag"] for row in grouped] == ["Alternate Reality - Canon Divergence"]
    assert grouped[0]["children"] == []


def test_group_tag_rows_by_parent_preserves_sort_order_for_top_level_and_children():
    tags = [
        ("Alternate Reality", 5, "freeform"),
        ("Alternate Reality - Canon Divergence", 2, "freeform"),
        ("Alternate Reality - Fantasy", 4, "freeform"),
        ("Angst", 3, None),
    ]
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence", "Alternate Reality - Fantasy"}}
    grouped = _group_tag_rows_by_parent(tags, children)
    assert [row["tag"] for row in grouped] == ["Alternate Reality", "Angst"]
    # Children ride along in the same relative order they had in `tags`.
    assert [c["tag"] for c in grouped[0]["children"]] == ["Alternate Reality - Canon Divergence", "Alternate Reality - Fantasy"]


def test_association_parents_fandom_returns_the_single_resolved_fandom():
    parents = _association_parents(
        "Hermione Granger", "fandom", {"Hermione Granger": "Harry Potter"}, {}, {}, {}, {}, {}, {}
    )
    assert parents == ["Harry Potter"]


def test_association_parents_fandom_no_fandom_is_ungrouped():
    parents = _association_parents("Random Character", "fandom", {}, {}, {}, {}, {}, {}, {})
    assert parents == []


def test_association_parents_fandom_inherits_up_the_same_category_chain():
    parent_of = {"Anxious Shane Hollander": "Anxious Character"}
    tag_fandoms = {"Anxious Character": "No Fandom", "Anxious Shane Hollander": "Heated Rivalry"}
    assert _association_parents("Anxious Shane Hollander", "fandom", tag_fandoms, parent_of, {}, {}, {}, {}, {}) == ["Heated Rivalry"]


def test_association_parents_fandom_explicit_no_fandom_gets_its_own_group():
    # A real, deliberate "No Fandom" choice -- distinct from a tag nobody's
    # classified either way yet (test_association_parents_fandom_no_fandom_is_ungrouped) --
    # groups under a "No Fandom" heading instead of disappearing.
    tag_fandoms = {"Coffee Shop AU": "No Fandom"}
    assert _association_parents("Coffee Shop AU", "fandom", tag_fandoms, {}, {}, {}, {}, {}, {}) == ["No Fandom"]


def test_association_parents_fandom_inherited_explicit_no_fandom_groups_too():
    parent_of = {"Anxious Shane Hollander": "Anxious Character"}
    tag_fandoms = {"Anxious Character": "No Fandom"}
    assert _association_parents("Anxious Shane Hollander", "fandom", tag_fandoms, parent_of, {}, {}, {}, {}, {}) == ["No Fandom"]


def test_association_parents_character_unions_relationship_and_freeform_links():
    relationship_characters = {"Harry/Draco": {0: "Harry Potter", 1: "Draco Malfoy"}}
    freeform_characters = {"Angst": {"Draco Malfoy", "Ron Weasley"}}
    parents = _association_parents("Harry/Draco", "character", {}, {}, relationship_characters, {}, {}, {}, {})
    assert parents == ["Draco Malfoy", "Harry Potter"]
    parents = _association_parents("Angst", "character", {}, {}, {}, freeform_characters, {}, {}, {})
    assert parents == ["Draco Malfoy", "Ron Weasley"]


def test_association_parents_character_has_no_effect_on_unlinked_tag():
    assert _association_parents("Fluff", "character", {}, {}, {}, {}, {}, {}, {}) == []


def test_association_parents_relationship_returns_sorted_freeform_links():
    freeform_relationships = {"Angst": {"Harry/Draco", "Ron/Hermione"}}
    parents = _association_parents("Angst", "relationship", {}, {}, {}, {}, freeform_relationships, {}, {})
    assert parents == ["Harry/Draco", "Ron/Hermione"]


def test_association_parents_freeform_returns_freeform_tags_linking_back_to_a_character():
    character_freeform_tags = {"Draco Malfoy": {"Angst", "Whump"}}
    parents = _association_parents("Draco Malfoy", "freeform", {}, {}, {}, {}, {}, character_freeform_tags, {})
    assert parents == ["Angst", "Whump"]


def test_association_parents_freeform_unions_character_and_relationship_reverse_links():
    character_freeform_tags = {"Harry/Draco": {"Enemies to Lovers"}}
    relationship_freeform_tags = {"Harry/Draco": {"Slow Burn"}}
    parents = _association_parents("Harry/Draco", "freeform", {}, {}, {}, {}, {}, character_freeform_tags, relationship_freeform_tags)
    assert parents == ["Enemies to Lovers", "Slow Burn"]


def test_association_parents_freeform_has_no_effect_on_unlinked_tag():
    assert _association_parents("Random Freeform", "freeform", {}, {}, {}, {}, {}, {}, {}) == []


def test_association_parents_unknown_dimension_returns_nothing():
    assert _association_parents("Angst", "nonsense", {}, {}, {}, {}, {}, {}, {}) == []


def test_group_tag_rows_by_association_nests_tags_under_their_resolved_fandom():
    tags = [("Harry Potter", 5, "fandom"), ("Hermione Granger", 3, "character")]
    tag_fandoms = {"Hermione Granger": "Harry Potter"}
    grouped = _group_tag_rows_by_association(tags, "fandom", tag_fandoms, {}, {}, {}, {}, "count_desc")
    # The synthetic "Harry Potter" group heading and the real "Harry Potter"
    # fandom tag (which has no fandom of its own -- untouched, stays
    # ungrouped) are separate rows sharing a name; both must appear.
    group_heading = next(r for r in grouped if r["children"])
    assert group_heading["tag"] == "Harry Potter"
    assert group_heading["category"] is None
    assert [c["tag"] for c in group_heading["children"]] == ["Hermione Granger"]


def test_group_tag_rows_by_association_tag_with_no_association_stays_standalone():
    tags = [("Fluff", 2, "freeform")]
    grouped = _group_tag_rows_by_association(tags, "fandom", {}, {}, {}, {}, {}, "count_desc")
    assert grouped == [{"tag": "Fluff", "count": 2, "category": "freeform", "children": []}]


def test_group_tag_rows_by_association_tag_with_multiple_parents_appears_under_each():
    tags = [
        ("Angst", 4, "freeform"),
        ("Harry Potter", 5, "character"),
        ("Draco Malfoy", 3, "character"),
    ]
    freeform_characters = {"Angst": {"Harry Potter", "Draco Malfoy"}}
    grouped = _group_tag_rows_by_association(tags, "character", {}, {}, {}, freeform_characters, {}, "count_desc")
    headings = {row["tag"]: row for row in grouped if row["children"]}
    assert set(headings) == {"Harry Potter", "Draco Malfoy"}
    assert headings["Harry Potter"]["children"] == [{"tag": "Angst", "count": 4, "category": "freeform", "children": []}]
    assert headings["Draco Malfoy"]["children"] == [{"tag": "Angst", "count": 4, "category": "freeform", "children": []}]


def test_group_tag_rows_by_association_group_count_sums_its_members():
    tags = [("Harry Potter", 5, "fandom"), ("Hermione Granger", 3, "character"), ("Ron Weasley", 2, "character")]
    tag_fandoms = {"Hermione Granger": "Harry Potter", "Ron Weasley": "Harry Potter"}
    grouped = _group_tag_rows_by_association(tags, "fandom", tag_fandoms, {}, {}, {}, {}, "count_desc")
    heading = next(r for r in grouped if r["children"])
    assert heading["count"] == 5


def test_group_tag_rows_by_association_freeform_dimension_groups_characters_by_linking_freeform_tags():
    # The reverse of "Freeform organized by Character": Character rows
    # grouped under every Freeform tag that links to them.
    tags = [
        ("Draco Malfoy", 5, "character"),
        ("Harry Potter", 3, "character"),
        ("Ron Weasley", 2, "character"),
    ]
    freeform_characters = {"Angst": {"Draco Malfoy", "Harry Potter"}, "Fluff": {"Draco Malfoy"}}
    grouped = _group_tag_rows_by_association(tags, "freeform", {}, {}, {}, freeform_characters, {}, "count_desc")
    headings = {row["tag"]: row for row in grouped if row["children"]}
    assert set(headings) == {"Angst", "Fluff"}
    assert {c["tag"] for c in headings["Angst"]["children"]} == {"Draco Malfoy", "Harry Potter"}
    assert {c["tag"] for c in headings["Fluff"]["children"]} == {"Draco Malfoy"}
    # Ron Weasley has no Freeform link at all -- stays standalone.
    assert any(row["tag"] == "Ron Weasley" and not row["children"] for row in grouped)


def test_group_tag_rows_by_association_merges_groups_and_standalone_by_sort_order():
    tags = [("Aaa Standalone", 10, "freeform"), ("Harry Potter", 1, "fandom"), ("Hermione Granger", 1, "character")]
    tag_fandoms = {"Hermione Granger": "Harry Potter"}
    grouped = _group_tag_rows_by_association(tags, "fandom", tag_fandoms, {}, {}, {}, {}, "count_desc")
    # Sorted together by count_desc -- the standalone tag's high count puts
    # it first, not after every group the way a "groups always first" order would.
    assert grouped[0]["tag"] == "Aaa Standalone"


def test_flatten_tag_options_lists_a_parent_immediately_followed_by_its_children():
    names = ["Alternate Reality", "Alternate Reality - Canon Divergence", "Angst"]
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    assert _flatten_tag_options(names, children) == [
        ("Alternate Reality", 0),
        ("Alternate Reality - Canon Divergence", 1),
        ("Angst", 0),
    ]


def test_flatten_tag_options_nests_multiple_levels():
    names = ["Harry Potter", "Harry Potter/Hermione Granger", "Hermione Granger"]
    children = {"Harry Potter": {"Harry Potter/Hermione Granger"}, "Harry Potter/Hermione Granger": {"Hermione Granger"}}
    assert _flatten_tag_options(names, children) == [
        ("Harry Potter", 0),
        ("Harry Potter/Hermione Granger", 1),
        ("Hermione Granger", 2),
    ]


def test_flatten_tag_options_orphan_becomes_top_level_when_parent_not_in_names():
    names = ["Alternate Reality - Canon Divergence"]
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    assert _flatten_tag_options(names, children) == [("Alternate Reality - Canon Divergence", 0)]


def test_flatten_tag_options_siblings_are_alphabetical_at_each_level():
    names = ["Zeta", "Alpha"]
    assert _flatten_tag_options(names, {}) == [("Alpha", 0), ("Zeta", 0)]
