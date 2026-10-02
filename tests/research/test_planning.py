"""Chapter investigations and termination invariants, using fictional sources."""
import asyncio
import json
from copy import deepcopy

import pytest

from scripts.outline_iteration_fixture import prepare, ScenarioAgent, SEED, search_action
from src.research.controller import Controller
from src.research.models import (ResearchContract, OutlineState, OutlinePatchAction,
    InitializationProposal, SearchAction, ResearchGap)
from src.research.outline import InvalidOutline, apply_patch, initialize_outline, ordered_nodes
from src.research.loop import ResearchStopped
from src.research.storage import Store
from src.research.jsonio import load


@pytest.fixture
def run(tmp_path):
    data, _ = prepare(tmp_path/'fixture')
    store = Store(tmp_path/'run', namespace_seed=SEED)
    store.init_run('planning', 1, 'test')
    c = Controller(store, ResearchContract.model_validate(data), tmp_path/'fixture', 120, [80,10,10])
    c.roles = ScenarioAgent()
    try:
        yield c, store
    finally:
        c.close(); store.close()


def gap_plans(step, packet):
    if step in (0, 2):
        action = search_action(1 if step == 0 else 2)
        action.update(target_node_ids=['section-2'], research_goal='Investigate CSV export conditions')
        if step == 2:
            action['target_gap_ids'] = ['row-boundary']
        return action
    if step == 1:
        return dict(kind='patch_outline', base_outline_version=1, reason_refs=['q1.1.e1'], operations=[
            dict(kind='mark_gap', node_id='section-2', gap_id='row-boundary', gap_kind='condition',
                 requirement_ids=['R1'], reason='Which row limit applies to CSV export?')])
    if step == 4:
        review = json.loads(packet['outline_review_json'])
        claim = next(c for c in review['supported_by_requirement']['R1'] if c['claim_id']=='claim-2')
        return dict(kind='patch_outline', base_outline_version=3, reason_refs=['row-boundary'], operations=[
            dict(kind='resolve_gap', node_id='section-2', gap_id='row-boundary', requirement_ids=['R1'],
                 resolution_refs=[claim['claim_version_id'], 'q2'], reason='Audited official row limit addresses the question.')])
    return dict(kind='terminate', coverage_refs=packet['coverage_refs'], proposed_outcome='complete')


def test_gap_blocks_completion_until_targeted_research_and_review(run, tmp_path):
    c, store = run
    agent = ScenarioAgent(plans=gap_plans)
    base = agent.generate
    packets = []
    async def generate(request):
        packets.append((request.role, request.data_packet))
        return await base(request)
    agent.generate = generate
    c.roles = agent
    assert asyncio.run(c.run()) == 'complete'
    gap = c.head('outline_gap', 'row-boundary')['payload']
    assert gap['status'] == 'resolved'
    assert gap['investigation_refs'] == ['q2']
    assert gap['resolution_audit_id']
    assert 'unresolved chapter gaps' in c.heads('rejection')[0]['payload']['reason']
    assert len(c.heads('gap_review')) == 1
    assert len(c.heads('outline_review')) == 1
    scopes = [json.loads(p['research_scope_json']) for role,p in packets if role=='researcher.extract']
    assert scopes[1]['target_gap_ids'] == ['row-boundary']
    assert scopes[1]['research_goal'] == 'Investigate CSV export conditions'
    assert scopes[1]['questions'][0]['node_id'] == 'section-2'
    assert c.head('research_progress', '00000004')['payload']['resolved_gap_ids'] == ['row-boundary']
    exported = load(tmp_path/'run/outline.json')
    assert exported['current']['gaps'][0]['status'] == 'resolved'
    assert exported['current']['outline_version'] == 4
    assert exported['gap_review'][0]['verdict']['verdict'] == 'pass'
    assert exported['research_progress'][-1]['resolved_gap_ids'] == ['row-boundary']


def test_gap_closure_crash_resumes_without_duplicate_audit(run, tmp_path):
    c, store = run
    c.roles = ScenarioAgent(plans=gap_plans)
    original = c.commit
    def crash(key, *args, **kwargs):
        receipt = original(key, *args, **kwargs)
        if key == 'outline-patch:4':
            raise RuntimeError('crash after gap closure')
        return receipt
    c.commit = crash
    with pytest.raises(RuntimeError, match='crash after gap closure'):
        asyncio.run(c.run())
    c.commit = original
    assert asyncio.run(c.run()) == 'complete'
    assert len([r for r,k in c.roles.calls if r=='auditor.gap']) == 1
    assert c.head('outline','outline')['payload']['outline_version'] == 4


def test_gap_audit_can_reject_related_but_insufficient_evidence(run):
    c, store = run
    agent = ScenarioAgent(plans=gap_plans)
    base = agent.generate
    async def generate(request):
        response = await base(request)
        if request.role == 'auditor.gap':
            value = json.loads(response.raw_text)
            value.update(verdict='missing', reason='The scope does not answer the chapter question.')
            response.raw_text = json.dumps(value)
        return response
    agent.generate = generate
    c.roles = agent
    with pytest.raises(ResearchStopped):
        asyncio.run(c.run())
    assert c.head('outline_gap','row-boundary')['payload']['status']=='investigating'
    assert not c.heads('freeze')


