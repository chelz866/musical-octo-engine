import os
import tempfile
import time
from unittest.mock import patch

from app.ao3_client import Repository, _remove_stale_duplicate_files, _work_id_from_url, build_client


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


def test_build_client_has_no_login_error_when_no_credentials_given():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as state:
        client = build_client(downloads, os.path.join(state, "log.jsonl"), state)
        assert client.login_error is None


def test_build_client_has_no_login_error_when_login_succeeds():
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as state:
        with patch.object(Repository, "login", return_value=None):
            client = build_client(downloads, os.path.join(state, "log.jsonl"), state, "user", "pass")
        assert client.login_error is None


def test_build_client_survives_a_failed_login_instead_of_raising():
    # Repository.login raises on bad credentials (or an AO3-side challenge
    # blocking an automated login) -- letting that escape build_client
    # used to take the whole download worker down before a single item was
    # attempted, with nothing downloaded and nothing logged. It should
    # come back as a working (unauthenticated) client instead, with the
    # failure recorded on login_error for the caller to surface.
    with tempfile.TemporaryDirectory() as downloads, tempfile.TemporaryDirectory() as state:
        with patch.object(Repository, "login", side_effect=Exception("Invalid username or password.")):
            client = build_client(downloads, os.path.join(state, "log.jsonl"), state, "user", "wrong-pass")
        assert client.login_error == "Invalid username or password."
        assert client.ao3 is not None
