"""Typed conflict proposals and conservative, deterministic disposition rules.

Source-aware comparison follows SciFact, DRAGged and Astute RAG. Temporal
scope is explicit (Graphiti); disclosure never changes truth status.
"""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator

from .models import StrictModel


ConflictType = Literal['no_conflict', 'complementary', 'scope_difference',
                       'temporal_change', 'factual_contradiction',
                       'opinion_difference', 'uncertain']
Disposition = Literal['resolved_by_scope', 'resolved_by_correction',
                      'resolved_by_reproduction', 'weighted_conclusion',
                      'unresolved_disclosed', 'requires_user_judgment']


class EvidenceBasis(StrictModel):
    evidence_version_id: str
    quote: str = Field(min_length=1)


class ClaimFrame(StrictModel):
    claim_version_id: str
    subject: str | None = None
    metric: str | None = None
    time: str | None = None
    version: str | None = None
    conditions: list[str] = Field(default_factory=list)
    unit: str | None = None
    method: str | None = None
    basis: list[EvidenceBasis] = Field(min_length=1)


class EvidenceRelationAudit(StrictModel):
    claim_version_id: str
    evidence_version_id: str
    relation: Literal['supports', 'refutes', 'contextualizes', 'uncertain']
    scope_match: bool
    basis: EvidenceBasis
    reason: str = Field(min_length=1)


class ConflictProposal(StrictModel):
    conflict_type: ConflictType
    frames: list[ClaimFrame] = Field(min_length=2, max_length=2)
    relations: list[EvidenceRelationAudit] = Field(default_factory=list)
    disposition: Disposition | None = None
    reason: str = Field(min_length=1)
    # A concrete evidence request, not an ungrounded resolution.
    next_investigation: str = ''

    @model_validator(mode='after')
    def check_disposition(self):
        if self.conflict_type in {'no_conflict', 'complementary'}:
            if self.disposition is not None:
                raise ValueError('non-conflicts have no disposition')
        elif self.disposition is None:
            raise ValueError('conflicts need a proposed disposition')
        return self


class ConflictVerification(StrictModel):
    checked_claim_version_ids: list[str] = Field(min_length=2, max_length=2)
    verdict: Literal['accept', 'reject', 'uncertain']
    original_claims_scoped: bool
    reason: str = Field(min_length=1)


class ConflictCase(StrictModel):
    schema_version: Literal[1] = 1
    claim_version_ids: list[str] = Field(min_length=2)
    evidence_version_ids: list[str] = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    conflict_type: ConflictType
    severity: Literal['blocking', 'material'] = 'blocking'
    status: Literal['open', 'investigating', 'resolved', 'unresolved', 'dismissed']
    disposition: Disposition | None = None
    input_manifest_hash: str
    contract_hash: str
    comparison_id: str
    review_version_id: str
    reason: str
    next_investigation: str = ''


def adjudicate(proposal: ConflictProposal, review: ConflictVerification):
    """Only independently checked non-conflicts/scoped statements are cleared.

Correction, reproduction and weighting require additional audited material;
an LLM assertion alone must not invalidate either original evidence item.
New material is processed by the same comparison pipeline.
"""
    if review.verdict != 'accept':
        return 'unresolved', 'unresolved_disclosed'
    if any(r.relation == 'refutes' and r.scope_match for r in proposal.relations):
        return 'unresolved', 'unresolved_disclosed'
    if proposal.conflict_type in {'no_conflict', 'complementary'}:
        return 'dismissed', None
    if (proposal.conflict_type in {'scope_difference', 'temporal_change'}
            and proposal.disposition == 'resolved_by_scope'
            and review.original_claims_scoped):
        return 'resolved', 'resolved_by_scope'
    if proposal.disposition == 'requires_user_judgment':
        return 'unresolved', 'requires_user_judgment'
    return 'unresolved', 'unresolved_disclosed'


def disclosure_allowed(requirement, cases, *, unknown=False):
    if not cases:
        return True
    checks = [c for c in requirement.acceptance_checks if c.code == 'conflict_disposition']
    allowed = set.intersection(*(set(c.params.allowed) for c in checks)) if checks else set()
    for case in cases:
        if case.get('status') == 'resolved':
            if checks and case.get('disposition') not in allowed:
                return False
        elif (case.get('status') != 'unresolved'
              or case.get('disposition') != 'unresolved_disclosed'
              or 'unresolved_disclosed' not in allowed
              or not (requirement.answer_mode == 'compare_evidence' or (unknown and requirement.allow_unknown))):
            return False
    return True
