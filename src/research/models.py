from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


def utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be UTC ISO-8601 ending in Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must be UTC")
    return parsed


class Scope(StrictModel):
    subjects: list[str] = Field(min_length=1)
    regions: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class Condition(StrictModel):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    unit: str | None = None


class ClaimScope(Scope):
    conditions: list[Condition] = Field(default_factory=list)


class TimeScope(StrictModel):
    start: str | None = None
    end: str | None = None
    as_of: str

    @model_validator(mode="after")
    def check_order(self) -> "TimeScope":
        end = utc(self.end) if self.end else utc(self.as_of)
        if self.start and utc(self.start) > end:
            raise ValueError("start is later than end")
        if end > utc(self.as_of):
            raise ValueError("end is later than as_of")
        return self


class CheckCode(StrEnum):
    HAS_CURRENT_SUPPORT = "has_current_support"
    MIN_OBSERVATION_GROUPS = "min_observation_groups"
    SOURCE_ATTRIBUTION = "source_attribution"
    FRESHNESS = "freshness"
    NUMERIC_RECOMPUTED = "numeric_recomputed"
    CHALLENGE_ATTEMPTED = "challenge_attempted"
    CONFLICT_DISPOSITION = "conflict_disposition"
    ANSWER_IN_REPORT = "answer_in_report"


class SupportParams(StrictModel):
    claim_kinds: list[Literal["attributed", "factual", "comparative", "derived", "inference"]] = Field(min_length=1)
    min_claims: int = Field(gt=0)


class ObservationParams(StrictModel):
    minimum: Literal[1, 2]


class AttributionParams(StrictModel):
    required_class: Literal["primary_official", "primary_study", "secondary"]


class FreshnessParams(StrictModel):
    max_age_days: int = Field(ge=0)
    basis: Literal["published_at", "event_at"]


class RecomputeParams(StrictModel):
    absolute_tolerance: str

    @field_validator("absolute_tolerance")
    @classmethod
    def decimal_nonnegative(cls, value: str) -> str:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise ValueError("absolute_tolerance must be a nonnegative finite decimal")
        return value


class ChallengeParams(StrictModel):
    families: list[Literal["primary", "independent", "counterevidence"]] = Field(min_length=1)
    min_successful_queries: int = Field(gt=0)


class ConflictParams(StrictModel):
    allowed: list[Literal["resolved_by_scope", "resolved_by_correction", "resolved_by_reproduction", "weighted_conclusion", "unresolved_disclosed", "requires_user_judgment"]] = Field(min_length=1)


class AnswerParams(StrictModel):
    require_limitation_disclosure: bool


PARAMS = {
    CheckCode.HAS_CURRENT_SUPPORT: ("research", SupportParams),
    CheckCode.MIN_OBSERVATION_GROUPS: ("research", ObservationParams),
    CheckCode.SOURCE_ATTRIBUTION: ("research", AttributionParams),
    CheckCode.FRESHNESS: ("research", FreshnessParams),
    CheckCode.NUMERIC_RECOMPUTED: ("research", RecomputeParams),
    CheckCode.CHALLENGE_ATTEMPTED: ("research", ChallengeParams),
    CheckCode.CONFLICT_DISPOSITION: ("research", ConflictParams),
    CheckCode.ANSWER_IN_REPORT: ("delivery", AnswerParams),
}


