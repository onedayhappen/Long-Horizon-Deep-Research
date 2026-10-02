"""Regenerate checked-in public research schemas."""
import json
from pathlib import Path

from src.research.config import ResearchConfig
from src.research.conflicts import ClaimFrame, EvidenceRelationAudit, ConflictCase, ConflictProposal, ConflictVerification
from src.research.reuse_models import ReuseSelection, ReusePolicy, ReuseAssessment, ReuseMappingProposal, SourceValidationResult, InvalidationRequest
from src.research.models import (
    ResearchContract, QuerySpec, SearchBatch, FetchRequest, FetchResult,
    ParsedDocument, RoleRequest, RoleResponse, CoverageAssessment, StopDecision,
    SearchAction, OutlinePatchAction, TerminateProposal, InitializationProposal,
    QuestionSpaceAudit, SearchBiasVerdict, CandidateEvidenceBundle,
    EvidenceAuditVerdict, CounterAuditVerdict, CoverageProposal, DraftSection,
    ReportAudit,
    BudgetSnapshot, OutlineOperation, OutlineNode, OutlineState, ResearchGap,
    SourceSelection, GapVerdict, OutlineReview, InspectMaterialAction,
)
from src.research.visual_models import (DocumentMap, FigureArtifact, PDFRegion, VisualLocator, ReadFigure,
    FigureReadPlan, VisualClaimProposal, VisualObservationProposal, VisualAudit, VisionProfile)


MODELS = (ResearchConfig, ResearchContract, QuerySpec, SearchBatch, FetchRequest, FetchResult,
          ParsedDocument, RoleRequest, RoleResponse, CoverageAssessment, StopDecision,
          SearchAction, OutlinePatchAction, TerminateProposal, InitializationProposal,
          QuestionSpaceAudit, SearchBiasVerdict, CandidateEvidenceBundle,
          EvidenceAuditVerdict, CounterAuditVerdict, CoverageProposal, DraftSection,
          ReportAudit, BudgetSnapshot, OutlineOperation, OutlineNode, OutlineState,
          ResearchGap, SourceSelection, GapVerdict, OutlineReview, InspectMaterialAction,
          ReuseSelection, ReusePolicy, ReuseAssessment, ReuseMappingProposal, SourceValidationResult, InvalidationRequest,
          DocumentMap, FigureArtifact, PDFRegion, VisualLocator, ReadFigure, FigureReadPlan, VisualClaimProposal,
          VisualObservationProposal, VisualAudit, VisionProfile,
          ClaimFrame, EvidenceRelationAudit, ConflictCase, ConflictProposal, ConflictVerification)
TARGET = Path(__file__).resolve().parents[1] / "src/research/schemas/v1"


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        (TARGET / f"{model.__name__}.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
