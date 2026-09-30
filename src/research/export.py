from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .jsonio import canonical
from .storage import Store


def _write(path: Path, value: object) -> None:
    raw = canonical(value)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".export-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def export_artifacts(store: Store) -> list[Path]:
    run = store.run()
    if run["lifecycle_status"] != "complete":
        raise ValueError("run has no completed report")
    report_path = store.run_dir / "report.md"
    report = report_path.read_bytes()
    report_hash = hashlib.sha256(report).hexdigest()
    drafts = [json.loads(row["payload_json"]) for row in store.db.execute("SELECT v.payload_json FROM entity_versions v JOIN entity_heads h ON v.kind=h.kind AND v.id=h.id AND v.version=h.version WHERE v.kind='draft' AND h.validity='current' ORDER BY v.id")]
    evidence = []
    for row in store.db.execute("SELECT id,payload_json FROM entity_versions WHERE kind='evidence' ORDER BY id,version"):
        item = json.loads(row["payload_json"])
        snapshot = store.db.execute("SELECT payload_json FROM entity_versions WHERE kind='snapshot' AND id=? ORDER BY version DESC LIMIT 1", (item["snapshot_id"],)).fetchone()
        item["id"] = row["id"]
        item["snapshot"] = json.loads(snapshot[0]) if snapshot else None
        evidence.append(item)
    coverage = [json.loads(row["payload_json"]) for row in store.db.execute("SELECT v.payload_json FROM entity_versions v JOIN entity_heads h ON v.kind=h.kind AND v.id=h.id AND v.version=h.version WHERE v.kind='coverage' ORDER BY v.id")]
    conflicts = [json.loads(row["payload_json"]) for row in store.db.execute("SELECT payload_json FROM entity_versions WHERE kind='conflict' ORDER BY id,version")]
    citations = sorted({evidence_id for draft in drafts for fact in draft["facts"] if fact["kind"] == "factual" for evidence_id in fact["evidence_ids"]})
    metadata = {"schema_version": 1, "run_id": run["run_id"], "outcome": run["research_outcome"], "report_hash": report_hash, "stop_version_id": run["current_stop_version_id"], "citation_evidence_ids": citations, "sections": drafts}
    paths = []
    history = [dict(version=r['version'], version_id=r['version_id'], **json.loads(r['payload_json'])) for r in store.db.execute("SELECT * FROM entity_versions WHERE kind='outline' ORDER BY version")]
    outline = {'current': history[-1] if history else None, 'revision_count': max(0,len(history)-1), 'history': history}
    metadata['outline_version'] = history[-1].get('outline_version',history[-1]['version']) if history else None
    for name, value in (("report.json", metadata), ("evidence.json", evidence), ("coverage.json", coverage), ("conflicts.json", conflicts), ("outline.json", outline)):
        target = store.run_dir / name
        _write(target, value)
        paths.append(target)
    return paths