class AcceptanceCheck(StrictModel):
    check_id: str = Field(min_length=1)
    code: CheckCode
    stage: Literal["research", "delivery"]
    params: SupportParams | ObservationParams | AttributionParams | FreshnessParams | RecomputeParams | ChallengeParams | ConflictParams | AnswerParams
    reason: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def typed_params(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        copy = dict(value)
        code = CheckCode(copy["code"])
        copy["code"] = code
        stage, param_type = PARAMS[code]
        if copy.get("stage") != stage:
            raise ValueError(f"{code} belongs to {stage} stage")
        copy["params"] = param_type.model_validate(copy.get("params"), strict=True)
        return copy


class Requirement(StrictModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    priority: Literal["must", "should", "optional"]
    weight: int = Field(gt=0)
    allow_unknown: bool
    unknown_check_ids: list[str]
    acceptance_checks: list[AcceptanceCheck] = Field(min_length=2)

    @model_validator(mode="after")
    def checks_valid(self) -> "Requirement":
        ids = [check.check_id for check in self.acceptance_checks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate check_id")
        if not any(c.stage == "research" for c in self.acceptance_checks):
            raise ValueError("research check required")
        if not any(c.code == CheckCode.ANSWER_IN_REPORT for c in self.acceptance_checks):
            raise ValueError("answer_in_report check required")
        eligible = {c.check_id for c in self.acceptance_checks if c.code in (CheckCode.CHALLENGE_ATTEMPTED, CheckCode.CONFLICT_DISPOSITION)}
        if self.allow_unknown and (not self.unknown_check_ids or not set(self.unknown_check_ids) <= eligible):
            raise ValueError("allow_unknown requires investigation check IDs")
        if not self.allow_unknown and self.unknown_check_ids:
            raise ValueError("unknown checks require allow_unknown")
        return self


class OutputContract(StrictModel):
    language: str = Field(min_length=1)
    format: Literal["markdown"]
    required_sections: list[str] = Field(min_length=1)
    max_characters: int = Field(gt=0)


class StopPolicy(StrictModel):
    should_weighted_fraction: float = Field(default=0.90, ge=0, le=1)


class ReuseTimeRule(StrictModel):
    temporal_mode: Literal['historical_as_of', 'current_at_as_of'] = 'historical_as_of'
    knowledge_mode: Literal['strict_as_of', 'retrospective'] = 'strict_as_of'
    material_class: Literal['volatile', 'news', 'versioned', 'stable', 'unclassified'] = 'unclassified'
    max_validation_age_seconds: int = Field(default=0, ge=0)


class RequirementReuseRule(StrictModel):
    requirement_id: str
    rule: ReuseTimeRule


class ReusePolicy(StrictModel):
    schema_version: Literal[1] = 1
    default_rule: ReuseTimeRule = Field(default_factory=ReuseTimeRule)
    requirement_rules: list[RequirementReuseRule] = Field(default_factory=list)

    def rule_for(self, requirement_id):
        return next((item.rule for item in self.requirement_rules if item.requirement_id == requirement_id), self.default_rule)


class ResearchContract(StrictModel):
    schema_version: Literal[1]
    contract_id: str = Field(min_length=1)
    version: int = Field(gt=0)
    question: str = Field(min_length=1)
    scope: Scope
    as_of: str
    requirements: list[Requirement] = Field(min_length=1)
    output_contract: OutputContract
    stop_policy: StopPolicy = Field(default_factory=StopPolicy)
    reuse_policy: ReusePolicy = Field(default_factory=ReusePolicy)

    @model_validator(mode="after")
    def validate_contract(self) -> "ResearchContract":
        utc(self.as_of)
        if not any(r.priority == "must" for r in self.requirements):
            raise ValueError("at least one must requirement required")
        ids = [r.id for r in self.requirements]
        checks = [c.check_id for r in self.requirements for c in r.acceptance_checks]
        if len(set(ids)) != len(ids) or len(set(checks)) != len(checks):
            raise ValueError("duplicate requirement or check ID")
        rule_ids = [r.requirement_id for r in self.reuse_policy.requirement_rules]
        if len(rule_ids) != len(set(rule_ids)) or not set(rule_ids) <= set(ids):
            raise ValueError('reuse_policy has duplicate or unknown requirement IDs')
        for requirement in self.requirements:
            if self.reuse_policy.rule_for(requirement.id).temporal_mode == 'current_at_as_of' and not any(c.code == CheckCode.FRESHNESS for c in requirement.acceptance_checks):
                raise ValueError('current_at_as_of requires a freshness check')
        return self


class QuerySpec(StrictModel):
    query_id: str
    question_id: str
    text: str = Field(min_length=1)
    strategy_family: Literal["primary", "independent", "counterevidence"]
    language: str
    country_code: str | None
    source_class_targets: list[Literal["primary_official", "primary_study", "secondary"]]
    max_results: int = Field(gt=0)


class SearchHit(StrictModel):
    hit_id: str
    url: str
    title: str
    snippet: str
    rank: int = Field(ge=0)
    publisher: str | None = None
    published_at: str | None = None


class Usage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: str | None = None
    basis: Literal["provider", "reserved_upper_bound", "unknown"] = "unknown"


class SearchBatch(StrictModel):
    query_id: str
    hits: list[SearchHit]
    status: Literal["hit", "empty", "error"]
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> "SearchBatch":
        if (self.status == "hit") != bool(self.hits) or (self.status == "error" and not self.error):
            raise ValueError("search status inconsistent with hits/error")
        return self


class FetchRequest(StrictModel):
    source_id: str
    url: str


class FetchResult(StrictModel):
    source_id: str
    requested_url: str
    final_url: str
    status: Literal["ok", "source_unavailable", "js_required", "ocr_required", "rejected"]
    mime: str | None
    headers: dict[str, str]
    blob_hash: str | None
    fetched_at: str
    error: str | None


class ParsedBlock(StrictModel):
    id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    page: int | None = Field(default=None, ge=1)


class ParsedDocument(StrictModel):
    parser_id: str
    parser_version: str
    text_blob_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str | None
    blocks: list[ParsedBlock]


class TextSpan(StrictModel):
    kind: Literal["text_span"] = "text_span"
    text_blob_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prefix: str
    suffix: str
    page: int | None = Field(default=None, ge=1)
    block_id: str | None = None

    @model_validator(mode="after")
    def ordered(self) -> "TextSpan":
        if self.end <= self.start:
            raise ValueError("empty locator")
        return self


class EntityRef(StrictModel):
    kind: str
    id: str
    version: int = Field(gt=0)
    version_id: str


class CoverageAssessment(StrictModel):
    requirement_id: str
    stage: Literal["research", "delivery"]
    contract_version: int = Field(gt=0)
    input_manifest_hash: str
    disposition: Literal["satisfied", "bounded_unknown", "open", "blocked"]
    passed_check_ids: list[str]
    missing_check_ids: list[str]
    claim_version_ids: list[str]
    audit_id: str


class GateResult(StrictModel):
    code: str
    status: Literal["pass", "fail", "not_applicable"]
    reason: str
    refs: list[str] = Field(default_factory=list)


class StopDecision(StrictModel):
    outcome: Literal["complete", "complete_with_limitations", "incomplete_budget", "incomplete_plateau", "incomplete_no_progress", "incomplete_report_audit_loop", "blocked", "failed", "cancelled"]
    evaluated_state_version: int = Field(ge=0)
    terminal_state_version: int = Field(gt=0)
    input_manifest_hash: str
    gate_results: list[GateResult]
    unresolved_ids: list[str]
    budget_snapshot: "BudgetSnapshot"
    outline_version: int | None = None
    research_rounds: int = Field(default=0, ge=0)
    planner_steps: int = Field(default=0, ge=0)
    report_hash: str | None = None
    coverage_version_ids: list[str] = Field(default_factory=list)


class BudgetSnapshot(StrictModel):
    external_calls: int = Field(ge=0)
    usage_unknown_attempts: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: str | None = None


class SearchAction(StrictModel):
    kind: Literal["search"] = "search"
    question_id: str
    query_plan: list[QuerySpec] = Field(min_length=1)
    acceptance_check_ids: list[str]
    max_external_calls: int = Field(gt=0)


class OutlinePatchAction(StrictModel):
    kind: Literal["patch_outline"] = "patch_outline"
    base_outline_version: int = Field(gt=0)
    operations: list["OutlineOperation"] = Field(min_length=1)
    reason_refs: list[str] = Field(min_length=1)


class OutlineNode(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    parent_id: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    active: bool = True
    notes: list[str] = Field(default_factory=list)
    replaced_by: list[str] = Field(default_factory=list)


class OutlineState(StrictModel):
    outline_version: int = Field(gt=0)
    question_ids: list[str]
    nodes: list[OutlineNode] = Field(min_length=1)
    reason_refs: list[str] = Field(default_factory=list)
    operations: list[dict] = Field(default_factory=list)


class OutlineOperation(StrictModel):
    kind: Literal["add", "split", "merge", "move", "narrow_claim", "bind_evidence", "add_counterview", "mark_gap", "retire_node"]
    node_id: str
    target_parent_id: str | None = None
    title: str | None = None
    reason: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    # split supplies replacement nodes; merge names the nodes to absorb.
    nodes: list[OutlineNode] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class TerminateProposal(StrictModel):
    kind: Literal["terminate"] = "terminate"
    coverage_refs: list[str]
    proposed_outcome: str


ResearchAction = Annotated[SearchAction | OutlinePatchAction | TerminateProposal, Field(discriminator="kind")]


class RoleRequest(StrictModel):
    role: Literal["planner.initialize", "auditor.question_space", "planner.next", "researcher.extract", "auditor.evidence", "auditor.counter_entailment", "auditor.provenance", "auditor.conflict", "auditor.coverage", "auditor.search_bias", "writer.section", "auditor.report", "planner.reuse_map"]
    logical_action_key: str
    input_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_version: int = Field(gt=0)
    policy_version: int = Field(gt=0)
    system_template_id: str
    system_template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_packet: dict[str, str | int | bool | list[str]]
    response_schema_id: str
    response_schema_version: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class RoleResponse(StrictModel):
    raw_text: str
    finish_reason: str
    provider_request_id: str | None
    usage: Usage
    model_profile_hash: str


class InitializationProposal(StrictModel):
    question_ids: list[str] = Field(min_length=1)
    outline_sections: list[str] = Field(min_length=1)
    outline_nodes: list[OutlineNode] = Field(default_factory=list)


class QuestionSpaceAudit(StrictModel):
    review_status: Literal["pass", "missing", "needs_clarification"]
    dimensions_checked: list[str] = Field(min_length=1)
    missing_questions: list[str]


class SearchBiasVerdict(StrictModel):
    review_status: Literal["pass", "missing"]
    source_classes_seen: list[str]
    query_families_seen: list[str]
    blind_spots: list[str]


class CandidateEvidence(StrictModel):
    claim_id: str
    claim_text: str = Field(min_length=1)
    claim_kind: Literal["attributed", "factual", "comparative", "derived", "inference"]
    requirement_ids: list[str] = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    source_class: Literal["primary_official", "primary_study", "secondary", "unknown"]
    observation_root: str


class CandidateEvidenceBundle(StrictModel):
    candidates: list[CandidateEvidence]


class EvidenceAuditVerdict(StrictModel):
    verdict: Literal["supported", "not_supported", "uncertain"]
    reason: str = Field(min_length=1)
    source_class_verified: bool


class CounterAuditVerdict(StrictModel):
    verdict: Literal["no_objection", "found_issue", "inconclusive"]
    reason: str = Field(min_length=1)


class CoverageProposal(StrictModel):
    requirement_id: str
    disposition: Literal["satisfied", "bounded_unknown", "open", "blocked"]
    passed_check_ids: list[str]
    missing_check_ids: list[str]
    claim_version_ids: list[str]
    investigation_refs: list[str]
    resolved_gap_ids: list[str] = Field(default_factory=list)


class DraftSentence(StrictModel):
    id: str
    text: str
    fact_ids: list[str]


class DraftParagraph(StrictModel):
    id: str
    sentences: list[DraftSentence]


class FactualFact(StrictModel):
    kind: Literal["factual"] = "factual"
    id: str
    requirement_ids: list[str] = Field(min_length=1)
    claim_version_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class BoundedUnknownFact(StrictModel):
    kind: Literal["bounded_unknown"] = "bounded_unknown"
    id: str
    requirement_ids: list[str] = Field(min_length=1)
    coverage_assessment_version_id: str
    investigation_refs: list[str] = Field(min_length=1)


class DisputeFact(StrictModel):
    kind: Literal["dispute"] = "dispute"
    id: str
    requirement_ids: list[str] = Field(min_length=1)
    conflict_version_id: str
    evidence_ids: list[str] = Field(min_length=2)


Fact = Annotated[FactualFact | BoundedUnknownFact | DisputeFact, Field(discriminator="kind")]


class DraftSection(StrictModel):
    section_id: str
    revision: int = Field(ge=0)
    outline_version: int = Field(gt=0)
    paragraphs: list[DraftParagraph]
    facts: list[Fact]
    open_questions: list[str]


class ReportFinding(StrictModel):
    fact_id: str | None
    check_code: str
    severity: Literal["blocking", "material", "minor"]
    repair_kind: str
    refs: list[str]


class ReportAudit(StrictModel):
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: list[ReportFinding]
    checked_fact_ids: list[str]
    checked_section_ids: list[str]
    answered_requirement_ids: list[str]
