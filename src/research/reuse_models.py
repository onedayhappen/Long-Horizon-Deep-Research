"""Public contracts for explicit, purpose-specific research reuse."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .models import StrictModel, utc, ReuseTimeRule, RequirementReuseRule, ReusePolicy


class ReuseSelection(StrictModel):
    schema_version: Literal[1] = 1
    source_question_ids: list[str] = Field(default_factory=list)
    source_evidence_version_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonempty(self):
        if not (self.source_question_ids or self.source_evidence_version_ids):
            raise ValueError("reuse selection must contain questions or evidence versions")
        return self


class SourceValidationResult(StrictModel):
    """A tool observation, never a model's assertion that a source is current."""
    validation_key: str
    checked_at: str
    status: Literal["unchanged", "changed", "withdrawn", "unknown"]
    method: Literal["origin_fetch", "publisher_status", "retraction_lookup", "replay"]
    checked_channels: list[str] = Field(min_length=1)
    checks_performed: list[str] = Field(min_length=1)
    result_refs: list[str] = Field(min_length=1)
    # Exact observed document hash binds the tool result to the copied snapshot.
    observed_raw_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_time(self):
        utc(self.checked_at)
        if self.status == "unchanged" and self.observed_raw_hash is None:
            raise ValueError("unchanged source needs its observed hash")
        return self


class ReuseBinding(StrictModel):
    evidence_version_id: str
    claim_version_id: str
    requirement_id: str
    scope_matches: bool
    time_matches: bool
    reason: str = Field(min_length=1)


class ReuseMappingProposal(StrictModel):
    bindings: list[ReuseBinding]


class InvalidationRequest(StrictModel):
    schema_version: Literal[1] = 1
    subject_version_ids: list[str] = Field(min_length=1, max_length=128)
    reason_code: Literal["content_error", "withdrawn", "parser_error", "source_identity_error", "audit_revoked", "local_corruption"]
    affected_requirement_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(min_length=1)


class ReuseAssessment(StrictModel):
    import_id: str | None
    evidence_version_id: str
    claim_version_id: str
    requirement_id: str
    contract_version: int
    policy_hash: str
    input_manifest_hash: str
    checks: dict[str, Literal["pass", "fail", "unknown"]]
    disposition: Literal["reuse_candidate", "reused", "requires_refresh", "rejected", "needs_reaudit"]
    reason_codes: list[str]
    evaluated_at: str
    recheck_due_at: str | None = None
    validation_refs: list[str] = Field(default_factory=list)
    generation: int
    local_audit_refs: list[str] = Field(default_factory=list)
    critical_reasons: list[str] = Field(default_factory=list)
