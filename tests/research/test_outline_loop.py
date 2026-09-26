import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from lh_harness.research.cli import main
from lh_harness.research.controller import Controller
from lh_harness.research.jsonio import load
from lh_harness.research.loop import ResearchStopped
from lh_harness.research.models import InitializationProposal, OutlinePatchAction, ResearchContract
from lh_harness.research.outline import apply_patch, initialize_outline, InvalidOutline
from lh_harness.research.storage import Store
from scripts.outline_iteration_fixture import prepare, ScenarioAgent, SEED, search_action

FIXTURE=Path(__file__).parents[1]/'fixtures/research/outline_iteration'


def make_controller(tmp_path, plans=None, **limits):
    fixture=tmp_path/'fixture';contract,_=prepare(fixture)
    store=Store(tmp_path/'run',namespace_seed=SEED);store.init_run('case',1,'test')
    controller=Controller(store,ResearchContract.model_validate(contract),fixture,120,[80,10,10],**limits)
    agent=ScenarioAgent(plans=plans);controller.roles=agent
    return controller,store,agent


def test_replay_search_patch_search_freeze_and_export(tmp_path):
    assert main(['run','--contract',str(FIXTURE/'contract.json'),'--config',str(FIXTURE/'research.toml'),'--runs-root',str(tmp_path),'--run-id','dynamic'])==0
    root=tmp_path/'dynamic'
    outline=load(root/'outline.json')
    assert outline['revision_count']==1
    assert [r['outline_version'] for r in outline['history']]==[1,2]
    assert outline['history'][1]['reason_refs']==['q1.1.e1']
    assert '适用条件' in (root/'report.md').read_text('utf-8')
    assert {s['outline_version'] for s in load(root/'report.json')['sections']}=={2}
    store=Store(root)
    assert store.status()['research_rounds']==2
    assert len(list(store.db.execute("SELECT * FROM entity_versions WHERE kind='coverage'")))>=3
    before=store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0];store.close()
    assert main(['resume','--run-dir',str(root)])==0
    store=Store(root);assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==before;store.close()


def test_crash_after_outline_commit_does_not_apply_patch_twice(tmp_path):
    c,s,a=make_controller(tmp_path)
    original=c.commit
    def crash(key,*args,**kwargs):
        result=original(key,*args,**kwargs)
        if key=='outline-patch:1':
            raise RuntimeError('simulated crash after commit')
        return result
    c.commit=crash
    with pytest.raises(RuntimeError,match='simulated crash'):
        asyncio.run(c.run())
    c.close();s.close()
    s=Store(tmp_path/'run',namespace_seed=SEED)
    c=Controller(s,ResearchContract.model_validate(load(tmp_path/'fixture/contract.json')),tmp_path/'fixture',120,[80,10,10]);c.roles=a
    try:
        assert asyncio.run(c.run())=='complete'
        assert s.status()['outline_revision_count']==1
        assert s.db.execute("SELECT COUNT(*) FROM attempts WHERE action_id='role:plan:1'").fetchone()[0]==1
        assert s.db.execute("SELECT COUNT(*) FROM attempts WHERE action_id='search:q1'").fetchone()[0]==1
    finally:
        c.close();s.close()


def test_premature_termination_is_rejected_three_times(tmp_path):
    def plans(step,p):
        return dict(kind='terminate',coverage_refs=p['coverage_refs'],proposed_outcome='complete')
    c,s,a=make_controller(tmp_path,plans)
    try:
        with pytest.raises(ResearchStopped) as exc:
            asyncio.run(c.run())
        assert exc.value.outcome=='incomplete_no_progress'
        assert len(c.heads('rejection'))==3
        assert not c.heads('draft')
        assert not any(role=='auditor.coverage' for role,_ in a.calls)
    finally:
        c.close();s.close()


def test_round_limit_does_not_count_outline_patch(tmp_path):
    c,s,a=make_controller(tmp_path,max_rounds=1)
    try:
        with pytest.raises(ResearchStopped) as exc:
            asyncio.run(c.run())
        assert exc.value.outcome=='incomplete_budget'
        assert str(exc.value)=='round_limit'
        assert s.status()['outline_revision_count']==1
        assert s.status()['research_rounds']==1
        assert not c.heads('draft')
    finally:
        c.close();s.close()


