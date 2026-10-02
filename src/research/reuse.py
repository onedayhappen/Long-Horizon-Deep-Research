"""Freeze, verify and import one explicitly authorized run.

The manifest, not live source heads, is the recovery input. No source verdict is
ever installed as a local evidence link.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from .extract import verify_locator
from .jsonio import canonical, digest, load
from .models import TextSpan, ResearchContract
from .reuse_models import ReuseSelection
from .reuse_paths import ReuseError, checked_blob_path, safe_path, verify_identity
from .storage import Store, now, stamp

MAX_EVIDENCE = 1000
MAX_BLOBS = 512 * 1024 * 1024
MAX_NODES = 10000
MAX_EDGES = 30000
MAX_MANIFEST = 16 * 1024 * 1024
MAX_ANCESTORS = 32
ALLOWED_KINDS = {'source', 'snapshot', 'evidence', 'claim', 'audit', 'historical_audit', 'conflict', 'provenance', 'limitation', 'figure', 'document_map'}


def entity(store, version_id):
    row = store.db.execute('SELECT * FROM entity_versions WHERE version_id=?', (version_id,)).fetchone()
    if row is None:
        raise ReuseError('missing_dependency', version_id)
    result = dict(row)
    result['payload'] = json.loads(result.pop('payload_json'))
    if digest(result['payload']) != result['payload_hash']:
        raise ReuseError('integrity_error', version_id)
    return result


def head(store, kind, entity_id):
    row = store.db.execute('SELECT v.version_id FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE v.kind=? AND v.id=?', (kind, entity_id)).fetchone()
    if row is None:
        raise ReuseError('missing_dependency', f'{kind}:{entity_id}')
    return entity(store, row[0])


def snapshot_for(store, evidence):
    explicit = evidence['payload'].get('snapshot_version_id')
    if explicit:
        return entity(store, explicit)
    dependency = store.db.execute("SELECT d.from_version_id FROM dependency_edges d JOIN entity_versions v ON v.version_id=d.from_version_id WHERE d.to_version_id=? AND v.kind='snapshot'", (evidence['version_id'],)).fetchone()
    return entity(store, dependency[0]) if dependency else head(store, 'snapshot', evidence['payload']['snapshot_id'])


def _limit(nodes, edges, blobs):
    counts = {'evidence': sum(r['kind'] == 'evidence' for r in nodes.values()), 'nodes': len(nodes), 'edges': edges, 'blob_bytes': sum(blobs.values())}
    if any(counts[k] > limit for k, limit in [('evidence', MAX_EVIDENCE), ('nodes', MAX_NODES), ('edges', MAX_EDGES), ('blob_bytes', MAX_BLOBS)]):
        raise ReuseError('reuse_selection_required', canonical(counts).decode())


def _verify_blob(source, sha, root):
    path = checked_blob_path(source.run_dir, sha, root)
    row = source.db.execute('SELECT * FROM blobs WHERE sha256=?', (sha,)).fetchone()
    if row is None or row['relative_path'] != f'blobs/{sha}':
        raise ReuseError('missing_dependency', sha)
    h = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk)
            if size > MAX_BLOBS:
                raise ReuseError('reuse_selection_required', 'blob exceeds byte limit')
            h.update(chunk)
    if h.hexdigest() != sha or size != row['size']:
        raise ReuseError('integrity_error', f'blob {sha}')
    return size


def _bindings(source):
    """Exact candidate provenance exists before either audit has completed."""
    pairs = set()
    for row in source.db.execute("SELECT receipt_json FROM proposals WHERE proposal_id LIKE 'candidate:%'"):
        refs = {r['kind']: r['version_id'] for r in json.loads(row[0])['refs']}
        if 'claim' in refs and 'evidence' in refs:
            pairs.add((refs['evidence'], refs['claim']))
    for row in source.db.execute('SELECT evidence_version_id,claim_version_id FROM evidence_links'):
        pairs.add(tuple(row))
    for row in source.db.execute('SELECT evidence_version_id,claim_version_id FROM reuse_candidates'):
        pairs.add(tuple(row))
    return sorted(pairs)


def freeze_manifest(source, target, contract, selection):
    identity = verify_identity(source, target.run_dir.parent, target_instance=target.db.execute('SELECT run_instance_id FROM run_identity').fetchone()[0])
    source_contract = ResearchContract.model_validate(load(safe_path(source.run_dir / 'contract.json', target.run_dir.parent))).model_dump(mode='json')
    pairs = _bindings(source)
    questions = set(selection.source_question_ids) if selection else set()
    known_questions = {r['id'] for r in source_contract['requirements']}
    if questions - known_questions:
        raise ReuseError('invalid_selection', 'unknown source question')
    seeds = set(selection.source_evidence_version_ids) if selection else set()
    if selection:
        for eid, cid in pairs:
            if questions.intersection(entity(source, cid)['payload'].get('requirement_ids', [])):
                seeds.add(eid)
    else:
        for row in source.db.execute('SELECT evidence_version_id, audit_version_id FROM evidence_links'):
            audit = entity(source, row['audit_version_id'])['payload']
            if audit.get('forward', {}).get('verdict') == 'supported' and audit.get('reverse', {}).get('verdict') == 'no_objection':
                seeds.add(row['evidence_version_id'])
    nodes, blobs, queue, bindings, edges = {}, {}, sorted(seeds), set(), []
    conflicts = list(source.db.execute("SELECT v.* FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE kind='conflict'"))
    while queue:
        vid = queue.pop(0)
        if vid in nodes:
            continue
        item = entity(source, vid)
        if item['kind'] not in ALLOWED_KINDS:
            raise ReuseError('missing_dependency', f'unsupported dependency kind {item["kind"]}')
        if item['payload'].get('access_level', 'public') not in {'public', 'local'}:
            raise ReuseError('access_restricted', vid)
        nodes[vid] = item
        p = item['payload']
        if item['kind'] == 'evidence':
            if p.get('validity') == 'invalidated':
                raise ReuseError('invalid_locator', vid)
            snap = snapshot_for(source, item)
            _verify_blob(source, snap['payload']['text_hash'], target.run_dir.parent)
            queue.append(snap['version_id'])
            for eid, cid in pairs:
                if eid == vid:
                    bindings.add((eid, cid))
                    queue.append(cid)
            for link in source.db.execute('SELECT * FROM evidence_links WHERE evidence_version_id=?', (vid,)):
                queue.append(link['audit_version_id'])
                edges.append(dict(link))
            text = source.read_blob(snap['payload']['text_hash']).decode('utf-8')
            if p['locator']['kind'] == 'pdf_region':
                from .visual_runtime import verify_visual_evidence
                verify_visual_evidence(source, p)
                queue.append(p['locator']['figure_version_id'])
            elif not verify_locator(text, TextSpan.model_validate(p['locator']), p['excerpt']):
                raise ReuseError('invalid_locator', vid)
        elif item['kind'] == 'figure':
            from .visual_models import FigureArtifact
            artifact = FigureArtifact.model_validate(p['artifact'])
            queue.append(p['document_map_version_id'])
            for image in artifact.images:
                blobs[image.blob_hash] = _verify_blob(source, image.blob_hash, target.run_dir.parent)
        elif item['kind'] == 'document_map':
            from .visual_models import DocumentMap
            document = DocumentMap.model_validate(p['document'])
            blobs[document.raw_hash] = _verify_blob(source, document.raw_hash, target.run_dir.parent)
        elif item['kind'] == 'snapshot':
            if p.get('validity') == 'invalidated':
                raise ReuseError('integrity_error', 'invalidated snapshot')
            queue.append(head(source, 'source', p['source_id'])['version_id'])
            for key in ('raw_hash', 'text_hash'):
                sha = p[key]
                if sha not in blobs:
                    blobs[sha] = _verify_blob(source, sha, target.run_dir.parent)
        elif item['kind'] == 'claim':
            # A conflict's other claim must bring its own original evidence.
            for eid, cid in pairs:
                if cid == vid:
                    queue.append(eid)
        # Only explicitly typed dependencies; arbitrary neighbours are not walked.
        for dep in source.db.execute('SELECT * FROM dependency_edges WHERE to_version_id=?', (vid,)):
            if dep['reason'] in {'snapshot', 'evidence', 'claim', 'audit', 'context', 'limitation', 'provenance', 'conflict',
                                 'figure', 'document_map', 'visual_evidence', 'visual_audit'}:
                queue.append(dep['from_version_id'])
                edges.append(dict(dep))
        for row in conflicts:
            cp = json.loads(row['payload_json'])
            refs = cp.get('claim_version_ids', []) + cp.get('evidence_version_ids', [])
            ids = cp.get('claim_ids', []) + cp.get('evidence_ids', [])
            if cp.get('severity', 'blocking') in {'blocking', 'material'} and (vid in refs or item['id'] in ids):
                queue.append(row['version_id'])
                queue.extend(refs)
                for kind, key in [('claim', 'claim_ids'), ('evidence', 'evidence_ids')]:
                    queue.extend(head(source, kind, x)['version_id'] for x in cp.get(key, []))
                if not refs and not ids:
                    raise ReuseError('missing_dependency', 'conflict has no exact subjects')
        _limit(nodes, len(edges), blobs)
        queue = sorted(set(queue) - nodes.keys())
    if any(nodes[vid]['kind'] != 'evidence' for vid in seeds):
        raise ReuseError('invalid_selection', 'seed must be an evidence version')
    if any(eid not in {e for e, _ in bindings} for eid in seeds):
        raise ReuseError('missing_dependency', 'evidence has no recorded candidate claim')
    investigations = []
    for row in source.db.execute("SELECT v.* FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE kind='query'"):
        q = json.loads(row['payload_json'])
        if q['question_id'] in questions or (not selection and any(q['question_id'] in nodes[c]['payload'].get('requirement_ids', []) for _, c in bindings)):
            result = source.db.execute("SELECT v.payload_json FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE kind='query_result' AND id=?", (q['query_id'],)).fetchone()
            investigations.append({'query': q, 'result': json.loads(result[0]) if result else {'status': 'failed'}, 'origin_version_id': row['version_id']})
    ancestors = {}
    tail = source.db.execute('SELECT sequence,notice_hash FROM invalidation_outbox ORDER BY sequence DESC LIMIT 1').fetchone()
    ancestors[identity['run_instance_id']] = dict(run_instance_id=identity['run_instance_id'], run_id=source.run()['run_id'], locator=str(source.run_dir), owner=identity['owner'], cursor=0, cursor_hash='')
    lineage, origins = [], {}
    for vid in sorted(nodes):
        origin = source.db.execute('SELECT origin_json FROM reuse_mappings WHERE target_version_id=?', (vid,)).fetchone()
        direct = {'run_id': source.run()['run_id'], 'run_instance_id': identity['run_instance_id'], 'version_id': vid, 'payload_hash': nodes[vid]['payload_hash']}
        origins[vid] = json.loads(origin[0]) if origin else nodes[vid]['payload'].get('origin_ref', direct)
        lineage.append(dict(ancestor_instance_id=identity['run_instance_id'], ancestor_version_id=vid, ancestor_payload_hash=nodes[vid]['payload_hash'], source_local_version_id=vid))
        for edge in source.db.execute('SELECT * FROM reuse_lineage WHERE local_version_id=?', (vid,)):
            ancestor = source.db.execute('SELECT * FROM reuse_ancestors WHERE run_instance_id=?', (edge['ancestor_instance_id'],)).fetchone()
            if ancestor is None:
                raise ReuseError('missing_dependency', 'lineage ancestor')
            location = safe_path(Path(ancestor['locator']), target.run_dir.parent)
            upstream = Store(location, readonly=True)
            try:
                verified = verify_identity(upstream, target.run_dir.parent, target_instance=target.db.execute('SELECT run_instance_id FROM run_identity').fetchone()[0])
                if verified['run_instance_id'] != ancestor['run_instance_id']:
                    raise ReuseError('integrity_error', 'ancestor identity changed')
            finally:
                upstream.close()
            ancestors[ancestor['run_instance_id']] = {k: ancestor[k] for k in ('run_instance_id', 'run_id', 'locator', 'owner')}
            ancestors[ancestor['run_instance_id']].update(cursor=0, cursor_hash='')
            lineage.append(dict(ancestor_instance_id=edge['ancestor_instance_id'], ancestor_version_id=edge['ancestor_version_id'], ancestor_payload_hash=edge['ancestor_payload_hash'], source_local_version_id=vid))
    if len(ancestors) > MAX_ANCESTORS:
        raise ReuseError('reuse_selection_required', 'ancestor limit')
    templates = []
    selected_claims = {cid for _, cid in bindings}
    for row in source.db.execute("SELECT v.* FROM entity_versions v JOIN entity_heads h USING(kind,id,version) WHERE kind='draft'"):
        draft = json.loads(row['payload_json'])
        if any(selected_claims.intersection(f.get('claim_version_ids', [])) for f in draft.get('facts', [])):
            templates.append(dict(section_role='section', order=len(templates), heading_pattern='{section_title}',
                                  paragraph_slots=len(draft.get('paragraphs', [])), table_column_roles=[], origin_ref=dict(run_instance_id=identity['run_instance_id'], version_id=row['version_id'], payload_hash=row['payload_hash'])))
    manifest = dict(schema_version=1, source_run_id=source.run()['run_id'], source_instance_id=identity['run_instance_id'], source_dir=str(source.run_dir),
                    source_state_version=source.run()['state_version'], source_contract=source_contract,
                    target_contract_hash=digest(contract.model_dump(mode='json')), selection=selection.model_dump() if selection else None,
                    nodes=[nodes[k] for k in sorted(nodes)], edges=edges, bindings=sorted(bindings), blobs=blobs,
                    origins=origins, lineage=lineage, ancestors=list(ancestors.values()), investigations=investigations,
                    source_outcome=source.run()['research_outcome'], source_usage_reference=source.status()['budget'],
                    templates=templates, frozen_outbox_high_water=tail[0] if tail else 0)
    if len(canonical(manifest)) > MAX_MANIFEST:
        raise ReuseError('reuse_selection_required', 'manifest limit')
    return manifest


def plan_import(target, source_dir, contract, selection, owner, generation):
    root = target.run_dir.parent
    source_dir = safe_path(Path(source_dir), root)
    if source_dir == target.run_dir:
        raise ReuseError('access_restricted', 'cannot import the target run')
    safe_path(source_dir / 'state.sqlite', root)
    # Inspect before opening a writable coordination connection; do not migrate or
    # execute source-provided triggers while acquiring its lease.
    source = Store(source_dir, readonly=True)
    try:
        verify_identity(source, root)
        sql = "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        if [tuple(r) for r in source.db.execute(sql)] != [tuple(r) for r in target.db.execute(sql)]:
            raise ReuseError('integrity_error', 'unexpected source schema')
    finally:
        source.close()
    source = Store(source_dir)
    source_owner = f'reuse:{uuid.uuid4()}'
    source_generation = None
    try:
        source_generation = source.acquire(source_owner, seconds=300)
        manifest = freeze_manifest(source, target, contract, selection)
        key = digest({k: manifest[k] for k in ('source_run_id', 'source_instance_id', 'source_state_version', 'selection', 'target_contract_hash')})
        with target.transaction() as db:
            target.check_lease(db, owner, generation)
            db.execute('INSERT OR IGNORE INTO reuse_imports(import_key,manifest_hash,manifest_json,state) VALUES(?,?,?,?)', (key, digest(manifest), canonical(manifest).decode(), 'planned'))
        return key
    finally:
        if source_generation is not None:
            source.release(source_owner, source_generation)
        source.close()


def resume_imports(target, contract, owner, generation):
    for row in list(target.db.execute("SELECT * FROM reuse_imports WHERE state!='committed'")):
        manifest = json.loads(row['manifest_json'])
        key = row['import_key']
        if digest(manifest) != row['manifest_hash'] or manifest['target_contract_hash'] != digest(contract.model_dump(mode='json')):
            raise ReuseError('integrity_error', 'frozen manifest changed')
        try:
            with target.transaction() as db:
                target.check_lease(db, owner, generation)
                db.execute("UPDATE reuse_imports SET state='copying',error_code=NULL WHERE import_key=?", (key,))
            for index, (sha, size) in enumerate(sorted(manifest['blobs'].items())):
                existing = target.db.execute('SELECT 1 FROM blobs WHERE sha256=?', (sha,)).fetchone()
                if existing:
                    target.read_blob(sha)
                else:
                    path = checked_blob_path(Path(manifest['source_dir']), sha, target.run_dir.parent)
                    target.copy_blob(path, sha, size)
                with target.transaction() as db:
                    target.check_lease(db, owner, generation)
                    db.execute('UPDATE reuse_imports SET copied_blobs=? WHERE import_key=?', (index + 1, key))
            _commit_import(target, row, manifest, owner, generation)
        except Exception as exc:
            with target.transaction() as db:
                db.execute('UPDATE reuse_imports SET error_code=? WHERE import_key=?', (getattr(exc, 'code', type(exc).__name__), key))
            raise


def _commit_import(target, row, manifest, owner, generation):
    key = row['import_key']
    namespace = target.namespace_seed or uuid.UUID(target.db.execute('SELECT run_instance_id FROM run_identity').fetchone()[0])
    # A source may export several immutable versions of the same entity. Give
    # each its own target identity; iteration order must never choose its head.
    idmap = {n['version_id']: 'reuse-' + digest([key, n['kind'], n['version_id']])[:24] for n in manifest['nodes']}
    versions = {n['version_id']: str(uuid.uuid5(namespace, key + ':' + n['version_id'])) for n in manifest['nodes']}
    nodes = {n['version_id']: n for n in manifest['nodes']}
    def target_id(kind, source_id, owner_node):
        exact = [edge['from_version_id'] for edge in manifest['edges'] if edge.get('to_version_id') == owner_node['version_id'] and
                 edge.get('from_version_id') in nodes and nodes[edge['from_version_id']]['kind'] == kind and nodes[edge['from_version_id']]['id'] == source_id]
        candidates = [n for n in manifest['nodes'] if n['kind'] == kind and n['id'] == source_id]
        if len(set(exact)) > 1 or not exact and len(candidates) != 1:
            raise ReuseError('missing_dependency', f'ambiguous {kind}:{source_id}')
        return idmap[exact[0] if exact else candidates[0]['version_id']]

    def rewrite(value, owner_node, field=''):
        if isinstance(value, dict):
            return {k: rewrite(v, owner_node, k) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v, owner_node, field) for v in value]
        if isinstance(value, str):
            if value in versions:
                return versions[value]
            kind = {'snapshot_id': 'snapshot', 'source_id': 'source', 'claim_id': 'claim', 'claim_ids': 'claim', 'evidence_id': 'evidence', 'evidence_ids': 'evidence'}.get(field)
            if kind:
                return target_id(kind, value, owner_node)
        return value
    with target.transaction() as db:
        target.check_lease(db, owner, generation)
        if db.execute('SELECT state FROM reuse_imports WHERE import_key=?', (key,)).fetchone()[0] == 'committed':
            return
        refs = []
        for n in manifest['nodes']:
            kind = 'historical_audit' if n['kind'] == 'audit' else n['kind']
            payload = rewrite(n['payload'], n) if kind != 'historical_audit' else {'historical_payload': n['payload']}
            payload['origin_ref'] = manifest['origins'][n['version_id']]
            if kind == 'claim':
                payload.update(validity='candidate', epistemic='candidate', requirement_ids=[])
            ref = target.append_entity(db, kind, idmap[n['version_id']], payload, versions[n['version_id']])
            refs.append(ref.model_dump())
            direct = dict(run_id=manifest['source_run_id'], run_instance_id=manifest['source_instance_id'], version_id=n['version_id'], payload_hash=n['payload_hash'])
            db.execute('INSERT INTO reuse_mappings VALUES(?,?,?,?,?,?)', (key, manifest['source_instance_id'], n['version_id'], ref.version_id, canonical(payload['origin_ref']).decode(), canonical(direct).decode()))
        for eid, cid in manifest['bindings']:
            historical = any(e.get('claim_version_id') == cid and e.get('evidence_version_id') == eid and
                             nodes[e['audit_version_id']]['payload'].get('forward', {}).get('verdict') == 'supported' and
                             nodes[e['audit_version_id']]['payload'].get('reverse', {}).get('verdict') == 'no_objection' for e in manifest['edges'])
            db.execute('INSERT INTO reuse_candidates VALUES(?,?,?,?,?)', (versions[eid], versions[cid], key, 'historical_verified' if historical else 'reuse_candidate', canonical(nodes[cid]['payload'].get('requirement_ids', [])).decode()))
            db.execute('INSERT INTO reuse_fts VALUES(?,?,?)', (versions[eid], versions[cid], nodes[cid]['payload']['text'] + '\n' + nodes[eid]['payload']['excerpt']))
            db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (versions[eid], versions[cid], 'evidence'))
        for n in manifest['nodes']:
            kind, p = n['kind'], n['payload']
            dependency = ('snapshot', p['snapshot_id']) if kind == 'evidence' else ('source', p['source_id']) if kind == 'snapshot' else None
            if dependency:
                parent_id = target_id(*dependency, n)
                parent = next(v for v in manifest['nodes'] if idmap[v['version_id']] == parent_id)
                db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (versions[parent['version_id']], versions[n['version_id']], dependency[0]))
        for e in manifest['edges']:
            if 'from_version_id' in e:
                if e['from_version_id'] not in versions or e['to_version_id'] not in versions:
                    raise ReuseError('missing_dependency', 'dependency edge outside closure')
                db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (versions[e['from_version_id']], versions[e['to_version_id']], e['reason']))
        for a in manifest['ancestors']:
            db.execute('INSERT OR IGNORE INTO reuse_ancestors(run_instance_id,run_id,locator,owner,cursor,cursor_hash) VALUES(?,?,?,?,?,?)', tuple(a[k] for k in ('run_instance_id', 'run_id', 'locator', 'owner', 'cursor', 'cursor_hash')))
        for e in manifest['lineage']:
            db.execute('INSERT OR IGNORE INTO reuse_lineage(ancestor_instance_id,ancestor_version_id,ancestor_payload_hash,local_version_id) VALUES(?,?,?,?)', (e['ancestor_instance_id'], e['ancestor_version_id'], e['ancestor_payload_hash'], versions[e['source_local_version_id']] ))
        for index, investigation in enumerate(manifest['investigations']):
            target.append_entity(db, 'investigation_history', f'{key}:{index}', dict(investigation, source_run_id=manifest['source_run_id'], source_outcome=manifest['source_outcome']))
        for index, template in enumerate(manifest.get('templates', [])):
            target.append_entity(db, 'section_template', f'{key}:{index}', template)
        # Safe section structure never copies user-authored headings or sentences.
        target.append_entity(db, 'reuse_record', key, dict(import_id=key, imported_at=stamp(now()), manifest_hash=row['manifest_hash'], source_usage_reference=manifest['source_usage_reference']))
        db.execute('UPDATE run_identity SET parent_run_id=?,parent_instance_id=?', (manifest['source_run_id'], manifest['source_instance_id']))
        db.execute('UPDATE runs SET state_version=state_version+1')
        receipt = dict(import_key=key, refs=refs, state_version=target.run()['state_version'])
        db.execute("UPDATE reuse_imports SET state='committed',receipt_json=?,error_code=NULL WHERE import_key=?", (canonical(receipt).decode(), key))
        db.execute("INSERT INTO events(state_version,kind,payload_json,created_at) VALUES(?,'reuse_import_committed',?,?)", (target.run()['state_version'], canonical({'import_key': key, 'manifest_hash': row['manifest_hash']}).decode(), stamp(now())))


def reuse_summary(store):
    counts = {r['disposition']: r['n'] for r in store.db.execute('SELECT disposition,COUNT(*) n FROM reuse_bindings GROUP BY disposition')}
    imported = store.db.execute('SELECT COUNT(DISTINCT evidence_version_id) FROM reuse_candidates').fetchone()[0]
    scheduled = store.db.execute('SELECT COUNT(DISTINCT evidence_version_id) FROM reuse_bindings').fetchone()[0]
    return dict(imported_evidence=imported, not_scheduled=imported - scheduled, bindings=counts,
                lineage=[dict(r) for r in store.db.execute('SELECT run_id,run_instance_id,cursor,status,checked_at FROM reuse_ancestors ORDER BY run_instance_id')],
                imports=[dict(r) for r in store.db.execute('SELECT import_key,state,error_code,copied_blobs FROM reuse_imports')])
