import os
import sqlite3
import tempfile

from app import catalog_import, db, scanner

# Matches the header of the export catalog_import.py was built against:
# Path,Title,Author,Category,Genre,Language,Status,Published,Updated,Packaged,
# Rating,Warnings,Chapters,Words,Publisher,Story URL,Author URL,Summary,
# Relationships,Series,Series URL,Comments,Kudos,Collections
_SOURCE_COLUMNS = [
    "Path", "Title", "Author", "Category", "Genre", "Language", "Status", "Published",
    "Rating", "Warnings", "Chapters", "Words", "Story URL", "Summary", "Relationships", "Series",
]


def _make_source_db(tmp, rows: list[dict], table="works") -> str:
    path = os.path.join(tmp, "source.db")
    conn = sqlite3.connect(path)
    quoted_columns = [f'"{c}"' for c in _SOURCE_COLUMNS]
    cols_sql = ", ".join(f'{c} TEXT' for c in quoted_columns)
    conn.execute(f'CREATE TABLE "{table}" ({cols_sql})')
    insert_sql = (
        f'INSERT INTO "{table}" ({", ".join(quoted_columns)}) '
        f'VALUES ({", ".join("?" for _ in _SOURCE_COLUMNS)})'
    )
    for row in rows:
        conn.execute(insert_sql, [row.get(c) for c in _SOURCE_COLUMNS])
    conn.commit()
    conn.close()
    return path


_SAMPLE_ROW = {
    "Path": "Completed/glass in the park - itadorihoney - 35282845.epub",
    "Title": "glass in the park",
    "Author": "itadorihoney",
    "Category": "Tokyo Revengers (Anime), Tokyo Revengers (Manga)",
    "Genre": "F/M, Smut, Unprotected Sex",
    "Language": "English",
    "Published": "2021-11-23",
    "Rating": "Explicit",
    "Warnings": "No Archive Warnings Apply",
    "Chapters": "1/1",
    "Words": "8,983",
    "Story URL": "https://archiveofourown.org/works/35282845",
    "Summary": "friends to lovers",
    "Relationships": "Takemichi/Reader",
    "Series": "",
}


def test_work_id_from_story_url():
    assert catalog_import._work_id_from_story_url("https://archiveofourown.org/works/35282845") == "35282845"
    assert catalog_import._work_id_from_story_url("https://archiveofourown.org/works/35282845/chapters/1") == "35282845"
    assert catalog_import._work_id_from_story_url("https://archiveofourown.org/works/35282845?view_full_work=true") == "35282845"
    assert catalog_import._work_id_from_story_url(None) is None
    assert catalog_import._work_id_from_story_url("not a url") is None


def test_split_tags():
    assert catalog_import._split_tags("A, B, C") == ["A", "B", "C"]
    assert catalog_import._split_tags("") == []
    assert catalog_import._split_tags(None) == []
    assert catalog_import._split_tags("Solo") == ["Solo"]


def test_parse_int():
    assert catalog_import._parse_int("8,983") == 8983
    assert catalog_import._parse_int(42) == 42
    assert catalog_import._parse_int(None) is None
    assert catalog_import._parse_int("not a number") is None


def test_parse_chapters():
    assert catalog_import._parse_chapters("1/1") == (1, 1)
    assert catalog_import._parse_chapters("10/?") == (10, None)
    assert catalog_import._parse_chapters(None) == (None, None)


