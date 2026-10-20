from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class UntrustedImmuneMemoryError(ValueError):
    """Raised when a cached immune patch cannot be trusted."""


class ImmuneMemory:
    _SIG_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    _SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
    _SIGNATURE_FIELD = "signature"
    # A fixed, 256-bit key derived from a domain-separated value keeps local
    # development deterministic without relying on a predictable short key.
    _DEVELOPMENT_SIGNING_KEY = hashlib.sha256(
        b"omniimmune:immune-memory:development-signing-key:v1"
    ).digest()

    def __init__(self, root: Path | str = "artifacts/memory") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _validate_sig_id(cls, sig_id: str) -> None:
        if not isinstance(sig_id, str) or cls._SIG_ID_PATTERN.fullmatch(sig_id) is None:
            raise ValueError("Invalid sig_id")

    @classmethod
    def _signing_key(cls) -> bytes:
        configured = os.getenv("OMNI_SIGNING_KEY")
        return configured.encode("utf-8") if configured else cls._DEVELOPMENT_SIGNING_KEY

    @classmethod
    def _canonical_message(
        cls, sig_id: str, strategy: str, base_file_sha256: str, modified_source: str
    ) -> bytes:
        # Keep the signed message exactly aligned with the memory contract:
        # sig_id + strategy + base-file digest + modified source.
        return f"{sig_id}{strategy}{base_file_sha256}{modified_source}".encode("utf-8")

    @classmethod
    def _sign(
        cls, sig_id: str, strategy: str, base_file_sha256: str, modified_source: str
    ) -> str:
        return hmac.new(
            cls._signing_key(),
            cls._canonical_message(sig_id, strategy, base_file_sha256, modified_source),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def _validate_sha256(cls, value: Any, field_name: str) -> str:
        if not isinstance(value, str) or cls._SHA256_PATTERN.fullmatch(value) is None:
            raise UntrustedImmuneMemoryError(f"Invalid {field_name} in immune memory")
        return value

    @classmethod
    def _normalise_patch(cls, sig_id: str, patch_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch_data, dict):
            raise ValueError("patch_data must be a dictionary")

        strategy = patch_data.get("strategy", "legacy")
        modified_source = patch_data.get("modified_source", "")
        if not isinstance(strategy, str) or not strategy:
            raise ValueError("strategy must be a non-empty string")
        if not isinstance(modified_source, str):
            raise ValueError("modified_source must be a string")

        # Older callers did not persist a base hash.  Deriving one from their
        # source keeps those entries readable while all newly written entries
        # still carry the explicit hash supplied by the patch producer.
        base_file_sha256 = patch_data.get("base_file_sha256")
        if base_file_sha256 is None:
            base_file_sha256 = hashlib.sha256(modified_source.encode("utf-8")).hexdigest()
        if not isinstance(base_file_sha256, str) or cls._SHA256_PATTERN.fullmatch(base_file_sha256) is None:
            raise ValueError("base_file_sha256 must be a SHA-256 hex digest")

        normalised = dict(patch_data)
        normalised.update(
            {
                "strategy": strategy,
                "base_file_sha256": base_file_sha256,
                "modified_source": modified_source,
            }
        )
        normalised[cls._SIGNATURE_FIELD] = cls._sign(
            sig_id, strategy, base_file_sha256, modified_source
        )
        return normalised

    def save_patch(self, sig_id: str, patch_data: dict[str, Any]) -> Path:
        self._validate_sig_id(sig_id)
        patch_data = self._normalise_patch(sig_id, patch_data)
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

    def get_patch(
        self,
        sig_id: str,
        base_file_sha256: str | None = None,
        *,
        expected_base_file_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        self._validate_sig_id(sig_id)
        if base_file_sha256 is not None and expected_base_file_sha256 is not None:
            if base_file_sha256 != expected_base_file_sha256:
                raise ValueError("Conflicting expected base-file SHA-256 values")
        expected_base = (
            expected_base_file_sha256
            if expected_base_file_sha256 is not None
            else base_file_sha256
        )
        if expected_base is not None:
            self._validate_sha256(expected_base, "expected base_file_sha256")
        path = self.root / f"{sig_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            raise UntrustedImmuneMemoryError("Immune memory payload is not an object")

        strategy = value.get("strategy")
        base_hash = value.get("base_file_sha256")
        modified_source = value.get("modified_source")
        signature = value.get(self._SIGNATURE_FIELD)
        stored_sig_id = value.get("sig_id")
        if (
            not isinstance(strategy, str)
            or not strategy
            or not isinstance(modified_source, str)
            or (stored_sig_id is not None and stored_sig_id != sig_id)
            or not isinstance(signature, str)
        ):
            raise UntrustedImmuneMemoryError("Malformed immune memory payload")
        base_hash = self._validate_sha256(base_hash, "base_file_sha256")
        if expected_base is not None and not hmac.compare_digest(base_hash, expected_base):
            raise UntrustedImmuneMemoryError("Immune memory base file does not match")

        expected_signature = self._sign(sig_id, strategy, base_hash, modified_source)
        if not hmac.compare_digest(signature, expected_signature):
            raise UntrustedImmuneMemoryError("Immune memory signature is invalid")
        return value

    def get_vaccinated_count(self) -> int:
        return sum(1 for path in self.root.glob("*.json") if path.is_file())

    def clear(self) -> int:
        removed = 0
        for path in self.root.glob("*.json"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed
