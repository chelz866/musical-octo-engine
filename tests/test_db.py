import os
import tempfile

from app import db


def test_set_and_get_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_fields(path, "123", title="Fixed Title", author="Fixed Author", fandoms=["Torchwood", "Doctor Who"])

        override = db.get_override(path, "123")
        assert override.title == "Fixed Title"
        assert override.author == "Fixed Author"
        assert override.fandoms == ["Torchwood", "Doctor Who"]
        assert override.dismissed is False


def test_set_fields_overwrites_previous_values():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_fields(path, "123", title="First", author=None, fandoms=None)
        db.set_fields(path, "123", title="Second", author="Someone", fandoms=["X"])

        override = db.get_override(path, "123")
        assert override.title == "Second"
        assert override.author == "Someone"
        assert override.fandoms == ["X"]


def test_set_dismissed_preserves_other_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "app.db")
        db.init_db(path)
        db.set_fields(path, "123", title="Fixed Title", author=None, fandoms=None)
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
        db.set_fields(path, "1", title="A", author=None, fandoms=None)
        db.set_dismissed(path, "2", True)

        all_overrides = db.get_all_overrides(path)
        assert set(all_overrides) == {"1", "2"}
        assert all_overrides["1"].title == "A"
        assert all_overrides["2"].dismissed is True
