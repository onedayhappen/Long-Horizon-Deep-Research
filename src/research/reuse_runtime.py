"""Demand-driven candidate mapping, document checks and local link audits."""
from __future__ import annotations

import json
import re
from datetime import timedelta

from .jsonio import canonical, digest, load
from .models import EvidenceAuditVerdict, CounterAuditVerdict, Usage, utc
from .reuse import entity, head
from .reuse_models import ReuseMappingProposal, ReuseAssessment, SourceValidationResult
from .reuse_paths import ReuseError
from .storage import stamp


def time_checks(snapshot, requirement, contract):
    rule = contract.reuse_policy.rule_for(requirement.id)
    as_of = utc(contract.as_of)
    checks, reasons = {}, []
    published = snapshot.get('published_at')
    if rule.knowledge_mode == 'strict_as_of':
        checks['knowledge_cutoff'] = 'unknown' if not published else 'pass' if utc(published) <= as_of else 'fail'
        if checks['knowledge_cutoff'] != 'pass':
            reasons.append('missing_date' if not published else 'future_knowledge')
    for check in requirement.acceptance_checks:
        if check.code == 'freshness':
            basis = snapshot.get(check.params.basis)
            ok = basis is not None and 0 <= (as_of - utc(basis)).total_seconds() <= check.params.max_age_days * 86400
            checks[check.check_id] = 'pass' if ok else 'unknown' if basis is None else 'fail'
            if not ok:
                reasons.append('freshness_required')
    start, end = snapshot.get('valid_from'), snapshot.get('valid_to')
    if start and as_of < utc(start) or end and as_of >= utc(end):
        checks['valid_interval'] = 'fail'
        reasons.append('time_scope_mismatch')
    return checks, sorted(set(reasons))


def validation_current(record, rule, generation, clock):
    p = record['payload'] if record else None
    if not p or p['status'] != 'unchanged':
        return False
    if rule.max_validation_age_seconds == 0:
        return p['generation'] == generation
    return utc(p['checked_at']) <= clock() < utc(p['checked_at']) + timedelta(seconds=rule.max_validation_age_seconds)


