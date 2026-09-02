import os
import tempfile

import pytest

from app import db


def test_set_title_author_and_get():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "123", "Fixed Title", "Fixed Author")

        override = db.get_override(path, "123")
        assert override.title == "Fixed Title"
        assert override.author == "Fixed Author"
        assert override.dismissed is False


def test_set_title_author_overwrites_previous_values():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "123", "First", None)
        db.set_title_author(path, "123", "Second", "Someone")

        override = db.get_override(path, "123")
        assert override.title == "Second"
        assert override.author == "Someone"


def test_set_dismissed_preserves_other_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "123", "Fixed Title", None)
        db.set_dismissed(path, "123", True)

        override = db.get_override(path, "123")
        assert override.title == "Fixed Title"
        assert override.dismissed is True

        db.set_dismissed(path, "123", False)
        override = db.get_override(path, "123")
        assert override.dismissed is False
        assert override.title == "Fixed Title"


def test_get_override_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_override(path, "does-not-exist") is None


def test_get_all_overrides():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_title_author(path, "1", "A", None)
        db.set_dismissed(path, "2", True)

        all_overrides = db.get_all_overrides(path)
        assert set(all_overrides) == {"1", "2"}
        assert all_overrides["1"].title == "A"
        assert all_overrides["2"].dismissed is True


def test_set_and_get_all_tag_categories():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_categories(path, {"Torchwood": "fandom", "Ianto Jones": "character"})

        categories = db.get_all_tag_categories(path)
        assert categories == {"Torchwood": "fandom", "Ianto Jones": "character"}


def test_set_tag_categories_overwrites_existing_entries():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_categories(path, {"Torchwood": "freeform"})
        db.set_tag_categories(path, {"Torchwood": "fandom", "Doctor Who": "fandom"})

        categories = db.get_all_tag_categories(path)
        assert categories == {"Torchwood": "fandom", "Doctor Who": "fandom"}


def test_get_all_tag_categories_empty_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_all_tag_categories(path) == {}


def test_set_and_get_all_tag_media_types():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_media_types(path, "Doctor Who", {"TV Shows"})
        db.set_tag_media_types(path, "Harry Potter", {"Books & Literature"})

        assert db.get_all_tag_media_types(path) == {
            "Doctor Who": {"TV Shows"},
            "Harry Potter": {"Books & Literature"},
        }


def test_set_tag_media_types_allows_more_than_one_at_once():
    # A Fandom can genuinely belong to more than one AO3-style category
    # (e.g. a franchise spanning both a movie and a comic line).
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_media_types(path, "Doctor Who", {"TV Shows", "Books & Literature"})

        assert db.get_all_tag_media_types(path) == {"Doctor Who": {"TV Shows", "Books & Literature"}}


def test_set_tag_media_types_replaces_the_whole_set():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_media_types(path, "Doctor Who", {"TV Shows"})
        db.set_tag_media_types(path, "Doctor Who", {"Movies"})

        assert db.get_all_tag_media_types(path) == {"Doctor Who": {"Movies"}}


def test_set_tag_media_types_empty_set_clears_the_tag_entirely():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_media_types(path, "Doctor Who", {"TV Shows"})
        db.set_tag_media_types(path, "Harry Potter", {"Books & Literature"})

        db.set_tag_media_types(path, "Doctor Who", set())

        assert db.get_all_tag_media_types(path) == {"Harry Potter": {"Books & Literature"}}


def test_get_all_tag_media_types_empty_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_all_tag_media_types(path) == {}


def test_create_metatag_round_trip_and_top_level_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        top_id = db.create_metatag(path, "Love", None)
        child_id = db.create_metatag(path, "Characters in love", top_id)

        assert db.get_all_metatags(path) == {
            top_id: ("Love", None),
            child_id: ("Characters in love", top_id),
        }


def test_create_metatag_refuses_a_duplicate_name():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_metatag(path, "Love", None)
        with pytest.raises(ValueError):
            db.create_metatag(path, "Love", None)


def test_delete_metatag_refuses_a_node_with_children():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        top_id = db.create_metatag(path, "Love", None)
        db.create_metatag(path, "Characters in love", top_id)
        with pytest.raises(ValueError):
            db.delete_metatag(path, top_id)
        assert top_id in db.get_all_metatags(path)


def test_delete_metatag_refuses_a_node_with_linked_tags():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        leaf_id = db.create_metatag(path, "Ilya is in love", None)
        db.add_tag_to_metatag(path, leaf_id, "Ilya loves Shane")
        with pytest.raises(ValueError):
            db.delete_metatag(path, leaf_id)
        assert leaf_id in db.get_all_metatags(path)


