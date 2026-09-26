"""Regenerate checked-in public research schemas."""
import json
from pathlib import Path

from lh_harness.research.config import ResearchConfig
from lh_harness.research.models import (
    ResearchContract, QuerySpec, SearchBatch, FetchRequest, FetchResult,
    ParsedDocument, RoleRequest, RoleResponse, CoverageAssessment, StopDecision,
    SearchAction, OutlinePatchAction, TerminateProposal, InitializationProposal,
    QuestionSpaceAudit, SearchBiasVerdict, CandidateEvidenceBundle,
    EvidenceAuditVerdict, CounterAuditVerdict, CoverageProposal, DraftSection,
    ReportAudit,
    BudgetSnapshot, OutlineOperation, OutlineNode, OutlineState,
)


MODELS = (ResearchConfig, ResearchContract, QuerySpec, SearchBatch, FetchRequest, FetchResult,
          ParsedDocument, RoleRequest, RoleResponse, CoverageAssessment, StopDecision,
          SearchAction, OutlinePatchAction, TerminateProposal, InitializationProposal,
          QuestionSpaceAudit, SearchBiasVerdict, CandidateEvidenceBundle,
          EvidenceAuditVerdict, CounterAuditVerdict, CoverageProposal, DraftSection,
          ReportAudit, BudgetSnapshot, OutlineOperation, OutlineNode, OutlineState)
TARGET = Path(__file__).resolve().parents[1] / "src/lh_harness/research/schemas/v1"


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        (TARGET / f"{model.__name__}.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