def initial(c):
    return initialize_outline(InitializationProposal(question_ids=['R1'], outline_sections=['结论','依据与限制']), c.contract)


def patch(c, outline, *operations, **kwargs):
    action = OutlinePatchAction.model_validate(dict(base_outline_version=outline.outline_version,
        reason_refs=['e1'], operations=[dict(reason='Fictional evidence', requirement_ids=['R1'], **op) for op in operations]))
    return apply_patch(outline, action, c.contract, {'e1'}, set(), {'e1'}, **kwargs)


def test_gap_cannot_be_retired_deferred_or_closed_without_approval(run):
    c,_ = run
    outline = patch(c, initial(c), dict(kind='mark_gap', node_id='section-1', gap_id='g'))
    before = outline.model_dump()
    for operation in (
        dict(kind='retire_node',node_id='section-1'),
        dict(kind='resolve_gap',node_id='section-1',gap_id='g',resolution_refs=['e1']),
        dict(kind='resolve_gap',node_id='section-1',gap_id='g',resolution_status='deferred',resolution_refs=['e1']),
    ):
        with pytest.raises(InvalidOutline):
            patch(c, outline, operation)
    assert outline.model_dump() == before


def test_split_and_merge_transfer_gap_ownership_and_sibling_order(run):
    c,_ = run
    outline = patch(c, initial(c), dict(kind='mark_gap',node_id='section-2',gap_id='g'))
    outline = patch(c, outline, dict(kind='split',node_id='section-2',nodes=[
        dict(id='a',title='A',requirement_ids=['R1']),dict(id='b',title='B',requirement_ids=['R1'])]))
    assert outline.gaps[0].node_ids == ['a','b']
    outline = patch(c, outline, dict(kind='reorder',node_id='b',before_id='a'))
    assert [n.id for n in ordered_nodes(outline)] == ['section-1','section-2','b','a']
    outline = patch(c, outline, dict(kind='merge',node_id='a',target_node_ids=['b']))
    assert outline.gaps[0].node_ids == ['a']
    assert next(n for n in outline.nodes if n.id=='a').gap_ids == ['g']
    assert not next(n for n in outline.nodes if n.id=='b').active


def test_research_scope_rejects_unrelated_or_inactive_targets(run):
    c,_ = run
    outline = initial(c)
    action = SearchAction.model_validate(dict(search_action(1), target_node_ids=['invented']))
    with pytest.raises(InvalidOutline,match='active target'):
        c.search_scope(action,outline)
    action.target_node_ids = ['section-2']
    action.target_gap_ids = ['invented']
    with pytest.raises(InvalidOutline,match='unknown gap'):
        c.search_scope(action,outline)


@pytest.mark.parametrize('change_goal,refresh', [(False,False), (True,False), (False,True)])
def test_same_url_reuses_fetch_but_new_goal_reextracts(run, change_goal, refresh):
    c,store = run
    outline = initial(c)
    c.commit_outline('initialization',outline)
    c.commit('loop:initialize', {}, [('loop','controller',dict(step=0))])
    first = SearchAction.model_validate(dict(search_action(1), research_goal='Find export capability'))
    asyncio.run(c.research_action(first,outline))
    second = deepcopy(c.search._batches['q1'])
    second['query_id']='q3'; second['hits'][0]['hit_id']='q3.1'
    c.search._batches['q3']=second
    action = first.model_copy(deep=True)
    action.refresh_sources = refresh
    action.query_plan[0].query_id='q3'; action.query_plan[0].text='Another export query'
    if change_goal:
        action.research_goal='Explain export conditions'
        base=c.roles.generate
        async def generate(request):
            response=await base(request)
            if request.role=='researcher.extract':
                value=json.loads(response.raw_text)
                value['candidates'][0].update(claim_id='claim-new',claim_text='虚构产品 A 官方声明支持导出 CSV 文件。',excerpt='虚构产品 A 官方声明支持导出 CSV 文件。')
                response.raw_text=json.dumps(value,ensure_ascii=False)
            return response
        c.roles.generate=generate
    asyncio.run(c.research_action(action,outline))
    assert store.db.execute("SELECT COUNT(*) FROM attempts WHERE action_id LIKE 'fetch:%'").fetchone()[0]==(2 if refresh else 1)
    extracts=[key for role,key in c.roles.calls if role=='researcher.extract']
    assert len(extracts)==(2 if change_goal else 1)


def test_source_selection_happens_before_fetch(run):
    c,store=run
    c.planning_config.max_sources_per_action=1
    batch=c.search._batches['q1']
    hit=deepcopy(c.search._batches['q2']['hits'][0]); hit['hit_id']='q1.2'
    batch['hits'].append(hit)
    asyncio.run(c.research_action(SearchAction.model_validate(search_action(1)), initial(c)))
    assert len(c.heads('source_selection'))==1
    assert store.db.execute("SELECT COUNT(*) FROM attempts WHERE action_id LIKE 'fetch:%'").fetchone()[0]==1


