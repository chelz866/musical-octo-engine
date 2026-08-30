from datetime import datetime

from app.main import (
    FACETS,
    SORT_OPTIONS,
    _completion_status,
    _entry_matches,
    _facet_suggestions,
    _filter_query_string,
    _selected_with_counts,
    _static_facet_counts,
    paginate,
)
from app.scanner import WorkEntry


def _filters(facets=None, word_min=None, word_max=None, q="", sort="title"):
    return {
        "facets": {name: [] for name in FACETS} | (facets or {}),
        "word_min": word_min,
        "word_max": word_max,
        "q": q,
        "sort": sort,
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


def test_entry_matches_ors_within_one_facet():
    entry = WorkEntry(work_id="1", rating="Mature")
    filters = _filters(facets={"rating": ["Explicit", "Mature"]})
    assert _entry_matches(entry, filters) is True


def test_entry_matches_exclude_skips_that_facet():
    entry = WorkEntry(work_id="1", rating="Mature")
    filters = _filters(facets={"rating": ["Explicit"]})
    assert _entry_matches(entry, filters, exclude="rating") is True
    assert _entry_matches(entry, filters) is False


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


def test_filter_query_string_round_trips_multiple_facets():
    filters = _filters(facets={"rating": ["Explicit"], "freeform": ["Angst", "Fluff"]}, q="test")
    qs = _filter_query_string(filters)
    assert qs.startswith("&")
    assert "rating=Explicit" in qs
    assert "freeform=Angst" in qs
    assert "freeform=Fluff" in qs
    assert "q=test" in qs


def test_filter_query_string_excludes_one_value():
    filters = _filters(facets={"freeform": ["Angst", "Fluff"]})
    qs = _filter_query_string(filters, exclude_key="freeform", exclude_value="Angst")
    assert "freeform=Fluff" in qs
    assert "freeform=Angst" not in qs


def test_filter_query_string_empty_when_no_filters_active():
    assert _filter_query_string(_filters()) == ""


def test_sort_newest_prefers_mtime_then_log_timestamp_then_min():
    with_mtime = WorkEntry(work_id="1", mtime=datetime(2024, 1, 1))
    with_log_timestamp = WorkEntry(work_id="2", log_timestamp="01/01/2023, 00:00:00")
    with_neither = WorkEntry(work_id="3")

    entries = [with_mtime, with_log_timestamp, with_neither]
    entries.sort(key=SORT_OPTIONS["newest"])
    assert [e.work_id for e in entries] == ["3", "2", "1"]
