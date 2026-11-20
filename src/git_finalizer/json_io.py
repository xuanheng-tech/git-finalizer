"""Strict JSON parsing, canonical serialization, and hashing helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class DuplicateKeyError(ValueError):
    """Raised when a JSON object declares a duplicate key."""


class NonFiniteConstantError(ValueError):
    """Raised when JSON contains NaN, Infinity, or -Infinity."""


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a dict while rejecting duplicate JSON object keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise NonFiniteConstantError(f"non-finite JSON constant is not allowed: {token}")


def loads_unique(raw: str | bytes, *, allow_nonfinite: bool = False) -> Any:
    """Decode strict UTF-8 JSON rejecting duplicate keys and optional non-finite numbers."""
    text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
    kwargs: dict[str, Any] = {"object_pairs_hook": unique_object}
    if not allow_nonfinite:
        kwargs["parse_constant"] = _reject_constant
    return json.loads(text, **kwargs)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
