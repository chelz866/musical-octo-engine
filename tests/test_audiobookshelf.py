import json
import sqlite3

from app.audiobookshelf import item_url, load_matches

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


def _make_abs_db(path: str, items: list[tuple[str, str, str, str]], books: dict[str, tuple] | None = None) -> None:
    """items: (item_id, path, libraryId, mediaId).
    books: mediaId -> (title, description, genres_json[, language]).
    """
    books = books or {}
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE libraryItems (id TEXT PRIMARY KEY, path TEXT, authorNamesFirstLast TEXT, libraryId TEXT, mediaId TEXT)")
    conn.execute("CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT, description TEXT, language TEXT, genres TEXT)")
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
