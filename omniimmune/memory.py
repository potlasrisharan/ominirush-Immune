from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class ImmuneMemory:
    _SIG_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def __init__(self, root: Path | str = "artifacts/memory") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _validate_sig_id(cls, sig_id: str) -> None:
        if not isinstance(sig_id, str) or cls._SIG_ID_PATTERN.fullmatch(sig_id) is None:
            raise ValueError("Invalid sig_id")

    def save_patch(self, sig_id: str, patch_data: dict[str, Any]) -> Path:
        self._validate_sig_id(sig_id)
        destination = self.root / f"{sig_id}.json"
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{sig_id}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                json.dump(patch_data, temporary, sort_keys=True, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)
        return destination

    def get_patch(self, sig_id: str) -> dict[str, Any] | None:
        self._validate_sig_id(sig_id)
        path = self.root / f"{sig_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def get_vaccinated_count(self) -> int:
        return sum(1 for path in self.root.glob("*.json") if path.is_file())

    def clear(self) -> int:
        removed = 0
        for path in self.root.glob("*.json"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed
