from app.main import paginate


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
