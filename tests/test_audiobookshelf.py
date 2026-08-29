import sqlite3

from app.audiobookshelf import item_url, load_matches

FANFIC_LIBRARY = "89973a2b-bced-4abc-8c74-d8672154c5d7"
OTHER_LIBRARY = "1a613b3f-a7aa-4297-ab38-01bf85475cdb"


def _make_abs_db(path: str, rows: list[tuple[str, str, str]]) -> None:
    """rows: (item_id, path, library_id) -- matches the real Audiobookshelf
    libraryItems table closely enough for the columns load_matches reads.
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE libraryItems (id TEXT PRIMARY KEY, path TEXT, libraryId TEXT)")
    conn.executemany("INSERT INTO libraryItems (id, path, libraryId) VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_load_matches_extracts_work_id_from_real_ao3dl_naming(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(db_path, [
        ("be740dc5-3a18-491d-8979-8b215ff7514c", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY),
        ("770b6e54-0aaa-48d4-8c1f-2d1c45ed9023", "/storage/fics/ao3-dl/downloads/9851081 Winterheart - orphan_account.epub", FANFIC_LIBRARY),
    ])

    matches = load_matches(db_path, FANFIC_LIBRARY)

    assert matches == {
        "9778112": "be740dc5-3a18-491d-8979-8b215ff7514c",
        "9851081": "770b6e54-0aaa-48d4-8c1f-2d1c45ed9023",
    }


def test_load_matches_ignores_other_libraries(tmp_path):
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(db_path, [
        ("a1", "/storage/comics/9999999 Not A Fic.epub", OTHER_LIBRARY),
        ("a2", "/storage/fics/ao3-dl/downloads/9778112 Sanctuary - SailorChibi.epub", FANFIC_LIBRARY),
    ])

    matches = load_matches(db_path, FANFIC_LIBRARY)

    assert matches == {"9778112": "a2"}


def test_load_matches_skips_filenames_without_a_leading_work_id(tmp_path):
    # older imports (e.g. the "ogFics" library in the real export) don't
    # have the AO3 work id in the filename -- these just don't match, they
    # shouldn't raise or produce a bogus entry.
    db_path = str(tmp_path / "absdatabase.sqlite")
    _make_abs_db(db_path, [
        ("a1", "/storage/ogFics/A_Cup_of_Good_Intentions.epub", FANFIC_LIBRARY),
    ])

    assert load_matches(db_path, FANFIC_LIBRARY) == {}


def test_load_matches_missing_db_file_returns_empty(tmp_path):
    assert load_matches(str(tmp_path / "does-not-exist.sqlite"), FANFIC_LIBRARY) == {}


def test_load_matches_returns_empty_for_blank_path():
    assert load_matches("", FANFIC_LIBRARY) == {}


def test_load_matches_invalid_database_returns_empty(tmp_path):
    # a real file that exists but isn't a valid Audiobookshelf database
    # (e.g. wrong path configured, or missing the libraryItems table)
    db_path = str(tmp_path / "not-abs.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()

    assert load_matches(db_path, FANFIC_LIBRARY) == {}


def test_item_url_handles_trailing_slash_in_base_url():
    assert item_url("http://host:13378/audiobookshelf/", "abc-123") == "http://host:13378/audiobookshelf/item/abc-123"
    assert item_url("http://host:13378/audiobookshelf", "abc-123") == "http://host:13378/audiobookshelf/item/abc-123"
