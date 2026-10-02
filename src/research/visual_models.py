from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Extraction, Visual


class VisualError(ValueError):
    """A bounded, actionable failure; never a successful visual observation."""


def canonical(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strict_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise VisualError("protocol_error: duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise VisualError("protocol_error: non-finite JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class WorkerConfig(Model):
    extraction: Extraction
    visual: Visual


class VisionProfile(Model):
    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    supports_images: Literal[True]
    max_image_bytes: int = Field(gt=0)
    max_image_pixels: int = Field(gt=0)
    max_images: int = Field(gt=0)
    max_request_bytes: int = Field(gt=0)


Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=12000)]
BBox = Annotated[list[float], Field(min_length=4, max_length=4)]


def check_bbox(box: BBox, width: float, height: float):
    x0, y0, x1, y1 = box
    if not all(math.isfinite(x) for x in box) or not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise VisualError("invalid_region: bbox must lie inside the unrotated page")


class PDFRegion(Model):
    kind: Literal["pdf_region"] = "pdf_region"
    raw_hash: Hash
    page: Annotated[int, Field(strict=True, ge=1)]
    bbox: BBox
    page_size: tuple[float, float]
    cropbox: BBox
    rotation: Literal[0, 90, 180, 270]
    coordinates: Literal["pymupdf_unrotated_points"] = "pymupdf_unrotated_points"

    @model_validator(mode="after")
    def valid_box(self):
        check_bbox(self.bbox, *self.page_size)
        return self


class RegionProposal(Model):
    page: Annotated[int, Field(strict=True, ge=1)]
    bbox: BBox | None = None
    detail: Annotated[bool, Field(strict=True)] = False


class TextBlock(Model):
    id: Text
    page: int
    bbox: BBox
    text: Text


class FigureCandidate(Model):
    id: Text
    figure_label: Text
    caption_refs: list[str]
    mention_refs: list[str]
    pages: list[int]
    extraction_status: Literal["ambiguous"] = "ambiguous"


class DocumentMap(Model):
    raw_hash: Hash
    parser_version: Text
    pages: list[dict]
    blocks: list[TextBlock]
    figures: list[FigureCandidate]
    status: Literal["text_available", "needs_ocr"]


class RenderedImage(Model):
    blob_hash: Hash
    locator: PDFRegion
    width: Annotated[int, Field(strict=True, gt=0)]
    height: Annotated[int, Field(strict=True, gt=0)]
    dpi: Literal[150, 300]
    # Six affine coefficients mapping PNG pixel coordinates into unrotated PDF points.
    pixel_to_pdf: tuple[float, float, float, float, float, float]
    purpose: Literal["context", "detail"]


class FigureArtifact(Model):
    schema_version: Literal[1] = 1
    snapshot_ref: Hash
    figure_label: Text
    caption_refs: list[str]
    mention_refs: list[str]
    page_regions: list[PDFRegion] = Field(min_length=1, max_length=6)
    images: list[RenderedImage] = Field(min_length=1, max_length=6)
    render_profile: dict
    extraction_status: Literal["located", "ambiguous"]

    @model_validator(mode="after")
    def image_bindings(self):
        if self.page_regions != [i.locator for i in self.images]:
            raise VisualError("invalid_figure: regions must match image locators")
        if any(r.raw_hash != self.snapshot_ref for r in self.page_regions):
            raise VisualError("invalid_figure: every image must derive from this snapshot")
        if self.extraction_status == "located" and not (self.caption_refs or self.mention_refs):
            raise VisualError("invalid_figure: missing caption/body association")
        return self


def region_from_pixels(image: RenderedImage, bbox: BBox) -> PDFRegion:
    """Validate a proposed PNG region and convert it, including raster rounding."""
    check_bbox(bbox, image.width, image.height)
    a, b, c, d, e, f = image.pixel_to_pdf
    x0, y0, x1, y1 = bbox
    points = [(a*x + c*y + e, b*x + d*y + f) for x in (x0, x1) for y in (y0, y1)]
    original = image.locator.bbox
    converted = (max(original[0], min(p[0] for p in points)),
                 max(original[1], min(p[1] for p in points)),
                 min(original[2], max(p[0] for p in points)),
                 min(original[3], max(p[1] for p in points)))
    return PDFRegion.model_validate({**image.locator.model_dump(), "bbox": converted})


class ReadFigure(Model):
    action: Literal["read_figure"] = "read_figure"
    figure_ref: Text
    question_id: Text
    goal: Text
    requirement_refs: list[str] = Field(default_factory=list)
    expected_evidence: Text = "Visible observations with conditions and limitations"


class FigureSelection(Model):
    figure_ref: str
    goal: Text
    regions: list[RegionProposal] = Field(default_factory=list, max_length=6)


class FigureReadPlan(Model):
    reads: list[FigureSelection] = Field(max_length=3)
    # Every candidate must be selected or explicitly classified. A body mention
    # cannot be dismissed just because its caption lacks a query keyword.
    irrelevant: dict[str, Text] = Field(default_factory=dict)
    unresolved: dict[str, Text] = Field(default_factory=dict)


class VisualLocator(Model):
    kind: Literal["pdf_region"] = "pdf_region"
    figure_version_id: str
    raw_hash: Hash
    page_regions: list[PDFRegion] = Field(min_length=1)
    image_blob_hashes: list[Hash] = Field(min_length=1)
    caption_refs: list[str]


class VisualClaimProposal(Model):
    claim_text: Text
    claim_kind: Literal["attributed", "factual", "comparative", "inference"]
    requirement_ids: list[str] = Field(min_length=1)
    source_class: Literal["primary_official", "primary_study", "secondary", "unknown"]
    observation: "VisualObservationProposal"


class VisionProbeResult(Model):
    marker: str


class ReportedNumber(Model):
    category: Literal["reported"]
    value: Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]
    unit: Text
    label_transcription: Text
    image_hash: Hash
    # Pixel box of the printed label, NOT a inferred curve coordinate.
    label_bbox: BBox
    conditions: list[Text] = Field(min_length=1)


class VisualObservationProposal(Model):
    figure_type: Literal["architecture", "plot", "example", "heatmap", "table", "other"]
    description: Text
    subpanels: list[Text]
    axes_and_units: list[Text]
    legend: list[Text]
    conditions: list[Text]
    transcriptions: list[Text]
    limitations: list[Text]
    pending_checks: list[Text]
    numbers: list[ReportedNumber]
    # No excerpt field: visual interpretation is never a verbatim quotation.


AUDIT_CHECKS = frozenset({"crop_complete", "caption_pairing", "axes_units_scales", "legend",
                          "conditions", "error_bars", "precision", "text_consistency"})


class VisualAudit(Model):
    verdict: Literal["accept", "reject", "unreadable", "conflict"]
    checks: dict[str, Literal["pass", "fail", "not_applicable"]]
    findings: list[Text]
    limitations: list[Text]
    source_class_verified: bool = False

    @model_validator(mode="after")
    def complete(self):
        if set(self.checks) != AUDIT_CHECKS:
            raise VisualError("protocol_error: all visual audit checks are required")
        if self.verdict == "accept" and (
            "fail" in self.checks.values()
            or any(self.checks[k] != "pass" for k in ("crop_complete", "caption_pairing", "precision", "text_consistency"))
        ):
            raise VisualError("protocol_error: acceptance requires passing mandatory checks")
        return self