def test_delete_metatag_succeeds_on_a_true_empty_leaf():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        leaf_id = db.create_metatag(path, "Ilya is in love", None)
        db.delete_metatag(path, leaf_id)
        assert db.get_all_metatags(path) == {}


def test_add_and_remove_tag_from_metatag():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        leaf_id = db.create_metatag(path, "Ilya is in love", None)
        db.add_tag_to_metatag(path, leaf_id, "Ilya loves Shane")
        assert db.get_all_metatag_tags(path) == {leaf_id: {"Ilya loves Shane"}}

        db.remove_tag_from_metatag(path, leaf_id, "Ilya loves Shane")
        assert db.get_all_metatag_tags(path) == {}


def test_a_tag_can_be_linked_to_more_than_one_metatag():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        love_id = db.create_metatag(path, "Love", None)
        angst_id = db.create_metatag(path, "Angst", None)
        db.add_tag_to_metatag(path, love_id, "Ilya loves Shane")
        db.add_tag_to_metatag(path, angst_id, "Ilya loves Shane")

        assert db.get_all_metatag_tags(path) == {
            love_id: {"Ilya loves Shane"},
            angst_id: {"Ilya loves Shane"},
        }


def test_set_tag_wrangling_synonym_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "MCU", "synonym", "Marvel Cinematic Universe")

        assert db.get_tag_synonyms(path) == {"MCU": "Marvel Cinematic Universe"}
        assert db.get_tag_children(path) == {}
        assert db.get_all_tag_wranglings(path) == {"MCU": ("synonym", "Marvel Cinematic Universe")}


def test_set_tag_wrangling_child_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "Alternate Reality - Canon Divergence", "child", "Alternate Reality")
        db.set_tag_wrangling(path, "Alternate Reality - Fantasy", "child", "Alternate Reality")

        assert db.get_tag_children(path) == {
            "Alternate Reality": {"Alternate Reality - Canon Divergence", "Alternate Reality - Fantasy"},
        }
        assert db.get_tag_synonyms(path) == {}


def test_set_tag_wrangling_overwrites_existing_relation_for_same_tag():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "MCU", "child", "Marvel Cinematic Universe")
        db.set_tag_wrangling(path, "MCU", "synonym", "Marvel Cinematic Universe")

        assert db.get_all_tag_wranglings(path) == {"MCU": ("synonym", "Marvel Cinematic Universe")}


def test_set_tag_wrangling_rejects_self_wrangle():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        try:
            db.set_tag_wrangling(path, "MCU", "synonym", "MCU")
            assert False, "expected ValueError"
        except ValueError:
            pass
        assert db.get_all_tag_wranglings(path) == {}


def test_set_tag_wrangling_allows_chaining_into_an_already_wrangled_target():
    # Multi-level hierarchies are the whole point now (e.g. a Character
    # wrangled under a Relationship, itself wrangled under a Fandom).
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "B", "synonym", "C")
        db.set_tag_wrangling(path, "A", "synonym", "B")
        assert db.get_all_tag_wranglings(path) == {"A": ("synonym", "B"), "B": ("synonym", "C")}


def test_set_tag_wrangling_allows_wrangling_a_tag_that_already_has_followers():
    # A node can be both a parent (things point at it) and itself a child
    # (it points at something else) at the same time -- that's just a tree.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "A", "synonym", "B")
        db.set_tag_wrangling(path, "B", "synonym", "C")
        assert db.get_all_tag_wranglings(path) == {"A": ("synonym", "B"), "B": ("synonym", "C")}


def test_set_tag_wrangling_rejects_a_direct_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "A", "child", "B")
        try:
            db.set_tag_wrangling(path, "B", "child", "A")
            assert False, "expected ValueError"
        except ValueError:
            pass
        assert "B" not in db.get_all_tag_wranglings(path)


def test_set_tag_wrangling_rejects_an_indirect_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "A", "child", "B")
        db.set_tag_wrangling(path, "B", "child", "C")
        try:
            db.set_tag_wrangling(path, "C", "child", "A")
            assert False, "expected ValueError"
        except ValueError:
            pass
        assert "C" not in db.get_all_tag_wranglings(path)


def test_remove_tag_wrangling():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "MCU", "synonym", "Marvel Cinematic Universe")
        db.remove_tag_wrangling(path, "MCU")

        assert db.get_all_tag_wranglings(path) == {}
        # Removing frees it up to be wrangled again without the "already
        # has followers" guard firing on stale data.
        db.set_tag_wrangling(path, "MCU", "synonym", "Marvel Cinematic Universe")
        assert db.get_all_tag_wranglings(path) == {"MCU": ("synonym", "Marvel Cinematic Universe")}


