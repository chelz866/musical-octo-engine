import os
import tempfile
import time

from app.ao3_client import _remove_stale_duplicate_files, _work_id_from_url


def test_work_id_from_url_extracts_the_trailing_id():
    assert _work_id_from_url("https://archiveofourown.org/works/12345") == "12345"


def test_work_id_from_url_strips_a_trailing_slash():
    assert _work_id_from_url("https://archiveofourown.org/works/12345/") == "12345"


def test_remove_stale_duplicate_files_leaves_a_single_file_alone():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "12345 Some Title - Author.epub")
        open(path, "w").close()

        _remove_stale_duplicate_files(tmp, "12345")

        assert os.listdir(tmp) == ["12345 Some Title - Author.epub"]


def test_remove_stale_duplicate_files_keeps_only_the_newest_of_two():
    with tempfile.TemporaryDirectory() as tmp:
        old_path = os.path.join(tmp, "12345 Some Title - Author.epub")
        new_path = os.path.join(tmp, "12345_Some Title - Author.epub")
        open(old_path, "w").close()
        open(new_path, "w").close()

        old_time = time.time() - 90 * 24 * 3600
        os.utime(old_path, (old_time, old_time))

        _remove_stale_duplicate_files(tmp, "12345")

        assert os.listdir(tmp) == ["12345_Some Title - Author.epub"]


def test_remove_stale_duplicate_files_only_touches_files_for_the_given_work_id():
    with tempfile.TemporaryDirectory() as tmp:
        old_path = os.path.join(tmp, "12345 Some Title - Author.epub")
        new_path = os.path.join(tmp, "12345_Some Title - Author.epub")
        other_work_path = os.path.join(tmp, "99999 Unrelated Work - Author.epub")
        open(old_path, "w").close()
        open(new_path, "w").close()
        open(other_work_path, "w").close()

        old_time = time.time() - 90 * 24 * 3600
        os.utime(old_path, (old_time, old_time))

        _remove_stale_duplicate_files(tmp, "12345")

        assert sorted(os.listdir(tmp)) == ["12345_Some Title - Author.epub", "99999 Unrelated Work - Author.epub"]


def test_remove_stale_duplicate_files_no_op_when_nothing_matches():
    with tempfile.TemporaryDirectory() as tmp:
        _remove_stale_duplicate_files(tmp, "12345")
        assert os.listdir(tmp) == []