def test_duplicate_search_is_not_a_new_round(tmp_path):
    def plans(step,p):
        result=search_action(1)
        result['query_plan'][0]['query_id']=f'renamed-{step}' if step else 'q1'
        return result
    c,s,a=make_controller(tmp_path,plans)
    try:
        with pytest.raises(ResearchStopped) as exc:
            asyncio.run(c.run())
        assert exc.value.outcome=='incomplete_no_progress'
        assert s.status()['research_rounds']==1
    finally:
        c.close();s.close()


@pytest.mark.parametrize('mutation,message',[
    (lambda p:p.update(base_outline_version=99),'stale'),
    (lambda p:p.update(reason_refs=['invented']),'current evidence'),
    (lambda p:p['operations'][0].update(node_id='section-1'),'new ID'),
    (lambda p:p['operations'][0].update(target_parent_id='new'),'cycle'),
    (lambda p:p['operations'][0].update(requirement_ids=['unknown']),'unknown'),
])
def test_invalid_patch_leaves_original_unchanged(tmp_path,mutation,message):
    data,_=prepare(tmp_path/'fixture');contract=ResearchContract.model_validate(data)
    outline=initialize_outline(InitializationProposal(question_ids=['R1'],outline_sections=['结论','依据与限制']),contract)
    before=outline.model_dump()
    patch=dict(kind='patch_outline',base_outline_version=1,reason_refs=['e1'],operations=[dict(kind='add',node_id='new',title='条件',reason='新证据',requirement_ids=['R1'])])
    mutation(patch)
    with pytest.raises(InvalidOutline,match=message):
        apply_patch(outline,OutlinePatchAction.model_validate(patch),contract,{'e1'},set(),{'e1'})
    assert outline.model_dump()==before


def test_retire_cannot_remove_required_section(tmp_path):
    data,_=prepare(tmp_path/'fixture');contract=ResearchContract.model_validate(data)
    outline=initialize_outline(InitializationProposal(question_ids=['R1'],outline_sections=['结论','依据与限制']),contract)
    patch=OutlinePatchAction.model_validate(dict(kind='patch_outline',base_outline_version=1,reason_refs=['e1'],operations=[dict(kind='retire_node',node_id='section-1',reason='attempt to drop scope',requirement_ids=['R1'])]))
    with pytest.raises(InvalidOutline,match='required output section'):
        apply_patch(outline,patch,contract,{'e1'},set(),{'e1'})


@pytest.mark.parametrize('always_fail',[False,True])
def test_report_rewrites_only_affected_section_and_bounds_retries(tmp_path,always_fail):
    c,s,a=make_controller(tmp_path)
    base=a.generate
    audits=0
    async def generate(request):
        nonlocal audits
        response=await base(request)
        if request.role=='auditor.report':
            audits+=1
            if always_fail or audits==1:
                value=json.loads(response.raw_text)
                value['findings']=[dict(fact_id='f-condition',check_code='condition_wording',severity='material',repair_kind='rewrite_section',refs=['conditions'])]
                response.raw_text=json.dumps(value,ensure_ascii=False)
        return response
    a.generate=generate
    try:
        if always_fail:
            with pytest.raises(ResearchStopped) as exc:
                asyncio.run(c.run())
            assert exc.value.outcome=='incomplete_report_audit_loop'
            assert audits==3
        else:
            assert asyncio.run(c.run())=='complete'
            assert audits==2
        writes=[key for role,key in a.calls if role=='writer.section']
        assert sum(key.startswith('write:2:0') for key in writes)==1
        assert sum(key.startswith('write:2:1') for key in writes)==1
        assert sum(key.startswith('write:2:2') for key in writes)==(3 if always_fail else 2)
        assert s.db.execute('SELECT COUNT(*) FROM revision_jobs').fetchone()[0]==(2 if always_fail else 1)
    finally:
        c.close();s.close()