def test_get_all_tag_descendants_flattens_multi_level_child_chains():
    # Precomputed transitive closure of 'child' edges -- a grandparent
    # should map directly to its grandchild, not just its own child.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "Harry Potter/Hermione Granger", "child", "Harry Potter")
        db.set_tag_wrangling(path, "Hermione Granger", "child", "Harry Potter/Hermione Granger")

        descendants = db.get_all_tag_descendants(path)
        assert descendants["Harry Potter"] == {"Harry Potter/Hermione Granger", "Hermione Granger"}
        assert descendants["Harry Potter/Hermione Granger"] == {"Hermione Granger"}
        assert "Hermione Granger" not in descendants


def test_get_all_tag_descendants_ignores_synonym_edges():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "MCU", "synonym", "Marvel Cinematic Universe")
        assert db.get_all_tag_descendants(path) == {}


def test_get_all_tag_descendants_updates_when_a_wrangling_is_removed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_tag_wrangling(path, "Alternate Reality - Canon Divergence", "child", "Alternate Reality")
        assert db.get_all_tag_descendants(path) == {"Alternate Reality": {"Alternate Reality - Canon Divergence"}}

        db.remove_tag_wrangling(path, "Alternate Reality - Canon Divergence")
        assert db.get_all_tag_descendants(path) == {}


def test_init_db_migrates_legacy_is_fandom_to_category():
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        # Simulate rows from before `category` existed -- is_fandom-only.
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO tag_flags (tag, is_fandom) VALUES (?, ?)", ("Torchwood", 1))
        conn.execute("INSERT INTO tag_flags (tag, is_fandom) VALUES (?, ?)", ("Ianto Jones", 0))
        conn.commit()
        conn.close()

        db.init_db(path)  # migration runs here

        categories = db.get_all_tag_categories(path)
        assert categories == {"Torchwood": "fandom", "Ianto Jones": "freeform"}

        # idempotent: running init_db again doesn't clobber a subsequent
        # explicit correction
        db.set_tag_categories(path, {"Ianto Jones": "character"})
        db.init_db(path)
        assert db.get_all_tag_categories(path)["Ianto Jones"] == "character"


def test_init_db_migrates_legacy_single_value_tag_media_types():
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        # Simulate rows from before a Fandom could have more than one media
        # type -- the old (tag TEXT PRIMARY KEY, media_type) shape.
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE tag_media_types")
        conn.execute("CREATE TABLE tag_media_types (tag TEXT PRIMARY KEY, media_type TEXT NOT NULL)")
        conn.execute("INSERT INTO tag_media_types (tag, media_type) VALUES (?, ?)", ("Doctor Who", "TV Shows"))
        conn.commit()
        conn.close()

        db.init_db(path)  # migration runs here

        assert db.get_all_tag_media_types(path) == {"Doctor Who": {"TV Shows"}}

        # idempotent: running init_db again doesn't wipe a subsequent
        # multi-value addition
        db.set_tag_media_types(path, "Doctor Who", {"TV Shows", "Books & Literature"})
        db.init_db(path)
        assert db.get_all_tag_media_types(path) == {"Doctor Who": {"TV Shows", "Books & Literature"}}


def _sample_work_row(work_id="1", **overrides):
    row = {c: None for c in db.WORKS_CACHE_COLUMNS}
    row.update({
        "work_id": work_id, "title": "A Title", "author": "An Author",
        "size_bytes": 123, "on_disk": 1, "log_success": 1,
    })
    row.update(overrides)
    return row


def test_save_and_load_works_cache_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_works_cache(path, [_sample_work_row("1"), _sample_work_row("2", title="Other")])

        rows = db.load_works_cache(path)
        assert {r["work_id"] for r in rows} == {"1", "2"}
        assert next(r for r in rows if r["work_id"] == "1")["title"] == "A Title"


def test_save_works_cache_replaces_previous_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_works_cache(path, [_sample_work_row("1")])
        db.save_works_cache(path, [_sample_work_row("2")])

        rows = db.load_works_cache(path)
        assert {r["work_id"] for r in rows} == {"2"}


def test_pop_legacy_tracked_feeds_returns_empty_when_no_legacy_table(tmp_path):
    path = str(tmp_path / "app.db")
    db.init_db(path)
    assert db.pop_legacy_tracked_feeds(path) == []


