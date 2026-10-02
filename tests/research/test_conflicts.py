"""Fictional conflict protocol tests; not measurements of model accuracy."""
import asyncio
import json

import pytest

from src.research.conflicts import ConflictProposal, ConflictVerification, adjudicate
from src.research.controller import Controller
from src.research.extract import locate
from src.research.jsonio import canonical, load
from src.research.models import DraftSection, EntityRef, ResearchContract, RoleResponse
from src.research.storage import Store
from src.research.budget import BudgetDenied
from tests.research.test_reuse import ReuseAgent, FIXTURE
from scripts.research_review_fixture import conflict_response


class ConflictAgent(ReuseAgent):
    def __init__(self, conflict_type='factual_contradiction', disposition='unresolved_disclosed'):
        super().__init__()
        self.conflict_type = conflict_type
        self.disposition = disposition
        self.scoped = True
        self.review_verdict = 'accept'
        self.omit_dispute = False

    async def generate(self, request):
        p = request.data_packet
        if request.role == 'auditor.conflict':
            self.calls.append((request.role, p))
            value = conflict_response(p)
            if p['phase'] == 'compare':
                value.update(conflict_type=self.conflict_type, disposition=self.disposition,
                             next_investigation='Obtain the original CSV experiment settings and correction record.')
                material = json.loads(p['material_json'])
                value['relations'] = [dict(claim_version_id=material[0]['claim']['version_id'],
                    evidence_version_id=material[1]['evidence'][0]['version_id'], relation='refutes', scope_match=self.conflict_type == 'factual_contradiction',
                    basis=value['frames'][1]['basis'][0], reason='Fictional explicitly opposite result.')]
            else:
                value.update(verdict=self.review_verdict, original_claims_scoped=self.scoped)
        elif request.role == 'auditor.coverage':
            self.calls.append((request.role, p))
            rows = self.controller.evidence_rows()[p['requirement_id']]
            value = dict(requirement_id=p['requirement_id'], disposition='open' if p['missing_check_ids'] else 'satisfied',
                passed_check_ids=p['passed_check_ids'], missing_check_ids=p['missing_check_ids'],
                claim_version_ids=sorted({r['claim_ref']['version_id'] for r in rows}), investigation_refs=[])
            requirement = self.controller.contract.requirements[0]
            if requirement.allow_unknown and p['missing_check_ids']:
                value.update(disposition='bounded_unknown', investigation_refs=['counter-query'])
        elif request.role == 'writer.section' and not self.omit_dispute:
            material = json.loads(p['section_material_json'])
            if not material.get('conflicts'):
                return await super().generate(request)
            self.calls.append((request.role, p))
            facts = [dict(kind='dispute', id=p['section_id'] + str(i), requirement_ids=['R1'],
                conflict_version_id=case['version_id'],
                evidence_ids=sorted({e['id'] for e in material['dispute_evidence']
                    if e['version_id'] in case['payload']['evidence_version_ids']}))
                for i, case in enumerate(material['conflicts'])]
            value = dict(section_id=p['section_id'], revision=p['revision'], outline_version=p['outline_version'],
                paragraphs=[dict(id='p', sentences=[dict(id='s', text='Source A says export succeeds; source B says it fails. The disagreement remains unresolved.', fact_ids=[f['id'] for f in facts])])],
                facts=facts, open_questions=[])
        else:
            return await super().generate(request)
        return RoleResponse(raw_text=canonical(value).decode(), finish_reason='stop', provider_request_id=None,
                            usage={'basis': 'unknown'}, model_profile_hash='fictional-conflicts')


def setup_controller(tmp_path, *, compare=False, allowed=True, calls=120):
    data = load(FIXTURE / 'contract.json')
    data['requirements'][0]['answer_mode'] = 'compare_evidence' if compare else 'resolve_fact'
    if allowed:
        data['requirements'][0]['acceptance_checks'].append(dict(check_id='R1.conflicts', code='conflict_disposition',
            stage='research', params=dict(allowed=['resolved_by_scope', 'unresolved_disclosed']), reason='Test conflict checks'))
    contract = ResearchContract.model_validate(data)
    store = Store(tmp_path / 'run')
    store.init_run('conflicts', 1, 'test')
    (store.run_dir / 'contract.json').write_bytes(canonical(contract.model_dump(mode='json')))
    c = Controller(store, contract, FIXTURE, calls, [80, 10, 10])
    agent = ConflictAgent()
    c.roles = agent
    agent.controller = c
    add_material(c, 'a', 'CSV export succeeds for 100 rows.')
    add_material(c, 'b', 'CSV export fails for 100 rows.')
    return c, store, agent