def test_report_new_evidence_gap_blocks_immediate_termination(tmp_path):
    c,s,a=make_controller(tmp_path)
    base=a.generate
    async def generate(request):
        response=await base(request)
        if request.role=='auditor.report':
            value=json.loads(response.raw_text)
            value['findings']=[dict(fact_id='f-condition',check_code='missing_scope',severity='blocking',repair_kind='collect_evidence',refs=['conditions'])]
            response.raw_text=json.dumps(value,ensure_ascii=False)
        return response
    a.generate=generate
    try:
        with pytest.raises(ResearchStopped) as exc:
            asyncio.run(c.run())
        assert exc.value.outcome=='incomplete_no_progress'
        assert c.open_report_gaps()
        assert c.head('draft','conditions') is None
        assert c.head('draft','section-1') is not None
        assert len([x for x in a.calls if x[0]=='auditor.report'])==1
    finally:
        c.close();s.close()


def test_report_gap_can_research_new_evidence_and_complete(tmp_path):
    import hashlib
    c,s,a=make_controller(tmp_path)
    extra='虚构产品 A 的 100 行限制指单次导出，较大的表格可分批导出。'
    raw=f'<p>{extra}</p>'.encode();fixture=tmp_path/'fixture'
    (fixture/'sources/3.html').write_bytes(raw)
    url='https://example.org/fictional-outline/3'
    c.fetch._sources[url]=dict(path='sources/3.html',mime='text/html',sha256=hashlib.sha256(raw).hexdigest())
    c.search._batches['q3']=dict(query_id='q3',status='hit',hits=[dict(hit_id='q3.1',url=url,title='虚构分批导出说明',snippet=extra,rank=1)],usage={'basis':'unknown'},error=None)
    base=a.generate;audits=0
    async def generate(request):
        nonlocal audits
        response=await base(request);value=json.loads(response.raw_text);p=request.data_packet
        if request.role=='planner.next' and request.logical_action_key=='plan:4':
            value=search_action(2);value['query_plan'][0].update(query_id='q3',text='虚构产品 A 单次导出与分批导出的条件')
        elif request.role=='researcher.extract' and p['source_id']=='q3.1':
            value['candidates'][0].update(claim_id='claim-3',claim_text=extra,excerpt=extra)
        elif request.role=='auditor.coverage':
            d=json.loads(p['research_digest_json'])
            if any(r['id']=='claim-3' for r in d['claim']):
                value['resolved_gap_ids']=[r['id'] for r in d['report_gaps'] if r['payload']['status']=='open']
        elif request.role=='writer.section' and p['section_id']=='conditions' and p['revision']==1:
            d=json.loads(p['section_material_json']);claim=next(r for r in d['claims'] if r['id']=='claim-3')
            value['facts'].append(dict(kind='factual',id='f-batch',requirement_ids=['R1'],claim_version_ids=[claim['version_id']],evidence_ids=['q3.1.e1']))
            value['paragraphs'][0]['sentences'].append(dict(id='batch-sentence',text=extra,fact_ids=['f-batch']))
        elif request.role=='auditor.report':
            audits+=1
            if audits==1:
                value['findings']=[dict(fact_id='f-condition',check_code='batch_scope',severity='material',repair_kind='collect_evidence',refs=['conditions'])]
        response.raw_text=json.dumps(value,ensure_ascii=False);return response
    a.generate=generate
    try:
        assert asyncio.run(c.run())=='complete'
        assert not c.open_report_gaps()
        assert s.status()['research_rounds']==3
        assert audits==2
        assert '可分批导出' in (tmp_path/'run/report.md').read_text('utf-8')
        assert len([key for role,key in a.calls if role=='writer.section' and key.startswith('write:2:0')])==1
    finally:
        c.close();s.close()