def test_pop_legacy_tracked_feeds_migrates_and_drops_old_tables(tmp_path):
    import sqlite3

    path = str(tmp_path / "app.db")
    db.init_db(path)

    # simulate a database from before the switch to the `reader` library,
    # which stored tracked feeds in this same app.db
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tracked_feeds (id INTEGER PRIMARY KEY, url TEXT, label TEXT, title TEXT)")
    conn.execute("CREATE TABLE feed_entries (feed_id INTEGER, work_id TEXT)")
    conn.execute("INSERT INTO tracked_feeds (url, label) VALUES (?, ?)", ("https://example.com/a.atom", "A"))
    conn.execute("INSERT INTO tracked_feeds (url, label) VALUES (?, ?)", ("https://example.com/b.atom", None))
    conn.commit()
    conn.close()

    migrated = db.pop_legacy_tracked_feeds(path)
    assert set(migrated) == {("https://example.com/a.atom", "A"), ("https://example.com/b.atom", None)}

    # idempotent: tables are gone, so a second call finds nothing to migrate
    assert db.pop_legacy_tracked_feeds(path) == []

    conn = sqlite3.connect(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "tracked_feeds" not in tables
    assert "feed_entries" not in tables


def test_set_title_author_preserves_dismissed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_dismissed(path, "1", True)

        db.set_title_author(path, "1", "New Title", "New Author")

        override = db.get_override(path, "1")
        assert override.dismissed is True
        assert override.title == "New Title"
        assert override.author == "New Author"


def test_init_db_adds_missing_column_to_existing_table_without_losing_data():
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")

        # Simulate a database created by an older version of the app, before
        # fandom_candidates existed on works_cache.
        old_columns = [c for c in db.WORKS_CACHE_COLUMNS if c != "fandom_candidates"]
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE works_cache ({', '.join(f'{c} TEXT' for c in old_columns)}, PRIMARY KEY (work_id))")
        conn.execute(
            f"INSERT INTO works_cache ({', '.join(old_columns)}) VALUES ({', '.join('?' for _ in old_columns)})",
            tuple("1" if c == "work_id" else None for c in old_columns),
        )
        conn.commit()
        conn.close()

        db.init_db(path)  # should migrate in place, not wipe existing rows

        rows = db.load_works_cache(path)
        assert len(rows) == 1
        assert rows[0]["work_id"] == "1"
        assert rows[0]["fandom_candidates"] is None

        # and the newly-added column is now usable
        db.save_works_cache(path, [{**{c: None for c in db.WORKS_CACHE_COLUMNS}, "work_id": "2", "fandom_candidates": "Torchwood"}])
        assert db.load_works_cache(path)[0]["fandom_candidates"] == "Torchwood"


def test_init_db_migrates_word_count_and_chapters_as_integer_columns():
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")

        # Simulate a database from before word_count/chapters_have/
        # chapters_total/language existed on works_cache.
        old_columns = [c for c in db.WORKS_CACHE_COLUMNS if c not in ("word_count", "chapters_have", "chapters_total", "language")]
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE works_cache ({', '.join(f'{c} TEXT' for c in old_columns)}, PRIMARY KEY (work_id))")
        conn.execute(
            f"INSERT INTO works_cache ({', '.join(old_columns)}) VALUES ({', '.join('?' for _ in old_columns)})",
            tuple("1" if c == "work_id" else None for c in old_columns),
        )
        conn.commit()
        conn.close()

        db.init_db(path)

        db.save_works_cache(path, [{
            **{c: None for c in db.WORKS_CACHE_COLUMNS},
            "work_id": "2", "language": "en", "word_count": 22513, "chapters_have": 1, "chapters_total": 1,
        }])
        row = db.load_works_cache(path)[0]
        assert row["language"] == "en"
        assert row["word_count"] == 22513
        assert row["chapters_have"] == 1
        assert row["chapters_total"] == 1


def test_save_and_get_all_abs_matches():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_abs_matches(path, {"1": "item-a", "2": "item-b"})

        assert db.get_all_abs_matches(path) == {"1": "item-a", "2": "item-b"}


def test_save_abs_matches_replaces_previous_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.save_abs_matches(path, {"1": "item-a"})
        db.save_abs_matches(path, {"2": "item-b"})

        assert db.get_all_abs_matches(path) == {"2": "item-b"}


def test_get_all_abs_matches_empty_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_all_abs_matches(path) == {}


def test_meta_get_and_set():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_meta(path, "last_refreshed_at") is None

        db.set_meta(path, "last_refreshed_at", "2026-01-01T00:00:00")
        assert db.get_meta(path, "last_refreshed_at") == "2026-01-01T00:00:00"

        db.set_meta(path, "last_refreshed_at", "2026-01-02T00:00:00")
        assert db.get_meta(path, "last_refreshed_at") == "2026-01-02T00:00:00"


def test_count_users_and_create_user():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.count_users(path) == 0

        db.create_user(path, "admin", "hashed", "admin")
        assert db.count_users(path) == 1

        users = db.list_users(path)
        assert len(users) == 1
        assert users[0].username == "admin"
        assert users[0].role == "admin"
        assert users[0].is_admin is True


def test_get_user_credentials_round_trip_and_missing_user():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed-pw", "admin")

        result = db.get_user_credentials(path, "admin")
        assert result is not None
        user, password_hash = result
        assert user.username == "admin"
        assert password_hash == "hashed-pw"

        assert db.get_user_credentials(path, "does-not-exist") is None


def test_get_user_by_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "friend", "hashed", "user")
        created = db.list_users(path)[0]

        fetched = db.get_user_by_id(path, created.id)
        assert fetched.username == "friend"
        assert fetched.is_admin is False

        assert db.get_user_by_id(path, 9999) is None


