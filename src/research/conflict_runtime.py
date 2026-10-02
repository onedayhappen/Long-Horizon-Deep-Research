"""Incremental, version-bound cross-source comparison and delivery checks."""
from __future__ import annotations

import json
from itertools import combinations

from .conflicts import ConflictCase, ConflictProposal, ConflictVerification, adjudicate, disclosure_allowed
from .jsonio import canonical, digest
from .models import DisputeFact, EntityRef, FactualFact, TextSpan
from .extract import verify_locator
from .prompts import template


class ConflictRuntimeMixin:
    def conflict_dependency(self, sources, target, reason='conflict'):
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            for source in sources:
                db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (source, target, reason))

    def bind_candidate_evidence(self, claim_ref, evidence_ref):
        # Retain candidates rejected by a local entailment audit as well.
        self.conflict_dependency([evidence_ref['version_id']], claim_ref['version_id'], 'claim_evidence')
        evidence = self.head('evidence', evidence_ref['id'])
        snapshot = self.conflict_snapshot(evidence)
        self.conflict_dependency([snapshot['version_id']], evidence_ref['version_id'], 'snapshot')

    def conflict_snapshot(self, evidence):
        p = evidence['payload']
        rows = self.store.db.execute("SELECT * FROM entity_versions WHERE kind='snapshot' AND id=? ORDER BY version DESC", (p['snapshot_id'],))
        for row in rows:
            snapshot = json.loads(row['payload_json'])
            locator = p['locator']
            if (p.get('snapshot_version_id') == row['version_id'] or
                not p.get('snapshot_version_id') and
                (snapshot.get('text_hash') == locator.get('text_blob_hash') if locator['kind'] == 'text_span'
                 else snapshot.get('raw_hash') == locator.get('raw_hash'))):
                return dict(id=row['id'], version=row['version'], version_id=row['version_id'], payload=snapshot)
        raise RuntimeError('conflict evidence is missing its exact snapshot')

    def conflict_material(self):
        all_claims = {r['version_id']: r for r in self.heads('claim')}
        claims = {r['version_id']: r for r in all_claims.values()
                  if r['payload'].get('validity') == 'current'}
        for case in self.heads('conflict'):
            p = case['payload']
            if not p.get('origin_ref'):
                continue
            for cid in p.get('claim_version_ids', []):
                claim = all_claims.get(cid)
                if claim and claim['payload'].get('validity') == 'candidate':
                    # Historical evidence is only comparison context. This does
                    # not authorize support or inherit source requirement IDs.
                    claims[cid] = dict(claim, payload=dict(claim['payload'],
                        requirement_ids=[r.id for r in self.contract.requirements]))
        evidence = {r['version_id']: r for r in self.heads('evidence')
                    if r['payload'].get('validity') == 'active'}
        pairs = {(r['claim_version_id'], r['evidence_version_id'])
                 for r in self.store.db.execute('SELECT l.*,v.payload_json FROM evidence_links l JOIN entity_versions v ON v.version_id=l.audit_version_id')
                 if 'forward' in json.loads(r['payload_json'])}
        pairs.update((r['to_version_id'], r['from_version_id']) for r in self.store.db.execute(
            "SELECT * FROM dependency_edges WHERE reason='claim_evidence'"))
        # Old runs already persisted candidate provenance in proposal receipts.
        for row in self.store.db.execute("SELECT receipt_json FROM proposals WHERE proposal_id LIKE 'candidate:%'"):
            refs = {r['kind']: r['version_id'] for r in json.loads(row[0])['refs']}
            if 'claim' in refs and 'evidence' in refs:
                pairs.add((refs['claim'], refs['evidence']))
        # Historical imports are candidates, including the unselected other side.
        pairs.update((r['claim_version_id'], r['evidence_version_id']) for r in self.store.db.execute(
            'SELECT * FROM reuse_candidates'))
        pairs.update((r['local_claim_version_id'], r['evidence_version_id']) for r in self.store.db.execute(
            'SELECT * FROM reuse_bindings WHERE local_claim_version_id IS NOT NULL'))
        result = []
        for cid in sorted(claims):
            rows = [evidence[eid] for c, eid in sorted(pairs) if c == cid and eid in evidence]
            if not rows:
                continue
            snapshots = []
            for e in rows:
                snapshot = self.conflict_snapshot(e)
                if snapshot and snapshot not in snapshots:
                    snapshots.append(snapshot)
            result.append(dict(claim=claims[cid], evidence=rows, snapshots=snapshots))
        return result

    def conflict_pair_tasks(self):
        # All retained claims, including cross-section pairs. No semantic top-k
        # silently drops potential counterevidence; processing is incremental.
        for left, right in combinations(self.conflict_material(), 2):
            if not set(left['claim']['payload']['requirement_ids'] + right['claim']['payload']['requirement_ids']) & {r.id for r in self.contract.requirements}:
                continue
            material = [left, right]
            token = digest(dict(material=material, contract=self.contract.model_dump(mode='json'),
                                template=template('auditor.conflict'), policy=1,
                                model=self.api_model_name,
                                model_profile=getattr(self.roles, 'profile_hash', None)))
            yield token, material

    def conflict_scan_state(self):
        tasks = list(self.conflict_pair_tasks())
        completed = {r['id']: r for r in self.heads('conflict_comparison')}
        pending = [key for key, _ in tasks if key not in completed]
        return dict(manifest_hash=digest([key for key, _ in tasks]),
                    total=len(tasks), pending_ids=pending,
                    comparison_refs=[completed[key]['version_id'] for key, _ in tasks if key in completed])

    def current_conflicts(self, requirement_id=None):
        current_tasks = {key for key, _ in self.conflict_pair_tasks()}
        result = []
        current_claims = {r['version_id']: r for r in self.heads('claim')}
        current_evidence = {r['version_id'] for r in self.heads('evidence')}
        local_cases = [r for r in self.heads('conflict') if not r['payload'].get('origin_ref')
                       and r['payload'].get('comparison_id') in current_tasks
                       and self.head('conflict_comparison', r['payload']['comparison_id'])]
        for row in self.heads('conflict'):
            p = row['payload']
            if p.get('comparison_id') and not p.get('origin_ref'):
                # A changed input never inherits an old resolution. Pending scan
                # blocks readiness; the old case remains in immutable history.
                if p['comparison_id'] not in current_tasks:
                    continue
                if p.get('status') == 'dismissed':
                    continue
            elif not (set(p.get('claim_version_ids', [])) & current_claims.keys()
                      or set(p.get('evidence_version_ids', [])) & current_evidence):
                continue
            if p.get('origin_ref') or p.get('reason') == 'visual_text_conflict':
                # A new local, two-sided comparison supersedes the imported
                # disposition, including when it confirms an unresolved conflict.
                if any(set(p.get('evidence_version_ids', [])) <= set(local['payload']['evidence_version_ids'])
                       for local in local_cases) and p.get('evidence_version_ids'):
                    continue
                p = dict(p, status='open', disposition=None)
            rids = set(p.get('requirement_ids', []))
            for cid in p.get('claim_version_ids', []):
                rids.update(current_claims.get(cid, {}).get('payload', {}).get('requirement_ids', []))
            # Missing legacy requirement scope is not a reason to hide a conflict.
            if not rids:
                rids = {r.id for r in self.contract.requirements}
            if requirement_id is None or requirement_id in rids:
                result.append(dict(row, payload=dict(p, requirement_ids=sorted(rids))))
        return result

    def conflict_claim_blocked(self, claim_version_id):
        return any(claim_version_id in r['payload'].get('claim_version_ids', [])
                   and r['payload'].get('status') != 'resolved' for r in self.current_conflicts())

    def reuse_conflict_cleared(self, evidence_version_id, claim_version_id):
        if self.conflict_scan_state()['pending_ids']:
            return False
        if any(evidence_version_id in r['payload'].get('evidence_version_ids', [])
               and r['payload'].get('status') != 'resolved' for r in self.current_conflicts()):
            return False
        tasks = {key for key, _ in self.conflict_pair_tasks()}
        matches = [r['payload'] for r in self.heads('conflict')
                   if not r['payload'].get('origin_ref') and r['payload'].get('comparison_id') in tasks
                   and evidence_version_id in r['payload'].get('evidence_version_ids', [])
                   and self.head('conflict_comparison', r['payload']['comparison_id'])]
        return bool(matches) and all(p['status'] in {'dismissed', 'resolved'} for p in matches)

    def conflict_requirement_ready(self, requirement, *, unknown=False):
        if self.conflict_scan_state()['pending_ids']:
            return False
        cases = [r['payload'] for r in self.current_conflicts(requirement.id)]
        return disclosure_allowed(requirement, cases, unknown=unknown)

    def conflict_digest(self):
        state = self.conflict_scan_state()
        return dict(scan=state, cases=self.current_conflicts())

    def conflict_freeze(self):
        state = self.conflict_digest()
        return dict(manifest_hash=digest(state), scan=state['scan'],
                    case_refs=[r['version_id'] for r in state['cases']])

    def require_conflict_ready(self, assessments):
        by_id = {a.requirement_id: a for a in assessments}
        for requirement in self.contract.requirements:
            assessment = by_id.get(requirement.id)
            if assessment and assessment.disposition in {'satisfied', 'bounded_unknown'}:
                if not self.conflict_requirement_ready(requirement, unknown=assessment.disposition == 'bounded_unknown'):
                    raise RuntimeError('conflict checks are incomplete or unresolved: ' + requirement.id)

    def validate_conflict_basis(self, proposal, material):
        claims = {item['claim']['version_id']: item for item in material}
        evidence = {e['version_id']: e for item in material for e in item['evidence']}
        if len({f.claim_version_id for f in proposal.frames}) != 2 or {f.claim_version_id for f in proposal.frames} != set(claims):
            raise RuntimeError('conflict frames must identify both exact claims')

        def check_basis(basis, allowed):
            if basis.evidence_version_id not in allowed:
                raise RuntimeError('conflict cites evidence outside supplied material')
            p = evidence[basis.evidence_version_id]['payload']
            text = p.get('excerpt') or p.get('observation', {}).get('description', '')
            if basis.quote not in text:
                raise RuntimeError('conflict basis is not an exact evidence quote')
            if p['locator']['kind'] == 'text_span':
                source = self.store.read_blob(p['locator']['text_blob_hash']).decode('utf-8')
                if not verify_locator(source, TextSpan.model_validate(p['locator']), p['excerpt']):
                    raise RuntimeError('conflict evidence locator is invalid')
            else:
                from .visual_runtime import verify_visual_evidence
                verify_visual_evidence(self.store, p)

        for frame in proposal.frames:
            for basis in frame.basis:
                check_basis(basis, {e['version_id'] for e in claims[frame.claim_version_id]['evidence']})
        seen = set()
        for relation in proposal.relations:
            pair = (relation.claim_version_id, relation.evidence_version_id)
            if relation.claim_version_id not in claims or relation.evidence_version_id != relation.basis.evidence_version_id or pair in seen:
                raise RuntimeError('invalid or duplicate cross-evidence relation')
            seen.add(pair)
            check_basis(relation.basis, evidence)

    async def review_conflicts(self):
        for key, material in self.conflict_pair_tasks():
            if self.head('conflict_comparison', key):
                self.repair_conflict_dependencies(key, material)
                continue
            packet = {'material_json': canonical(material).decode(), 'phase': 'compare'}
            images, visual_context = [], []
            for item in material:
                for evidence in item['evidence']:
                    if evidence['payload']['locator']['kind'] == 'pdf_region':
                        from .visual_runtime import verify_visual_evidence
                        artifact, context = verify_visual_evidence(self.store, evidence['payload'])
                        images.extend(image.blob_hash for image in artifact.images)
                        visual_context.append(dict(evidence_version_id=evidence['version_id'], context=context))
            images = sorted(set(images))
            if visual_context:
                packet['visual_context_json'] = canonical(visual_context).decode()
            if len(canonical(packet)) > self.planning_config.context_characters * 3:
                from .loop import ResearchStopped
                raise ResearchStopped('incomplete_context', 'conflict material exceeds context limit')
            proposal = await self.role('auditor.conflict', 'conflict-compare:' + key, packet, ConflictProposal, image_hashes=images)
            self.validate_conflict_basis(proposal, material)
            claim_ids = [item['claim']['version_id'] for item in material]
            evidence_ids = sorted({e['version_id'] for item in material for e in item['evidence']})
            # A fresh call checks both sides even for a proposed dismissal.
            review = await self.role('auditor.conflict', 'conflict-review:' + key,
                dict(packet, phase='verify', proposal_json=proposal.model_dump_json()), ConflictVerification, image_hashes=images)
            if len(set(review.checked_claim_version_ids)) != 2 or set(review.checked_claim_version_ids) != set(claim_ids):
                raise RuntimeError('conflict verification omits a side')
            status, disposition = adjudicate(proposal, review)
            reviewed = dict(proposal=proposal.model_dump(mode='json'), review=review.model_dump(mode='json'))
            audit = self.commit('conflict-audit:' + key, reviewed, [('conflict_review', key, reviewed)])['refs'][0]
            self.conflict_dependency(claim_ids + evidence_ids, audit['version_id'])
            requirements = sorted(set(r for item in material for r in item['claim']['payload']['requirement_ids']) & {r.id for r in self.contract.requirements})
            case = ConflictCase(claim_version_ids=claim_ids, evidence_version_ids=evidence_ids,
                requirement_ids=requirements, conflict_type=proposal.conflict_type, status=status,
                disposition=disposition, input_manifest_hash=key, contract_hash=digest(self.contract.model_dump(mode='json')),
                comparison_id=key, review_version_id=audit['version_id'], reason=proposal.reason + '; ' + review.reason,
                next_investigation=proposal.next_investigation)
            payload = case.model_dump(mode='json')
            if review.verdict == 'accept':
                # Store explicit refutations without treating failed support as
                # refutation; these audits are not ordinary support approvals.
                for relation in proposal.relations:
                    if relation.relation == 'uncertain' or not relation.scope_match:
                        continue
                    c = next(i['claim'] for i in material if i['claim']['version_id'] == relation.claim_version_id)
                    e = next(e for i in material for e in i['evidence'] if e['version_id'] == relation.evidence_version_id)
                    relation_id = digest([key, relation.model_dump(mode='json')])
                    relation_ref = self.commit('conflict-relation:' + relation_id, relation.model_dump(mode='json'),
                        [('audit', 'conflict-relation:' + relation_id, {'relation': relation.model_dump(mode='json'), 'review_version_id': audit['version_id']})])['refs'][0]
                    self.store.add_link('conflict-link:' + relation_id,
                        EntityRef(kind='claim', **{k: c[k] for k in ('id', 'version', 'version_id')}),
                        EntityRef(kind='evidence', **{k: e[k] for k in ('id', 'version', 'version_id')}),
                        EntityRef.model_validate(relation_ref), relation.relation, self.owner, self.generation)
            # Commit the completion marker only after all relation links exist.
            receipt = self.commit('conflict-complete:' + key, payload,
                [('conflict', key, payload), ('conflict_comparison', key, dict(input_manifest_hash=key, status=status))])
            for ref in receipt['refs']:
                self.conflict_dependency([audit['version_id']], ref['version_id'])
        self.refresh_conflict_state()

    def repair_conflict_dependencies(self, key, material):
        audit = self.head('conflict_review', key)
        case = self.head('conflict', key)
        comparison = self.head('conflict_comparison', key)
        if not audit or not case or not comparison:
            raise RuntimeError('incomplete persisted conflict decision')
        inputs = [i['claim']['version_id'] for i in material]
        inputs += [e['version_id'] for i in material for e in i['evidence']]
        self.conflict_dependency(inputs, audit['version_id'])
        self.conflict_dependency([audit['version_id']], case['version_id'])
        self.conflict_dependency([audit['version_id']], comparison['version_id'])

    def refresh_conflict_state(self):
        state = self.conflict_freeze()
        old = self.head('conflict_scan', 'current')
        if not state['scan']['total'] and not state['case_refs'] and old is None:
            return
        if old and old['payload'] == state:
            return
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            # A new candidate, changed source or changed disposition invalidates
            # all cached authorization decisions. Historical delivery stays saved.
            db.execute("UPDATE entity_heads SET validity='needs_reaudit' WHERE kind IN ('coverage','freeze','plan','outline_review') OR (kind='audit' AND id='report')")
            self.store.append_entity(db, 'conflict_scan', 'current', state)
            db.execute("UPDATE runs SET state_version=state_version+1,lifecycle_status='active',research_outcome=NULL,current_stop_version_id=NULL")

    def dispute_material(self, requirement_ids):
        cases = {r['version_id']: r for rid in requirement_ids for r in self.current_conflicts(rid)
                 if r['payload'].get('status') != 'resolved'}
        ids = {vid for r in cases.values() for vid in r['payload'].get('evidence_version_ids', [])}
        return dict(conflicts=list(cases.values()),
                    dispute_evidence=[r for r in self.heads('evidence') if r['version_id'] in ids])

    def validate_dispute_facts(self, sections, assessments):
        cases = {r['version_id']: r for r in self.current_conflicts()}
        evidence = {r['version_id']: r for r in self.heads('evidence')}
        by_requirement = {a.requirement_id: a for a in assessments}
        mentioned = set()
        citation_evidence = set()
        for _, section in sections:
            cited_facts = {fid for p in section.paragraphs for s in p.sentences for fid in s.fact_ids}
            for fact in section.facts:
                if isinstance(fact, FactualFact) and any(self.conflict_claim_blocked(cid) for cid in fact.claim_version_ids):
                    raise RuntimeError('contested claim cannot be rendered as an established fact')
                if not isinstance(fact, DisputeFact):
                    continue
                row = cases.get(fact.conflict_version_id)
                if not row or fact.id not in cited_facts:
                    raise RuntimeError('dispute must cite a current conflict in report prose')
                case = row['payload']
                if not set(fact.requirement_ids) <= set(case['requirement_ids']):
                    raise RuntimeError('dispute cites unrelated requirements')
                required = {evidence[vid]['id'] for vid in case['evidence_version_ids'] if vid in evidence}
                if len(required) < 2 or len(required) != len(set(case['evidence_version_ids'])) or set(fact.evidence_ids) != required:
                    raise RuntimeError('dispute must cite all current evidence on both sides')
                for vid in case['evidence_version_ids']:
                    p = evidence[vid]['payload']
                    if p['locator']['kind'] == 'pdf_region':
                        from .visual_runtime import verify_visual_evidence
                        verify_visual_evidence(self.store, p)
                    else:
                        text = self.store.read_blob(p['locator']['text_blob_hash']).decode('utf-8')
                        if not verify_locator(text, TextSpan.model_validate(p['locator']), p['excerpt']):
                            raise RuntimeError('dispute evidence locator is invalid')
                for rid in fact.requirement_ids:
                    req = next(r for r in self.contract.requirements if r.id == rid)
                    assessment = by_requirement.get(rid)
                    if not assessment or not self.conflict_requirement_ready(req, unknown=assessment.disposition == 'bounded_unknown'):
                        raise RuntimeError('contract does not permit this dispute disclosure')
                    mentioned.add((fact.conflict_version_id, rid))
                citation_evidence.update(required)
        for vid, row in cases.items():
            if row['payload'].get('status') == 'resolved':
                continue
            for rid in row['payload']['requirement_ids']:
                assessment = by_requirement.get(rid)
                if assessment and assessment.disposition in {'satisfied', 'bounded_unknown'} and (vid, rid) not in mentioned:
                    raise RuntimeError('report omits a material conflict: ' + row['id'])
        return citation_evidence
