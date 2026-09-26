"""Explicit fictional scenario: search -> evidence-based outline add -> search -> stop.

The scripted agent is solely a test-data author. Production replay never calls it.
"""
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from lh_harness.research.controller import Controller
from lh_harness.research.jsonio import canonical, digest, load
from lh_harness.research.models import RoleResponse, ResearchContract
from lh_harness.research.storage import Store

SEED = '8135a02a-fc08-4e48-9339-89b5c141e159'
TEXTS = ['虚构产品 A 官方声明支持导出 CSV 文件。', '虚构产品 A 的 CSV 导出仅适用于最多 100 行的数据。']


def search_action(number):
    return dict(kind='search', question_id='R1', query_plan=[dict(query_id=f'q{number}', question_id='R1',
        text=['虚构产品 A 官方 导出 CSV','虚构产品 A CSV 导出 限制 反例'][number-1],
        strategy_family='primary' if number==1 else 'counterevidence', language='zh-CN', country_code=None,
        source_class_targets=['primary_official'], max_results=1)],
        acceptance_check_ids=['R1.support','R1.attribution'], max_external_calls=10)


class ScenarioAgent:
    """Manually specified fictional claims/audits; IDs and hashes follow requests."""
    def __init__(self, root=None, plans=None):
        self.root, self.plans = root, plans
        self.indexed = {}
        self.calls = []

    async def generate(self, request):
        role, key, p = request.role, request.logical_action_key, request.data_packet
        self.calls.append((role,key))
        if role == 'planner.initialize':
            result = dict(question_ids=['R1'],outline_sections=['结论','依据与限制'])
        elif role == 'auditor.question_space':
            result = dict(review_status='pass',dimensions_checked=['官方声明','导出条件','不等同独立测量'],missing_questions=[])
        elif role == 'planner.next':
            step = int(key.split(':')[1])
            if self.plans is not None:
                result = self.plans(step,p)
            elif step == 0:
                result = search_action(1)
            elif step == 1:
                result = dict(kind='patch_outline',base_outline_version=1,reason_refs=['q1.1.e1'],operations=[
                    dict(kind='add',node_id='conditions',target_parent_id='section-2',title='适用条件',reason='已找到导出声明，需要单独调查其适用条件',requirement_ids=['R1'])])
            elif step == 2:
                result = search_action(2)
            else:
                result = dict(kind='terminate',coverage_refs=p['coverage_refs'],proposed_outcome='complete')
        elif role == 'researcher.extract':
            number = 1 if p['source_id']=='q1.1' else 2
            result = dict(candidates=[dict(claim_id=f'claim-{number}',claim_text=TEXTS[number-1],claim_kind='attributed',
                requirement_ids=['R1'],excerpt=TEXTS[number-1],source_class='primary_official',observation_root='fictional-official')])
        elif role == 'auditor.evidence':
            result = dict(verdict='supported',reason='虚构原文逐字支持该官方归因陈述；不解释为独立验证',source_class_verified=True)
        elif role == 'auditor.counter_entailment':
            result = dict(verdict='no_objection',reason='该陈述保留了来源归因；限制信息单独提取和写入条件章节')
        elif role == 'auditor.search_bias':
            d=json.loads(p['research_digest_json'])
            result = dict(review_status='pass',source_classes_seen=['primary_official'],
                query_families_seen=sorted({q['payload']['strategy_family'] for q in d['query']}),blind_spots=['虚构示例，无独立性能验证'])
        elif role == 'auditor.coverage':
            d=json.loads(p['research_digest_json'])
            result=dict(requirement_id='R1',disposition='open' if p['missing_check_ids'] else 'satisfied',
                passed_check_ids=p['passed_check_ids'],missing_check_ids=p['missing_check_ids'],
                claim_version_ids=[c['version_id'] for c in d['claim']],investigation_refs=[])
        elif role == 'writer.section':
            material=json.loads(p['section_material_json']); claims={c['id']:c['version_id'] for c in material['claims']}
            section=p['section_id'];facts=[]
            if section=='section-1':
                text='虚构产品 A 官方声明支持 CSV 导出，使用时需同时考虑下述适用条件。'
                facts=[dict(kind='factual',id='f-export',requirement_ids=['R1'],claim_version_ids=[claims['claim-1']],evidence_ids=['q1.1.e1'])]
            elif section=='conditions':
                text=TEXTS[1]
                facts=[dict(kind='factual',id='f-condition',requirement_ids=['R1'],claim_version_ids=[claims['claim-2']],evidence_ids=['q2.1.e1'])]
            else:
                text='本例来源和数据均为虚构，仅用于验证动态大纲执行流程；未进行独立性能实测。'
            result=dict(section_id=section,revision=p.get('revision',0),outline_version=p['outline_version'],
                paragraphs=[dict(id=section+'-p',sentences=[dict(id=section+'-s',text=text,fact_ids=[f['id'] for f in facts])])],facts=facts,open_questions=[])
        elif role=='auditor.report':
            result=dict(report_hash=p['report_hash'],findings=[],checked_fact_ids=p['fact_ids'],checked_section_ids=p['section_ids'],answered_requirement_ids=['R1'])
        else:
            raise AssertionError((role,key))
        response=RoleResponse(raw_text=canonical(result).decode(),finish_reason='stop',provider_request_id=None,
            usage={'input_tokens':1,'output_tokens':1,'cost':None,'basis':'provider'},model_profile_hash='fictional-outline-fixture-v1')
        if self.root:
            index=f'{role}|{key}|{request.input_manifest_hash}'
            path=f'roles/{digest(index)}.json';self.indexed[index]=path
            (self.root/path).write_bytes(canonical(response.model_dump(mode='json')))
        return response