def test_set_user_password_updates_hash():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "old-hash", "admin")
        user_id = db.list_users(path)[0].id

        db.set_user_password(path, user_id, "new-hash")

        _, password_hash = db.get_user_credentials(path, "admin")
        assert password_hash == "new-hash"


def test_session_create_get_and_delete():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.create_session(path, "tok123", user_id, "2026-01-01T00:00:00")
        user = db.get_session_user(path, "tok123")
        assert user is not None
        assert user.username == "admin"

        db.delete_session(path, "tok123")
        assert db.get_session_user(path, "tok123") is None


def test_get_session_user_unknown_token_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        assert db.get_session_user(path, "does-not-exist") is None


def test_bookmark_add_remove_and_get_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        assert db.get_bookmarked_work_ids(path, user_id) == set()

        db.add_bookmark(path, user_id, "123", "2026-01-01T00:00:00")
        db.add_bookmark(path, user_id, "456", "2026-01-01T00:00:00")
        assert db.get_bookmarked_work_ids(path, user_id) == {"123", "456"}

        db.remove_bookmark(path, user_id, "123")
        assert db.get_bookmarked_work_ids(path, user_id) == {"456"}


def test_add_bookmark_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.add_bookmark(path, user_id, "123", "2026-01-01T00:00:00")
        db.add_bookmark(path, user_id, "123", "2026-01-02T00:00:00")  # should not raise or duplicate
        assert db.get_bookmarked_work_ids(path, user_id) == {"123"}


def test_bookmarks_are_scoped_per_user():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        db.create_user(path, "friend", "hashed", "user")
        admin_id = db.get_user_credentials(path, "admin")[0].id
        friend_id = db.get_user_credentials(path, "friend")[0].id

        db.add_bookmark(path, admin_id, "123", "2026-01-01T00:00:00")
        assert db.get_bookmarked_work_ids(path, admin_id) == {"123"}
        assert db.get_bookmarked_work_ids(path, friend_id) == set()


def test_set_bookmark_note_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id
        db.add_bookmark(path, user_id, "123", "2026-01-01T00:00:00")

        db.set_bookmark_note(path, user_id, "123", "read this on a rainy day")
        assert db.get_bookmark_notes(path, user_id) == {"123": "read this on a rainy day"}


def test_set_bookmark_note_is_noop_when_not_bookmarked():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.set_bookmark_note(path, user_id, "123", "no bookmark for this work")
        assert db.get_bookmark_notes(path, user_id) == {}


def test_set_bookmark_note_blank_clears_it():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id
        db.add_bookmark(path, user_id, "123", "2026-01-01T00:00:00")

        db.set_bookmark_note(path, user_id, "123", "a note")
        db.set_bookmark_note(path, user_id, "123", "   ")
        assert db.get_bookmark_notes(path, user_id) == {}


def test_removing_bookmark_clears_its_note():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id
        db.add_bookmark(path, user_id, "123", "2026-01-01T00:00:00")
        db.set_bookmark_note(path, user_id, "123", "a note")

        db.remove_bookmark(path, user_id, "123")
        db.add_bookmark(path, user_id, "123", "2026-01-02T00:00:00")
        assert db.get_bookmark_notes(path, user_id) == {}


def test_create_theme_does_not_activate_it():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        theme_id = db.create_theme(path, user_id, "Dark", "body { color: red; }", "2026-01-01T00:00:00")
        assert db.get_active_theme_id(path, user_id) is None
        assert db.get_active_theme_css(path, user_id) is None
        assert db.list_user_themes(path, user_id) == [{"id": theme_id, "name": "Dark", "css": "body { color: red; }"}]


