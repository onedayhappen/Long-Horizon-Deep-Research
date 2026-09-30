from __future__ import annotations

from pathlib import Path

from .jsonio import load
from datetime import datetime, timezone
import hashlib

from .models import FetchRequest, FetchResult, RoleRequest, RoleResponse, SearchBatch, QuerySpec


class FixtureMismatch(RuntimeError):
    pass


class ReplayRoleBackend:
    def __init__(self, fixture_dir: Path):
        self.fixture_dir = fixture_dir
        manifest = load(fixture_dir / "fixture_manifest.json")
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise FixtureMismatch("invalid replay manifest")
        self._responses = manifest.get("roles", {})

    async def generate(self, request: RoleRequest) -> RoleResponse:
        key = f"{request.role}|{request.logical_action_key}|{request.input_manifest_hash}"
        path = self._responses.get(key)
        if not isinstance(path, str) or not path.startswith("roles/") or ".." in Path(path).parts:
            raise FixtureMismatch(f"fixture_mismatch: {key}")
        return RoleResponse.model_validate(load(self.fixture_dir / path), strict=True)


class ReplaySearchProvider:
    def __init__(self, fixture_dir: Path):
        data = load(fixture_dir / "search.json")
        if not isinstance(data, dict):
            raise FixtureMismatch("invalid search fixture")
        self._batches = data

    async def search(self, query: QuerySpec) -> SearchBatch:
        response = self._batches.get(query.query_id)
        if response is None:
            raise FixtureMismatch(f"missing search query {query.query_id}")
        batch = SearchBatch.model_validate(response, strict=True)
        if batch.query_id != query.query_id:
            raise FixtureMismatch("search query ID mismatch")
        return batch


class ReplayFetchProvider:
    def __init__(self, fixture_dir: Path):
        self.fixture_dir = fixture_dir
        manifest = load(fixture_dir / "fixture_manifest.json")
        self._sources = manifest.get("sources", {})
        self._fetched_at = manifest.get("fixed_start", "2026-09-01T00:00:00Z")

    async def fetch(self, request: FetchRequest) -> tuple[FetchResult, bytes]:
        record = self._sources.get(request.url)
        if not isinstance(record, dict):
            raise FixtureMismatch(f"missing fetch fixture: {request.url}")
        path = record.get("path")
        if not isinstance(path, str) or not path.startswith("sources/") or ".." in Path(path).parts:
            raise FixtureMismatch("invalid source fixture path")
        raw = (self.fixture_dir / path).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != record.get("sha256"):
            raise FixtureMismatch("source fixture hash mismatch")
        return FetchResult(source_id=request.source_id, requested_url=request.url, final_url=request.url, status="ok", mime=record.get("mime"), headers={}, blob_hash=sha, fetched_at=self._fetched_at, error=None), raw
