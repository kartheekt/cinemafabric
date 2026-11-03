"""Optional accounts: people, password sign-in, sessions and daily allowances."""

import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path

from ..db import open_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    daily_usd REAL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires REAL NOT NULL
);
"""
SESSION_DAYS = 14
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(_hash_password(password, bytes.fromhex(salt)).split("$")[2], digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, path: Path):
        self.db = open_db(path, SCHEMA)
        self._dummy = _hash_password(secrets.token_urlsafe(8))

    def count(self) -> int:
        return self.db.query("SELECT COUNT(*) AS n FROM users")[0]["n"]

    def create(self, email: str, name: str, password: str, is_admin: bool = False) -> dict:
        email = email.strip().lower()
        if not EMAIL.match(email):
            raise ValueError("that doesn't look like an email address")
        if len(password) < 10:
            raise ValueError("use a password of at least 10 characters")
        if self.db.query("SELECT 1 FROM users WHERE email = ?", (email,)):
            raise ValueError("there's already an account with that email")
        user_id = secrets.token_hex(6)
        self.db.execute("INSERT INTO users (id, email, name, password, is_admin, created) VALUES (?,?,?,?,?,?)",
                        (user_id, email, name.strip()[:80] or email.split("@")[0], _hash_password(password),
                         int(is_admin), time.time()))
        return self.get(user_id) or {}

    def get(self, user_id: str) -> dict | None:
        rows = self.db.query("SELECT id, email, name, is_admin, daily_usd FROM users WHERE id = ?", (user_id,))
        return {**rows[0], "is_admin": bool(rows[0]["is_admin"])} if rows else None

    def all(self) -> list[dict]:
        return [{**r, "is_admin": bool(r["is_admin"])}
                for r in self.db.query("SELECT id, email, name, is_admin, daily_usd FROM users ORDER BY created")]

    def sign_in(self, email: str, password: str) -> str | None:
        rows = self.db.query("SELECT id, password FROM users WHERE email = ?", (email.strip().lower(),))
        ok = _check_password(password, rows[0]["password"] if rows else self._dummy)
        if not rows or not ok:
            return None
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES (?,?,?)",
                        (_token_hash(token), rows[0]["id"], time.time() + SESSION_DAYS * 86400))
        return token

    def user_for(self, token: str | None) -> dict | None:
        if not token:
            return None
        rows = self.db.query("SELECT user_id, expires FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        if not rows or rows[0]["expires"] < time.time():
            return None
        return self.get(rows[0]["user_id"])

    def sign_out(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