def add_material(c, name, text, *, approved=True):
    sha = c.store.put_blob(text.encode())
    c.commit('source:' + name + sha, text, [('source', name, dict(url='https://example.org/' + name)),
        ('snapshot', name, dict(source_id=name, url='https://example.org/' + name,
         raw_hash=sha, text_hash=sha, fetched_at='2026-09-01T00:00:00Z', parser='fixture'))])
    evidence = dict(snapshot_id=name, excerpt=text, locator=locate(text, text).model_dump(),
                    source_class='primary_official', observation_root=name, validity='active')
    claim = dict(text=text, kind='attributed', validity='current', requirement_ids=['R1'])
    refs = c.commit('candidate:' + name + sha, dict(evidence=evidence, claim=claim),
        [('claim', name, claim), ('evidence', name, evidence), ('audit', name,
         dict(forward=dict(verdict='supported' if approved else 'not_supported', source_class_verified=True),
              reverse=dict(verdict='no_objection')))])['refs']
    c.bind_candidate_evidence(refs[0], refs[1])
    if approved:
        c.store.add_link('link:' + refs[1]['version_id'], *(EntityRef.model_validate(r) for r in refs),
                         'supports', c.owner, c.generation)
    return refs


def close(c, s):
    c.close()
    s.close()


def test_conflict_detects_explicit_refutation_and_blocks_factual_support(tmp_path):
    c, s, agent = setup_controller(tmp_path)
    try:
        assert c.conflict_scan_state()['pending_ids']
        asyncio.run(c.review_conflicts())
        assert not c.conflict_scan_state()['pending_ids']
        assert c.current_conflicts()[0]['payload']['status'] == 'unresolved'
        assert not c.evidence_rows()['R1']
        assert not c.conflict_requirement_ready(c.contract.requirements[0])
        assert s.db.execute("SELECT COUNT(*) FROM evidence_links WHERE relation='refutes'").fetchone()[0] == 1
        before = c.usage_snapshot()['external_calls']
        asyncio.run(c.review_conflicts())
        assert c.usage_snapshot()['external_calls'] == before == 2
    finally:
        close(c, s)


def test_missing_contract_check_cannot_bypass_conflict(tmp_path):
    c, s, _ = setup_controller(tmp_path, compare=True, allowed=False)
    try:
        asyncio.run(c.review_conflicts())
        assert not c.conflict_requirement_ready(c.contract.requirements[0])
    finally:
        close(c, s)


def test_scope_split_requires_already_qualified_claims(tmp_path):
    c, s, agent = setup_controller(tmp_path)
    agent.conflict_type, agent.disposition = 'scope_difference', 'resolved_by_scope'
    agent.scoped = False
    try:
        asyncio.run(c.review_conflicts())
        assert c.current_conflicts()[0]['payload']['status'] == 'unresolved'
    finally:
        close(c, s)


def test_scoped_resolution_invalidated_by_new_evidence_version(tmp_path):
    c, s, agent = setup_controller(tmp_path)
    agent.conflict_type, agent.disposition = 'scope_difference', 'resolved_by_scope'
    try:
        asyncio.run(c.review_conflicts())
        assert c.conflict_requirement_ready(c.contract.requirements[0])
        old = c.conflict_freeze()
        add_material(c, 'b', 'CSV export fails for ALL dataset sizes.')
        assert not c.conflict_requirement_ready(c.contract.requirements[0])
        assert old != c.conflict_freeze()
        agent.conflict_type, agent.disposition = 'factual_contradiction', 'unresolved_disclosed'
        asyncio.run(c.review_conflicts())
        assert not c.conflict_requirement_ready(c.contract.requirements[0])
    finally:
        close(c, s)


