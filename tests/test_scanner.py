import os
import tempfile

from app.scanner import scan

# Real content doesn't matter here -- these exercise the filename matching,
# not epub parsing (a bad zip still produces a WorkEntry with parse_error set,
# it's just not skipped).


def test_matches_underscore_separated_filenames():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "7773_The_Business__Basingstoke.epub"), "w").close()
        result = scan(tmp, None)
    assert {e.work_id for e in result.entries} == {"7773"}
    assert result.stats.total_on_disk == 1


def test_matches_space_separated_filenames():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "1011406 Messy - artist_artists.epub"), "w").close()
        result = scan(tmp, None)
    assert {e.work_id for e in result.entries} == {"1011406"}
    assert result.stats.total_on_disk == 1


def test_ignores_non_epub_and_non_matching_files():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "readme.txt"), "w").close()
        open(os.path.join(tmp, "not_an_id.epub"), "w").close()
        result = scan(tmp, None)
    assert result.entries == []
    assert result.stats.total_on_disk == 0
