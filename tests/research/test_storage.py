import uuid

import pytest

from src.research.storage import RevisionConflict, RunBusy, Store


def test_commit_idempotency_and_stale(tmp_path):
    store = Store(tmp_path / "run")
    store.init_run("run", 1, "hash")
    generation = store.acquire("worker")
    first = store.commit_proposal("p1", "a1", 0, "manifest", {"data": 1}, "worker", generation, [("claim", "c1", {"text": "x"})])
    assert first["state_version"] == 1
    assert store.commit_proposal("p1", "a1", 0, "manifest", {"data": 1}, "worker", generation) == first
    with pytest.raises(RevisionConflict):
        store.commit_proposal("p2", "a2", 0, "manifest", {"data": 2}, "worker", generation)
    assert store.run()["state_version"] == 1
    store.release("worker", generation)
    store.close()


def test_kind_and_lease_fencing(tmp_path):
    store = Store(tmp_path / "run")
    store.init_run("run", 1, "hash")
    generation = store.acquire("first")
    with pytest.raises(RunBusy):
        store.acquire("second")
    with store.transaction() as db:
        db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z'")
    newer = store.acquire("second")
    with pytest.raises(RunBusy):
        store.commit_proposal("p", "a", 0, "m", {}, "first", generation)
    receipt = store.commit_proposal("p", "a", 0, "m", {}, "second", newer, [("snapshot", "s", {})])
    fake_claim = dict(receipt["refs"][0], kind="claim")
    with pytest.raises(ValueError):
        with store.transaction() as db:
            from src.research.models import EntityRef
            store.validate_ref(db, EntityRef.model_validate(fake_claim))
    store.close()


def test_blob_integrity(tmp_path):
    store = Store(tmp_path / "run")
    sha = store.put_blob(b"hello")
    assert store.read_blob(sha) == b"hello"
    (store.blob_dir / sha).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="hash"):
        store.read_blob(sha)
    store.close()