def test_plateau_needs_three_audited_zero_novelty_strategies(tmp_path):
    from scripts.outline_iteration_fixture import TEXTS
    def plans(step,p):
        if step==4:
            return dict(kind='terminate',coverage_refs=p['coverage_refs'],proposed_outcome='incomplete_plateau')
        result=search_action(1)
        result['query_plan'][0].update(query_id=f'q{step+1}',text=f'虚构导出条件策略 {step}',strategy_family=['primary','primary','independent','counterevidence'][step])
        return result
    c,s,a=make_controller(tmp_path,plans)
    for i in range(2,5):
        batch=deepcopy(c.search._batches['q1']);batch['query_id']=f'q{i}';batch['hits'][0]['hit_id']=f'q{i}.1'
        c.search._batches[f'q{i}']=batch
    base=a.generate
    async def generate(request):
        response=await base(request)
        if request.role=='researcher.extract':
            value=json.loads(response.raw_text)
            value['candidates'][0].update(claim_id='claim-1',claim_text=TEXTS[0],excerpt=TEXTS[0])
            response.raw_text=json.dumps(value,ensure_ascii=False)
        return response
    a.generate=generate
    try:
        with pytest.raises(ResearchStopped) as exc:
            asyncio.run(c.run())
        assert exc.value.outcome=='incomplete_plateau'
        assert s.status()['research_rounds']==4
        assert [r['novelty'] for r in c.head('loop','controller')['payload']['recent_rounds']]==[0,0,0]
        assert not c.heads('draft')
    finally:
        c.close();s.close()


def test_outline_operations_keep_history_and_requirement_mapping(tmp_path):
    data,_=prepare(tmp_path/'fixture');contract=ResearchContract.model_validate(data)
    original=initialize_outline(InitializationProposal(question_ids=['R1'],outline_sections=['结论','依据与限制']),contract)
    outline=original
    operations=[
        dict(kind='add',node_id='detail',title='宽泛说明'),
        dict(kind='narrow_claim',node_id='detail',title='仅在条件内成立'),
        dict(kind='mark_gap',node_id='detail'),
        dict(kind='add_counterview',node_id='counter',title='反例'),
        dict(kind='split',node_id='detail',nodes=[dict(id='a',title='条件 A',requirement_ids=['R1']),dict(id='b',title='条件 B',requirement_ids=['R1'])]),
        dict(kind='move',node_id='counter',target_parent_id='detail'),
        dict(kind='merge',node_id='a',target_node_ids=['b']),
        dict(kind='retire_node',node_id='counter'),
    ]
    for operation in operations:
        operation.update(reason='证据揭示条件差异',requirement_ids=['R1'])
        patch=OutlinePatchAction.model_validate(dict(kind='patch_outline',base_outline_version=outline.outline_version,reason_refs=['e1'],operations=[operation]))
        outline=apply_patch(outline,patch,contract,{'e1'},set(),{'e1'})
    assert original.outline_version==1 and len(original.nodes)==2
    assert outline.outline_version==9
    assert next(n for n in outline.nodes if n.id=='b').replaced_by==['a']
    assert not next(n for n in outline.nodes if n.id=='counter').active


def test_elapsed_budget_is_persisted_and_checked_on_resume(tmp_path):
    c,s,a=make_controller(tmp_path,max_duration_seconds=1)
    c.last_tick-=2
    try:
        with pytest.raises(ResearchStopped) as exc:
            asyncio.run(c.run())
        assert exc.value.outcome=='incomplete_budget'
    finally:
        c.close();s.close()


    s=Store(tmp_path/'run',namespace_seed=SEED)
    c=Controller(s,ResearchContract.model_validate(load(tmp_path/'fixture/contract.json')),tmp_path/'fixture',120,[80,10,10],max_duration_seconds=1)
    try:
        with pytest.raises(ResearchStopped,match='time_limit'):
            asyncio.run(c.run())
    finally:
        c.close();s.close()


def test_search_action_has_its_own_call_limit(tmp_path):
    from lh_harness.research.budget import BudgetDenied
    def plans(step,p):
        result=search_action(1);result['max_external_calls']=2;return result
    c,s,a=make_controller(tmp_path,plans)
    try:
        with pytest.raises(BudgetDenied,match='action_call_limit'):
            asyncio.run(c.run())
        assert not any(role=='researcher.extract' for role,_ in a.calls)
    finally:
        c.close();s.close()
