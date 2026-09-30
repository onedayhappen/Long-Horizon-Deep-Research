"""A file mailbox for a foreground assistant, with no embedded model credentials.

The worker writes a content-bound request and waits. The assistant supplies an
envelope with that request_id, input_hash, producer and result. Completed exchanges
remain on disk; requests cannot accidentally consume another stage's answer.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

from .jsonio import canonical, digest, load
from .models import FetchRequest, FetchResult, QuerySpec, RoleRequest, RoleResponse, SearchBatch
from .prompts import template


class AssistantMailbox:
    def __init__(self, directory: Path, timeout: float = 3600):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "blobs").mkdir(exist_ok=True)
        self.deadline = time.monotonic() + timeout

    async def exchange(self, kind: str, logical_key: str, payload: dict) -> dict:
        input_hash = digest(payload)
        request_id = digest({"kind": kind, "logical_key": logical_key, "input_hash": input_hash})
        base = self.directory / request_id
        request = {"schema_version": 1, "request_id": request_id, "kind": kind,
                   "logical_key": logical_key, "input_hash": input_hash, "payload": payload}
        request_path = base.with_suffix(".request.json")
        if not request_path.exists():
            temp = base.with_suffix(".tmp")
            temp.write_bytes(canonical(request))
            os.replace(temp, request_path)
        response_path = base.with_suffix(".response.json")
        print(f"ASSISTANT_REQUEST {kind} {logical_key} {request_path}", flush=True)
        while not response_path.exists():
            if time.monotonic() >= self.deadline:
                from .loop import ResearchStopped
                raise ResearchStopped('incomplete_budget','time_limit')
            await asyncio.sleep(0.2)
        response = load(response_path, max_bytes=4_000_000)
        if not isinstance(response, dict) or response.get("request_id") != request_id or response.get("input_hash") != input_hash:
            raise ValueError("assistant response does not match pending request")
        if not isinstance(response.get("producer"), str) or not response["producer"].strip():
            raise ValueError("assistant response must record its producer")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("assistant result must be an object")
        return result


class ExternalRoleBackend:
    def __init__(self, mailbox: AssistantMailbox):
        self.mailbox = mailbox

    async def generate(self, request: RoleRequest) -> RoleResponse:
        result = await self.mailbox.exchange("role", request.logical_action_key,
            {"request": request.model_dump(mode="json"), "system_prompt": template(request.role)})
        response = RoleResponse.model_validate(result, strict=True)
        if response.finish_reason != "stop":
            raise ValueError("unfinished assistant role response")
        return response


class ExternalSearchProvider:
    def __init__(self, mailbox: AssistantMailbox):
        self.mailbox = mailbox

    async def search(self, query: QuerySpec) -> SearchBatch:
        result = await self.mailbox.exchange("search", query.query_id, query.model_dump(mode="json"))
        batch = SearchBatch.model_validate(result, strict=True)
        if batch.query_id != query.query_id:
            raise ValueError("search response query mismatch")
        return batch


class ExternalFetchProvider:
    def __init__(self, mailbox: AssistantMailbox):
        self.mailbox = mailbox

    async def fetch(self, request: FetchRequest) -> tuple[FetchResult, bytes]:
        result = await self.mailbox.exchange("fetch", request.source_id, request.model_dump(mode="json"))
        fetched = FetchResult.model_validate(result, strict=True)
        if fetched.source_id != request.source_id or fetched.requested_url != request.url or fetched.status != "ok":
            raise ValueError("fetch response does not match request or failed")
        sha = fetched.blob_hash or ""
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid snapshot digest")
        raw = (self.mailbox.directory / "blobs" / sha).read_bytes()
        if len(raw) > 8_000_000 or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("snapshot integrity/size check failed")
        return fetched, raw
