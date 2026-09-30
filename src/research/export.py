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
        historical = store.db.execute('SELECT artifacts_json FROM delivery_archives ORDER BY state_version DESC LIMIT 1').fetchone()
        if historical is None:
            raise ValueError("run has no completed report")
        archive = json.loads(historical[0])
        paths = []
        for name, sha in archive.items():
            if name not in {'report.md', 'report.json', 'evidence.json', 'coverage.json', 'conflicts.json', 'outline.json', 'stop.json'}:
                raise ValueError('invalid archived artifact name')
            target = store.run_dir / name
            target.write_bytes(store.read_blob(sha))
            paths.append(target)
        return paths
    report_path = store.run_dir / "report.md"
    if not report_path.exists():
        stop = store.db.execute('SELECT payload_json FROM entity_versions WHERE version_id=?', (run['current_stop_version_id'],)).fetchone()
        if stop is None or not json.loads(stop[0]).get('report_hash'):
            raise ValueError('completed run has no verified report blob')
        report_path.write_bytes(store.read_blob(json.loads(stop[0])['report_hash']))
    report = report_path.read_bytes()
    report_hash = hashlib.sha256(report).hexdigest()
    stop = store.db.execute('SELECT payload_json FROM entity_versions WHERE version_id=?', (run['current_stop_version_id'],)).fetchone()
    if stop is None or json.loads(stop[0]).get('report_hash') != report_hash:
        raise ValueError('report integrity check failed')
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
    if store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone():
        from .reuse import reuse_summary
        metadata['reuse_summary'] = reuse_summary(store)
        metadata['parent_run_id'] = store.db.execute('SELECT parent_run_id FROM run_identity').fetchone()[0]
        metadata['reuse_manifest_hashes'] = [r[0] for r in store.db.execute('SELECT manifest_hash FROM reuse_imports')]
        metadata['origins'] = [json.loads(r[0]) for r in store.db.execute('SELECT origin_json FROM reuse_mappings ORDER BY target_version_id')]
        metadata['lineage_watermarks'] = [dict(r) for r in store.db.execute('SELECT run_id,cursor AS sequence,checked_at FROM reuse_ancestors')]
    for name, value in (("report.json", metadata), ("evidence.json", evidence), ("coverage.json", coverage), ("conflicts.json", conflicts), ("outline.json", outline)):
        target = store.run_dir / name
        _write(target, value)
        paths.append(target)
    return paths


def archive_delivery(store):
    run = store.run()
    artifacts = {}
    for name in ('report.md', 'report.json', 'evidence.json', 'coverage.json', 'conflicts.json', 'outline.json', 'stop.json'):
        artifacts[name] = store.put_blob((store.run_dir / name).read_bytes())
    with store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO delivery_archives VALUES(?,?,?)', (run['current_stop_version_id'], run['state_version'], canonical(artifacts).decode()))
