"""Bounded, pull-based invalidation over explicitly authorized ancestors."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .jsonio import canonical, digest
from .reuse import entity
from .reuse_models import InvalidationRequest
from .reuse_paths import ReuseError, safe_path, verify_identity
from .storage import Store, now, stamp
from .database import STORAGE_BUSY

PAGE_SIZE = 256
PAGE_BYTES = 2 * 1024 * 1024
NOTICE_BYTES = 1024 * 1024


def invalidate_local(store, db, version_ids, cause_ids, requirements=()):
    """Invalidate only the exact binding and its local delivery dependencies."""
    affected = set(version_ids)
    queue = list(affected)
    while queue:
        for row in db.execute('SELECT to_version_id FROM dependency_edges WHERE from_version_id=?', (queue.pop(),)):
            if row[0] not in affected:
                affected.add(row[0])
                queue.append(row[0])
    rids = set()
    for binding in list(db.execute('SELECT * FROM reuse_bindings')):
        if not affected.intersection((binding['evidence_version_id'], binding['claim_version_id'], binding['local_claim_version_id'], binding['local_audit_version_id'])):
            continue
        if requirements and binding['requirement_id'] not in requirements:
            continue
        causes = sorted(set(json.loads(binding['cause_notice_ids'])) | set(cause_ids))
        if binding['disposition'] != 'needs_reaudit':
            db.execute("UPDATE reuse_bindings SET disposition='needs_reaudit',epoch=epoch+1,cause_notice_ids=? WHERE binding_id=?", (canonical(causes).decode(), binding['binding_id']))
        else:
            db.execute('UPDATE reuse_bindings SET cause_notice_ids=? WHERE binding_id=?', (canonical(causes).decode(), binding['binding_id']))
        rids.add(binding['requirement_id'])
        if binding['local_claim_version_id']:
            affected.add(binding['local_claim_version_id'])
    for vid in affected:
        row = db.execute('SELECT kind,id,version,payload_json FROM entity_versions WHERE version_id=?', (vid,)).fetchone()
        if row is None:
            continue
        # Imported evidence remains readable as a candidate. Support eligibility
        # is tracked on each binding, never inferred from blob availability.
        if row['kind'] in {'claim', 'audit'}:
            db.execute("UPDATE entity_heads SET validity='needs_reaudit' WHERE kind=? AND id=? AND version=?", (row['kind'], row['id'], row['version']))
            rids.update(json.loads(row['payload_json']).get('requirement_ids', []))
    for rid in rids:
        db.execute("UPDATE entity_heads SET validity='needs_reaudit' WHERE kind='coverage' AND id=?", (rid,))
    for row in list(db.execute("SELECT v.* FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE v.kind='draft' AND h.validity='current'")):
        draft = json.loads(row['payload_json'])
        if any(set(f.get('requirement_ids', [])).intersection(rids) or set(f.get('claim_version_ids', [])).intersection(affected) for f in draft.get('facts', [])):
            db.execute("UPDATE entity_heads SET validity='needs_reaudit' WHERE kind='draft' AND id=?", (row['id'],))
    if rids:
        db.execute("UPDATE entity_heads SET validity='needs_reaudit' WHERE kind IN ('freeze','plan') OR (kind='audit' AND id='report')")
        # Preserve old delivery records; only current completion is revoked.
        db.execute("UPDATE runs SET lifecycle_status='active',phase='researching',research_outcome=NULL,current_stop_version_id=NULL")
    return sorted(affected)


def publish_invalidation(store, request: InvalidationRequest, owner, generation):
    identity = store.db.execute('SELECT * FROM run_identity').fetchone()
    subjects = [entity(store, vid) for vid in sorted(set(request.subject_version_ids))]
    notice_id = digest({'issuer': identity['run_instance_id'], 'request': request.model_dump()})
    with store.transaction() as db:
        store.check_lease(db, owner, generation)
        old = db.execute('SELECT sequence FROM invalidation_outbox WHERE notice_id=?', (notice_id,)).fetchone()
        if old:
            return old[0]
        tail = db.execute('SELECT sequence,notice_hash FROM invalidation_outbox ORDER BY sequence DESC LIMIT 1').fetchone()
        sequence, previous = (tail[0] + 1, tail[1]) if tail else (1, '')
        payload = dict(notice_id=notice_id, issuer_run_id=store.run()['run_id'], issuer_instance_id=identity['run_instance_id'],
                       sequence=sequence, previous_notice_hash=previous, subject_refs=[r['version_id'] for r in subjects],
                       subject_hashes=[r['payload_hash'] for r in subjects], reason_code=request.reason_code,
                       affected_requirement_ids=request.affected_requirement_ids, evidence_refs=request.evidence_refs,
                       created_at=stamp(now()))
        if len(canonical(payload)) > NOTICE_BYTES:
            raise ReuseError('integrity_error', 'notice exceeds byte limit')
        invalidate_local(store, db, request.subject_version_ids, [notice_id], request.affected_requirement_ids)
        db.execute('INSERT INTO invalidation_outbox VALUES(?,?,?,?,?)', (sequence, notice_id, previous, digest(payload), canonical(payload).decode()))
        db.execute('UPDATE runs SET state_version=state_version+1')
        db.execute("INSERT INTO events(state_version,kind,payload_json,created_at) VALUES(?,'invalidation_published',?,?)", (store.run()['state_version'], canonical(payload).decode(), stamp(now())))
        return sequence


def _open_ancestor(store, ancestor):
    location = safe_path(Path(ancestor['locator']), store.run_dir.parent)
    safe_path(location / 'state.sqlite', store.run_dir.parent)
    source = Store(location, readonly=True)
    try:
        identity = verify_identity(source, store.run_dir.parent)
        if identity['run_instance_id'] != ancestor['run_instance_id']:
            raise ReuseError('integrity_error', 'ancestor run instance changed')
        return source
    except BaseException:
        source.close()
        raise


def relocate_source(store, directory, owner, generation):
    location = safe_path(Path(directory), store.run_dir.parent)
    source = Store(location, readonly=True)
    try:
        identity = verify_identity(source, store.run_dir.parent)
        ancestor = store.db.execute('SELECT * FROM reuse_ancestors WHERE run_instance_id=?', (identity['run_instance_id'],)).fetchone()
        if ancestor is None:
            raise ReuseError('access_restricted', 'not an authorized ancestor')
        for edge in store.db.execute('SELECT * FROM reuse_lineage WHERE ancestor_instance_id=?', (identity['run_instance_id'],)):
            if entity(source, edge['ancestor_version_id'])['payload_hash'] != edge['ancestor_payload_hash']:
                raise ReuseError('integrity_error', 'relocated version differs')
        if ancestor['cursor']:
            notice = source.db.execute('SELECT notice_hash FROM invalidation_outbox WHERE sequence=?', (ancestor['cursor'],)).fetchone()
            if not notice or notice[0] != ancestor['cursor_hash']:
                raise ReuseError('integrity_error', 'relocated notice history differs')
        with store.transaction() as db:
            store.check_lease(db, owner, generation)
            db.execute("UPDATE reuse_ancestors SET locator=?,status='unknown' WHERE run_instance_id=?", (str(location), identity['run_instance_id']))
    finally:
        source.close()


def sync_lineage(store, owner, generation, *, seconds=5, clock=now):
    deadline = time.monotonic() + seconds
    complete = True
    for initial in list(store.db.execute('SELECT * FROM reuse_ancestors ORDER BY run_instance_id')):
        if time.monotonic() >= deadline:
            complete = False
            break
        source = None
        try:
            source = _open_ancestor(store, initial)
            source.db.execute('BEGIN')
            # A fixed high water makes this round finite even while A is writing.
            high = initial['high_water']
            if high is None:
                high = source.db.execute('SELECT COALESCE(MAX(sequence),0) FROM invalidation_outbox').fetchone()[0]
            if high < initial['cursor']:
                raise ReuseError('integrity_error', 'ancestor outbox rolled back')
            if initial['cursor']:
                anchor = source.db.execute('SELECT notice_hash FROM invalidation_outbox WHERE sequence=?', (initial['cursor'],)).fetchone()
                if not anchor or anchor[0] != initial['cursor_hash']:
                    raise ReuseError('integrity_error', 'cursor hash changed')
            with store.transaction() as db:
                store.check_lease(db, owner, generation)
                db.execute("UPDATE reuse_ancestors SET high_water=?,status='pending' WHERE run_instance_id=?", (high, initial['run_instance_id']))
            cursor, last_hash = initial['cursor'], initial['cursor_hash']
            while cursor < high and time.monotonic() < deadline:
                page = list(source.db.execute('SELECT * FROM invalidation_outbox WHERE sequence>? AND sequence<=? ORDER BY sequence LIMIT ?', (cursor, high, PAGE_SIZE)))
                source.db.execute('COMMIT')
                source.db.execute('BEGIN')
                if not page:
                    raise ReuseError('integrity_error', 'missing notice sequence')
                batch, size = [], 0
                for row in page:
                    raw = row['payload_json'].encode('utf-8')
                    if len(raw) > NOTICE_BYTES:
                        raise ReuseError('integrity_error', 'oversized notice')
                    if size + len(raw) > PAGE_BYTES:
                        break
                    size += len(raw)
                    batch.append(row)
                with store.transaction() as db:
                    store.check_lease(db, owner, generation)
                    for row in batch:
                        p = json.loads(row['payload_json'])
                        if row['sequence'] != cursor + 1 or row['previous_notice_hash'] != last_hash or digest(p) != row['notice_hash']:
                            raise ReuseError('integrity_error', 'notice chain mismatch')
                        if p['issuer_instance_id'] != initial['run_instance_id'] or p['sequence'] != row['sequence'] or p['notice_id'] != row['notice_id'] or p['previous_notice_hash'] != last_hash:
                            raise ReuseError('integrity_error', 'notice identity mismatch')
                        if len(p['subject_refs']) != len(p['subject_hashes']) or len(p['subject_refs']) > 128:
                            raise ReuseError('integrity_error', 'notice subjects mismatch')
                        affected = []
                        for vid, sha in zip(p['subject_refs'], p['subject_hashes']):
                            for edge in db.execute('SELECT * FROM reuse_lineage WHERE ancestor_instance_id=? AND ancestor_version_id=?', (initial['run_instance_id'], vid)):
                                if sha != edge['ancestor_payload_hash']:
                                    raise ReuseError('integrity_error', 'notice subject hash mismatch')
                                old = db.execute('SELECT 1 FROM invalidation_receipts WHERE issuer_instance_id=? AND notice_id=? AND local_version_id=?', (initial['run_instance_id'], p['notice_id'], edge['local_version_id'])).fetchone()
                                if not old:
                                    # Local media failure is not evidence that an
                                    # independently copied and checked blob is bad.
                                    if p['reason_code'] != 'local_corruption':
                                        affected.append(edge['local_version_id'])
                                    db.execute('INSERT INTO invalidation_receipts VALUES(?,?,?)', (initial['run_instance_id'], p['notice_id'], edge['local_version_id']))
                        if affected:
                            # Ancestor requirement IDs are not target IDs; an
                            # uncertain scope is conservatively reviewed locally.
                            affected = invalidate_local(store, db, affected, [p['notice_id']])
                            store.append_entity(db, 'invalidation_observation', p['notice_id'], dict(notice=p, local_version_ids=affected))
                            db.execute('UPDATE runs SET state_version=state_version+1')
                        cursor, last_hash = row['sequence'], row['notice_hash']
                    db.execute('UPDATE reuse_ancestors SET cursor=?,cursor_hash=?,checked_at=? WHERE run_instance_id=?', (cursor, last_hash, stamp(clock()), initial['run_instance_id']))
            done = cursor == high
            with store.transaction() as db:
                db.execute('UPDATE reuse_ancestors SET status=?,high_water=?,checked_at=? WHERE run_instance_id=?', ('synced' if done else 'pending', None if done else high, stamp(clock()), initial['run_instance_id']))
            complete &= done
        except (OSError, ReuseError, RuntimeError, *STORAGE_BUSY) as exc:
            with store.transaction() as db:
                db.execute("UPDATE reuse_ancestors SET status='lineage_unavailable' WHERE run_instance_id=?", (initial['run_instance_id'],))
            if isinstance(exc, ReuseError) and exc.code in {'integrity_error', 'access_restricted'}:
                raise
            complete = False
        finally:
            if source is not None:
                source.close()
    return complete