class ReuseRuntimeMixin:
    def reuse_notices(self, evidence, claim):
        return [r['payload']['notice'] for r in self.heads('invalidation_observation')
                if {evidence['version_id'], claim['version_id']}.intersection(r['payload']['local_version_ids'])]

    def reuse_barrier(self):
        from .lineage import sync_lineage
        from .loop import ResearchStopped
        if not sync_lineage(self.store, self.owner, self.generation, clock=self.clock):
            raise ResearchStopped('blocked', 'lineage_unavailable')

    def reuse_binding_valid(self, binding):
        if binding['disposition'] != 'reused':
            return False
        requirement = next(r for r in self.contract.requirements if r.id == binding['requirement_id'])
        assessment = entity(self.store, binding['assessment_version_id'])['payload']
        validation = entity(self.store, assessment['validation_refs'][0]) if assessment['validation_refs'] else None
        return validation_current(validation, self.contract.reuse_policy.rule_for(requirement.id), self.generation, self.clock)

    def expire_reuse(self):
        expired = [r for r in self.store.db.execute("SELECT * FROM reuse_bindings WHERE disposition='reused'") if not self.reuse_binding_valid(r)]
        if expired:
            with self.store.transaction() as db:
                self.store.check_lease(db, self.owner, self.generation)
                for binding in expired:
                    # Expiration changes source eligibility, not semantic meaning.
                    db.execute("UPDATE reuse_bindings SET disposition='requires_refresh' WHERE binding_id=?", (binding['binding_id'],))
                    db.execute("UPDATE entity_heads SET validity='needs_reaudit' WHERE kind='coverage' AND id=?", (binding['requirement_id'],))
                db.execute("UPDATE entity_heads SET validity='needs_reaudit' WHERE kind IN ('plan','freeze') OR (kind='audit' AND id='report')")
                db.execute("UPDATE runs SET state_version=state_version+1,lifecycle_status='active',research_outcome=NULL,current_stop_version_id=NULL")

    def reuse_candidates_for(self, requirement, limit=20):
        rows = list(self.store.db.execute('SELECT * FROM reuse_candidates ORDER BY evidence_version_id,claim_version_id'))
        reviewed = set()
        for item in self.heads('reuse_batch'):
            if item['payload']['requirement_id'] == requirement.id:
                reviewed.update(tuple(x) for x in item['payload']['candidate_pairs'])
        words = re.findall(r'[\w]+', requirement.question.casefold())
        scores = {}
        if words:
            query = ' OR '.join('"' + word.replace('"', '""') + '"' for word in words)
            for r in self.store.db.execute('SELECT evidence_version_id,claim_version_id,bm25(reuse_fts) score FROM reuse_fts WHERE reuse_fts MATCH ?', (query,)):
                scores[r['evidence_version_id'], r['claim_version_id']] = r['score']
        rows = [r for r in rows if (r['evidence_version_id'], r['claim_version_id']) not in reviewed]
        rows.sort(key=lambda r: (requirement.id not in json.loads(r['source_requirement_ids']), scores.get((r['evidence_version_id'], r['claim_version_id']), 0), r['evidence_version_id'], r['claim_version_id']))
        return rows[:limit]

    async def review_reuse(self):
        """One bounded batch per unsatisfied requirement; unchosen stays unscheduled."""
        if not self.store.db.execute('SELECT 1 FROM reuse_candidates LIMIT 1').fetchone():
            return
        self.reuse_barrier()
        self.expire_reuse()
        # Resume already mapped work first, including a crash after an audit result.
        for binding in list(self.store.db.execute("SELECT * FROM reuse_bindings WHERE disposition IN ('reuse_candidate','requires_refresh','needs_reaudit') ORDER BY binding_id")):
            await self.audit_reuse_binding(binding)
        for requirement in self.contract.requirements:
            rows = self.evidence_rows()[requirement.id]
            minimum = max((c.params.min_claims for c in requirement.acceptance_checks if c.code == 'has_current_support'), default=1)
            if len({r['claim_ref']['version_id'] for r in rows}) >= minimum:
                continue
            candidates = self.reuse_candidates_for(requirement)
            if not candidates:
                continue
            material = []
            for c in candidates:
                e, claim = entity(self.store, c['evidence_version_id']), entity(self.store, c['claim_version_id'])
                snap = head(self.store, 'snapshot', e['payload']['snapshot_id'])
                # Explicitly disjoint scope is a deterministic rejection. Missing
                # fields and aliases stay for semantic mapping.
                imp = json.loads(self.store.db.execute('SELECT manifest_json FROM reuse_imports WHERE import_key=?', (c['import_key'],)).fetchone()[0])
                old_scope = imp['source_contract']['scope']
                new_scope = self.contract.scope.model_dump()
                # Only canonical numeric versions / ISO region codes are safe to
                # compare without an alias-aware semantic judgment.
                mismatch = False
                for field, pattern in [('regions', r'[A-Z]{2}'), ('versions', r'\d+(?:\.\d+)*')]:
                    old, new = old_scope.get(field, []), new_scope.get(field, [])
                    if old and new and all(re.fullmatch(pattern, x) for x in [*old, *new]) and set(old).isdisjoint(new):
                        mismatch = True
                if mismatch:
                    # Record known incompatibility without spending a mapping call.
                    pair = (e['version_id'], claim['version_id'])
                    bid = digest([*pair, requirement.id, self.contract.version])
                    mapping = dict(evidence_version_id=pair[0], claim_version_id=pair[1], requirement_id=requirement.id,
                                   scope_matches=False, time_matches=True, reason='Explicit version or region mismatch')
                    with self.store.transaction() as db:
                        db.execute('INSERT OR IGNORE INTO reuse_bindings(binding_id,evidence_version_id,claim_version_id,requirement_id,mapping_json,disposition) VALUES(?,?,?,?,?,?)',
                                   (bid, *pair, requirement.id, canonical(mapping).decode(), 'reuse_candidate'))
                    await self.audit_reuse_binding(self.store.db.execute('SELECT * FROM reuse_bindings WHERE binding_id=?', (bid,)).fetchone())
                    continue
                material.append(dict(evidence_version_id=e['version_id'], claim_version_id=claim['version_id'], claim=claim['payload']['text'],
                                     excerpt=e['payload']['excerpt'], snapshot=snap['payload']))
            batch_key = digest({'requirement': requirement.model_dump(mode='json'), 'pairs': [tuple(c[k] for k in ('evidence_version_id','claim_version_id')) for c in candidates]})
            proposal = await self.role('planner.reuse_map', f'reuse-map:{batch_key}',
                                       {'requirement_id': requirement.id, 'candidates_json': canonical(material).decode()}, ReuseMappingProposal) if material else ReuseMappingProposal(bindings=[])
            allowed = {(c['evidence_version_id'], c['claim_version_id']) for c in candidates}
            selected = set()
            with self.store.transaction() as db:
                self.store.check_lease(db, self.owner, self.generation)
                for mapping in proposal.bindings:
                    pair = (mapping.evidence_version_id, mapping.claim_version_id)
                    if pair not in allowed or mapping.requirement_id != requirement.id or pair in selected:
                        raise ReuseError('invalid_mapping', 'mapping must use a unique supplied pair and current requirement')
                    selected.add(pair)
                    bid = digest([*pair, requirement.id, self.contract.version])
                    db.execute('INSERT OR IGNORE INTO reuse_bindings(binding_id,evidence_version_id,claim_version_id,requirement_id,mapping_json,disposition) VALUES(?,?,?,?,?,?)',
                               (bid, *pair, requirement.id, mapping.model_dump_json(), 'reuse_candidate'))
                self.store.append_entity(db, 'reuse_batch', batch_key, dict(requirement_id=requirement.id, candidate_pairs=sorted(allowed)))
                db.execute('UPDATE runs SET state_version=state_version+1')
            for binding in list(self.store.db.execute("SELECT * FROM reuse_bindings WHERE disposition='reuse_candidate' ORDER BY binding_id")):
                await self.audit_reuse_binding(binding)

    async def validate_reuse_source(self, snapshot, requirement, notices=()):
        rule = self.contract.reuse_policy.rule_for(requirement.id)
        policy_hash = digest(self.contract.reuse_policy.model_dump())
        key = digest({'url': snapshot['payload']['url'], 'raw_hash': snapshot['payload']['raw_hash'],
                      'parser': snapshot['payload']['parser'], 'policy': policy_hash, 'checks': ['document_identity', 'correction_status'],
                      'notice_ids': sorted(n['notice_id'] for n in notices)})
        previous = self.head('source_validation', key)
        if validation_current(previous, rule, self.generation, self.clock):
            return previous
        # A failed observation is not retried without bound in the same session.
        if previous and previous['payload']['generation'] == self.generation and previous['payload']['status'] != 'unchanged':
            return previous
        sequence = previous['version'] + 1 if previous else 1
        action_id = f'source-validation:{key}:{self.generation}:{sequence}'
        packet = dict(validation_key=key, url=snapshot['payload']['url'], raw_hash=snapshot['payload']['raw_hash'],
                      checks_required=['document_identity', 'correction_status'], snapshot_version_id=snapshot['version_id'])
        if notices:
            packet['invalidation_notices'] = list(notices)
        cached = self.store.db.execute('SELECT result_json FROM actions WHERE action_id=?', (action_id,)).fetchone()
        if cached and cached[0]:
            result = SourceValidationResult.model_validate_json(cached[0])
        else:
            attempt = self.budget.reserve(action_id, 'source_validation', digest(packet), 'research', self.owner, self.generation)
            try:
                if self.source_validator is not None:
                    result = await self.source_validator(packet)
                    result = SourceValidationResult.model_validate(result) if isinstance(result, dict) else result
                elif hasattr(self.fetch, 'mailbox'):
                    response = await self.fetch.mailbox.exchange('source_validation', action_id, packet)
                    result = SourceValidationResult.model_validate(response, strict=True)
                elif self.fixture_dir:
                    manifest = load(self.fixture_dir / 'fixture_manifest.json')
                    result = manifest.get('source_validations', {}).get(packet['url'])
                    if result is None:
                        raise ReuseError('requires_refresh', 'missing source validation fixture')
                    result = SourceValidationResult.model_validate(dict(result, validation_key=key))
                else:
                    raise ReuseError('requires_refresh', 'source validation provider unavailable')
                if result.validation_key != key or utc(result.checked_at) > self.clock():
                    raise ReuseError('integrity_error', 'source validation identity or time mismatch')
                if result.status == 'unchanged' and (result.observed_raw_hash != packet['raw_hash'] or not set(packet['checks_required']) <= set(result.checks_performed)):
                    raise ReuseError('integrity_error', 'incomplete document status check')
                self.budget.settle(attempt, Usage(basis='unknown'))
                with self.store.transaction() as db:
                    db.execute("UPDATE actions SET state='result_saved',result_json=?,result_hash=? WHERE action_id=?", (result.model_dump_json(), digest(result.model_dump()), action_id))
            except Exception as exc:
                self.budget.settle(attempt, Usage(basis='unknown'), error=type(exc).__name__)
                import httpx
                if isinstance(exc, (OSError, httpx.HTTPError)) or isinstance(exc, ReuseError) and exc.code == 'requires_refresh':
                    result = SourceValidationResult(validation_key=key, checked_at=stamp(self.clock()), status='unknown',
                        method='publisher_status', checked_channels=[packet['url']], checks_performed=['attempted_correction_status'], result_refs=[f'attempt:{attempt}'])
                    with self.store.transaction() as db:
                        db.execute("UPDATE actions SET result_json=?,result_hash=? WHERE action_id=?", (result.model_dump_json(), digest(result.model_dump()), action_id))
                else:
                    raise
        payload = dict(result.model_dump(), generation=self.generation, snapshot_version_id=snapshot['version_id'], policy_hash=policy_hash, action_id=action_id)
        self.commit(action_id, payload, [('source_validation', key, payload)])
        return self.head('source_validation', key)

    async def audit_reuse_binding(self, binding):
        bid = binding['binding_id']
        e = entity(self.store, binding['evidence_version_id'])
        claim = entity(self.store, binding['claim_version_id'])
        snap = head(self.store, 'snapshot', e['payload']['snapshot_id'])
        requirement = next(r for r in self.contract.requirements if r.id == binding['requirement_id'])
        mapping = json.loads(binding['mapping_json'])
        rule = self.contract.reuse_policy.rule_for(requirement.id)
        checks, reasons = time_checks(snap['payload'], requirement, self.contract)
        notices = self.reuse_notices(e, claim)
        if any(n['reason_code'] == 'withdrawn' for n in notices):
            checks['source_withdrawal'] = 'fail'
            reasons.append('source_withdrawn')
        checks.update(scope='pass' if mapping['scope_matches'] else 'fail', time_scope='pass' if mapping['time_matches'] else 'fail')
        if not mapping['scope_matches']:
            reasons.append('scope_mismatch')
        if not mapping['time_matches']:
            reasons.append('time_scope_mismatch')
        # Read and validate copied bytes before supplying any audit context.
        raw = self.store.read_blob(snap['payload']['raw_hash'])
        text = self.store.read_blob(snap['payload']['text_hash']).decode('utf-8')
        from .extract import verify_locator
        from .models import TextSpan, EntityRef
        if not verify_locator(text, TextSpan.model_validate(e['payload']['locator']), e['payload']['excerpt']):
            raise ReuseError('invalid_locator', e['version_id'])
        disposition = 'rejected' if any(r in reasons for r in ('scope_mismatch', 'time_scope_mismatch', 'future_knowledge', 'source_withdrawn')) else 'requires_refresh' if reasons else 'reuse_candidate'
        validation = None
        if disposition == 'reuse_candidate':
            validation = await self.validate_reuse_source(snap, requirement, notices)
            checks['source_status'] = 'pass' if validation_current(validation, rule, self.generation, self.clock) else 'fail'
            if checks['source_status'] != 'pass':
                disposition = 'rejected' if validation['payload']['status'] == 'withdrawn' else 'requires_refresh'
                reasons.append('source_' + validation['payload']['status'])
        audit_refs = []
        # TTL changes do not change the subject of an otherwise valid local audit.
        semantic = dict(evidence=e['version_id'], claim=claim['version_id'], requirement=requirement.model_dump(mode='json'),
                        contract=self.contract.model_dump(mode='json'), epoch=binding['epoch'], raw_hash=snap['payload']['raw_hash'])
        token = digest(semantic)
        if disposition == 'reuse_candidate':
            locator = e['payload']['locator']
            packet = dict(claim_version_id=claim['version_id'], evidence_id=e['id'], excerpt_hash=locator['quote_hash'],
                          source_class=e['payload']['source_class'], url=snap['payload']['url'], claim_text=claim['payload']['text'],
                          excerpt=e['payload']['excerpt'], source_context=text[max(0, locator['start'] - 1500):locator['end'] + 1500],
                          requirement_id=requirement.id, requirement_question=requirement.question)
            if notices:
                packet['invalidation_notices_json'] = canonical(notices).decode()
            conflicts = self.reuse_conflict_context(e, claim)
            if conflicts:
                packet['conflicts_json'] = canonical(conflicts).decode()
            forward = await self.role('auditor.evidence', f'reuse-forward:{bid}:{token}', packet, EvidenceAuditVerdict)
            reverse = await self.role('auditor.counter_entailment', f'reuse-reverse:{bid}:{token}', packet, CounterAuditVerdict)
            payload = dict(forward=forward.model_dump(), reverse=reverse.model_dump(), input_manifest_hash=digest(packet))
            receipt = self.commit(f'reuse-audit:{bid}:{token}', payload, [('audit', f'reuse-audit:{bid}', payload)])
            audit_refs = [receipt['refs'][0]['version_id']]
            disposition = 'reused' if forward.verdict == 'supported' and reverse.verdict == 'no_objection' else 'rejected'
            if disposition == 'rejected':
                reasons.append('local_audit_failed')
            if conflicts:
                # The existing controller has no conflict-disposition gate. Keep
                # these bindings blocked until that explicit gate is satisfied;
                # a generic no_objection response cannot resolve a known conflict.
                disposition = 'requires_refresh'
                reasons.append('blocking_conflict')
        assessment = ReuseAssessment(import_id=self.store.db.execute('SELECT import_key FROM reuse_candidates WHERE evidence_version_id=? AND claim_version_id=?', (e['version_id'], claim['version_id'])).fetchone()[0],
            evidence_version_id=e['version_id'], claim_version_id=claim['version_id'], requirement_id=requirement.id, contract_version=self.contract.version,
            policy_hash=digest(self.contract.reuse_policy.model_dump()), input_manifest_hash=digest([token, validation['version_id'] if validation else None, disposition]),
            checks=checks, disposition=disposition, reason_codes=sorted(set(reasons)) or ['exact_material'], evaluated_at=stamp(self.clock()),
            recheck_due_at=stamp(utc(validation['payload']['checked_at']) + timedelta(seconds=rule.max_validation_age_seconds)) if validation and rule.max_validation_age_seconds else None,
            validation_refs=[validation['version_id']] if validation else [], generation=self.generation, local_audit_refs=audit_refs,
            critical_reasons=['must_requirement'] if requirement.priority == 'must' else [])
        old = self.head('reuse_assessment', bid)
        if old and old['payload']['input_manifest_hash'] == assessment.input_manifest_hash and old['payload']['generation'] == self.generation:
            return
        self.reuse_barrier()
        latest = self.store.db.execute('SELECT epoch FROM reuse_bindings WHERE binding_id=?', (bid,)).fetchone()[0]
        if latest != binding['epoch']:
            return  # Notification arrived during the audit; its result stays historical.
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            aref = self.store.append_entity(db, 'reuse_assessment', bid, assessment.model_dump())
            local_claim = binding['local_claim_version_id']
            if disposition == 'reused':
                cp = dict(claim['payload'], validity='current', epistemic='supported', requirement_ids=[requirement.id])
                local = self.head('claim', f'reuse-supported:{bid}')
                if local and local['payload'] == cp:
                    cref = EntityRef(kind='claim', **{k: local[k] for k in ('id', 'version', 'version_id')})
                else:
                    cref = self.store.append_entity(db, 'claim', f'reuse-supported:{bid}', cp)
                local_claim = cref.version_id
                db.execute('INSERT OR REPLACE INTO evidence_links(link_id,claim_version_id,evidence_version_id,audit_version_id,relation) VALUES(?,?,?,?,?)',
                           (f'reuse-link:{bid}', local_claim, e['version_id'], audit_refs[0], 'supports'))
                for dep in [e['version_id'], claim['version_id']]:
                    db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (dep, local_claim, 'evidence'))
                for edge in list(db.execute('SELECT * FROM reuse_lineage WHERE local_version_id=?', (claim['version_id'],))):
                    db.execute('INSERT OR IGNORE INTO reuse_lineage VALUES(?,?,?,?,?)', (edge['ancestor_instance_id'], edge['ancestor_version_id'], edge['ancestor_payload_hash'], local_claim, 'derived_from'))
            db.execute('UPDATE reuse_bindings SET disposition=?,assessment_version_id=?,local_claim_version_id=?,local_audit_version_id=? WHERE binding_id=?',
                       (disposition, aref.version_id, local_claim, audit_refs[0] if audit_refs else None, bid))
            db.execute('UPDATE runs SET state_version=state_version+1')

    def reuse_conflict_context(self, evidence, claim):
        result = []
        for row in self.heads('conflict'):
            p = row['payload']
            if p.get('severity', 'blocking') not in {'blocking', 'material'}:
                continue
            refs = p.get('claim_version_ids', []) + p.get('evidence_version_ids', [])
            ids = p.get('claim_ids', []) + p.get('evidence_ids', [])
            if {evidence['version_id'], claim['version_id']}.intersection(refs) or {evidence['id'], claim['id']}.intersection(ids):
                context = []
                for vid in p.get('evidence_version_ids', []):
                    context.append(entity(self.store, vid)['payload'])
                for eid in p.get('evidence_ids', []):
                    context.append(head(self.store, 'evidence', eid)['payload'])
                result.append(dict(conflict=p, evidence=context))
        return result

    def assert_reuse_delivery(self):
        self.reuse_barrier()
        self.expire_reuse()
        from .loop import ResearchReopened
        used = {vid for draft in self.heads('draft') for fact in draft['payload']['facts'] for vid in fact.get('claim_version_ids', [])}
        for binding in self.store.db.execute('SELECT * FROM reuse_bindings WHERE local_claim_version_id IS NOT NULL'):
            if binding['local_claim_version_id'] in used and not self.reuse_binding_valid(binding):
                raise ResearchReopened('reuse source eligibility changed before delivery')
