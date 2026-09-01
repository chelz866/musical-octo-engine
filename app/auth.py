"""Password hashing and session token helpers for the login system.

Sessions are opaque random tokens stored server-side (see db.py's sessions
table) rather than signed cookies, so logging out -- or an admin resetting
someone's password -- actually invalidates the session, and it avoids
adding a cookie-signing dependency for what's ultimately a single-box,
private-network app. Sessions don't expire on a timer; they last until
logout, a deliberate simplification for that same reason.
"""

import hashlib
import secrets
from dataclasses import dataclass

_PBKDF2_ITERATIONS = 200_000


@dataclass
class User:
    id: int
    username: str
    role: str  # "admin" | "user"
    timezone: str | None = None  # IANA name, e.g. "America/New_York" -- None means no conversion

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    salt_hex, _, digest_hex = stored_hash.partition("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS)
    return secrets.compare_digest(digest.hex(), digest_hex)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)
