from pathlib import Path

from src.research.jsonio import load
from src.research.models import CoverageAssessment, ResearchContract
from src.research.policy import (
    AuditedLink, ObservationRoot, QueryOutcome, RootRelation, challenge_satisfied,
    claim_epistemic, coverage_gates, independent_count,
)


ROOT = Path(__file__).parents[1] / "fixtures" / "research" / "success"


def test_claim_requires_both_audits():
    positive = AuditedLink("c", "e", "supports", True, "supported", "no_objection")
    challenged = AuditedLink("c", "e", "supports", True, "supported", "found_issue")
    not_supported = AuditedLink("c", "e", "refutes", True, "not_supported", "no_objection")
    assert claim_epistemic([positive]) == "supported"
    assert claim_epistemic([challenged]) == "insufficient"
    assert claim_epistemic([not_supported]) == "insufficient"


def test_reprint_and_unknown_do_not_create_independence():
    roots = [ObservationRoot("vendor", frozenset({"e1"}), True), ObservationRoot("reprint", frozenset({"e2"}), True), ObservationRoot("lab", frozenset({"e3"}), True)]
    reprint = RootRelation("vendor", "reprint", "reprints", "confirmed", frozenset({"e2"}))
    assert not independent_count(roots, [reprint], {"e1", "e2"}, 2)
    assert not independent_count(roots, [reprint], {"e1", "e3"}, 2)
    independent = RootRelation("vendor", "lab", "independently_observes", "confirmed", frozenset({"e1", "e3"}))
    assert independent_count(roots, [reprint, independent], {"e1", "e3"}, 2)


def test_empty_counts_as_attempt_not_source():
    assert challenge_satisfied([QueryOutcome("counterevidence", "empty")], {"counterevidence"}, 1)
    assert not challenge_satisfied([QueryOutcome("counterevidence", "failed")], {"counterevidence"}, 1)


def test_research_ready_before_draft():
    contract = ResearchContract.model_validate(load(ROOT / "contract.json"), strict=True)
    assessment = CoverageAssessment(requirement_id="R1", stage="research", contract_version=1, input_manifest_hash="hash", disposition="satisfied", passed_check_ids=["R1.support", "R1.attribution"], missing_check_ids=[], claim_version_ids=["c.v1"], audit_id="audit")
    gates = coverage_gates(contract, [assessment], question_audit_passed=True, search_bias_passed=True, coverage_audit_passed=True)
    assert gates.research_ready and not gates.delivery_ready
