import asyncio
import hashlib
import json

import pytest

from lh_harness.research.assisted import AssistantMailbox, ExternalFetchProvider
from lh_harness.research.jsonio import canonical, digest
from lh_harness.research.models import FetchRequest


def answer(mailbox, kind, key, payload, result, *, wrong_hash=False):
    input_hash = digest(payload)
    request_id = digest({"kind": kind, "logical_key": key, "input_hash": input_hash})
    envelope = {"request_id": request_id, "input_hash": "wrong" if wrong_hash else input_hash,
                "producer": "test-assistant", "result": result}
    (mailbox.directory / f"{request_id}.response.json").write_bytes(canonical(envelope))


def test_assisted_exchange_rejects_stale_answer(tmp_path):
    box = AssistantMailbox(tmp_path)
    answer(box, "role", "a", {"prompt": "current"}, {}, wrong_hash=True)
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(box.exchange("role", "a", {"prompt": "current"}))


def test_assisted_fetch_checks_snapshot_hash(tmp_path):
    box = AssistantMailbox(tmp_path)
    request = FetchRequest(source_id="s", url="https://docs.python.org/3.14/library/threading.html")
    sha = hashlib.sha256(b"original").hexdigest()
    (box.directory / "blobs" / sha).write_bytes(b"tampered")
    answer(box, "fetch", "s", request.model_dump(mode="json"), {
        "source_id": "s", "requested_url": request.url, "final_url": request.url, "status": "ok",
        "mime": "text/html", "headers": {}, "blob_hash": sha,
        "fetched_at": "2026-09-26T00:00:00Z", "error": None})
    with pytest.raises(ValueError, match="integrity"):
        asyncio.run(ExternalFetchProvider(box).fetch(request))
