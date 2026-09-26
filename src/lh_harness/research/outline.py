"""Deterministic, transactional-input validation for evidence-driven outlines."""
from .models import InitializationProposal, OutlineNode, OutlineState, OutlinePatchAction, ResearchContract


class InvalidOutline(ValueError):
    pass


def validate_outline(outline: OutlineState, contract: ResearchContract) -> None:
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


def initialize_outline(proposal: InitializationProposal, contract: ResearchContract) -> OutlineState:
    nodes = proposal.outline_nodes or [OutlineNode(id=f'section-{i+1}', title=title,
        requirement_ids=[r.id for r in contract.requirements]) for i, title in enumerate(proposal.outline_sections)]
    state = OutlineState(outline_version=1, question_ids=proposal.question_ids, nodes=nodes)
    validate_outline(state, contract)
    return state


def ordered_nodes(outline: OutlineState) -> list[OutlineNode]:
    result = []
    def visit(parent):
        for n in outline.nodes:
            if n.active and n.parent_id == parent:
                result.append(n)
                visit(n.id)
    visit(None)
    return result


def apply_patch(outline: OutlineState, patch: OutlinePatchAction, contract: ResearchContract,
                valid_reasons: set[str], claim_ids: set[str], evidence_ids: set[str]) -> OutlineState:
    if patch.base_outline_version != outline.outline_version:
        raise InvalidOutline('stale base_outline_version')
    if not patch.reason_refs or not set(patch.reason_refs) <= valid_reasons:
        raise InvalidOutline('patch needs current evidence or gap references')
    result = outline.model_copy(deep=True)
    by_id = {n.id: n for n in result.nodes}
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
                result.nodes.append(child); by_id[child.id] = child
            n.replaced_by = [child.id for child in op.nodes]
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
        elif op.kind == 'move':
            n.parent_id = op.target_parent_id
        elif op.kind == 'narrow_claim':
            if not op.title or op.title == n.title:
                raise InvalidOutline('narrow_claim needs a narrower title')
            n.title = op.title
            n.claim_ids = op.claim_ids
            n.evidence_ids = op.evidence_ids
        elif op.kind == 'mark_gap':
            marker = 'gap: ' + op.reason
            if marker in n.notes:
                raise InvalidOutline('gap already recorded')
            n.notes.append(marker)
        elif op.kind == 'retire_node':
            n.active = False
    # Reject structural no-ops before adding audit metadata.
    if result.nodes == outline.nodes:
        raise InvalidOutline('patch has no semantic effect')
    for n in result.nodes:
        if not set(n.claim_ids) <= claim_ids or not set(n.evidence_ids) <= evidence_ids:
            raise InvalidOutline('node binds unknown claim/evidence')
    result.outline_version += 1
    result.reason_refs = patch.reason_refs
    result.operations = [op.model_dump(mode='json') for op in patch.operations]
    validate_outline(result, contract)
    return result
