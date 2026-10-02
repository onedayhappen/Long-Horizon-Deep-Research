"""Evidence-led refinement from a flat skeleton to a three-level report."""
import asyncio
import json

import pytest

from scripts.outline_iteration_fixture import prepare, ScenarioAgent, SEED, search_action, TEXTS
from src.research.controller import Controller
from src.research.models import ResearchContract, OutlineState, OutlineNode, OutlinePatchAction
from src.research.outline import InvalidOutline, apply_patch, validate_outline, section_context
from src.research.jsonio import load
from src.research.storage import Store


def test_search_refines_two_then_three_levels_and_writes_scoped_details(tmp_path):
    fixture = tmp_path / 'fixture'
    data, _ = prepare(fixture)
    store = Store(tmp_path / 'run', namespace_seed=SEED)
    store.init_run('hierarchy', 1, 'test')
    controller = Controller(store, ResearchContract.model_validate(data), fixture, 120, [80, 10, 10])
    packets = []

    def plans(step, packet):
        if step in (0, 2):
            return search_action(1 if step == 0 else 2)
        if step == 1:
            return dict(kind='patch_outline', base_outline_version=1, reason_refs=['q1.1.e1'], operations=[
                dict(kind='add', node_id='export-detail', target_parent_id='section-2',
                     title='CSV 导出', reason='官方声明提供独立的导出主题', requirement_ids=['R1'],
                     claim_ids=['claim-1'], evidence_ids=['q1.1.e1'])])
        if step == 4:
            assert 'research gates are not ready' in packet['last_feedback']
            review = json.loads(packet['outline_review_json'])
            # Narrow bindings on the existing node must not hide newly found support.
            assert {c['claim_id'] for c in review['supported_by_requirement']['R1']} == {'claim-1', 'claim-2'}
            return dict(kind='patch_outline', base_outline_version=2, reason_refs=['q2.1.e1'], operations=[
                dict(kind='split', node_id='export-detail', reason='新资料揭示需要分别解释的格式与行数边界',
                     requirement_ids=['R1'], nodes=[
                         dict(id='format', title='导出格式', requirement_ids=['R1'], claim_ids=['claim-1'], evidence_ids=['q1.1.e1']),
                         dict(id='row-limit', title='数据行数限制', requirement_ids=['R1'], claim_ids=['claim-2'], evidence_ids=['q2.1.e1'])]),
                dict(kind='bind_evidence', node_id='export-detail', reason='概述同时覆盖能力与边界',
                     requirement_ids=['R1'], claim_ids=['claim-1', 'claim-2'], evidence_ids=['q1.1.e1', 'q2.1.e1'])])
        return dict(kind='terminate', coverage_refs=packet['coverage_refs'], proposed_outcome='complete')

    agent = ScenarioAgent(plans=plans)
    base = agent.generate

    async def generate(request):
        packet = request.data_packet
        packets.append((request.role, packet))
        response = await base(request)
        value = json.loads(response.raw_text)
        if request.role == 'auditor.question_space':
            review = json.loads(packet['outline_review_json'])
            if len(review['supported_by_requirement']['R1']) == 2 and len(review['nodes']) == 3:
                value.update(review_status='missing', missing_questions=[
                    'export-detail 应按 q2.1.e1 展开数据行数限制，并区分格式能力。'])
        if request.role == 'writer.section' and packet['section_id'] in {'format', 'row-limit'}:
            material = json.loads(packet['section_material_json'])
            number = 1 if packet['section_id'] == 'format' else 2
            fact_id = f'fact-{packet["section_id"]}'
            value['facts'] = [dict(kind='factual', id=fact_id, requirement_ids=['R1'],
                                   claim_version_ids=[material['claims'][0]['version_id']],
                                   evidence_ids=[f'q{number}.1.e1'])]
            value['paragraphs'][0]['sentences'] = [dict(id=f'sentence-{number}', text=TEXTS[number-1], fact_ids=[fact_id])]
        response.raw_text = json.dumps(value, ensure_ascii=False)
        return response

    agent.generate = generate
    controller.roles = agent
    try:
        assert asyncio.run(controller.run()) == 'complete'
        assert store.status()['research_rounds'] == 2
        assert store.status()['outline_revision_count'] == 2
        assert len(controller.heads('rejection')) == 1
        outline = load(tmp_path / 'run/outline.json')
        assert [o['outline_version'] for o in outline['history']] == [1, 2, 3]
        assert all(n['parent_id'] is None for n in outline['history'][0]['nodes'])
        report = (tmp_path / 'run/report.md').read_text('utf-8')
        headings = [line for line in report.splitlines() if line.startswith('#')]
        assert headings == ['## 结论', '## 依据与限制', '### CSV 导出', '#### 导出格式', '#### 数据行数限制']
        assert TEXTS[0] in report and TEXTS[1] in report
        exported = load(tmp_path / 'run/report.json')
        assert [s['section_id'] for s in exported['sections']] == ['section-1', 'section-2', 'export-detail', 'format', 'row-limit']
        assert [s['level'] for s in exported['section_hierarchy']] == [1, 1, 2, 3, 3]
        assert exported['section_hierarchy'][-1]['parent_id'] == 'export-detail'
        assert {s['outline_version'] for s in exported['sections']} == {3}
        writes = {p['section_id']: p for role, p in packets if role == 'writer.section'}
        assert json.loads(writes['export-detail']['section_context_json'])['writing_role'] == 'overview'
        leaf = json.loads(writes['row-limit']['section_context_json'])
        assert leaf['path'] == ['依据与限制', 'CSV 导出', '数据行数限制']
        assert leaf['writing_role'] == 'detail'
        assert [s['id'] for s in leaf['siblings']] == ['format']
        for section, number in [('format', 1), ('row-limit', 2)]:
            material = json.loads(writes[section]['section_material_json'])
            assert [c['id'] for c in material['claims']] == [f'claim-{number}']
            assert [e['id'] for e in material['evidence']] == [f'q{number}.1.e1']
        # Evidence-only bindings narrow both sides of the audited pair.
        node = OutlineNode(id='probe', title='probe', requirement_ids=['R1'], evidence_ids=['q2.1.e1'])
        assert [c['id'] for c in controller.section_material(node)['claims']] == ['claim-2']
        node.claim_ids = ['claim-1']
        assert controller.section_material(node)['claims'] == []
        assert controller.section_material(node)['evidence'] == []
        assert 'outline_json' in next(p for role, p in packets if role == 'auditor.report')
    finally:
        controller.close()
        store.close()


