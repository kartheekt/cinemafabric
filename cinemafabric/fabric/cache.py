"""Content-addressed cache of task results."""

import hashlib
import json
import uuid
from pathlib import Path

from .types import TaskRequest


class ResultCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(req: TaskRequest) -> str:
        blob = json.dumps(
            {"kind": req.kind.value, "tier": req.tier.value, "payload": req.payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        path = self.root / key[:2] / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, entry: dict) -> None:
        path = self.root / key[:2] / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(path)
