"""A small on-disk JSON cache, so reruns on the same captions don't pay for the same API call twice."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def text_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class JsonCache:
    """key -> JSON value, stored in one file. A missing or damaged file starts an empty cache."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._data: dict = {}
        if self.path and self.path.is_file():
            try:
                with self.path.open(encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._data = {}

    def get(self, key: str):
        return self._data.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def set(self, key: str, value) -> None:
        self._data[key] = value

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)   # atomic on Windows and macOS: no half-written cache
