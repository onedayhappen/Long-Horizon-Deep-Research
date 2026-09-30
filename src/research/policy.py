from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import CoverageAssessment, ResearchContract


@dataclass(frozen=True)
class AuditedLink:
    claim_version_id: str
    evidence_id: str
    relation: Literal["supports", "refutes", "contextualizes"]
    scope_match: bool
    forward_verdict: Literal["supported", "not_supported", "uncertain"]
    reverse_verdict: Literal["no_objection", "found_issue", "inconclusive"]
    evidence_active: bool = True
    claim_current: bool = True

    @property
    def qualified(self) -> bool:
        return self.scope_match and self.evidence_active and self.claim_current and self.forward_verdict == "supported" and self.reverse_verdict == "no_objection"


def claim_epistemic(links: list[AuditedLink]) -> str:
    if not links:
        return "candidate"
    supports = any(link.qualified and link.relation == "supports" for link in links)
    refutes = any(link.qualified and link.relation == "refutes" for link in links)
    if supports and refutes:
        return "contested"
    if supports:
        return "supported"
    if refutes:
        return "refuted"
    return "insufficient"


@dataclass(frozen=True)
class ObservationRoot:
    root_id: str
    evidence_ids: frozenset[str]
    eligible: bool


@dataclass(frozen=True)
class RootRelation:
    left: str
    right: str
    relation: Literal["shares_measurement", "reprints", "uses_dataset", "independently_observes", "relationship_unknown"]
    review_status: Literal["confirmed", "suspected", "unknown"]
    basis_evidence_ids: frozenset[str]


def observation_groups(roots: list[ObservationRoot], relations: list[RootRelation]) -> dict[str, str]:
    parent = {root.root_id: root.root_id for root in roots}

    def find(item: str) -> str:
        while parent[item] != item:
            item = parent[item]
        return item

    for edge in relations:
        if edge.review_status == "confirmed" and edge.basis_evidence_ids and edge.relation in {"shares_measurement", "reprints", "uses_dataset"}:
            if edge.left in parent and edge.right in parent:
                parent[find(edge.right)] = find(edge.left)
    return {item: find(item) for item in parent}


def independent_count(roots: list[ObservationRoot], relations: list[RootRelation], supporting_evidence_ids: set[str], minimum: int) -> bool:
    if minimum not in (1, 2):
        raise ValueError("minimum must be 1 or 2")
    groups = observation_groups(roots, relations)
    eligible = sorted({groups[r.root_id] for r in roots if r.eligible and r.evidence_ids & supporting_evidence_ids})
    if minimum == 1:
        return bool(eligible)
    for left in eligible:
        for right in eligible:
            if left >= right:
                continue
            relevant = [e for e in relations if {groups.get(e.left), groups.get(e.right)} == {left, right}]
            if any(e.relation == "independently_observes" and e.review_status == "confirmed" and e.basis_evidence_ids for e in relevant) and not any(e.relation != "independently_observes" or e.review_status != "confirmed" for e in relevant):
                return True
    return False


@dataclass(frozen=True)
class QueryOutcome:
    family: str
    status: Literal["hit", "empty", "failed", "blocked"]


def challenge_satisfied(outcomes: list[QueryOutcome], families: set[str], minimum: int) -> bool:
    successful = [q for q in outcomes if q.status in {"hit", "empty"}]
    return len(successful) >= minimum and families <= {q.family for q in successful}


def accepted(contract: ResearchContract, assessment: CoverageAssessment) -> bool:
    requirement = next((r for r in contract.requirements if r.id == assessment.requirement_id), None)
    if requirement is None or assessment.contract_version != contract.version or assessment.stage != "research":
        return False
    research_ids = {c.check_id for c in requirement.acceptance_checks if c.stage == "research"}
    if assessment.disposition == "satisfied":
        return research_ids <= set(assessment.passed_check_ids) and not assessment.missing_check_ids
    if assessment.disposition == "bounded_unknown":
        return requirement.allow_unknown and set(requirement.unknown_check_ids) <= set(assessment.passed_check_ids) and set(assessment.missing_check_ids) == research_ids - set(assessment.passed_check_ids)
    return False


@dataclass(frozen=True)
class Gates:
    research_ready: bool
    delivery_ready: bool
    must_coverage: float
    should_coverage: float
    missing_requirements: tuple[str, ...]


def coverage_gates(contract: ResearchContract, assessments: list[CoverageAssessment], *, question_audit_passed: bool, search_bias_passed: bool, coverage_audit_passed: bool, report_audit_passed: bool = False, answered_requirement_ids: set[str] | None = None) -> Gates:
    accepted_ids = {a.requirement_id for a in assessments if accepted(contract, a)}
    must = [r for r in contract.requirements if r.priority == "must"]
    should = [r for r in contract.requirements if r.priority == "should"]
    must_fraction = sum(r.id in accepted_ids for r in must) / len(must)
    should_fraction = sum(r.weight for r in should if r.id in accepted_ids) / sum(r.weight for r in should) if should else 1.0
    ready = must_fraction == 1 and should_fraction >= contract.stop_policy.should_weighted_fraction and question_audit_passed and search_bias_passed and coverage_audit_passed
    answers = answered_requirement_ids or set()
    delivery = ready and report_audit_passed and {r.id for r in must} <= answers
    return Gates(ready, delivery, must_fraction, should_fraction, tuple(r.id for r in must if r.id not in accepted_ids))