def test_set_active_theme_switches_the_applied_css():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        light = db.create_theme(path, user_id, "Light", "body { color: black; }", "2026-01-01T00:00:00")
        dark = db.create_theme(path, user_id, "Dark", "body { color: white; }", "2026-01-01T00:00:00")

        db.set_active_theme(path, user_id, light)
        assert db.get_active_theme_id(path, user_id) == light
        assert db.get_active_theme_css(path, user_id) == "body { color: black; }"

        # Switching to another saved theme doesn't lose the first one.
        db.set_active_theme(path, user_id, dark)
        assert db.get_active_theme_id(path, user_id) == dark
        assert db.get_active_theme_css(path, user_id) == "body { color: white; }"
        assert len(db.list_user_themes(path, user_id)) == 2


def test_set_active_theme_none_reverts_to_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        theme_id = db.create_theme(path, user_id, "Dark", "body {}", "2026-01-01T00:00:00")
        db.set_active_theme(path, user_id, theme_id)
        db.set_active_theme(path, user_id, None)

        assert db.get_active_theme_id(path, user_id) is None
        assert db.get_active_theme_css(path, user_id) is None
        # Still saved, just not active.
        assert len(db.list_user_themes(path, user_id)) == 1


def test_set_active_theme_ignores_a_theme_owned_by_someone_else():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "alice", "hashed", "user")
        db.create_user(path, "bob", "hashed", "user")
        users = {u.username: u.id for u in db.list_users(path)}
        alice_id = users["alice"]
        bob_id = users["bob"]

        bobs_theme = db.create_theme(path, bob_id, "Bob's theme", "body {}", "2026-01-01T00:00:00")
        db.set_active_theme(path, alice_id, bobs_theme)

        assert db.get_active_theme_id(path, alice_id) is None


def test_update_theme_changes_name_and_css_without_touching_active_state():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        theme_id = db.create_theme(path, user_id, "Dark", "body { color: red; }", "2026-01-01T00:00:00")
        db.set_active_theme(path, user_id, theme_id)

        db.update_theme(path, user_id, theme_id, "Darker", "body { color: black; }")

        assert db.get_user_theme(path, user_id, theme_id) == {"id": theme_id, "name": "Darker", "css": "body { color: black; }"}
        assert db.get_active_theme_id(path, user_id) == theme_id
        assert db.get_active_theme_css(path, user_id) == "body { color: black; }"


def test_delete_theme_clears_it_as_active():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        theme_id = db.create_theme(path, user_id, "Dark", "body {}", "2026-01-01T00:00:00")
        db.set_active_theme(path, user_id, theme_id)

        db.delete_theme(path, user_id, theme_id)

        assert db.list_user_themes(path, user_id) == []
        assert db.get_active_theme_id(path, user_id) is None


def test_delete_theme_leaves_a_different_active_theme_alone():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        light = db.create_theme(path, user_id, "Light", "body {}", "2026-01-01T00:00:00")
        dark = db.create_theme(path, user_id, "Dark", "body {}", "2026-01-01T00:00:00")
        db.set_active_theme(path, user_id, dark)

        db.delete_theme(path, user_id, light)

        assert db.get_active_theme_id(path, user_id) == dark


def test_legacy_theme_css_migrates_into_a_named_active_theme():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        # Simulate a pre-migration row: the old single-theme column set
        # directly, as it would have been by the removed set_user_theme_css.
        with db._connect(path) as conn:
            conn.execute("UPDATE users SET theme_css = ? WHERE id = ?", ("body { color: red; }", user_id))

        db.init_db(path)  # re-running init_db performs the one-time migration

        themes = db.list_user_themes(path, user_id)
        assert len(themes) == 1
        assert themes[0]["name"] == "My Theme"
        assert themes[0]["css"] == "body { color: red; }"
        assert db.get_active_theme_css(path, user_id) == "body { color: red; }"

        # Idempotent: running init_db again doesn't create a duplicate.
        db.init_db(path)
        assert len(db.list_user_themes(path, user_id)) == 1


def test_user_home_edit_source_defaults_to_false():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        assert db.get_user_home_edit_source(path, user_id) is False


def test_user_home_edit_source_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.set_user_home_edit_source(path, user_id, True)
        assert db.get_user_home_edit_source(path, user_id) is True

        db.set_user_home_edit_source(path, user_id, False)
        assert db.get_user_home_edit_source(path, user_id) is False


