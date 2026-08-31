from datetime import datetime

from app.main import (
    EXCLUDE_FACETS,
    FACETS,
    SORT_OPTIONS,
    _active_chips,
    _build_autocomplete_index,
    _completion_status,
    _entry_matches,
    _facet_suggestions,
    _filter_query_string,
    _parse_date,
    _selected_with_counts,
    _filter_by_letter,
    _group_tag_rows_by_parent,
    _series_sort_key,
    _value_or_children_present,
    _sort_name_count_rows,
    _static_facet_counts,
    blurb_tag_line,
    paginate,
    sanitize_style_content,
    translate_ao3_skin_selectors,
)
from app.scanner import WorkEntry


def _filters(facets=None, exclude=None, word_min=None, word_max=None, crossover=None,
             date_from=None, date_to=None, bookmarked=False, unread=False, q="", sort="title", children=None):
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


def test_sort_newest_prefers_mtime_then_log_timestamp_then_min():
    with_mtime = WorkEntry(work_id="1", mtime=datetime(2024, 1, 1))
    with_log_timestamp = WorkEntry(work_id="2", log_timestamp="01/01/2023, 00:00:00")
    with_neither = WorkEntry(work_id="3")

    entries = [with_mtime, with_log_timestamp, with_neither]
    entries.sort(key=SORT_OPTIONS["newest"])
    assert [e.work_id for e in entries] == ["3", "2", "1"]


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


def test_group_tag_rows_by_parent_nests_child_under_parent():
    tags = [("Alternate Reality", 5, "freeform"), ("Alternate Reality - Canon Divergence", 2, "freeform")]
    children = {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}
    grouped = _group_tag_rows_by_parent(tags, children)
    assert [row["tag"] for row in grouped] == ["Alternate Reality"]
    assert grouped[0]["children"] == [("Alternate Reality - Canon Divergence", 2, "freeform")]


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
    assert [c[0] for c in grouped[0]["children"]] == ["Alternate Reality - Canon Divergence", "Alternate Reality - Fantasy"]
