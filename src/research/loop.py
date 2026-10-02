"""Persistent planner loop. Models propose; deterministic checks authorize progress."""
from __future__ import annotations

import json
import time
from pydantic import TypeAdapter

from .jsonio import canonical, digest
from .models import (InitializationProposal, OutlineState, OutlinePatchAction, CoverageAssessment,
                     QuestionSpaceAudit, ResearchAction, SearchAction, TerminateProposal)
from .policy import coverage_gates
from .outline import InvalidOutline, initialize_outline, apply_patch, ordered_nodes, section_context, MAX_OUTLINE_DEPTH
from .storage import now, stamp


class ResearchStopped(RuntimeError):
    def __init__(self, outcome: str, reason: str):
        super().__init__(reason)
        self.outcome = outcome


class ResearchReopened(RuntimeError):
    pass


class ResearchLoopMixin:
    def heads(self, kind):
        return [dict(id=r['id'], version=r['version'], version_id=r['version_id'], payload=json.loads(r['payload_json']))
                for r in self.store.db.execute('''SELECT v.* FROM entity_versions v
                    JOIN entity_heads h ON v.kind=h.kind AND v.id=h.id AND v.version=h.version
                    WHERE v.kind=? AND h.validity='current' ORDER BY v.id''', (kind,))]

    def head(self, kind, entity_id):
        return next((r for r in self.heads(kind) if r['id'] == entity_id), None)

    def tick(self, enforce=True):
        current = time.monotonic()
        elapsed = current - self.last_tick
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            db.execute("INSERT INTO events(state_version,kind,payload_json,created_at) VALUES(?, 'active_time', ?, ?)",
                       (self.store.run()['state_version'], canonical({'seconds': elapsed}).decode(), stamp(now())))
        self.last_tick = current
        seconds = sum(json.loads(r[0])['seconds'] for r in self.store.db.execute("SELECT payload_json FROM events WHERE kind='active_time'"))
        if enforce and seconds >= self.max_duration_seconds:
            raise ResearchStopped('incomplete_budget', 'time_limit')

    def usage_snapshot(self):
        rows = list(self.store.db.execute("SELECT settled_usage_json FROM budget_reservations WHERE status!='released'"))
        usages = [json.loads(r[0]) if r[0] else {} for r in rows]
        unknown = sum(u.get('basis', 'unknown') == 'unknown' for u in usages)
        tokens = None if any(u.get('input_tokens') is None or u.get('output_tokens') is None for u in usages) else sum(u['input_tokens'] + u['output_tokens'] for u in usages)
        return dict(external_calls=len(rows), usage_unknown_attempts=unknown, total_tokens=tokens, cost_usd=None)

    def stop_context(self):
        outline = self.head('outline','outline')
        state = self.head('loop','controller')
        return dict(outline_version=outline['version'] if outline else None,
            research_rounds=state['payload']['rounds'] if state else 0,
            planner_steps=len(self.heads('plan')), coverage_version_ids=[r['version_id'] for r in self.heads('coverage')])

    def research_digest(self):
        # No previous role conversations or web page bodies are passed to Planner.
        result = {kind: self.heads(kind) for kind in ('query', 'query_result', 'claim', 'evidence', 'snapshot')}
        result['evidence_audits'] = [r for r in self.heads('audit') if 'forward' in r['payload']]
        result['links'] = [dict(r) for r in self.store.db.execute('SELECT * FROM evidence_links ORDER BY link_id')]
        result['report_gaps'] = self.heads('report_gap')
        if self.store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone():
            from .reuse import reuse_summary
            result['reuse_summary'] = reuse_summary(self.store)
            # Operational sync timestamps do not affect semantic model hashes.
            result['reuse_summary'].pop('lineage', None)
            result['investigation_history'] = self.heads('investigation_history')
            result['reuse_assessments'] = self.heads('reuse_assessment')
        return result

    def planner_digest(self):
        result = self.research_digest()
        result['evidence'] = [dict(id=r['id'], version_id=r['version_id'], snapshot_id=r['payload']['snapshot_id'],
            source_class=r['payload']['source_class'], excerpt_hash=r['payload']['locator']['quote_hash']) for r in result['evidence']]
        return result

    def section_material(self, node):
        rows = self.evidence_rows()
        selected = [row for rid in node.requirement_ids for row in rows[rid]]
        if node.claim_ids:
            selected = [row for row in selected if row['claim_ref']['id'] in node.claim_ids]
        if node.evidence_ids:
            selected = [row for row in selected if row['evidence_ref']['id'] in node.evidence_ids]
        allowed = {row['claim_ref']['version_id'] for row in selected}
        evidence_ids = {row['evidence_ref']['id'] for row in selected}
        return {'claims': [r for r in self.heads('claim') if r['version_id'] in allowed],
                'evidence': [r for r in self.heads('evidence') if r['id'] in evidence_ids],
                'coverage': [r for r in self.heads('coverage') if r['id'] in node.requirement_ids]}

    def outline_review(self, outline):
        """Give planner and question audit the same audited basis for refinement.

        Keep the full requirement pool visible even when a node is narrowly bound:
        new evidence may warrant a sibling rather than expanding that node's scope.
        """
        claims = {r['version_id']: r for r in self.heads('claim')}
        support = {}
        for rid, rows in self.evidence_rows().items():
            grouped = {}
            for row in rows:
                claim = claims[row['claim_ref']['version_id']]
                item = grouped.setdefault(claim['id'], dict(claim_id=claim['id'],
                    claim_version_id=claim['version_id'], text=claim['payload']['text'], evidence_ids=[]))
                if row['evidence_ref']['id'] not in item['evidence_ids']:
                    item['evidence_ids'].append(row['evidence_ref']['id'])
            support[rid] = list(grouped.values())
        return dict(max_depth=MAX_OUTLINE_DEPTH, supported_by_requirement=support,
                    nodes=[dict(id=n.id, title=n.title, requirement_ids=n.requirement_ids,
                                claim_ids=n.claim_ids, evidence_ids=n.evidence_ids,
                                **section_context(outline, n)) for n in ordered_nodes(outline)])

    def evidence_rows(self):
        result = {r.id: [] for r in self.contract.requirements}
        claims = {r['version_id']: r for r in self.heads('claim')}
        evidence = {r['version_id']: r for r in self.heads('evidence')}
        audits = {r['version_id']: r for r in self.heads('audit')}
        semantic_claims = {}
        for link in self.store.db.execute("SELECT * FROM evidence_links WHERE relation='supports' ORDER BY link_id"):
            c, e, a = claims.get(link['claim_version_id']), evidence.get(link['evidence_version_id']), audits.get(link['audit_version_id'])
            if not c or not e or not a:
                continue
            verdict = a['payload']
            if verdict.get('forward', {}).get('verdict') != 'supported' or verdict.get('reverse', {}).get('verdict') != 'no_objection':
                continue
            if c['payload'].get('validity') != 'current' or e['payload'].get('validity') != 'active':
                continue
            binding = self.store.db.execute('SELECT * FROM reuse_bindings WHERE local_claim_version_id=?', (c['version_id'],)).fetchone()
            if binding and not self.reuse_binding_valid(binding):
                continue
            signature = digest({'text': ' '.join(c['payload']['text'].split()).casefold(), 'kind': c['payload']['kind'], 'requirements': sorted(c['payload']['requirement_ids'])})
            canonical_id = semantic_claims.setdefault(signature, c['version_id'])
            if canonical_id != c['version_id']:
                continue
            cref = {k: c[k] for k in ('id','version','version_id')}; cref['kind'] = 'claim'
            eref = {k: e[k] for k in ('id','version','version_id')}; eref['kind'] = 'evidence'
            for rid in c['payload']['requirement_ids']:
                result[rid].append(dict(claim_ref=cref, evidence_ref=eref, source_class=e['payload']['source_class'],
                    source_class_verified=verdict['forward']['source_class_verified'], kind=c['payload']['kind'], qualified=True))
        return result

    def novelty_keys(self):
        # Ignore renamed IDs and alternate locators for the same text.
        return sorted({digest({'claim': ' '.join(r['payload']['text'].split()).casefold(), 'kind': r['payload']['kind']})
                       for r in self.heads('claim')} |
                      {digest({'excerpt': ' '.join(r['payload']['excerpt'].split()).casefold()}) for r in self.heads('evidence')})

    async def question_audit(self, outline):
        packet = {'question': self.contract.question, 'question_ids': outline.question_ids,
                  'outline_json': outline.model_dump_json(),
                  'outline_review_json': canonical(self.outline_review(outline)).decode(),
                  'claim_summaries_json': canonical(self.heads('claim')).decode()}
        token = digest(packet)
        audit = await self.role('auditor.question_space', f'question-space:{token}', packet, QuestionSpaceAudit)
        if audit.review_status == 'pass' and audit.missing_questions:
            raise ValueError('question audit cannot pass with missing questions')
        self.commit(f'question-space:{token}', audit.model_dump(mode='json'), [('audit', 'question-space', audit.model_dump(mode='json'))])
        return audit

    def patch_reasons(self, assessments, question_audit):
        reasons = {r['id'] for r in self.heads('evidence')}
        reasons |= {r['version_id'] for r in self.heads('claim')}
        reasons |= {f'gap:{a.requirement_id}:{c}' for a in assessments for c in a.missing_check_ids}
        reasons |= {f'question-gap:{i}' for i, _ in enumerate(question_audit.missing_questions)}
        reasons |= {r['id'] for r in self.heads('report_gap') if r['payload']['status']=='open'}
        return reasons

    def open_report_gaps(self):
        return [r for r in self.heads('report_gap') if r['payload']['status']=='open']

    def validate_search(self, action, state, outline):
        if action.question_id not in outline.question_ids or any(q.question_id not in outline.question_ids for q in action.query_plan):
            raise InvalidOutline('search refers to unknown question')
        check_ids = {c.check_id for r in self.contract.requirements for c in r.acceptance_checks if c.stage == 'research'}
        if not action.acceptance_check_ids or not set(action.acceptance_check_ids) <= check_ids:
            raise InvalidOutline('search must name valid acceptance checks')
        query_ids = [q.query_id for q in action.query_plan]
        if len(query_ids) != len(set(query_ids)) or set(query_ids) & set(state['query_ids']):
            raise InvalidOutline('query IDs cannot be reused')
        old = {r['id']: r['payload'] for r in self.heads('query') if r['id'] in state['query_ids']}
        fingerprints = {' '.join(q['text'].split()).casefold() for q in old.values()}
        for q in action.query_plan:
            fingerprint = ' '.join(q.text.split()).casefold()
            if fingerprint in fingerprints:
                raise InvalidOutline('duplicate query is not a new research strategy')
            fingerprints.add(fingerprint)

    async def run(self):
        contract = self.contract
        requirements = [r.id for r in contract.requirements]
        if not self.head('outline', 'outline'):
            self.phase('scoping')
            init = await self.role('planner.initialize', 'initialize', {'question': contract.question, 'requirement_ids': requirements}, InitializationProposal)
            if not set(requirements) <= set(init.question_ids):
                raise InvalidOutline('initial decomposition omits a requirement')
            outline = initialize_outline(init, contract)
            self.commit('initialization', outline.model_dump(mode='json'), [('outline','outline',outline.model_dump(mode='json'))])
        if not self.head('loop', 'controller'):
            state = dict(step=0, rounds=0, no_progress=0, query_ids=[], recent_rounds=[], last_feedback='')
            self.commit('loop:initialize', state, [('loop','controller',state)])
        while True:
            self.tick()
            await self.review_reuse()
            self.phase('researching')
            state = self.head('loop', 'controller')['payload']
            step = state['step']
            interrupted_reopen = next((r for r in self.heads('report_gap') if r['payload'].get('planner_step')==step),None)
            if interrupted_reopen:
                recovered = dict(state, step=step+1, no_progress=0,
                    last_feedback='report audit reopened research: '+interrupted_reopen['id'])
                self.commit(f'loop:{step}',recovered,[('loop','controller',recovered)])
                continue
            pending = self.head('plan', str(step))
            if pending is None:
                outline = OutlineState.model_validate(self.head('outline','outline')['payload'])
                question_audit = await self.question_audit(outline)
                assessments, assessment_refs, bias, gates = [], {}, None, None
                if self.heads('query_result') or any(self.evidence_rows().values()):
                    assessments, assessment_refs, bias, gates = await self.assess(outline, question_audit)
                packet = {'requirement_ids': requirements, 'question_ids': outline.question_ids,
                          'outline_json': outline.model_dump_json(), 'research_digest_json': canonical(self.planner_digest()).decode(),
                          'outline_review_json': canonical(self.outline_review(outline)).decode(),
                          'coverage_json': canonical([a.model_dump(mode='json') for a in assessments]).decode(),
                          'coverage_refs': list(assessment_refs.values()), 'patch_reason_refs': sorted(self.patch_reasons(assessments, question_audit)),
                          'question_audit_json': question_audit.model_dump_json(), 'research_ready': bool(gates and gates.research_ready and not self.open_report_gaps()),
                          'rounds_completed': state['rounds'], 'rounds_remaining': max(0, self.max_rounds-state['rounds']),
                          'recent_rounds_json': canonical(state['recent_rounds'][-3:]).decode(), 'last_feedback': state['last_feedback'],
                          'budget_json': canonical(self.budget.snapshot()).decode()}
                action_key = f'plan:{step}'
                if self.store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone():
                    action_key += ':reuse:' + digest(packet)
                action = await self.role('planner.next', action_key, packet, TypeAdapter(ResearchAction))
                plan = dict(action=action.model_dump(mode='json'), before_novelty=self.novelty_keys(),
                            outline=outline.model_dump(mode='json'), question_audit=question_audit.model_dump(mode='json'),
                            assessments=[a.model_dump(mode='json') for a in assessments], assessment_refs=assessment_refs,
                            bias_passed=bool(bias and bias.review_status == 'pass'))
                self.commit(action_key, plan, [('plan',str(step),plan)])
            else:
                plan = pending['payload']
                action = TypeAdapter(ResearchAction).validate_python(plan['action'], strict=True)
                outline = OutlineState.model_validate(plan['outline'])
                question_audit = QuestionSpaceAudit.model_validate(plan['question_audit'])
                assessments = [CoverageAssessment.model_validate(a) for a in plan['assessments']]
                assessment_refs = plan['assessment_refs']
                gates = coverage_gates(contract, assessments, question_audit_passed=question_audit.review_status=='pass',
                    search_bias_passed=plan['bias_passed'], coverage_audit_passed=len(assessments)==len(contract.requirements))
            next_state = json.loads(json.dumps(state))
            next_state['step'] += 1
            try:
                if isinstance(action, SearchAction):
                    if state['rounds'] >= self.max_rounds:
                        raise ResearchStopped('incomplete_budget', 'round_limit')
                    self.validate_search(action, state, outline)
                    self.budget.action_scope = (f'plan:{step}',action.max_external_calls)
                    try:
                        await self.research_action(action, outline)
                    finally:
                        self.budget.action_scope = None
                    next_state['rounds'] += 1
                    next_state['query_ids'] += [q.query_id for q in action.query_plan]
                    new_keys = set(self.novelty_keys()) - set(plan['before_novelty'])
                    batches = {r['id']: r['payload'] for r in self.heads('query_result')}
                    audited_sources = {r['id'].rsplit('.e',1)[0] for r in self.heads('audit') if 'forward' in r['payload']}
                    material = any(hit['hit_id'] in audited_sources for q in action.query_plan for hit in batches[q.query_id]['hits'])
                    if material:
                        next_state['recent_rounds'].append(dict(novelty=len(new_keys), families=sorted({q.strategy_family for q in action.query_plan if batches[q.query_id]['status']=='hit'})))
                        next_state['recent_rounds'] = next_state['recent_rounds'][-self.saturation_effective_rounds:]
                    next_state['no_progress'] = 0
                    next_state['last_feedback'] = 'research committed; reconsider gaps and outline using new evidence'
                elif isinstance(action, OutlinePatchAction):
                    saved = self.head('outline_change', str(step))
                    if saved is None:
                        changed = apply_patch(outline, action, contract, self.patch_reasons(assessments, question_audit),
                            {r['id'] for r in self.heads('claim')}, {r['id'] for r in self.heads('evidence')})
                        payload = changed.model_dump(mode='json')
                        self.commit(f'outline-patch:{step}', payload, [('outline','outline',payload),('outline_change',str(step),payload)])
                    next_state['no_progress'] = 0
                    next_state['last_feedback'] = 'outline revised; question-space and coverage checks will use the new version'
                elif isinstance(action, TerminateProposal):
                    if not assessments or set(action.coverage_refs) != set(assessment_refs.values()):
                        raise InvalidOutline('termination needs current coverage version references')
                    if action.proposed_outcome in ('complete','complete_with_limitations'):
                        if not gates.research_ready or self.open_report_gaps():
                            raise InvalidOutline('research gates are not ready; continue evidence collection or revise outline')
                        # Freeze only after a planner termination proposal passes current gates.
                        freeze = dict(outline_version=outline.outline_version, coverage_refs=list(assessment_refs.values()),
                            question_space_passed=question_audit.review_status=='pass', search_bias_passed=plan['bias_passed'],
                            coverage_audit_passed=len(assessments)==len(contract.requirements))
                        freeze_key = f'freeze:{step}'
                        if self.store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone():
                            freeze_key += ':' + digest(freeze)
                        self.commit(freeze_key, freeze, [('freeze','outline',freeze)])
                        return await self.write_report(outline, assessments, assessment_refs)
                    if action.proposed_outcome == 'incomplete_plateau':
                        window = state['recent_rounds'][-self.saturation_effective_rounds:]
                        families = {f for r in window for f in r['families']}
                        required = {'primary','independent','counterevidence'}
                        if len(window) == self.saturation_effective_rounds and all(r['novelty']==0 for r in window) and required <= families and not gates.research_ready:
                            raise ResearchStopped('incomplete_plateau', 'zero_novelty_across_required_strategies_with_remaining_gaps')
                        raise InvalidOutline('plateau requires audited material, distinct strategies, zero novelty and unmet gates')
                    raise InvalidOutline('unsupported termination outcome')
            except ResearchReopened as exc:
                next_state['no_progress'] = 0
                next_state['last_feedback'] = str(exc)
            except InvalidOutline as exc:
                next_state['no_progress'] += 1
                next_state['last_feedback'] = str(exc)
                rejection = dict(step=step, action=action.model_dump(mode='json'), reason=str(exc))
                self.commit(f'rejection:{step}', rejection, [('rejection',str(step),rejection)])
            self.commit(f'loop:{step}', next_state, [('loop','controller',next_state)])
            if next_state['no_progress'] >= self.max_no_progress_actions:
                raise ResearchStopped('incomplete_no_progress', 'control_loop_stalled')