def test_disagreement_report_renders_and_exports_both_citations(tmp_path):
    c, s, _ = setup_controller(tmp_path, compare=True)
    try:
        assert asyncio.run(c.run()) == 'complete'
        report = (s.run_dir / 'report.md').read_text('utf-8')
        assert '[^a]' in report and '[^b]' in report
        assert 'unresolved' in report
        assert load(s.run_dir / 'report.json')['citation_evidence_ids'] == ['a', 'b']
        assert c.current_conflicts()[0]['payload']['status'] == 'unresolved'
    finally:
        close(c, s)


def test_writer_cannot_hide_dispute_as_factual(tmp_path):
    c, s, agent = setup_controller(tmp_path, compare=True)
    agent.omit_dispute = True
    try:
        with pytest.raises(RuntimeError, match='contested claim'):
            asyncio.run(c.run())
        assert s.run()['lifecycle_status'] != 'complete'
    finally:
        close(c, s)


def test_budget_exhaustion_leaves_pending_comparison(tmp_path):
    c, s, _ = setup_controller(tmp_path)
    c.budget.limits['research'] = 1
    try:
        with pytest.raises(BudgetDenied):
            asyncio.run(c.review_conflicts())
        assert c.conflict_scan_state()['pending_ids']
        assert not c.conflict_requirement_ready(c.contract.requirements[0])
        c.budget.limits['research'] = 10
        asyncio.run(c.review_conflicts())
        assert c.usage_snapshot()['external_calls'] == 2
    finally:
        close(c, s)


def test_rejected_local_evidence_remains_in_comparison_pool(tmp_path):
    c, s, _ = setup_controller(tmp_path)
    try:
        add_material(c, 'counter', 'CSV export silently loses data.', approved=False)
        assert len(c.conflict_material()) == 3
        asyncio.run(c.review_conflicts())
        assert c.conflict_scan_state()['total'] == 3
    finally:
        close(c, s)


def test_crash_after_decision_commit_is_idempotent(tmp_path):
    c, s, _ = setup_controller(tmp_path)
    commit = c.commit
    def crash(key, *args):
        result = commit(key, *args)
        if key.startswith('conflict-complete:'):
            raise RuntimeError('crash after conflict commit')
        return result
    c.commit = crash
    try:
        with pytest.raises(RuntimeError, match='crash after'):
            asyncio.run(c.review_conflicts())
        c.commit = commit
        asyncio.run(c.review_conflicts())
        assert len(c.heads('conflict')) == 1
        assert c.usage_snapshot()['external_calls'] == 2
        assert s.db.execute("SELECT COUNT(*) FROM evidence_links WHERE relation='refutes'").fetchone()[0] == 1
        case = c.heads('conflict')[0]
        assert s.db.execute('SELECT 1 FROM dependency_edges WHERE to_version_id=?', (case['version_id'],)).fetchone()
    finally:
        close(c, s)


@pytest.mark.parametrize('disposition', ['weighted_conclusion', 'resolved_by_correction', 'resolved_by_reproduction'])
def test_model_assertion_alone_cannot_settle_conflict(tmp_path, disposition):
    c, s, agent = setup_controller(tmp_path)
    agent.disposition = disposition
    try:
        asyncio.run(c.review_conflicts())
        assert c.current_conflicts()[0]['payload']['status'] == 'unresolved'
        assert not c.conflict_requirement_ready(c.contract.requirements[0])
    finally:
        close(c, s)


def test_fabricated_quote_cannot_complete_comparison(tmp_path):
    c, s, agent = setup_controller(tmp_path)
    generate = agent.generate
    async def fabricate(request):
        response = await generate(request)
        if request.role == 'auditor.conflict' and request.data_packet['phase'] == 'compare':
            value = json.loads(response.raw_text)
            value['frames'][0]['basis'][0]['quote'] = 'invented source statement'
            response.raw_text = canonical(value).decode()
        return response
    agent.generate = fabricate
    try:
        with pytest.raises(RuntimeError, match='exact evidence quote'):
            asyncio.run(c.review_conflicts())
        assert c.conflict_scan_state()['pending_ids']
    finally:
        close(c, s)


