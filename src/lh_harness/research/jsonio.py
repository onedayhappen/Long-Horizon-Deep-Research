from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def loads(raw: bytes | str, max_bytes: int = 1_000_000) -> object:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError("JSON payload exceeds size limit")
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def load(path: Path, max_bytes: int = 1_000_000) -> object:
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    return loads(raw, max_bytes)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()