def test_user_timezone_defaults_to_none_and_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        assert db.get_user_by_id(path, user_id).timezone is None

        db.set_user_timezone(path, user_id, "America/New_York")
        assert db.get_user_by_id(path, user_id).timezone == "America/New_York"

        db.set_user_timezone(path, user_id, None)
        assert db.get_user_by_id(path, user_id).timezone is None


def test_get_session_user_and_get_user_credentials_include_timezone():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id
        db.set_user_timezone(path, user_id, "Europe/London")

        db.create_session(path, "tok", user_id, "2026-01-01T00:00:00")
        assert db.get_session_user(path, "tok").timezone == "Europe/London"

        user, _ = db.get_user_credentials(path, "admin")
        assert user.timezone == "Europe/London"


def test_enqueue_downloads_adds_new_items_and_reports_count():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        added = db.enqueue_downloads(
            path,
            [("1", "https://archiveofourown.org/works/1", "Work One"), ("2", "https://archiveofourown.org/works/2", "Work Two")],
            "2026-01-01T00:00:00",
        )
        assert added == 2
        assert db.get_download_queue_counts(path) == {"pending": 2, "downloading": 0, "done": 0}


def test_enqueue_downloads_skips_a_work_id_already_in_the_queue():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        db.enqueue_downloads(path, [("1", "https://archiveofourown.org/works/1", "Work One")], "2026-01-01T00:00:00")
        added_again = db.enqueue_downloads(path, [("1", "https://archiveofourown.org/works/1", "Work One")], "2026-01-01T00:00:01")

        assert added_again == 0
        assert db.get_download_queue_counts(path)["pending"] == 1


def test_get_next_pending_download_returns_oldest_first_and_none_when_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        assert db.get_next_pending_download(path) is None

        db.enqueue_downloads(path, [("1", "https://archiveofourown.org/works/1", "First")], "2026-01-01T00:00:00")
        db.enqueue_downloads(path, [("2", "https://archiveofourown.org/works/2", "Second")], "2026-01-01T00:00:01")

        item = db.get_next_pending_download(path)
        assert item["work_id"] == "1"
        assert item["title"] == "First"


def test_mark_download_status_moves_an_item_out_of_pending():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        db.enqueue_downloads(path, [("1", "https://archiveofourown.org/works/1", "Work One")], "2026-01-01T00:00:00")
        item = db.get_next_pending_download(path)

        db.mark_download_status(path, item["id"], "downloading")
        assert db.get_download_queue_counts(path) == {"pending": 0, "downloading": 1, "done": 0}
        assert db.get_next_pending_download(path) is None

        db.mark_download_status(path, item["id"], "done", "2026-01-01T00:05:00")
        assert db.get_download_queue_counts(path) == {"pending": 0, "downloading": 0, "done": 1}


def test_a_stuck_downloading_row_resets_to_pending_on_init_db():
    # Simulates the app dying mid-item on a previous run -- init_db's
    # startup migration should put it back to pending so the worker
    # picks it up again instead of leaving it stuck forever.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.enqueue_downloads(path, [("1", "https://archiveofourown.org/works/1", "Work One")], "2026-01-01T00:00:00")
        item = db.get_next_pending_download(path)
        db.mark_download_status(path, item["id"], "downloading")

        db.init_db(path)  # re-running init_db performs the one-time-per-boot reset

        assert db.get_download_queue_counts(path) == {"pending": 1, "downloading": 0, "done": 0}


def test_clear_finished_downloads_removes_only_done_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.enqueue_downloads(
            path,
            [("1", "https://archiveofourown.org/works/1", "Done"), ("2", "https://archiveofourown.org/works/2", "Still Pending")],
            "2026-01-01T00:00:00",
        )
        done_item = db.get_next_pending_download(path)
        db.mark_download_status(path, done_item["id"], "done", "2026-01-01T00:05:00")

        db.clear_finished_downloads(path)

        assert db.get_download_queue_counts(path) == {"pending": 1, "downloading": 0, "done": 0}


def test_add_manual_links_adds_new_items_and_reports_count():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        added = db.add_manual_links(
            path,
            [("1", "https://archiveofourown.org/works/1"), ("2", "https://archiveofourown.org/works/2")],
            "2026-01-01T00:00:00",
        )

        assert added == 2
        assert [link["work_id"] for link in db.list_manual_links(path)] == ["1", "2"]


def test_add_manual_links_skips_a_work_id_already_present():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)

        db.add_manual_links(path, [("1", "https://archiveofourown.org/works/1")], "2026-01-01T00:00:00")
        added_again = db.add_manual_links(path, [("1", "https://archiveofourown.org/works/1")], "2026-01-01T00:00:01")

        assert added_again == 0
        assert len(db.list_manual_links(path)) == 1