def test_report_must_disclose_every_side(tmp_path):
    c, s, agent = setup_controller(tmp_path, compare=True)
    generate = agent.generate
    async def omit(request):
        response = await generate(request)
        if request.role == 'writer.section':
            value = json.loads(response.raw_text)
            value['facts'][0]['evidence_ids'] = ['a', 'a']
            response.raw_text = canonical(value).decode()
        return response
    agent.generate = omit
    try:
        with pytest.raises(RuntimeError, match='both sides'):
            asyncio.run(c.run())
    finally:
        close(c, s)


def test_bounded_unknown_needs_investigation_and_dispute_disclosure(tmp_path):
    c, s, _ = setup_controller(tmp_path)
    requirement = c.contract.requirements[0]
    requirement.allow_unknown = True
    requirement.unknown_check_ids = ['R1.conflicts']
    query = dict(query_id='counter-query', question_id='R1', text='CSV conflict experiment details',
        strategy_family='counterevidence', language='en', country_code=None,
        source_class_targets=['primary_official'], max_results=1)
    c.commit('query', query, [('query', 'counter-query', query),
        ('query_result', 'counter-query', dict(query_id='counter-query', status='empty', hits=[]))])
    try:
        assert asyncio.run(c.run()) == 'complete_with_limitations'
        assert c.heads('coverage')[0]['payload']['disposition'] == 'bounded_unknown'
        assert '[^a]' in (s.run_dir / 'report.md').read_text('utf-8')
    finally:
        close(c, s)


def test_changed_conflict_invalidates_saved_authorization(tmp_path):
    c, s, _ = setup_controller(tmp_path, compare=True)
    try:
        assert asyncio.run(c.run()) == 'complete'
        old = c.head('freeze', 'outline')['version_id']
        add_material(c, 'b', 'CSV export fails for every row count.')
        asyncio.run(c.review_conflicts())
        assert c.head('freeze', 'outline') is None
        assert not c.heads('coverage')
        assert s.run()['lifecycle_status'] == 'active'
        assert s.db.execute('SELECT 1 FROM entity_versions WHERE version_id=?', (old,)).fetchone()
    finally:
        close(c, s)


def test_conflict_response_schemas_are_available_to_live_backend():
    from src.research.model_backend import response_schema
    assert response_schema('ConflictProposal', 1)['additionalProperties'] is False
    assert response_schema('ConflictVerification', 1)['additionalProperties'] is False


def test_imported_resolution_needs_new_local_two_sided_review(tmp_path):
    from src.research.reuse import plan_import, resume_imports
    from src.research.lineage import sync_lineage
    from src.research.reuse_models import ReuseSelection
    c, source, agent = setup_controller(tmp_path, compare=True)
    agent.conflict_type, agent.disposition = 'scope_difference', 'resolved_by_scope'
    asyncio.run(c.review_conflicts())
    contract = c.contract
    selected = c.heads('evidence')[0]['version_id']
    c.close()
    target = Store(tmp_path / 'target')
    target.init_run('target', 1, 'test')
    generation = target.acquire('import')
    try:
        plan_import(target, source.run_dir, contract, ReuseSelection(source_evidence_version_ids=[selected]), 'import', generation)
        resume_imports(target, contract, 'import', generation)
        assert sync_lineage(target, 'import', generation)
    finally:
        target.release('import', generation)
    controller = Controller(target, contract, FIXTURE, 120, [80, 10, 10])
    local_agent = ConflictAgent()
    local_agent.controller = controller
    controller.roles = local_agent
    try:
        assert len(controller.heads('evidence')) == 2
        assert not controller.conflict_requirement_ready(contract.requirements[0])
        assert all(r['payload']['status'] == 'open' for r in controller.current_conflicts())
        asyncio.run(controller.review_conflicts())
        assert controller.current_conflicts()[0]['payload']['status'] == 'unresolved'
        assert controller.current_conflicts()[0]['payload'].get('origin_ref') is None
    finally:
        close(controller, target)
        source.close()
