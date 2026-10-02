import asyncio
import json
from pathlib import Path

import pytest

from scripts.outline_iteration_fixture import ScenarioAgent, prepare, SEED
from src.research.config import Writing
from src.research.controller import Controller
from src.research.models import ResearchContract
from src.research.storage import Store
from src.research.writing import previous_context, writing_brief, source_context


def controller(tmp_path, *, rich=False):
    fixture = tmp_path / 'fixture'
    data, _ = prepare(fixture)
    store = Store(tmp_path / 'run', namespace_seed=SEED)
    store.init_run('writing-test', 1, 'test')
    c = Controller(store, ResearchContract.model_validate(data), fixture, 120, [80, 10, 10])
    agent = ScenarioAgent()
    requests = []
    original = agent.generate

    async def generate(request):
        requests.append(request)
        response = await original(request)
        if rich and request.role == 'researcher.extract' and request.data_packet['source_id'] == 'q1.1':
            value = json.loads(response.raw_text)
            candidate = dict(value['candidates'][0], claim_id='claim-format',
                             claim_text='虚构产品 A 的官方资料将 CSV 列为导出格式。')
            value['candidates'].append(candidate)
            response.raw_text = json.dumps(value, ensure_ascii=False)
        return response

    agent.generate = generate
    c.roles = agent
    return c, store, requests


def test_writer_retrieves_bounded_context_and_keeps_role_limits_separate(tmp_path):
    c, store, requests = controller(tmp_path)
    c.writing_config = Writing(max_output_tokens=7000, previous_context_characters=20,
                               source_context_characters=60)
    try:
        assert asyncio.run(c.run()) == 'complete'
        writers = [r for r in requests if r.role == 'writer.section']
        assert len(writers) == 3  # Two claims do not justify automatic padding.
        assert all(r.max_output_tokens == 7000 for r in writers)
        assert all(r.max_output_tokens == 4096 for r in requests if r.role != 'writer.section')
        assert json.loads(writers[0].data_packet['previous_sections_json']) == []
        for request in writers:
            p = request.data_packet
            brief = json.loads(p['writing_brief_json'])
            assert len(brief['outline']) == 3
            assert brief['target_characters'] <= 1750
            prior = json.loads(p['previous_sections_json'])
            assert sum(len(x['text']) for x in prior) <= 20
            contexts = json.loads(p['source_context_json'])
            assert contexts and sum(len(x['text']) for x in contexts) <= 60
            material = json.loads(p['section_material_json'])
            assert {x['evidence_id'] for x in contexts} <= {x['id'] for x in material['evidence']}
            for context in contexts:
                item = next(e for e in material['evidence'] if e['id'] == context['evidence_id'])
                raw = store.read_blob(item['payload']['locator']['text_blob_hash']).decode()
                assert context['text'] == raw[context['start']:context['end']]
            pairs = json.loads(p['citation_pairs_json'])
            assert {x['claim_version_id'] for x in pairs} <= set(p['claim_version_ids'])
        assert json.loads(writers[1].data_packet['previous_sections_json'])[0]['truncated']
        audit = next(r for r in requests if r.role == 'auditor.report')
        assert len(json.loads(audit.data_packet['section_depth_json'])) == 3
        assert previous_context([], 0) == []
        assert source_context(store, {'evidence': []}, 0) == []
    finally:
        c.close()
        store.close()


def test_expansion_is_bounded_and_resume_reuses_saved_calls(tmp_path):
    c, store, requests = controller(tmp_path, rich=True)
    commit = c.commit
    crashed = False

    def crash_before_draft(key, *args, **kwargs):
        nonlocal crashed
        if key.startswith('draft:') and not crashed:
            crashed = True
            raise RuntimeError('crash after expansion response')
        return commit(key, *args, **kwargs)

    c.commit = crash_before_draft
    try:
        with pytest.raises(RuntimeError, match='crash after expansion'):
            asyncio.run(c.run())
        assert asyncio.run(c.run()) == 'complete'
        writers = [r for r in requests if r.role == 'writer.section']
        assert len(writers) == 6  # Three originals and one expansion per section.
        assert len({r.logical_action_key for r in writers}) == 6
        expansions = [r for r in writers if ':expand:' in r.logical_action_key]
        assert len(expansions) == 3
        for request in expansions:
            p = request.data_packet
            assert json.loads(p['previous_draft_json'])['section_id'] == p['section_id']
            status = json.loads(p['expansion_feedback_json'])
            assert status['available_claims'] == 3 and status['needs_expansion']
        assert store.db.execute("SELECT COUNT(*) FROM attempts WHERE action_id LIKE 'role:write:%'").fetchone()[0] == 6
    finally:
        c.close()
        store.close()


def test_expansion_preserves_first_draft_budget(tmp_path):
    c, store, requests = controller(tmp_path, rich=True)
    c.budget.limits['writing'] = 3
    try:
        assert asyncio.run(c.run()) == 'complete'
        writers = [r for r in requests if r.role == 'writer.section']
        assert len(writers) == 3
        assert not any(':expand:' in r.logical_action_key for r in writers)
        audit = next(r for r in requests if r.role == 'auditor.report')
        assert all(s['needs_expansion'] for s in json.loads(audit.data_packet['section_depth_json']))
    finally:
        c.close()
        store.close()


def test_expansion_cannot_cite_new_evidence(tmp_path):
    c, store, _ = controller(tmp_path, rich=True)
    generate = c.roles.generate

    async def invalid_expansion(request):
        response = await generate(request)
        if ':expand:' in request.logical_action_key:
            value = json.loads(response.raw_text)
            value['facts'][0]['evidence_ids'] = ['invented-source']
            response.raw_text = json.dumps(value)
        return response

    c.roles.generate = invalid_expansion
    try:
        with pytest.raises(RuntimeError, match='outside its section material'):
            asyncio.run(c.run())
        assert not (store.run_dir / 'report.md').exists()
    finally:
        c.close()
        store.close()


def test_writing_target_respects_small_contract(tmp_path):
    from src.research.models import OutlineState, OutlineNode
    data, _ = prepare(tmp_path / 'fixture')
    contract = ResearchContract.model_validate(data)
    contract.output_contract.max_characters = 100
    outline = OutlineState(outline_version=1, question_ids=['R1'], nodes=[
        OutlineNode(id='a', title='A', requirement_ids=['R1']),
        OutlineNode(id='b', title='B', requirement_ids=['R1'])])
    brief = writing_brief(outline, contract, Writing())
    assert brief['target_characters'] * 2 < 100