def test_remove_manual_link_removes_only_that_work_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.add_manual_links(
            path,
            [("1", "https://archiveofourown.org/works/1"), ("2", "https://archiveofourown.org/works/2")],
            "2026-01-01T00:00:00",
        )

        db.remove_manual_link(path, "1")

        assert [link["work_id"] for link in db.list_manual_links(path)] == ["2"]


def test_record_view_and_get_view_history_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.record_view(path, user_id, "1", "2026-01-01T00:00:00")
        db.record_view(path, user_id, "2", "2026-01-02T00:00:00")

        assert db.get_view_history(path, user_id) == {
            "1": "2026-01-01T00:00:00",
            "2": "2026-01-02T00:00:00",
        }


def test_record_view_upserts_to_the_latest_timestamp_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.record_view(path, user_id, "1", "2026-01-01T00:00:00")
        db.record_view(path, user_id, "1", "2026-01-05T12:00:00")

        history = db.get_view_history(path, user_id)
        assert history == {"1": "2026-01-05T12:00:00"}


def test_get_view_history_is_scoped_per_user():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        db.create_user(path, "friend", "hashed", "user")
        admin_id, friend_id = (u.id for u in db.list_users(path))

        db.record_view(path, admin_id, "1", "2026-01-01T00:00:00")
        db.record_view(path, friend_id, "2", "2026-01-01T00:00:00")

        assert db.get_view_history(path, admin_id) == {"1": "2026-01-01T00:00:00"}
        assert db.get_view_history(path, friend_id) == {"2": "2026-01-01T00:00:00"}


def test_user_abs_username_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        assert db.get_user_abs_username(path, user_id) is None

        db.set_user_abs_username(path, user_id, "chelz866")
        assert db.get_user_abs_username(path, user_id) == "chelz866"


def test_set_user_abs_username_blank_clears_it():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.set_user_abs_username(path, user_id, "chelz866")
        db.set_user_abs_username(path, user_id, "   ")
        assert db.get_user_abs_username(path, user_id) is None


def test_list_user_abs_usernames_only_includes_users_who_set_one():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        db.create_user(path, "friend", "hashed", "user")
        admin_id = db.get_user_credentials(path, "admin")[0].id
        friend_id = db.get_user_credentials(path, "friend")[0].id

        db.set_user_abs_username(path, admin_id, "chelz866")

        assert db.list_user_abs_usernames(path) == {admin_id: "chelz866"}
        assert friend_id not in db.list_user_abs_usernames(path)


def test_read_marks_round_trip_and_scoped_per_user():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        db.create_user(path, "friend", "hashed", "user")
        admin_id = db.get_user_credentials(path, "admin")[0].id
        friend_id = db.get_user_credentials(path, "friend")[0].id

        assert db.get_read_marked_work_ids(path, admin_id) == set()

        db.add_read_mark(path, admin_id, "123", "2026-01-01T00:00:00")
        assert db.get_read_marked_work_ids(path, admin_id) == {"123"}
        assert db.get_read_marked_work_ids(path, friend_id) == set()

        db.remove_read_mark(path, admin_id, "123")
        assert db.get_read_marked_work_ids(path, admin_id) == set()


def test_add_read_mark_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.add_read_mark(path, user_id, "123", "2026-01-01T00:00:00")
        db.add_read_mark(path, user_id, "123", "2026-01-02T00:00:00")
        assert db.get_read_marked_work_ids(path, user_id) == {"123"}


def test_save_abs_read_status_replaces_the_whole_snapshot_for_one_user():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        user_id = db.list_users(path)[0].id

        db.save_abs_read_status(path, user_id, {"1": "2026-01-01", "2": None})
        assert db.get_abs_read_work_ids(path, user_id) == {"1", "2"}

        db.save_abs_read_status(path, user_id, {"3": "2026-02-01"})
        assert db.get_abs_read_work_ids(path, user_id) == {"3"}


def test_abs_read_status_is_scoped_per_user():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.create_user(path, "admin", "hashed", "admin")
        db.create_user(path, "friend", "hashed", "user")
        admin_id = db.get_user_credentials(path, "admin")[0].id
        friend_id = db.get_user_credentials(path, "friend")[0].id

        db.save_abs_read_status(path, admin_id, {"1": "2026-01-01"})
        assert db.get_abs_read_work_ids(path, admin_id) == {"1"}
        assert db.get_abs_read_work_ids(path, friend_id) == set()
