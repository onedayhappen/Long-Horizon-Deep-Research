"""Deterministic, transactional-input validation for evidence-driven outlines."""
from .models import InitializationProposal, OutlineNode, OutlineState, OutlinePatchAction, ResearchContract, ResearchGap
from .jsonio import digest

MAX_OUTLINE_DEPTH = 4


class InvalidOutline(ValueError):
    pass


def validate_outline(outline: OutlineState, contract: ResearchContract, max_depth=MAX_OUTLINE_DEPTH) -> None:
    ids = [n.id for n in outline.nodes]
    if len(ids) != len(set(ids)):
        raise InvalidOutline('duplicate outline node ID')
    active = {n.id: n for n in outline.nodes if n.active}
    requirements = {r.id for r in contract.requirements}
    if not active or any(not set(n.requirement_ids) <= requirements for n in outline.nodes):
        raise InvalidOutline('empty outline or unknown requirement')
    must = {r.id for r in contract.requirements if r.priority == 'must'}
    if not must <= {r for n in active.values() for r in n.requirement_ids}:
        raise InvalidOutline('patch loses a must requirement')
    if not set(contract.output_contract.required_sections) <= {n.title for n in active.values()}:
        raise InvalidOutline('patch loses a required output section')
    for node in active.values():
        seen = {node.id}
        parent = node.parent_id
        while parent is not None:
            if parent in seen or parent not in active:
                raise InvalidOutline('outline cycle or inactive/missing parent')
            seen.add(parent)
            parent = active[parent].parent_id
        if len(seen) > max_depth:
            raise InvalidOutline(f'outline exceeds maximum depth {max_depth}')
    gaps = {g.id: g for g in outline.gaps}
    if len(gaps) != len(outline.gaps):
        raise InvalidOutline('duplicate gap ID')
    for gap in gaps.values():
        if not set(gap.node_ids) <= set(active):
            raise InvalidOutline('gap needs active owning nodes; transfer it before retirement')
        if not set(gap.requirement_ids) <= {rid for nid in gap.node_ids for rid in active[nid].requirement_ids}:
            raise InvalidOutline('gap loses its requirement mapping')
        if gap.priority == 'blocking' and gap.status == 'deferred':
            raise InvalidOutline('blocking gap cannot be deferred')
        if gap.status in {'resolved', 'accepted_unknown', 'deferred'} and not gap.resolution_audit_id:
            raise InvalidOutline('gap closure requires an audit')
    for node in active.values():
        if set(node.gap_ids) != {g.id for g in gaps.values() if node.id in g.node_ids}:
            raise InvalidOutline('node/gap ownership mismatch')


def section_context(outline: OutlineState, node: OutlineNode, max_depth=MAX_OUTLINE_DEPTH) -> dict:
    """Structural context, never an additional source of factual material."""
    active = {n.id: n for n in outline.nodes if n.active}
    ancestors = []
    parent = node.parent_id
    while parent is not None:
        ancestor = active[parent]
        ancestors.append(ancestor)
        parent = ancestor.parent_id
    ancestors.reverse()
    ordered = sorted(active.values(), key=lambda n: n.order)
    children = [n for n in ordered if n.parent_id == node.id]
    def summary(n):
        return dict(id=n.id, title=n.title, requirement_ids=n.requirement_ids,
                    claim_ids=n.claim_ids, evidence_ids=n.evidence_ids)
    return dict(level=len(ancestors)+1, max_depth=max_depth,
                path=[n.title for n in ancestors] + [node.title],
                ancestors=[summary(n) for n in ancestors],
                children=[summary(n) for n in children],
                siblings=[summary(n) for n in ordered
                          if n.parent_id == node.parent_id and n.id != node.id],
                writing_role='overview' if children else 'detail')


def initialize_outline(proposal: InitializationProposal, contract: ResearchContract, max_depth=MAX_OUTLINE_DEPTH) -> OutlineState:
    nodes = proposal.outline_nodes or [OutlineNode(id=f'section-{i+1}', title=title,
        requirement_ids=[r.id for r in contract.requirements]) for i, title in enumerate(proposal.outline_sections)]
    state = OutlineState(outline_version=1, question_ids=proposal.question_ids, nodes=nodes)
    for node in state.nodes:
        node.research_question = node.research_question or node.title
        node.purpose = node.purpose or 'Answer the mapped requirements within this section.'
    validate_outline(state, contract, max_depth)
    return state


