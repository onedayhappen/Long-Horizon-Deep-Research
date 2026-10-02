"""Executable reuse protocol tests; all documents and verdicts are fictional."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.research.controller import Controller
from src.research.extract import locate
from src.research.jsonio import canonical, load
from src.research.lineage import publish_invalidation, sync_lineage, relocate_source
from src.research.models import ResearchContract, RoleResponse, utc
from src.research.reuse import plan_import, resume_imports, entity, reuse_summary
from src.research.reuse_models import ReuseSelection, InvalidationRequest
from src.research.reuse_paths import ReuseError
from src.research.storage import Store, RunBusy

FIXTURE = Path(__file__).parents[1] / 'fixtures/research/success'
FIXED = '2026-09-01T00:00:00Z'
REUSE_FIXTURE = Path(__file__).parents[1] / 'fixtures/research/reuse'


def contract():
    data = load(REUSE_FIXTURE / 'target_contract.json')
    return ResearchContract.model_validate(data)


def new_store(root, name):
    store = Store(root / name)
    store.init_run(name, 1, 'test')
    (store.run_dir / 'contract.json').write_bytes(canonical(contract().model_dump(mode='json')))
    return store


def source_run(root, name='A', count=1, published_at='2026-08-01T00:00:00Z'):
    store = new_store(root, name)
    generation = store.acquire('source')
    text = (REUSE_FIXTURE / 'source.txt').read_text('utf-8')
    sha = store.put_blob(text.encode())
    with store.transaction() as db:
        store.append_entity(db, 'source', 'doc', {'url': 'https://example.org/doc'})
        store.append_entity(db, 'snapshot', 'doc', dict(source_id='doc', url='https://example.org/doc', raw_hash=sha, text_hash=sha,
                                                      fetched_at=FIXED, published_at=published_at, parser='fixture'))
    for i in range(count):
        evidence = dict(snapshot_id='doc', excerpt=text, locator=locate(text, text).model_dump(), source_class='primary_official', observation_root='original-document', validity='active')
        claim = dict(text=text, kind='attributed', requirement_ids=['R1'], validity='current')
        receipt = store.commit_proposal(f'candidate:e{i}', f'candidate:e{i}', store.run()['state_version'], 'test', {'i': i}, 'source', generation,
                                        [('evidence', f'e{i}', evidence), ('claim', f'c{i}', claim), ('audit', f'a{i}', dict(forward={'verdict': 'supported', 'source_class_verified': True}, reverse={'verdict': 'no_objection'}))])
        from src.research.models import EntityRef
        refs = [EntityRef.model_validate(r) for r in receipt['refs']]
        store.add_link(f'l{i}', refs[1], refs[0], refs[2], 'supports', 'source', generation)
    store.release('source', generation)
    return store


def import_source(source, target, c=None, selection=None):
    c = c or contract()
    generation = target.acquire('import')
    try:
        key = plan_import(target, source.run_dir, c, selection, 'import', generation)
        resume_imports(target, c, 'import', generation)
        assert sync_lineage(target, 'import', generation)
        return key
    finally:
        target.release('import', generation)


class ReuseAgent:
    def __init__(self):
        self.calls = []

    async def generate(self, request):
        role, p = request.role, request.data_packet
        self.calls.append((role, p))
        if role == 'planner.reuse_map':
            candidate = json.loads(p['candidates_json'])[0]
            value = {'bindings': [dict(evidence_version_id=candidate['evidence_version_id'], claim_version_id=candidate['claim_version_id'], requirement_id=p['requirement_id'], scope_matches=True, time_matches=True, reason='Fictional matching scope')]}
        elif role == 'auditor.evidence':
            value = dict(verdict='supported', reason='Fictional exact excerpt', source_class_verified=True)
        elif role == 'auditor.counter_entailment':
            value = dict(verdict='no_objection', reason='Attributed claim only')
        elif role == 'planner.initialize':
            value = dict(question_ids=['R1'], outline_sections=['结论', '依据与限制'])
        elif role == 'auditor.question_space':
            value = dict(review_status='pass', dimensions_checked=['scope'], missing_questions=[])
        elif role == 'auditor.search_bias':
            value = dict(review_status='pass', source_classes_seen=['primary_official'], query_families_seen=[], blind_spots=[])
        elif role == 'auditor.coverage':
            d = json.loads(p['research_digest_json'])
            claims = [x['version_id'] for x in d['claim'] if x['payload'].get('validity') == 'current']
            value = dict(requirement_id='R1', disposition='satisfied' if not p['missing_check_ids'] else 'open',
                         passed_check_ids=p['passed_check_ids'], missing_check_ids=p['missing_check_ids'], claim_version_ids=claims, investigation_refs=[])
        elif role == 'planner.next':
            value = dict(kind='terminate', coverage_refs=p['coverage_refs'], proposed_outcome='complete')
        elif role == 'auditor.conflict':
            from scripts.research_review_fixture import conflict_response
            value = conflict_response(p)
        elif role == 'auditor.outline':
            from scripts.research_review_fixture import outline_review_response
            value = outline_review_response(p)
        elif role == 'writer.section':
            material = json.loads(p['section_material_json'])
            fact = dict(kind='factual', id='fact-' + p['section_id'], requirement_ids=['R1'], claim_version_ids=[material['claims'][0]['version_id']], evidence_ids=[material['evidence'][0]['id']])
            value = dict(section_id=p['section_id'], revision=p['revision'], outline_version=p['outline_version'], facts=[fact],
                         paragraphs=[dict(id='p', sentences=[dict(id='s', text='Fictional official CSV claim.', fact_ids=[fact['id']])])], open_questions=[])
        elif role == 'auditor.report':
            value = dict(report_hash=p['report_hash'], findings=[], checked_fact_ids=p['fact_ids'], checked_section_ids=p['section_ids'], answered_requirement_ids=['R1'])
        else:
            raise AssertionError(role)
        return RoleResponse(raw_text=canonical(value).decode(), finish_reason='stop', provider_request_id=None, usage={'basis': 'unknown'}, model_profile_hash='reuse-test')


def controller(store, c=None):
    c = Controller(store, c or contract(), FIXTURE, 120, [80, 10, 10])
    c.clock = lambda: utc(FIXED)
    agent = ReuseAgent()
    c.roles = agent
    checks = []
    async def validation(packet):
        checks.append(packet)
        return dict(validation_key=packet['validation_key'], checked_at=FIXED, status='unchanged', method='replay',
                    checked_channels=['fixture publisher corrections'], checks_performed=packet['checks_required'],
                    result_refs=['fixture:publisher-status'], observed_raw_hash=packet['raw_hash'])
    c.source_validator = validation
    return c, agent, checks


def test_import_is_candidate_and_does_not_inherit_budget_or_support(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        import_source(a, b)
        assert b.db.execute('SELECT COUNT(*) FROM evidence_links').fetchone()[0] == 0
        assert b.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        assert b.db.execute("SELECT COUNT(*) FROM entity_heads WHERE kind='historical_audit'").fetchone()[0] == 1
        assert b.db.execute("SELECT COUNT(*) FROM entity_heads WHERE kind IN ('coverage','draft','stop')").fetchone()[0] == 0
        c, agent, checks = controller(b)
        try:
            assert c.evidence_rows()['R1'] == []
            assert asyncio.run(c.run()) == 'complete'
            assert len(checks) == 1
            assert len(c.evidence_rows()['R1']) == 1
            assert not any(role in {'researcher.extract'} for role, _ in agent.calls)
            for role, packet in agent.calls:
                if role in {'auditor.evidence', 'auditor.counter_entailment'}:
                    assert 'historical_audit' not in canonical(packet).decode()
            assert b.db.execute("SELECT COUNT(*) FROM actions WHERE kind IN ('search','fetch')").fetchone()[0] == 0
            assert load(b.run_dir / 'report.json')['parent_run_id'] == 'A'
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_import_recovery_uses_frozen_payload_and_is_idempotent(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    generation = b.acquire('import')
    try:
        key = plan_import(b, a.run_dir, contract(), None, 'import', generation)
        with a.transaction() as db:
            a.append_entity(db, 'claim', 'c0', {'text': 'Later changed head', 'requirement_ids': ['R1'], 'validity': 'current'})
        original = b.copy_blob
        def crash(*args):
            original(*args)
            raise RuntimeError('crash after blob')
        b.copy_blob = crash
        with pytest.raises(RuntimeError, match='crash'):
            resume_imports(b, contract(), 'import', generation)
        b.copy_blob = original
        resume_imports(b, contract(), 'import', generation)
        count = b.db.execute('SELECT COUNT(*) FROM entity_versions').fetchone()[0]
        resume_imports(b, contract(), 'import', generation)
        assert b.db.execute('SELECT COUNT(*) FROM entity_versions').fetchone()[0] == count
        assert b.db.execute('SELECT state FROM reuse_imports WHERE import_key=?', (key,)).fetchone()[0] == 'committed'
        assert 'Later changed head' not in '\n'.join(r[0] for r in b.db.execute('SELECT payload_json FROM entity_versions'))
    finally:
        b.release('import', generation); a.close(); b.close()


def test_source_busy_and_corrupt_blob_are_rejected(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    ag, bg = a.acquire('active'), b.acquire('import')
    try:
        with pytest.raises(RunBusy):
            plan_import(b, a.run_dir, contract(), None, 'import', bg)
        a.release('active', ag)
        blob = next(a.blob_dir.iterdir())
        blob.write_bytes(b'corrupt')
        with pytest.raises(ReuseError, match='integrity_error'):
            plan_import(b, a.run_dir, contract(), None, 'import', bg)
        assert not b.db.execute('SELECT 1 FROM reuse_imports').fetchone()
    finally:
        b.release('import', bg); a.close(); b.close()


def test_only_selected_candidate_is_audited_and_document_validation_shared(tmp_path):
    a, b = source_run(tmp_path, count=25), new_store(tmp_path, 'B')
    try:
        import_source(a, b)
        c, agent, checks = controller(b)
        try:
            asyncio.run(c.review_reuse())
            assert len([x for x in agent.calls if x[0] == 'auditor.evidence']) == 1
            assert len(checks) == 1
            assert reuse_summary(b)['not_scheduled'] == 24
            asyncio.run(c.review_reuse())
            assert len([x for x in agent.calls if x[0] == 'auditor.evidence']) == 1
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_three_generation_invalidation_reaches_c_without_b_worker(tmp_path):
    a, b, child = source_run(tmp_path), new_store(tmp_path, 'B'), new_store(tmp_path, 'C')
    try:
        import_source(a, b)
        cb, _, _ = controller(b)
        asyncio.run(cb.run()); cb.close()
        import_source(b, child)
        cc, _, _ = controller(child)
        asyncio.run(cc.run()); cc.close()
        ag = a.acquire('correct')
        vid = a.db.execute("SELECT version_id FROM entity_versions WHERE kind='evidence'").fetchone()[0]
        request = InvalidationRequest(subject_version_ids=[vid], reason_code='content_error', evidence_refs=['fixture:correction'])
        publish_invalidation(a, request, 'correct', ag)
        a.release('correct', ag)
        cg = child.acquire('sync')
        assert sync_lineage(child, 'sync', cg)
        assert child.run()['lifecycle_status'] == 'active'
        assert child.db.execute("SELECT COUNT(*) FROM reuse_bindings WHERE disposition='needs_reaudit'").fetchone()[0] > 0
        count = child.db.execute('SELECT COUNT(*) FROM invalidation_receipts').fetchone()[0]
        assert sync_lineage(child, 'sync', cg)
        assert child.db.execute('SELECT COUNT(*) FROM invalidation_receipts').fetchone()[0] == count
        child.release('sync', cg)
    finally:
        a.close(); b.close(); child.close()


def test_move_source_retains_blobs_but_blocks_until_identity_relocated(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    import_source(a, b)
    old = a.run_dir
    a.close()
    old.rename(tmp_path / 'moved-A')
    generation = b.acquire('sync')
    try:
        assert not sync_lineage(b, 'sync', generation)
        for row in b.db.execute('SELECT sha256 FROM blobs'):
            assert b.read_blob(row[0])
        relocate_source(b, tmp_path / 'moved-A', 'sync', generation)
        assert sync_lineage(b, 'sync', generation)
    finally:
        b.release('sync', generation); b.close()


def test_source_ttl_refresh_does_not_repeat_semantic_audits(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    cdata = contract().model_dump(mode='json')
    cdata['reuse_policy']['default_rule']['max_validation_age_seconds'] = 0
    target = ResearchContract.model_validate(cdata)
    try:
        import_source(a, b, target)
        c, agent, checks = controller(b, target)
        asyncio.run(c.review_reuse()); c.close()
        c, agent2, checks2 = controller(b, target)
        try:
            asyncio.run(c.review_reuse())
            assert len(checks2) == 1
            assert not any(role.startswith('auditor.') for role, _ in agent2.calls)
            assert len(c.evidence_rows()['R1']) == 1
        finally:
            c.close()
    finally:
        a.close(); b.close()


@pytest.mark.parametrize('date,expected', [(None, 'requires_refresh'), ('2027-01-01T00:00:00Z', 'rejected')])
def test_unknown_or_future_publication_never_grants_support(tmp_path, date, expected):
    a, b = source_run(tmp_path, published_at=date), new_store(tmp_path, 'B')
    try:
        import_source(a, b)
        c, agent, checks = controller(b)
        try:
            asyncio.run(c.review_reuse())
            assert not c.evidence_rows()['R1']
            assert b.db.execute('SELECT disposition FROM reuse_bindings').fetchone()[0] == expected
            assert not checks
            assert not any(role.startswith('auditor.') for role, _ in agent.calls)
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_selection_union_and_cross_root_rejection(tmp_path):
    a, b = source_run(tmp_path, count=2), new_store(tmp_path, 'B')
    try:
        evidence = a.db.execute("SELECT version_id FROM entity_versions WHERE kind='evidence' ORDER BY id").fetchone()[0]
        import_source(a, b, selection=ReuseSelection(source_evidence_version_ids=[evidence]))
        assert reuse_summary(b)['imported_evidence'] == 1
        outside = new_store(tmp_path / 'other-root', 'outside')
        generation = outside.acquire('import')
        try:
            with pytest.raises(ReuseError, match='access_restricted'):
                plan_import(outside, a.run_dir, contract(), None, 'import', generation)
        finally:
            outside.release('import', generation); outside.close()
    finally:
        a.close(); b.close()


def test_dependency_closure_preserves_both_sides_of_conflict(tmp_path):
    a, b = source_run(tmp_path, count=2), new_store(tmp_path, 'B')
    try:
        evidence = list(a.db.execute("SELECT version_id FROM entity_versions WHERE kind='evidence' ORDER BY id"))
        claims = list(a.db.execute("SELECT version_id FROM entity_versions WHERE kind='claim' ORDER BY id"))
        with a.transaction() as db:
            a.append_entity(db, 'conflict', 'conflict-1', dict(severity='blocking', claim_version_ids=[r[0] for r in claims], evidence_version_ids=[r[0] for r in evidence]))
        import_source(a, b, selection=ReuseSelection(source_evidence_version_ids=[evidence[0][0]]))
        assert reuse_summary(b)['imported_evidence'] == 2
        c, agent, _ = controller(b)
        try:
            asyncio.run(c.review_reuse())
            assert not c.evidence_rows()['R1']
            packet = next(p for role, p in agent.calls if role == 'auditor.counter_entailment')
            assert len(json.loads(packet['conflicts_json'])[0]['evidence']) == 2
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_limit_rejects_whole_import_without_silently_truncating(tmp_path, monkeypatch):
    import src.research.reuse as reuse
    a, b = source_run(tmp_path, count=2), new_store(tmp_path, 'B')
    monkeypatch.setattr(reuse, 'MAX_EVIDENCE', 1)
    generation = b.acquire('import')
    try:
        with pytest.raises(ReuseError, match='reuse_selection_required'):
            plan_import(b, a.run_dir, contract(), None, 'import', generation)
        assert not b.db.execute('SELECT 1 FROM reuse_imports').fetchone()
        assert not b.db.execute('SELECT 1 FROM reuse_candidates').fetchone()
    finally:
        b.release('import', generation); a.close(); b.close()


def test_pre_import_withdrawal_is_consumed_before_mapping(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        generation = a.acquire('correction')
        # A document-level withdrawal must reach its evidence and local uses.
        vid = a.db.execute("SELECT version_id FROM entity_versions WHERE kind='snapshot'").fetchone()[0]
        publish_invalidation(a, InvalidationRequest(subject_version_ids=[vid], reason_code='withdrawn', evidence_refs=['fixture:withdrawal']), 'correction', generation)
        a.release('correction', generation)
        import_source(a, b)
        c, agent, checks = controller(b)
        try:
            asyncio.run(c.review_reuse())
            assert not c.evidence_rows()['R1']
            assert b.db.execute('SELECT disposition FROM reuse_bindings').fetchone()[0] == 'rejected'
            assert not checks
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_notice_hash_failure_cannot_advance_cursor(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        import_source(a, b)
        ag = a.acquire('correction')
        vid = a.db.execute("SELECT version_id FROM entity_versions WHERE kind='evidence'").fetchone()[0]
        publish_invalidation(a, InvalidationRequest(subject_version_ids=[vid], reason_code='content_error', evidence_refs=['fixture:correction']), 'correction', ag)
        a.release('correction', ag)
        with a.transaction() as db:
            db.execute("UPDATE invalidation_outbox SET notice_hash='corrupt'")
        bg = b.acquire('sync')
        try:
            with pytest.raises(ReuseError, match='integrity_error'):
                sync_lineage(b, 'sync', bg)
            assert b.db.execute('SELECT cursor FROM reuse_ancestors').fetchone()[0] == 0
            assert not b.db.execute('SELECT 1 FROM invalidation_receipts').fetchone()
        finally:
            b.release('sync', bg)
    finally:
        a.close(); b.close()


def test_failed_investigation_import_is_not_a_new_search_obligation(tmp_path):
    a, b = new_store(tmp_path, 'A'), new_store(tmp_path, 'B')
    try:
        with a.transaction() as db:
            a.append_entity(db, 'query', 'q1', dict(query_id='q1', question_id='R1', text='old query'))
            a.append_entity(db, 'query_result', 'q1', dict(query_id='q1', status='empty', hits=[]))
            db.execute("UPDATE runs SET lifecycle_status='incomplete',research_outcome='incomplete_budget'")
        import_source(a, b, selection=ReuseSelection(source_question_ids=['R1']))
        assert b.db.execute("SELECT COUNT(*) FROM entity_heads WHERE kind='investigation_history'").fetchone()[0] == 1
        assert not b.db.execute("SELECT 1 FROM entity_heads WHERE kind='query_result'").fetchone()
        assert not b.db.execute('SELECT 1 FROM attempts').fetchone()
    finally:
        a.close(); b.close()


def test_wrong_owner_missing_identity_and_schema_rejected(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    bg = b.acquire('import')
    try:
        with a.transaction() as db:
            db.execute("UPDATE run_identity SET owner='someone-else'")
        with pytest.raises(ReuseError, match='access_restricted'):
            plan_import(b, a.run_dir, contract(), None, 'import', bg)
        with a.transaction() as db:
            db.execute('DELETE FROM run_identity')
        with pytest.raises(ReuseError, match='reuse_source_unverified'):
            plan_import(b, a.run_dir, contract(), None, 'import', bg)
    finally:
        b.release('import', bg); a.close(); b.close()


def test_source_trigger_is_never_executed_during_freeze(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    bg = b.acquire('import')
    try:
        a.db.execute("CREATE TRIGGER untrusted_source_trigger AFTER INSERT ON leases BEGIN UPDATE runs SET run_id='corrupted'; END")
        with pytest.raises(ReuseError, match='unexpected source schema'):
            plan_import(b, a.run_dir, contract(), None, 'import', bg)
        assert a.run()['run_id'] == 'A'
    finally:
        b.release('import', bg); a.close(); b.close()


def test_historical_export_survives_invalidation(tmp_path):
    from src.research.export import export_artifacts
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        import_source(a, b)
        c, _, _ = controller(b)
        asyncio.run(c.run()); c.close()
        original = (b.run_dir / 'report.md').read_bytes()
        ag = a.acquire('correction')
        vid = a.db.execute("SELECT version_id FROM entity_versions WHERE kind='evidence'").fetchone()[0]
        publish_invalidation(a, InvalidationRequest(subject_version_ids=[vid], reason_code='withdrawn', evidence_refs=['fixture:withdrawn']), 'correction', ag)
        a.release('correction', ag)
        bg = b.acquire('sync')
        assert sync_lineage(b, 'sync', bg)
        b.release('sync', bg)
        (b.run_dir / 'report.md').unlink()
        export_artifacts(b)
        assert (b.run_dir / 'report.md').read_bytes() == original
        assert b.run()['lifecycle_status'] != 'complete'
    finally:
        a.close(); b.close()


def test_source_validation_shared_across_two_requirements(tmp_path):
    data = contract().model_dump(mode='json')
    second = json.loads(json.dumps(data['requirements'][0]))
    second['id'] = 'R2'
    for check in second['acceptance_checks']:
        check['check_id'] = check['check_id'].replace('R1', 'R2')
    data['requirements'].append(second)
    target = ResearchContract.model_validate(data)
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        import_source(a, b, target)
        c, agent, checks = controller(b, target)
        try:
            asyncio.run(c.review_reuse())
            assert len(checks) == 1
            assert len([x for x in agent.calls if x[0] == 'auditor.evidence']) == 2
            assert len(c.evidence_rows()['R2']) == 1
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_source_validation_failure_does_not_fall_back_to_old_support(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        import_source(a, b)
        c, agent, checks = controller(b)
        original = c.source_validator
        async def unavailable(packet):
            result = await original(packet)
            result['status'] = 'unknown'
            return result
        c.source_validator = unavailable
        try:
            asyncio.run(c.review_reuse())
            assert not c.evidence_rows()['R1']
            assert not any(role.startswith('auditor.') for role, _ in agent.calls)
            assert b.db.execute('SELECT disposition FROM reuse_bindings').fetchone()[0] == 'requires_refresh'
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_expired_during_writing_cannot_be_delivered(tmp_path):
    from src.research.loop import ResearchReopened
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        import_source(a, b)
        c, _, _ = controller(b)
        try:
            asyncio.run(c.run())
            c.clock = lambda: utc(FIXED) + timedelta(hours=1)
            with pytest.raises(ResearchReopened):
                c.assert_reuse_delivery()
            assert b.run()['lifecycle_status'] != 'complete'
        finally:
            c.close()
    finally:
        a.close(); b.close()


def test_metrics_keep_negative_savings_and_unknown_denominators():
    from src.research.reuse_metrics import paired_metrics
    result = paired_metrics({'quality_passed': True, 'external_calls': 10, 'cost_usd': 0},
                            {'quality_passed': True, 'external_calls': 12, 'cost_usd': 0, 'imported_evidence': 0, 'used_imported_evidence': 0})
    assert result['costs']['external_calls']['reduction'] == -0.2
    assert result['costs']['cost_usd']['reduction'] is None
    assert result['reuse_hit_rate'] is None
    failed = paired_metrics({'quality_passed': False, 'external_calls': 10}, {'quality_passed': True, 'external_calls': 2})
    assert failed['costs']['external_calls']['reduction'] is None


def test_apsw_online_backup_has_blob_manifest_and_valid_foreign_keys(tmp_path):
    from src.research.database import Database
    source = source_run(tmp_path)
    try:
        path = tmp_path / 'snapshot.sqlite'
        source.db.backup_to(path)
        copy = Database(path, readonly=True)
        try:
            assert copy.execute('SELECT COUNT(*) FROM blobs').fetchone()[0] == 1
            assert copy.execute('PRAGMA foreign_key_check').fetchone() is None
            assert copy.execute('SELECT run_instance_id FROM run_identity').fetchone()[0] == source.db.execute('SELECT run_instance_id FROM run_identity').fetchone()[0]
        finally:
            copy.close()
    finally:
        source.close()


def test_reuse_replays_exact_saved_role_requests_without_live_calls(tmp_path):
    import uuid
    from src.research.jsonio import digest
    from src.research.replay import ReplayRoleBackend
    source = source_run(tmp_path)
    fixture = tmp_path / 'replay'
    (fixture / 'roles').mkdir(parents=True)
    indexed = {}
    seed = uuid.UUID('36f7f96e-8b53-43c9-a29a-690d350cbb23')
    try:
        for name in ('B-record', 'B-replay'):
            target = new_store(tmp_path, name)
            target.namespace_seed = seed
            try:
                import_source(source, target)
                c, agent, checks = controller(target)
                if name == 'B-record':
                    original = agent.generate
                    async def record(request):
                        response = await original(request)
                        key = f'{request.role}|{request.logical_action_key}|{request.input_manifest_hash}'
                        relative = 'roles/' + digest(key) + '.json'
                        indexed[key] = relative
                        (fixture / relative).write_bytes(canonical(response.model_dump()))
                        return response
                    agent.generate = record
                else:
                    c.roles = ReplayRoleBackend(fixture)
                try:
                    assert asyncio.run(c.run()) == 'complete'
                    assert len(checks) == 1
                finally:
                    c.close()
                if name == 'B-record':
                    (fixture / 'fixture_manifest.json').write_bytes(canonical({'schema_version': 1, 'roles': indexed}))
            finally:
                target.close()
    finally:
        source.close()


def test_cli_reuse_run_and_completed_resume(tmp_path, monkeypatch, capsys):
    from src.research.cli import main
    source = source_run(tmp_path)
    source.close()
    backend = ReuseAgent()
    monkeypatch.setattr('src.research.cli.configured_backend', lambda config: backend)
    backend.api_requests = 0
    async def exchange(self, kind, logical_key, payload):
        assert kind == 'source_validation', 'reuse must not search or fetch this fixture again'
        return dict(validation_key=payload['validation_key'], checked_at=FIXED, status='unchanged', method='replay',
                    checked_channels=['fixture publisher'], checks_performed=payload['checks_required'],
                    result_refs=['fixture:status'], observed_raw_hash=payload['raw_hash'])
    monkeypatch.setattr('src.research.assisted.AssistantMailbox.exchange', exchange)
    # Wall clock is real in assisted; use a sufficiently long explicit policy.
    data = contract().model_dump(mode='json')
    data['reuse_policy']['default_rule']['max_validation_age_seconds'] = 4000000000
    path = tmp_path / 'target.json'; path.write_bytes(canonical(data))
    config = tmp_path / 'assisted.toml'
    config.write_text('[research.execution]\nmode="assisted"\n[research.model]\nbackend="chat_json"\nbase_url="https://example.org"\nmodel="fixture"\n[research.search]\nprovider="external"\n[research.fetch]\nmode="external"\n', encoding='utf-8')
    assert main(['run', '--contract', str(path), '--config', str(config), '--runs-root', str(tmp_path), '--run-id', 'B', '--reuse-from', str(tmp_path/'A')]) == 0
    before = len(backend.calls)
    assert main(['resume', '--run-dir', str(tmp_path/'B')]) == 0
    assert len(backend.calls) == before
    assert main(['reuse-sync', '--run-dir', str(tmp_path/'B')]) == 0


def test_historical_evidence_stays_bound_to_its_exact_snapshot(tmp_path):
    a, b = source_run(tmp_path), new_store(tmp_path, 'B')
    try:
        original = a.db.execute("SELECT payload_json FROM entity_versions WHERE kind='snapshot'").fetchone()[0]
        payload = json.loads(original)
        payload['published_at'] = '2027-01-01T00:00:00Z'
        with a.transaction() as db:
            a.append_entity(db, 'snapshot', 'doc', payload)
        import_source(a, b)
        snapshot = b.db.execute("SELECT payload_json FROM entity_versions WHERE kind='snapshot'").fetchone()[0]
        assert json.loads(snapshot)['published_at'] == '2026-08-01T00:00:00Z'
    finally:
        a.close(); b.close()
