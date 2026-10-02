from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import Iterator

from .jsonio import canonical, digest
from .models import EntityRef
from .database import Database, Row


class RevisionConflict(RuntimeError):
    pass


class RunBusy(RuntimeError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, run_dir: Path, namespace_seed: str | None = None, readonly: bool = False):
        self.run_dir = run_dir.resolve()
        self.namespace_seed = uuid.UUID(namespace_seed) if namespace_seed else None
        self.db_path = self.run_dir / "state.sqlite"
        self.blob_dir = self.run_dir / "blobs"
        if not readonly:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.blob_dir.mkdir(exist_ok=True)
        elif not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        self.db = Database(self.db_path, readonly=readonly)
        self.db.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA trusted_schema=OFF")
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 3:
            raise RuntimeError("database schema is newer than this program")
        if self.db.execute("PRAGMA user_version").fetchone()[0] == 0 and not readonly:
            migration = files("src.research").joinpath("migrations/001_initial.sql").read_text("utf-8")
            self.db.executescript("BEGIN IMMEDIATE;\n" + migration + "\nPRAGMA user_version=1;\nCOMMIT;")
        if self.db.execute("PRAGMA user_version").fetchone()[0] == 1 and not readonly:
            migration = files('src.research').joinpath('migrations/002_reuse.sql').read_text('utf-8')
            self.db.executescript('BEGIN IMMEDIATE;\n' + migration + '\nPRAGMA user_version=2;\nCOMMIT;')
        if self.db.execute('PRAGMA user_version').fetchone()[0] == 2 and not readonly:
            migration = files('src.research').joinpath('migrations/003_visual.sql').read_text('utf-8')
            self.db.executescript('BEGIN IMMEDIATE;\n' + migration + '\nPRAGMA user_version=3;\nCOMMIT;')

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self._checkpoint()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def init_run(self, run_id: str, contract_version: int, config_hash: str, runs_root: Path | None = None) -> None:
        from .reuse_paths import principal
        with self.transaction() as db:
            db.execute("INSERT INTO runs(run_id,phase,lifecycle_status,contract_version,config_hash) VALUES(?,?,?,?,?)", (run_id, "scoping", "active", contract_version, config_hash))
            db.execute('INSERT INTO run_identity(singleton,run_instance_id,runs_root,owner) VALUES(1,?,?,?)',
                       (str(uuid.uuid4()), str((runs_root or self.run_dir.parent).resolve()), principal()))

    def _checkpoint(self):
        row = self.db.execute('SELECT * FROM runs').fetchone()
        if row is None or self.db.execute('SELECT 1 FROM research_checkpoints WHERE state_version=?', (row['state_version'],)).fetchone():
            return
        payload = {'run_id': row['run_id'], 'committed_state_version': row['state_version'], 'phase': row['phase'],
                   'event_seq': self.db.execute('SELECT COALESCE(MAX(event_seq),0) FROM events').fetchone()[0],
                   'contract_version': row['contract_version'],
                   'head_refs': [dict(r) for r in self.db.execute('SELECT h.kind,h.id,h.version,v.version_id,v.payload_hash,h.validity FROM entity_heads h JOIN entity_versions v USING(kind,id,version) ORDER BY h.kind,h.id')],
                   'pending_action_refs': [dict(r) for r in self.db.execute("SELECT action_id,state,input_manifest_hash FROM actions WHERE state NOT IN ('committed','result_saved') ORDER BY action_id")],
                   'pending_review_refs': [r[0] for r in self.db.execute("SELECT task_id FROM review_tasks WHERE status='pending' ORDER BY task_id")],
                   'revision_refs': [dict(r) for r in self.db.execute('SELECT * FROM revision_jobs ORDER BY job_id')],
                   'budget_ref': {'reserved_attempt_ids': [r[0] for r in self.db.execute("SELECT attempt_id FROM budget_reservations WHERE status!='released' ORDER BY attempt_id")]}}
        self.db.execute('INSERT INTO research_checkpoints VALUES(?,?,?)', (row['state_version'], digest(payload), canonical(payload).decode()))

    def run(self) -> Row:
        row = self.db.execute("SELECT * FROM runs").fetchone()
        if row is None:
            raise RuntimeError("run not initialized")
        return row

    def acquire(self, owner: str, seconds: int = 60) -> int:
        current = now()
        with self.transaction() as db:
            old = db.execute("SELECT * FROM leases WHERE singleton=1").fetchone()
            if old and datetime.fromisoformat(old["expires_at"].replace("Z", "+00:00")) > current:
                raise RunBusy("run_busy")
            generation = old["generation"] + 1 if old else 1
            db.execute("INSERT OR REPLACE INTO leases VALUES(1,?,?,?,?)", (owner, generation, stamp(current + timedelta(seconds=seconds)), stamp(current)))
        return generation

    def check_lease(self, db: Database, owner: str, generation: int) -> None:
        row = db.execute("SELECT * FROM leases WHERE singleton=1").fetchone()
        if not row or row["owner_id"] != owner or row["generation"] != generation or row["expires_at"] <= stamp(now()):
            raise RunBusy("lease expired or taken over")

    def heartbeat(self, owner: str, generation: int, seconds: int = 60) -> None:
        with self.transaction() as db:
            self.check_lease(db, owner, generation)
            current = now()
            db.execute("UPDATE leases SET expires_at=?, heartbeat_at=? WHERE singleton=1", (stamp(current + timedelta(seconds=seconds)), stamp(current)))

    def release(self, owner: str, generation: int) -> None:
        with self.transaction() as db:
            db.execute("UPDATE leases SET expires_at='1970-01-01T00:00:00Z' WHERE owner_id=? AND generation=?", (owner, generation))

    def put_blob(self, content: bytes) -> str:
        sha = hashlib.sha256(content).hexdigest()
        target = self.blob_dir / sha
        if not target.exists():
            fd, temp_name = tempfile.mkstemp(dir=self.blob_dir, prefix=".pending-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (sha, len(content), f"blobs/{sha}"))
        return sha

    def read_blob(self, sha: str) -> bytes:
        from .reuse_paths import checked_blob_path, ReuseError
        row = self.db.execute("SELECT relative_path FROM blobs WHERE sha256=?", (sha,)).fetchone()
        if not row:
            raise FileNotFoundError(sha)
        if row[0] != f'blobs/{sha}':
            raise ReuseError('integrity_error', 'blob path is not content addressed')
        content = checked_blob_path(self.run_dir, sha, self.run_dir).read_bytes()
        if hashlib.sha256(content).hexdigest() != sha:
            raise RuntimeError("blob hash mismatch")
        return content

    def validate_integrity(self):
        if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or self.db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('integrity_error: database check failed')
        for row in self.db.execute('SELECT payload_json,payload_hash FROM entity_versions'):
            if hashlib.sha256(row[0].encode('utf-8')).hexdigest() != row[1]:
                raise RuntimeError('integrity_error: entity hash mismatch')
        for row in self.db.execute('SELECT sha256 FROM blobs'):
            self.read_blob(row[0])

    def copy_blob(self, source: Path, sha: str, size: int):
        """Copy a previously bounded blob without loading it all into memory."""
        from .reuse_paths import checked_blob_path, file_owner, principal, ReuseError
        source = checked_blob_path(source.parent.parent, sha, self.run_dir.parent)
        if file_owner(source) != principal():
            raise ReuseError('access_restricted', 'blob owner differs')
        before = source.stat()
        fd, name = tempfile.mkstemp(dir=self.blob_dir, prefix='.import-')
        try:
            total, hasher = 0, hashlib.sha256()
            with source.open('rb') as src, os.fdopen(fd, 'wb') as dst:
                opened = os.fstat(src.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ReuseError('integrity_error', 'source replaced while opening')
                for chunk in iter(lambda: src.read(1024 * 1024), b''):
                    total += len(chunk)
                    if total > size:
                        raise ReuseError('integrity_error', 'blob grew during copy')
                    hasher.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if total != size or hasher.hexdigest() != sha:
                raise ReuseError('integrity_error', 'source blob changed during copy')
            os.replace(name, self.blob_dir / sha)
            with self.transaction() as db:
                db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)', (sha, size, f'blobs/{sha}'))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def append_entity(self, db: Database, kind: str, entity_id: str, payload: object, version_id: str | None = None) -> EntityRef:
        old = db.execute("SELECT version FROM entity_heads WHERE kind=? AND id=?", (kind, entity_id)).fetchone()
        version = old[0] + 1 if old else 1
        version_id = version_id or (str(uuid.uuid5(self.namespace_seed, f"{kind}:{entity_id}:{version}")) if self.namespace_seed else str(uuid.uuid4()))
        raw = canonical(payload)
        db.execute("INSERT INTO entity_versions VALUES(?,?,?,?,?,?)", (kind, entity_id, version, version_id, raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()))
        db.execute("INSERT INTO entity_heads(kind,id,version) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET version=excluded.version, validity='current'", (kind, entity_id, version))
        return EntityRef(kind=kind, id=entity_id, version=version, version_id=version_id)

    def validate_ref(self, db: Database, ref: EntityRef) -> None:
        row = db.execute("SELECT kind,id,version FROM entity_versions WHERE version_id=?", (ref.version_id,)).fetchone()
        if row is None or tuple(row) != (ref.kind, ref.id, ref.version):
            raise ValueError("entity reference mismatch")

    def commit_proposal(self, proposal_id: str, action_id: str, expected_state_version: int, input_manifest_hash: str, payload: object, owner: str, generation: int, entities: list[tuple[str,str,object]] = ()) -> dict[str, object]:
        proposal_hash = digest(payload)
        with self.transaction() as db:
            self.check_lease(db, owner, generation)
            existing = db.execute("SELECT proposal_hash,receipt_json FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if existing:
                if existing["proposal_hash"] != proposal_hash:
                    raise ValueError("proposal ID reused with different content")
                return json.loads(existing["receipt_json"])
            run = db.execute("SELECT * FROM runs").fetchone()
            if run["state_version"] != expected_state_version:
                raise RevisionConflict("stale proposal")
            refs = [self.append_entity(db, kind, entity_id, item).model_dump(mode="json") for kind, entity_id, item in entities]
            new_version = expected_state_version + 1
            receipt = {"proposal_id": proposal_id, "state_version": new_version, "refs": refs}
            db.execute("UPDATE runs SET state_version=?", (new_version,))
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)", (proposal_id, action_id, proposal_hash, expected_state_version, input_manifest_hash, canonical(receipt).decode()))
            db.execute("INSERT INTO events(state_version,kind,payload_json,created_at) VALUES(?,?,?,?)", (new_version, "proposal_committed", canonical(receipt).decode(), stamp(now())))
            return receipt

    def add_link(self, link_id: str, claim: EntityRef, evidence: EntityRef, audit: EntityRef, relation: str, owner: str, generation: int) -> None:
        with self.transaction() as db:
            self.check_lease(db, owner, generation)
            for ref, kind in ((claim,"claim"),(evidence,"evidence"),(audit,"audit")):
                self.validate_ref(db, ref)
                if ref.kind != kind:
                    raise ValueError(f"expected {kind} reference")
            result = db.execute("INSERT OR IGNORE INTO evidence_links(link_id,claim_version_id,evidence_version_id,audit_version_id,relation) VALUES(?,?,?,?,?)", (link_id, claim.version_id, evidence.version_id, audit.version_id, relation))
            if result.rowcount:
                db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (evidence.version_id, claim.version_id, 'evidence'))
                db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (audit.version_id, claim.version_id, 'audit'))
                payload = json.loads(db.execute('SELECT payload_json FROM entity_versions WHERE version_id=?', (evidence.version_id,)).fetchone()[0])
                if payload.get('snapshot_id'):
                    snapshot = db.execute("SELECT v.version_id,v.payload_json FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE kind='snapshot' AND id=?", (payload['snapshot_id'],)).fetchone()
                    if snapshot:
                        db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (snapshot['version_id'], evidence.version_id, 'snapshot'))
                        sid = json.loads(snapshot['payload_json']).get('source_id')
                        source = db.execute("SELECT v.version_id FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE kind='source' AND id=?", (sid,)).fetchone()
                        if source:
                            db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (source[0], snapshot['version_id'], 'source'))
                db.execute("UPDATE runs SET state_version=state_version+1")

    def status(self) -> dict[str, object]:
        self.db.execute("BEGIN")
        try:
            row = self.run()
            stop_row = self.db.execute("SELECT payload_json FROM entity_versions WHERE version_id=?", (row["current_stop_version_id"],)).fetchone() if row["current_stop_version_id"] else None
            stop = json.loads(stop_row[0]) if stop_row else None
            budget = {r["pool"]: r["count"] for r in self.db.execute("SELECT pool,COUNT(*) AS count FROM budget_reservations WHERE status!='released' GROUP BY pool")}
            pending = [r["task_id"] for r in self.db.execute("SELECT task_id FROM review_tasks WHERE status='pending'")]
            outline_row = self.db.execute("SELECT COUNT(*), MAX(version) FROM entity_versions WHERE kind='outline' AND id='outline'").fetchone()
            loop_row = self.db.execute("SELECT v.payload_json FROM entity_versions v JOIN entity_heads h ON v.kind=h.kind AND v.id=h.id AND v.version=h.version WHERE v.kind='loop' AND v.id='controller'").fetchone()
            loop = json.loads(loop_row[0]) if loop_row else {}
            planner_steps = self.db.execute("SELECT COUNT(*) FROM entity_heads WHERE kind='plan'").fetchone()[0]
        finally:
            self.db.execute("ROLLBACK")
        artifacts = [str(self.run_dir / name) for name in ("report.md", "report.json", "evidence.json", "coverage.json", "conflicts.json", "stop.json", "outline.json") if (self.run_dir / name).exists()]
        result = {"schema_version": 1, "run_id": row["run_id"], "phase": row["phase"], "lifecycle_status": row["lifecycle_status"], "research_outcome": row["research_outcome"], "state_version": row["state_version"], "outline_version": outline_row[1], "outline_revision_count": max(0,outline_row[0]-1), "research_rounds": loop.get('rounds',0), "planner_steps": planner_steps, "gate_results": stop["gate_results"] if stop else [], "budget": {"external_calls_by_pool": budget}, "pending_reviews": pending, "artifact_refs": artifacts}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='reuse_imports'").fetchone():
            from .reuse import reuse_summary
            result['reuse_summary'] = reuse_summary(self)
            result['current_validity'] = 'not_checked' if result['reuse_summary']['lineage'] else 'not_applicable'
        return result
