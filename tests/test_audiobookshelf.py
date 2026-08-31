import json
import sqlite3

from app.audiobookshelf import item_url, load_matches, load_read_work_ids

FANFIC_LIBRARY = "89973a2b-bced-4abc-8c74-d8672154c5d7"
OTHER_LIBRARY = "1a613b3f-a7aa-4297-ab38-01bf85475cdb"

# A real genres array pulled from a matched book row -- exercises that
# classify_subjects (via scanner) buckets it the same way it would the
# equivalent epub dc:subject list.
REAL_GENRES = [
    "Fanworks", "General Audiences", "Stranger Things (TV 2016)",
    "Steve Harrington & The Party", "Steve Harrington", "Gen",
    "Choose Not To Use Archive Warnings",
]


def _make_abs_db(
    path: str,
    items: list[tuple[str, str, str, str]],
    books: dict[str, tuple] | None = None,
    series: dict[str, str] | None = None,
    book_series: list[tuple[str, str, str, str, str]] | None = None,
    users: dict[str, str] | None = None,
    media_progresses: list[tuple[str, str, int, str | None, str]] | None = None,
) -> None:
    """items: (item_id, path, libraryId, mediaId).
    books: mediaId -> (title, description, genres_json[, language]).
    series: seriesId -> name.
    book_series: (id, sequence, createdAt, bookId, seriesId) rows -- real
    Audiobookshelf's join table between books and series.
    users: userId -> username.
    media_progresses: (id, mediaItemId, isFinished, finishedAt, userId) rows
    -- real Audiobookshelf's per-user progress table (confirmed against a
    real export; only the columns load_read_work_ids actually reads).
    """
    books = books or {}
    series = series or {}
    book_series = book_series or []
    users = users or {}
    media_progresses = media_progresses or []
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE libraryItems (id TEXT PRIMARY KEY, path TEXT, authorNamesFirstLast TEXT, libraryId TEXT, mediaId TEXT)")
    conn.execute("CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT, description TEXT, language TEXT, genres TEXT)")
    conn.execute("CREATE TABLE series (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE bookSeries (id TEXT PRIMARY KEY, sequence TEXT, createdAt TEXT, bookId TEXT, seriesId TEXT)")
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT)")
    conn.execute("CREATE TABLE mediaProgresses (id TEXT PRIMARY KEY, mediaItemId TEXT, isFinished INTEGER, finishedAt TEXT, userId TEXT)")
    conn.executemany(
        "INSERT INTO libraryItems (id, path, authorNamesFirstLast, libraryId, mediaId) VALUES (?, ?, 'Some Author', ?, ?)",
        items,
    )
    conn.executemany(
        "INSERT INTO books (id, title, description, language, genres) VALUES (?, ?, ?, ?, ?)",
        [
            (media_id, row[0], row[1], row[3] if len(row) > 3 else None, row[2])
            for media_id, row in books.items()
        ],
    )
    conn.executemany("INSERT INTO series (id, name) VALUES (?, ?)", list(series.items()))
    conn.executemany(
        "INSERT INTO bookSeries (id, sequence, createdAt, bookId, seriesId) VALUES (?, ?, ?, ?, ?)",
        book_series,
    )
    conn.executemany("INSERT INTO users (id, username) VALUES (?, ?)", list(users.items()))
    conn.executemany(
        "INSERT INTO mediaProgresses (id, mediaItemId, isFinished, finishedAt, userId) VALUES (?, ?, ?, ?, ?)",
        media_progresses,
    )
    conn.commit()
    conn.close()


def test_load_matches_extracts_work_id_and_book_metadata(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("be740dc5-3a18-491d-8979-8b215ff7514c", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", "A summary.", json.dumps(REAL_GENRES), "en")},
    )

    matches = load_matches(db_path, FANFIC_LIBRARY)

    assert set(matches) == {"9778112"}
    match = matches["9778112"]
    assert match.item_id == "be740dc5-3a18-491d-8979-8b215ff7514c"
    assert match.title == "Sanctuary"
    assert match.author == "Some Author"
    assert match.description == "A summary."
    assert match.language == "en"
    assert match.genres == REAL_GENRES


def test_load_matches_ignores_other_libraries(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [
            ("a1", "/storage/comics/9999999 Not A Fic.epub", OTHER_LIBRARY, "book-1"),
            ("a2", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-2"),
        ],
        books={
            "book-1": ("Not A Fic", None, "[]"),
            "book-2": ("Sanctuary", None, "[]"),
        },
    )

    matches = load_matches(db_path, FANFIC_LIBRARY)

    assert set(matches) == {"9778112"}


def test_load_matches_skips_filenames_without_a_leading_work_id(tmp_path):
    # older imports (e.g. the "ogFics" library in the real export) don't
    # have the AO3 work id in the filename -- these just don't match, they
    # shouldn't raise or produce a bogus entry.
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/ogFics/A_Cup_of_Good_Intentions.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("A Cup of Good Intentions", None, "[]")},
    )

    assert load_matches(db_path, FANFIC_LIBRARY) == {}


