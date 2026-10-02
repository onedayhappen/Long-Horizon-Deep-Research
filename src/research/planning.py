"""Persistent chapter investigations, bounded planning memory and final review."""
from __future__ import annotations

from .jsonio import canonical, digest
from .models import GapVerdict, OutlineReview, SourceSelection
from .outline import InvalidOutline, ordered_nodes, section_context


class PlanningMixin:
    def commit_outline(self, key, outline, extra=()):
        payload = outline.model_dump(mode='json')
        entities = [('outline', 'outline', payload)]
        entities += [('outline_gap', g.id, g.model_dump(mode='json')) for g in outline.gaps]
        return self.commit(key, payload, entities + list(extra))

    def search_scope(self, action, outline):
        active = {n.id: n for n in outline.nodes if n.active}
        gaps = {g.id: g for g in outline.gaps}
        if not set(action.target_gap_ids) <= gaps.keys():
            raise InvalidOutline('search targets an unknown gap')
        targets = set(action.target_node_ids)
        for gid in action.target_gap_ids:
            if gaps[gid].status not in {'open', 'investigating'}:
                raise InvalidOutline('search targets a closed gap; reopen it first')
            targets.update(gaps[gid].node_ids)
        if not targets:
            targets = {n.id for n in active.values() if action.question_id in n.requirement_ids}
        if not targets or not targets <= active.keys():
            raise InvalidOutline('search needs active target nodes')
        requirements = {rid for nid in targets for rid in active[nid].requirement_ids}
        if any(q.question_id not in requirements for q in action.query_plan):
            raise InvalidOutline('query is unrelated to target chapter requirements')
        if any(not set(gaps[gid].requirement_ids) <= requirements for gid in action.target_gap_ids):
            raise InvalidOutline('gap investigation loses requirement scope')
        return dict(target_node_ids=sorted(targets), target_gap_ids=action.target_gap_ids,
                    requirement_ids=sorted(requirements),
                    research_goal=action.research_goal.strip() or '; '.join(q.text for q in action.query_plan),
                    questions=[dict(node_id=nid, question=active[nid].research_question or active[nid].title,
                                    purpose=active[nid].purpose) for nid in sorted(targets)],
                    gaps=[gaps[gid].model_dump(mode='json') for gid in action.target_gap_ids])

    def finish_investigation(self, step, action, outline, scope):
        query_ids = [q.query_id for q in action.query_plan]
        row = dict(scope, query_ids=query_ids, outline_version=outline.outline_version)
        self.commit(f'investigation:{step}', row, [('investigation', str(step), row)])
        if not action.target_gap_ids or self.head('gap-search', str(step)):
            return
        changed = outline.model_copy(deep=True)
        for gap in changed.gaps:
            if gap.id in action.target_gap_ids:
                gap.status = 'investigating'
                gap.investigation_refs = sorted(set(gap.investigation_refs + query_ids))
        changed.outline_version += 1
        changed.operations = [dict(kind='investigate', query_ids=query_ids, gap_ids=action.target_gap_ids)]
        changed.reason_refs = query_ids
        self.commit_outline(f'gap-search:{step}', changed, [('gap-search', str(step), row)])

    def resolution_material(self, gap, refs, status):
        if not refs:
            raise InvalidOutline('gap closure needs resolution references')
        pairs = [r for rid in gap.requirement_ids for r in self.evidence_rows()[rid]]
        claims = {r['claim_ref']['version_id'] for r in pairs}
        qualified = [r for r in self.heads('claim') if r['version_id'] in claims and r['version_id'] in refs]
        investigations = {r['id'] for r in self.heads('query_result')
                          if r['payload']['status'] in {'hit', 'empty'} and r['id'] in gap.investigation_refs}
        if not set(refs) <= claims | investigations:
            raise InvalidOutline('gap closure cites unsupported or unrelated references')
        if status == 'resolved' and not qualified:
            raise InvalidOutline('resolved gap needs current audited support')
        if status == 'accepted_unknown':
            requirements = {r.id: r for r in self.contract.requirements}
            coverage = {r['id']: r['payload'] for r in self.heads('coverage')}
            if not set(refs) & investigations or any(
                not requirements[rid].allow_unknown or coverage.get(rid, {}).get('disposition') != 'bounded_unknown'
                for rid in gap.requirement_ids):
                raise InvalidOutline('unknown gap needs targeted investigation and accepted unknown coverage')
        if status == 'deferred' and gap.priority == 'blocking':
            raise InvalidOutline('blocking gap cannot be deferred')
        return dict(claims=qualified, pairs=[r for r in pairs if r['claim_ref']['version_id'] in refs],
                    investigations=[r for r in self.heads('query_result') if r['id'] in set(refs) & investigations])

    async def review_gap_resolutions(self, outline, patch):
        approvals = {}
        closing = [op for op in patch.operations if op.kind == 'resolve_gap']
        if closing and any(op.kind != 'resolve_gap' for op in patch.operations):
            raise InvalidOutline('resolve gaps separately from structural edits')
        if len({op.gap_id for op in closing}) != len(closing):
            raise InvalidOutline('cannot resolve a gap twice in one patch')
        for op in closing:
            gap = next((g for g in outline.gaps if g.id == op.gap_id and op.node_id in g.node_ids), None)
            if not gap or gap.status not in {'open', 'investigating'}:
                raise InvalidOutline('resolution targets missing or closed gap')
            material = self.resolution_material(gap, op.resolution_refs, op.resolution_status)
            packet = dict(gap_json=gap.model_dump_json(), proposed_status=op.resolution_status,
                          reason=op.reason, resolution_refs=op.resolution_refs,
                          material_json=canonical(material).decode(),
                          nodes_json=canonical([n.model_dump(mode='json') for n in outline.nodes if n.id in gap.node_ids]).decode())
            token = digest(packet)
            verdict = await self.role('auditor.gap', f'gap-review:{token}', packet, GapVerdict)
            if verdict.gap_id != gap.id or set(verdict.checked_refs) != set(op.resolution_refs):
                raise InvalidOutline('gap audit does not check the proposed resolution')
            if verdict.verdict != 'pass':
                raise InvalidOutline('gap audit rejected closure: ' + verdict.reason)
            receipt = self.commit(f'gap-review:{token}', verdict.model_dump(mode='json'),
                [('gap_review', token, dict(verdict=verdict.model_dump(mode='json'), inputs=packet))])
            approvals[digest(op.model_dump(mode='json'))] = receipt['refs'][0]['version_id']
        return approvals

    def blocking_gaps(self, outline):
        blocked = []
        for gap in outline.gaps:
            if gap.priority == 'blocking' and gap.status not in {'resolved', 'accepted_unknown'}:
                blocked.append(gap.id)
            elif gap.status in {'resolved', 'accepted_unknown'}:
                try:
                    self.resolution_material(gap, gap.resolution_refs, gap.status)
                except InvalidOutline:
                    blocked.append(gap.id)
        return blocked

    def planning_memory(self, outline):
        """Include complete items or explicit omissions; never slice away qualifiers."""
        state = self.head('loop', 'controller')
        inspected = state['payload'].get('inspect_summary_ids', []) if state else []
        rows = self.heads('research_summary')
        rows.sort(key=lambda r: (r['id'] in inspected, r['payload'].get('planner_step', -1)), reverse=True)
        result = dict(summaries=[], omitted_summary_ids=[],
                      recent_progress=[r['payload'] for r in self.heads('research_progress')][-3:])
        # Leave room for the contract, outline, evidence map and control metadata.
        limit = self.planning_config.context_characters // 3
        for row in rows:
            item = dict(id=row['id'], **row['payload'])
            candidate = dict(result, summaries=result['summaries'] + [item])
            if len(canonical(candidate).decode()) <= limit:
                result['summaries'].append(item)
            else:
                result['omitted_summary_ids'].append(row['id'])
        return result

    def bound_planner_packet(self, packet):
        limit = self.planning_config.context_characters
        # Required questions/gaps and evidence IDs cannot be silently dropped.
        if len(canonical(dict(packet, contract_json=self.contract.model_dump_json())).decode()) > limit:
            from .loop import ResearchStopped
            raise ResearchStopped('incomplete_context', 'planning_context_limit: reduce scope or increase planning.context_characters')
        return packet

    async def select_sources(self, hits, scope, key):
        unique = {}
        for hit in hits:
            unique.setdefault(hit.url, hit)
        hits = list(unique.values())
        if len(hits) <= self.planning_config.max_sources_per_action:
            return hits
        packet = dict(research_scope_json=canonical(scope).decode(),
                      candidates_json=canonical([h.model_dump(mode='json') for h in hits]).decode(),
                      max_sources=self.planning_config.max_sources_per_action)
        choice = await self.role('researcher.select_sources', f'select:{key}', packet, SourceSelection)
        ids = [h.hit_id for h in hits]
        if (len(choice.selected_hit_ids) > self.planning_config.max_sources_per_action or
            len(set(choice.selected_hit_ids)) != len(choice.selected_hit_ids) or
            not set(choice.selected_hit_ids) <= set(ids) or set(choice.reasons) != set(ids) or
            any(not reason.strip() for reason in choice.reasons.values())):
            raise InvalidOutline('source selection must account for all candidates within quota')
        self.commit(f'selection:{key}', choice.model_dump(mode='json'), [('source_selection', key, choice.model_dump(mode='json'))])
        return [h for h in hits if h.hit_id in choice.selected_hit_ids]

    async def final_outline_review(self, outline):
        if self.blocking_gaps(outline):
            raise InvalidOutline('unresolved chapter gaps: ' + ', '.join(self.blocking_gaps(outline)))
        nodes = ordered_nodes(outline)
        writing_remaining = self.budget.snapshot()['remaining']['writing']
        required_writes = sum(not self.head('draft', n.id) or
            self.head('draft', n.id)['payload']['outline_version'] != outline.outline_version or
            self.store.db.execute('SELECT COALESCE(MAX(revision),0) FROM revision_jobs WHERE section_id=? AND contract_version=?',
                (n.id, self.contract.version)).fetchone()[0] > self.head('draft', n.id)['payload']['revision']
            for n in nodes)
        if writing_remaining < required_writes:
            from .loop import ResearchStopped
            raise ResearchStopped('incomplete_budget', 'insufficient_writing_reserve')
        material = {n.id: self.section_material(n) for n in nodes}
        packet = dict(outline_json=outline.model_dump_json(),
                      node_material_json=canonical(material).decode(),
                      research_summaries_json=canonical(self.heads('research_summary')).decode())
        self.bound_planner_packet(packet)
        token = digest(packet)
        review = await self.role('auditor.outline', f'outline-review:{token}', packet, OutlineReview)
        if review.outline_version != outline.outline_version or len(review.nodes) != len(nodes) or {r.node_id for r in review.nodes} != {n.id for n in nodes}:
            raise InvalidOutline('outline review must cover every active node at the current version')
        self.commit(f'outline-review:{token}', review.model_dump(mode='json'), [('outline_review', token, review.model_dump(mode='json'))])
        if review.decision != 'ready' or review.missing_dimensions or review.unincorporated_summary_ids:
            raise InvalidOutline('outline review requests further research/refinement: ' + review.reason + '; ' + '; '.join(review.missing_dimensions))
        for item in review.nodes:
            node = next(n for n in nodes if n.id == item.node_id)
            allowed = {c['version_id'] for c in material[node.id]['claims']}
            if not set(item.claim_version_ids) <= allowed:
                raise InvalidOutline('outline review cites unsupported chapter material')
            if item.disposition == 'missing':
                raise InvalidOutline('chapter not ready: ' + node.id + ': ' + item.reason)
            if item.disposition == 'overview':
                if not any(n.parent_id == node.id for n in nodes):
                    raise InvalidOutline('a leaf cannot bypass evidence review as an overview')
            elif item.disposition == 'supported' and not item.claim_version_ids:
                raise InvalidOutline('supported chapter needs audited claims')
            elif item.disposition == 'accepted_unknown':
                coverage = {c['id']: c['payload']['disposition'] for c in material[node.id]['coverage']}
                if any(coverage.get(rid) != 'bounded_unknown' for rid in node.requirement_ids):
                    raise InvalidOutline('chapter unknown is not accepted by coverage')
        return token

    def record_progress(self, step, before, after, action, new_evidence=0):
        old = {g.id: g.status for g in before.gaps}
        resolved = [g.id for g in after.gaps if g.status in {'resolved', 'accepted_unknown'} and old.get(g.id) != g.status]
        added = [n.id for n in after.nodes if n.active and n.id not in {x.id for x in before.nodes}]
        value = dict(step=step, action=action.kind, new_evidence=new_evidence,
                     resolved_gap_ids=resolved, new_node_ids=added,
                     substantive=bool(new_evidence or resolved or added),
                     outline_version=after.outline_version)
        self.commit(f'progress:{step}', value, [('research_progress', f'{step:08d}', value)])
        return value
