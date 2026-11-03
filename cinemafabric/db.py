"""Thread-safe database access: SQLite files by default, or one Postgres database when CF_DATABASE_URL is set."""

import contextlib
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Database:
    """SQLite in WAL mode. Each service owns its own database file."""

    def __init__(self, path: Path, schema: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(schema)
            self._conn.commit()
        for f in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            with contextlib.suppress(OSError):
                os.chmod(f, 0o600)

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.query(f"PRAGMA table_info({table})")}

    def upsert(self, table: str, row: dict[str, Any], key: str) -> None:
        names = ", ".join(row)
        self.execute(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    @staticmethod
    def json_text(column: str, path: str) -> str:
        return f"json_extract({column}, '$.{path}')"

    @staticmethod
    def json_number(column: str, path: str) -> str:
        return f"json_extract({column}, '$.{path}')"

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PgDatabase(Database):
    """The same interface on Postgres, so several studio and fabric copies can share state."""

    def __init__(self, url: str, schema: str):
        import psycopg
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self._lock = threading.Lock()
        ddl = re.sub(r"\bREAL\b", "DOUBLE PRECISION", schema)
        with self._lock:
            for statement in (s.strip() for s in ddl.split(";")):
                if statement:
                    self._conn.execute(statement)

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        with self._lock:
            self._conn.execute(self._sql(sql), params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._conn.execute(self._sql(sql), params).fetchall())

    def columns(self, table: str) -> set[str]:
        rows = self.query("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
        return {r["column_name"] for r in rows}

    def upsert(self, table: str, row: dict[str, Any], key: str) -> None:
        names = ", ".join(row)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in row if c != key)
        self.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(row))}) "
                     f"ON CONFLICT ({key}) DO UPDATE SET {updates}", tuple(row.values()))

    @staticmethod
    def json_text(column: str, path: str) -> str:
        return f"({column}::jsonb #>> '{{{','.join(path.split('.'))}}}')"

    @staticmethod
    def json_number(column: str, path: str) -> str:
        return f"({column}::jsonb #>> '{{{','.join(path.split('.'))}}}')::double precision"


def open_db(path: Path, schema: str) -> Database:
    """Postgres when CF_DATABASE_URL points at one, otherwise the SQLite file at `path`."""
    from .config import get_settings

    url = get_settings().database_url
    if url.startswith(("postgres://", "postgresql://")):
        return PgDatabase(url, schema)
    return Database(path, schema)