def test_planning_memory_omits_whole_entries_preserving_qualifiers(run):
    c,_=run
    c.planning_config.context_characters=4000
    for i in range(8):
        value=dict(text=('detail '*70)+'ONLY within the stated conditions.',planner_step=i)
        c.commit(f'summary:{i}',value,[('research_summary',str(i),value)])
    memory=c.planning_memory(initial(c))
    assert memory['omitted_summary_ids']
    assert memory['summaries'][0]['id']=='7'
    assert all(s['text'].endswith('ONLY within the stated conditions.') for s in memory['summaries'])
    with pytest.raises(ResearchStopped,match='planning_context_limit'):
        c.bound_planner_packet({'required_questions':'x'*5000})


@pytest.mark.parametrize('failure', ['unapproved_claim', 'empty_leaf'])
def test_final_review_cannot_accept_unapproved_claim_or_empty_leaf(run, failure):
    c,_=run
    c.roles=ScenarioAgent()
    assert asyncio.run(c.run())=='complete'
    outline=OutlineState.model_validate(c.head('outline','outline')['payload'])
    # Change input enough to avoid the valid cached final review.
    outline.nodes[0].purpose='A different purpose needs a new assessment.'
    base=c.roles.generate
    async def generate(request):
        response=await base(request)
        if request.role=='auditor.outline':
            value=json.loads(response.raw_text)
            if failure == 'unapproved_claim':
                value['nodes'][0]['claim_version_ids']=['invented-claim-version']
            else:
                value['nodes'][0].update(disposition='overview', claim_version_ids=[])
            response.raw_text=json.dumps(value)
        return response
    c.roles.generate=generate
    with pytest.raises(InvalidOutline,match='unsupported chapter|leaf cannot bypass'):
        asyncio.run(c.final_outline_review(outline))


def test_changed_purpose_reopens_closed_gap_and_old_audits_cannot_freeze(run):
    c,store=run
    c.roles=ScenarioAgent(plans=gap_plans)
    assert asyncio.run(c.run())=='complete'
    outline=OutlineState.model_validate(c.head('outline','outline')['payload'])
    assert not c.blocking_gaps(outline)
    same=patch(c,outline,dict(kind='update_node',node_id='section-2',title='依据与限制',
        purpose=outline.nodes[1].purpose, research_question=outline.nodes[1].research_question),
        dict(kind='reorder',node_id='section-2',before_id='section-1'))
    assert same.gaps[0].status=='resolved'
    changed=patch(c,outline,dict(kind='update_node',node_id='section-2',purpose='Investigate a broader population.'))
    assert changed.gaps[0].status=='open'
    assert changed.gaps[0].resolution_refs==[]
    # Revocation of the exact support must block even an unchanged outline.
    with store.transaction() as db:
        db.execute("UPDATE entity_heads SET validity='stale' WHERE kind='audit' AND id='q2.1.e1'")
    assert c.blocking_gaps(outline)==['row-boundary']
    with pytest.raises(InvalidOutline,match='unresolved chapter gaps'):
        asyncio.run(c.final_outline_review(outline))


def test_unknown_closure_requires_targeted_success_and_contract_permission(run):
    c,_=run
    gap=ResearchGap(id='g',node_ids=['section-1'],requirement_ids=['R1'],question='Unmeasured conditions?',investigation_refs=['q1'])
    c.commit('empty',{},[('query_result','q1',dict(status='empty'))])
    with pytest.raises(InvalidOutline,match='accepted unknown'):
        c.resolution_material(gap,['q1'],'accepted_unknown')
    c.contract.requirements[0].allow_unknown=True
    c.commit('unknown',{},[('coverage','R1',dict(disposition='bounded_unknown'))])
    assert c.resolution_material(gap,['q1'],'accepted_unknown')['investigations']
    c.commit('failed',{},[('query_result','q1',dict(status='error'))])
    with pytest.raises(InvalidOutline,match='unsupported or unrelated'):
        c.resolution_material(gap,['q1'],'accepted_unknown')


def test_closing_reserve_prevents_search_spending_last_review_calls(run):
    c,_=run
    from src.research.budget import BudgetDenied
    from src.research.models import Usage
    c.budget.limits['research']=2
    c.budget.exploration_floor=1
    c.budget.action_scope=('search',10)
    attempt=c.budget.reserve('a','fetch','hash','research',c.owner,c.generation)
    c.budget.settle(attempt,Usage(basis='unknown'))
    with pytest.raises(BudgetDenied,match='closing_reserve'):
        c.budget.reserve('b','fetch','hash','research',c.owner,c.generation)
    c.budget.action_scope=None
    assert c.budget.reserve('review','role','hash','research',c.owner,c.generation)