def nested_outline():
    return OutlineState(outline_version=1, question_ids=['R1'], nodes=[
        OutlineNode(id='root', title='结论', requirement_ids=['R1']),
        OutlineNode(id='other', title='依据与限制', requirement_ids=['R1']),
        OutlineNode(id='child', title='能力', parent_id='root', requirement_ids=['R1']),
        OutlineNode(id='leaf', title='条件', parent_id='child', requirement_ids=['R1']),
    ])


@pytest.mark.parametrize('operation', [
    dict(kind='add', node_id='too-deep', title='第四层', target_parent_id='leaf'),
    dict(kind='split', node_id='leaf', nodes=[dict(id='a', title='A', requirement_ids=['R1']), dict(id='b', title='B', requirement_ids=['R1'])]),
    dict(kind='move', node_id='other', target_parent_id='leaf'),
])
def test_add_split_and_move_cannot_create_fourth_level(tmp_path, operation):
    data, _ = prepare(tmp_path / 'fixture')
    contract = ResearchContract.model_validate(data)
    outline = nested_outline()
    validate_outline(outline, contract)
    assert section_context(outline, outline.nodes[-1])['level'] == 3
    before = outline.model_dump()
    patch = OutlinePatchAction.model_validate(dict(base_outline_version=1, reason_refs=['e1'],
        operations=[dict(operation, reason='new evidence', requirement_ids=['R1'])]))
    with pytest.raises(InvalidOutline, match='maximum depth 3'):
        apply_patch(outline, patch, contract, {'e1'}, set(), {'e1'}, max_depth=3)
    assert outline.model_dump() == before


def test_moving_subtree_checks_descendant_depth(tmp_path):
    data, _ = prepare(tmp_path / 'fixture')
    outline = nested_outline()
    outline.nodes.append(OutlineNode(id='branch', title='分支', parent_id='other', requirement_ids=['R1']))
    patch = OutlinePatchAction.model_validate(dict(base_outline_version=1, reason_refs=['e1'],
        operations=[dict(kind='move', node_id='child', target_parent_id='branch', reason='regroup', requirement_ids=['R1'])]))
    with pytest.raises(InvalidOutline, match='maximum depth 3'):
        apply_patch(outline, patch, ResearchContract.model_validate(data), {'e1'}, set(), {'e1'}, max_depth=3)


@pytest.mark.parametrize('bindings, message', [({}, 'needs claim or evidence'), ({'claim_ids': ['missing']}, 'unknown claim/evidence')])
def test_binding_requires_existing_material(tmp_path, bindings, message):
    data, _ = prepare(tmp_path / 'fixture')
    outline = nested_outline()
    patch = OutlinePatchAction.model_validate(dict(base_outline_version=1, reason_refs=['e1'],
        operations=[dict(kind='bind_evidence', node_id='leaf', reason='scope material', requirement_ids=['R1'], **bindings)]))
    with pytest.raises(InvalidOutline, match=message):
        apply_patch(outline, patch, ResearchContract.model_validate(data), {'e1'}, set(), {'e1'})
