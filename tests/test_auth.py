from app import auth


def test_hash_and_verify_password_round_trip():
    stored = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", stored) is True


def test_verify_password_rejects_wrong_password():
    stored = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("wrong password", stored) is False


def test_hash_password_is_salted_differently_each_time():
    first = auth.hash_password("admin")
    second = auth.hash_password("admin")
    assert first != second
    assert auth.verify_password("admin", first) is True
    assert auth.verify_password("admin", second) is True


def test_generate_session_token_is_unique_and_reasonably_long():
    tokens = {auth.generate_session_token() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 32 for t in tokens)


def test_user_is_admin_property():
    admin = auth.User(id=1, username="admin", role="admin")
    regular = auth.User(id=2, username="friend", role="user")
    assert admin.is_admin is True
    assert regular.is_admin is False
