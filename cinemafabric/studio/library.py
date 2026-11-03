"""Characters saved from finished films, to cast again with the same name, look and reference sheet."""

import secrets
import shutil
import time
from pathlib import Path

from ..db import open_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    name TEXT NOT NULL,
    look TEXT NOT NULL,
    image TEXT NOT NULL,
    source_job TEXT,
    owner TEXT
);
"""


class CharacterLibrary:
    def __init__(self, db_path: Path, folder: Path):
        self.db = open_db(db_path, SCHEMA)
        if "owner" not in self.db.columns("characters"):
            self.db.execute("ALTER TABLE characters ADD COLUMN owner TEXT")
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _mine(owner: str | None) -> tuple[str, tuple]:
        return ("", ()) if owner is None else (" AND owner = ?", (owner,))

    def all(self, owner: str | None = None) -> list[dict]:
        where, params = self._mine(owner)
        return [self._public(r) for r in self.db.query(f"SELECT * FROM characters WHERE 1=1{where} ORDER BY created DESC",
                                                       params)]

    def pick(self, ids: list[str], owner: str | None = None) -> list[dict]:
        """The chosen characters with the file path of their sheet, in the order asked."""
        where, params = self._mine(owner)
        rows = {r["id"]: r for r in self.db.query(
            f"SELECT * FROM characters WHERE id IN ({','.join('?' * len(ids))}){where}", tuple(ids) + params)} if ids else {}
        missing = [i for i in ids if i not in rows]
        if missing:
            raise KeyError(f"no saved character {missing[0]}")
        return [{"id": i, "name": rows[i]["name"], "look": rows[i]["look"], "sheet": str(self.folder / rows[i]["image"])}
                for i in ids]

    def save(self, name: str, look: str, sheet: Path, source_job: str, owner: str | None = None) -> dict:
        where, params = self._mine(owner)
        existing = self.db.query(f"SELECT * FROM characters WHERE name = ? AND look = ?{where}", (name, look) + params)
        if existing:
            return self._public(existing[0])
        char_id = secrets.token_hex(5)
        shutil.copyfile(sheet, self.folder / f"{char_id}.png")
        self.db.execute("INSERT INTO characters (id, created, name, look, image, source_job, owner) VALUES (?,?,?,?,?,?,?)",
                        (char_id, time.time(), name, look, f"{char_id}.png", source_job, owner))
        return self._public(self.db.query("SELECT * FROM characters WHERE id = ?", (char_id,))[0])

    def owner_of(self, image: str) -> str | None:
        rows = self.db.query("SELECT owner FROM characters WHERE image = ?", (image,))
        return rows[0]["owner"] if rows else None

    def delete(self, char_id: str, owner: str | None = None) -> bool:
        where, params = self._mine(owner)
        rows = self.db.query(f"SELECT image FROM characters WHERE id = ?{where}", (char_id,) + params)
        if not rows:
            return False
        self.db.execute("DELETE FROM characters WHERE id = ?", (char_id,))
        (self.folder / rows[0]["image"]).unlink(missing_ok=True)
        return True

    @staticmethod
    def _public(row: dict) -> dict:
        return {"id": row["id"], "name": row["name"], "look": row["look"], "image": f"/library/{row['image']}",
                "source_job": row["source_job"]}