def test_load_matches_skips_library_items_with_no_matching_book(tmp_path):
    # mediaId pointing nowhere (e.g. a podcast item, or a book row that
    # somehow doesn't exist) -- the INNER JOIN just excludes it.
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "no-such-book")],
    )

    assert load_matches(db_path, FANFIC_LIBRARY) == {}


def test_load_matches_handles_missing_or_malformed_genres_json(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", None, None)},
    )

    assert load_matches(db_path, FANFIC_LIBRARY)["9778112"].genres == []


def test_load_matches_missing_db_file_returns_empty(tmp_path):
    assert load_matches(str(tmp_path / "does-not-exist.sqlite"), FANFIC_LIBRARY) == {}


def test_load_matches_returns_empty_for_blank_path():
    assert load_matches("", FANFIC_LIBRARY) == {}


def test_load_matches_invalid_database_returns_empty(tmp_path):
    # a real file that exists but isn't a valid Audiobookshelf database
    # (e.g. wrong path configured, or missing the expected tables)
    db_path = str(tmp_path / "not-abs.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()

    assert load_matches(db_path, FANFIC_LIBRARY) == {}


def test_item_url_handles_trailing_slash_in_base_url():
    assert item_url("http://host:13378/audiobookshelf/", "abc-123") == "http://host:13378/audiobookshelf/item/abc-123"
    assert item_url("http://host:13378/audiobookshelf", "abc-123") == "http://host:13378/audiobookshelf/item/abc-123"


def test_load_matches_includes_series_name_and_sequence(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", None, "[]")},
        series={"series-1": "Hogwarts Stranger Secrets"},
        book_series=[("bs-1", "5", "2026-06-30 21:36:13.633 +00:00", "book-1", "series-1")],
    )

    match = load_matches(db_path, FANFIC_LIBRARY)["9778112"]
    assert match.series == "Hogwarts Stranger Secrets"
    assert match.series_index == "5"


def test_load_matches_series_is_none_when_book_has_no_series_row(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", None, "[]")},
    )

    match = load_matches(db_path, FANFIC_LIBRARY)["9778112"]
    assert match.series is None
    assert match.series_index is None


def test_load_matches_picks_one_series_deterministically_when_book_has_multiple(tmp_path):
    # A book belonging to more than one series is an edge case ABS allows
    # but AO3 fics don't really hit -- picking the earliest-added row
    # deterministically (rather than an arbitrary one) is good enough.
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", None, "[]")},
        series={"series-1": "First Series", "series-2": "Second Series"},
        book_series=[
            ("bs-1", "2", "2026-06-30 21:36:20.000 +00:00", "book-1", "series-2"),
            ("bs-2", "1", "2026-06-30 21:36:13.633 +00:00", "book-1", "series-1"),
        ],
    )

    match = load_matches(db_path, FANFIC_LIBRARY)["9778112"]
    assert match.series == "First Series"
    assert match.series_index == "1"


def test_load_read_work_ids_returns_finished_at_for_the_named_user(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", None, "[]")},
        users={"user-1": "chelz866"},
        media_progresses=[("mp-1", "book-1", 1, "2026-06-30 21:57:40.894 +00:00", "user-1")],
    )

    finished = load_read_work_ids(db_path, FANFIC_LIBRARY, "chelz866")
    assert finished == {"9778112": "2026-06-30 21:57:40.894 +00:00"}


def test_load_read_work_ids_excludes_unfinished_progress(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", None, "[]")},
        users={"user-1": "chelz866"},
        media_progresses=[("mp-1", "book-1", 0, None, "user-1")],
    )

    assert load_read_work_ids(db_path, FANFIC_LIBRARY, "chelz866") == {}


def test_load_read_work_ids_scoped_to_the_named_user_only(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY, "book-1")],
        books={"book-1": ("Sanctuary", None, "[]")},
        users={"user-1": "chelz866", "user-2": "someone_else"},
        media_progresses=[("mp-1", "book-1", 1, "2026-06-30 21:57:40.894 +00:00", "user-2")],
    )

    assert load_read_work_ids(db_path, FANFIC_LIBRARY, "chelz866") == {}
    assert load_read_work_ids(db_path, FANFIC_LIBRARY, "someone_else") == {"9778112": "2026-06-30 21:57:40.894 +00:00"}


def test_load_read_work_ids_ignores_other_libraries(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(
        db_path,
        [("a1", "/storage/comics/9999999 Not A Fic.epub", OTHER_LIBRARY, "book-1")],
        books={"book-1": ("Not A Fic", None, "[]")},
        users={"user-1": "chelz866"},
        media_progresses=[("mp-1", "book-1", 1, "2026-06-30 21:57:40.894 +00:00", "user-1")],
    )

    assert load_read_work_ids(db_path, FANFIC_LIBRARY, "chelz866") == {}


def test_load_read_work_ids_blank_username_returns_empty(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(db_path, [])
    assert load_read_work_ids(db_path, FANFIC_LIBRARY, "") == {}


def test_load_read_work_ids_missing_db_file_returns_empty(tmp_path):
    assert load_read_work_ids(str(tmp_path / "does-not-exist.sqlite"), FANFIC_LIBRARY, "chelz866") == {}