def prepare(root):
    root=Path(root);(root/'sources').mkdir(parents=True,exist_ok=True);(root/'roles').mkdir(exist_ok=True)
    base=Path(__file__).resolve().parents[1]/'tests/fixtures/research/success'
    contract=load(base/'contract.json')
    contract['contract_id']='fictional-outline-iteration'
    contract['question']='虚构产品 A 官方声明支持哪些 CSV 导出能力和适用条件？'
    contract['requirements'][0]['acceptance_checks'][0]['params']['min_claims']=2
    (root/'contract.json').write_bytes(canonical(contract))
    (root/'research.toml').write_bytes((base/'research.toml').read_bytes())
    sources={};searches={}
    for i,text in enumerate(TEXTS,1):
        path=f'sources/{i}.html';raw=f'<html><body><p>{text}</p></body></html>'.encode()
        (root/path).write_bytes(raw);url=f'https://example.org/fictional-outline/{i}'
        sources[url]=dict(path=path,mime='text/html',sha256=hashlib.sha256(raw).hexdigest())
        searches[f'q{i}']=dict(query_id=f'q{i}',status='hit',hits=[dict(hit_id=f'q{i}.1',url=url,title=f'虚构资料 {i}',snippet=text,rank=1,publisher='虚构官方',published_at=None)],usage={'basis':'unknown'},error=None)
    (root/'search.json').write_bytes(canonical(searches))
    manifest=dict(schema_version=1,namespace_seed=SEED,fixed_start='2026-09-01T00:00:00Z',sources=sources,roles={})
    (root/'fixture_manifest.json').write_bytes(canonical(manifest))
    return contract,manifest


def build(root):
    root=Path(root);contract,manifest=prepare(root)
    with tempfile.TemporaryDirectory() as directory:
        store=Store(Path(directory),namespace_seed=SEED);store.init_run('outline-fixture-index',1,'fixture')
        controller=Controller(store,ResearchContract.model_validate(contract),root,120,[80,10,10])
        agent=ScenarioAgent(root);controller.roles=agent
        try:
            outcome=asyncio.run(controller.run())
            assert outcome=='complete'
            assert store.status()['outline_revision_count']==1
            assert store.status()['research_rounds']==2
        finally:
            controller.close();store.close()
    manifest['roles']=agent.indexed
    (root/'fixture_manifest.json').write_bytes(canonical(manifest))


if __name__=='__main__':
    build(Path(__file__).resolve().parents[1]/'tests/fixtures/research/outline_iteration')