def test_transform_row_splits_category_into_fandoms_and_genre_into_freeform():
    with tempfile.TemporaryDirectory() as tmp:
        source_path = _make_source_db(tmp, [_SAMPLE_ROW])
        conn = sqlite3.connect(source_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM works").fetchone()
        colmap = catalog_import._build_column_map([d[0] for d in conn.execute("SELECT * FROM works").description])
        record = catalog_import._transform_row(row, colmap)
        conn.close()

    assert record["work_id"] == "35282845"
    assert record["fandoms"] == ["Tokyo Revengers (Anime)", "Tokyo Revengers (Manga)"]
    assert record["relationships"] == ["Takemichi/Reader"]
    # "F/M" is AO3's own Category vocabulary, pulled out of Genre via
    # epub_meta.classify_subjects rather than left in as a freeform tag.
    assert record["categories"] == ["F/M"]
    assert record["freeform"] == ["Smut", "Unprotected Sex"]
    assert record["word_count"] == 8983
    assert record["chapters_have"] == 1 and record["chapters_total"] == 1


def test_transform_row_returns_none_without_a_parseable_work_id():
    with tempfile.TemporaryDirectory() as tmp:
        row = dict(_SAMPLE_ROW, **{"Story URL": ""})
        source_path = _make_source_db(tmp, [row])
        conn = sqlite3.connect(source_path)
        conn.row_factory = sqlite3.Row
        description = conn.execute("SELECT * FROM works").description
        colmap = catalog_import._build_column_map([d[0] for d in description])
        source_row = conn.execute("SELECT * FROM works").fetchone()
        record = catalog_import._transform_row(source_row, colmap)
        conn.close()

    assert record is None


def test_list_tables():
    with tempfile.TemporaryDirectory() as tmp:
        source_path = _make_source_db(tmp, [_SAMPLE_ROW], table="my_library")
        assert catalog_import.list_tables(source_path) == ["my_library"]


def test_import_from_sqlite_auto_detects_the_only_table():
    with tempfile.TemporaryDirectory() as tmp:
        source_path = _make_source_db(tmp, [_SAMPLE_ROW])
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        imported, skipped = catalog_import.import_from_sqlite(db_path, source_path)

        assert (imported, skipped) == (1, 0)
        rows = db.get_all_catalog_works(db_path)
        assert rows["35282845"]["title"] == "glass in the park"


def test_import_from_sqlite_requires_table_name_when_ambiguous():
    with tempfile.TemporaryDirectory() as tmp:
        source_path = _make_source_db(tmp, [_SAMPLE_ROW], table="a")
        conn = sqlite3.connect(source_path)
        conn.execute("CREATE TABLE b (x TEXT)")
        conn.commit()
        conn.close()
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        try:
            catalog_import.import_from_sqlite(db_path, source_path)
            assert False, "expected CatalogImportError"
        except catalog_import.CatalogImportError as exc:
            assert "a" in str(exc) and "b" in str(exc)


def test_import_from_sqlite_skips_rows_with_no_work_id():
    with tempfile.TemporaryDirectory() as tmp:
        good = _SAMPLE_ROW
        bad = dict(_SAMPLE_ROW, **{"Story URL": "", "Title": "No URL"})
        source_path = _make_source_db(tmp, [good, bad])
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        imported, skipped = catalog_import.import_from_sqlite(db_path, source_path)

        assert (imported, skipped) == (1, 1)


def test_import_from_sqlite_batches_across_multiple_rows():
    with tempfile.TemporaryDirectory() as tmp:
        rows = [dict(_SAMPLE_ROW, **{"Story URL": f"https://archiveofourown.org/works/{i}"}) for i in range(5)]
        source_path = _make_source_db(tmp, rows)
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        imported, skipped = catalog_import.import_from_sqlite(db_path, source_path, batch_size=2)

        assert (imported, skipped) == (5, 0)
        assert db.count_catalog_works(db_path) == 5


def test_tag_rows_for_record_flattens_every_list_field():
    record = {
        "work_id": "1",
        "fandoms": ["Doctor Who", "Torchwood"],
        "relationships": ["Ianto/Jack"],
        "freeform": ["Angst"],
        "warnings": ["No Archive Warnings Apply"],
        "categories": ["Gen"],
    }
    rows = catalog_import._tag_rows_for_record(record)
    assert set(rows) == {
        ("1", "fandom", "Doctor Who"),
        ("1", "fandom", "Torchwood"),
        ("1", "relationship", "Ianto/Jack"),
        ("1", "freeform", "Angst"),
        ("1", "warning", "No Archive Warnings Apply"),
        ("1", "category", "Gen"),
    }


def test_import_from_sqlite_populates_catalog_work_tags():
    with tempfile.TemporaryDirectory() as tmp:
        source_path = _make_source_db(tmp, [_SAMPLE_ROW])
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        catalog_import.import_from_sqlite(db_path, source_path)

        assert db.get_catalog_tag_values(db_path, "fandom") == {"Tokyo Revengers (Anime)", "Tokyo Revengers (Manga)"}
        assert db.get_catalog_tag_values(db_path, "relationship") == {"Takemichi/Reader"}
        works, _, _ = db.search_catalog_works(db_path, "fandom", "Tokyo Revengers (Anime)", 1, 25)
        assert works[0]["work_id"] == "35282845"


def test_imported_catalog_work_lands_in_catalog_works_not_load_cached():
    # catalog_works is deliberately never merged into scanner.py's
    # per-request WorkEntry pipeline (see scanner.py's own module
    # docstring) -- db.get_all_catalog_works is an unbounded fetchall over
    # the whole table, fine for this one-off import but not for something
    # re-run on every page load at real (multi-million-row) scale.
    with tempfile.TemporaryDirectory() as tmp:
        source_path = _make_source_db(tmp, [_SAMPLE_ROW])
        db_path = os.path.join(tmp, "app.db")
        db.init_db(db_path)

        catalog_import.import_from_sqlite(db_path, source_path)
        scanner.rebuild_work_tags(db_path)

        assert db.get_all_catalog_works(db_path)["35282845"]["title"] == "glass in the park"
        result = scanner.load_cached(db_path)
        assert result.entries == []