def ordered_nodes(outline: OutlineState) -> list[OutlineNode]:
    result = []
    def visit(parent):
        for n in sorted(outline.nodes, key=lambda n: n.order):
            if n.active and n.parent_id == parent:
                result.append(n)
                visit(n.id)
    visit(None)
    return result


def apply_patch(outline: OutlineState, patch: OutlinePatchAction, contract: ResearchContract,
                valid_reasons: set[str], claim_ids: set[str], evidence_ids: set[str], *,
                max_depth=MAX_OUTLINE_DEPTH, approved_resolutions=None) -> OutlineState:
    if patch.base_outline_version != outline.outline_version:
        raise InvalidOutline('stale base_outline_version')
    if not patch.reason_refs or not set(patch.reason_refs) <= valid_reasons:
        raise InvalidOutline('patch needs current evidence or gap references')
    result = outline.model_copy(deep=True)
    by_id = {n.id: n for n in result.nodes}
    approved_resolutions = approved_resolutions or {}
    def transfer(old_id, new_ids):
        for gap in result.gaps:
            if old_id in gap.node_ids:
                gap.node_ids = sorted((set(gap.node_ids) - {old_id}) | set(new_ids))
    for op in patch.operations:
        if not set(op.requirement_ids) <= {r.id for r in contract.requirements}:
            raise InvalidOutline('unknown operation requirement')
        if not set(op.claim_ids) <= claim_ids or not set(op.evidence_ids) <= evidence_ids:
            raise InvalidOutline('unknown claim/evidence binding')
        if op.kind in ('add', 'add_counterview'):
            if op.node_id in by_id or not op.title:
                raise InvalidOutline('add needs a new ID and title')
            n = OutlineNode(id=op.node_id, title=op.title, parent_id=op.target_parent_id,
                requirement_ids=op.requirement_ids, claim_ids=op.claim_ids, evidence_ids=op.evidence_ids,
                research_question=op.research_question or op.title, purpose=op.purpose or op.reason,
                order=max((x.order for x in result.nodes if x.parent_id==op.target_parent_id), default=-1)+1,
                notes=[op.reason])
            result.nodes.append(n); by_id[n.id] = n
            continue
        n = by_id.get(op.node_id)
        if n is None or not n.active:
            raise InvalidOutline('operation targets missing/inactive node')
        if op.kind == 'split':
            if len(op.nodes) < 2 or not set(n.requirement_ids) <= {r for child in op.nodes for r in child.requirement_ids}:
                raise InvalidOutline('split needs two children retaining requirement mappings')
            if any(child.id in by_id or not child.active for child in op.nodes):
                raise InvalidOutline('split requires fresh active child IDs')
            # The parent remains as a heading, preserving required-section anchors.
            for child in op.nodes:
                child = child.model_copy(deep=True)
                child.parent_id = n.id
                child.research_question = child.research_question or child.title
                child.purpose = child.purpose or op.reason
                result.nodes.append(child); by_id[child.id] = child
            n.replaced_by = [child.id for child in op.nodes]
            transfer(n.id, n.replaced_by)
        elif op.kind == 'merge':
            if not op.target_node_ids or n.id in op.target_node_ids:
                raise InvalidOutline('merge needs other source nodes')
            for source_id in op.target_node_ids:
                source = by_id.get(source_id)
                if source is None or not source.active:
                    raise InvalidOutline('merge source missing/inactive')
                n.requirement_ids = sorted(set(n.requirement_ids + source.requirement_ids))
                n.claim_ids = sorted(set(n.claim_ids + source.claim_ids))
                n.evidence_ids = sorted(set(n.evidence_ids + source.evidence_ids))
                for child in result.nodes:
                    if child.parent_id == source.id:
                        child.parent_id = n.id
                source.active = False; source.replaced_by = [n.id]
                transfer(source.id, [n.id])
        elif op.kind == 'move':
            if n.parent_id != op.target_parent_id:
                n.order = max((x.order for x in result.nodes if x.active and x.parent_id == op.target_parent_id), default=-1) + 1
            n.parent_id = op.target_parent_id
        elif op.kind == 'narrow_claim':
            if not op.title or op.title == n.title:
                raise InvalidOutline('narrow_claim needs a narrower title')
            n.title = op.title
            n.claim_ids = op.claim_ids
            n.evidence_ids = op.evidence_ids
        elif op.kind == 'bind_evidence':
            if not op.claim_ids and not op.evidence_ids:
                raise InvalidOutline('bind_evidence needs claim or evidence bindings')
            n.claim_ids = op.claim_ids
            n.evidence_ids = op.evidence_ids
        elif op.kind == 'update_node':
            scope_changed = ((op.research_question is not None and op.research_question != n.research_question) or
                             (op.purpose is not None and op.purpose != n.purpose))
            if op.title:
                n.title = op.title
            if op.research_question is not None:
                if not op.research_question.strip():
                    raise InvalidOutline('research question cannot be empty')
                n.research_question = op.research_question
            if op.purpose is not None:
                n.purpose = op.purpose
            # A changed question or purpose invalidates the earlier scope review.
            if scope_changed:
                for gap in result.gaps:
                    if n.id in gap.node_ids and gap.status in {'resolved', 'accepted_unknown', 'deferred'}:
                        gap.status = 'open'; gap.resolution_refs = []; gap.resolution_audit_id = None
        elif op.kind == 'reorder':
            if bool(op.before_id) == bool(op.after_id):
                raise InvalidOutline('reorder needs exactly one before_id or after_id')
            anchor = by_id.get(op.before_id or op.after_id)
            if not anchor or not anchor.active or anchor.id == n.id or anchor.parent_id != n.parent_id:
                raise InvalidOutline('reorder anchor must be a distinct active sibling')
            siblings = sorted((x for x in result.nodes if x.active and x.parent_id == n.parent_id and x.id != n.id), key=lambda x: x.order)
            position = next(i for i,x in enumerate(siblings) if x.id == anchor.id) + bool(op.after_id)
            siblings.insert(position, n)
            for index, sibling in enumerate(siblings):
                sibling.order = index
        elif op.kind == 'mark_gap':
            gap_id = op.gap_id or 'gap:' + digest({'node': n.id, 'question': op.reason})[:20]
            if any(g.id == gap_id or (n.id in g.node_ids and g.question == op.reason) for g in result.gaps):
                raise InvalidOutline('gap already recorded')
            result.gaps.append(ResearchGap(id=gap_id, node_ids=[n.id], requirement_ids=op.requirement_ids,
                question=op.reason, kind=op.gap_kind, priority=op.gap_priority))
        elif op.kind in {'resolve_gap', 'reopen_gap'}:
            gap = next((g for g in result.gaps if g.id == op.gap_id and n.id in g.node_ids), None)
            if gap is None:
                raise InvalidOutline('unknown gap or owning node')
            if op.kind == 'reopen_gap':
                gap.status = 'open'; gap.resolution_refs = []; gap.resolution_audit_id = None
            else:
                approval = approved_resolutions.get(digest(op.model_dump(mode='json')))
                if not approval:
                    raise InvalidOutline('gap resolution requires verified audit approval')
                gap.status = op.resolution_status
                gap.resolution_refs = op.resolution_refs
                gap.resolution_audit_id = approval
        elif op.kind == 'retire_node':
            if any(g for g in result.gaps if n.id in g.node_ids):
                if not op.target_node_ids or any(t not in by_id or not by_id[t].active or t == n.id for t in op.target_node_ids):
                    raise InvalidOutline('retiring a node requires transfer of its gaps')
                transfer(n.id, op.target_node_ids)
            n.active = False
    for node in result.nodes:
        node.gap_ids = [g.id for g in result.gaps if node.id in g.node_ids] if node.active else []
    # Reject structural no-ops before adding audit metadata.
    if result.nodes == outline.nodes and result.gaps == outline.gaps:
        raise InvalidOutline('patch has no semantic effect')
    for n in result.nodes:
        if not set(n.claim_ids) <= claim_ids or not set(n.evidence_ids) <= evidence_ids:
            raise InvalidOutline('node binds unknown claim/evidence')
    result.outline_version += 1
    result.reason_refs = patch.reason_refs
    result.operations = [op.model_dump(mode='json') for op in patch.operations]
    validate_outline(result, contract, max_depth)
    return result
