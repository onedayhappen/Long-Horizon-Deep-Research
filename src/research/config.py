from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import StrictModel


class Execution(StrictModel):
    mode: Literal["replay", "live", "assisted"]
    fixture_dir: Path | None = None


class Runtime(StrictModel):
    max_inflight_actions: Literal[1] = 1
    max_rounds: int = Field(default=20, gt=0)


class Budget(StrictModel):
    enforcement: Literal["calls_time", "bounded_usage"] = "calls_time"
    max_external_calls: int = Field(default=120, gt=0)
    max_duration_seconds: int = Field(default=3600, gt=0)
    pool_percentages: list[int] = Field(default_factory=lambda: [80, 10, 10], min_length=3, max_length=3)
    max_total_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: str | None = None

    @model_validator(mode="after")
    def check_pools(self) -> "Budget":
        if sum(self.pool_percentages) != 100 or min(self.pool_percentages) < 1:
            raise ValueError("pool_percentages must be positive and sum to 100")
        if any(self.max_external_calls * p // 100 == 0 for p in self.pool_percentages[:2]):
            raise ValueError("every call pool needs at least one call")
        if self.enforcement == "calls_time" and (self.max_total_tokens is not None or self.max_cost_usd is not None):
            raise ValueError("token/cost caps require bounded_usage")
        if self.enforcement == "bounded_usage" and self.max_total_tokens is None and self.max_cost_usd is None:
            raise ValueError("bounded_usage needs a token or cost cap")
        return self


class Model(StrictModel):
    backend: Literal["replay", "chat_json", "external"]
    api_key_env: str = "RESEARCH_MODEL_API_KEY"
    max_output_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_response_bytes: int = Field(default=262144, gt=0)
    request_timeout_seconds: int = Field(default=120, gt=0)
    thinking: Literal["enabled", "disabled"] | None = None
    base_url: str | None = None
    model: str | None = None
    profile_path: Path | None = None


class Search(StrictModel):
    provider: Literal["replay", "serper", "external"]
    api_key_env: str = "SERPER_API_KEY"
    max_results_per_query: int = Field(default=10, gt=0)
    max_query_variants: int = Field(default=5, gt=0)


class Fetch(StrictModel):
    mode: Literal["replay", "egress_proxy", "external"]
    request_timeout_seconds: int = Field(default=30, gt=0)
    max_response_bytes: int = Field(default=8_000_000, gt=0)
    max_redirects: int = Field(default=5, ge=0)
    egress_proxy: str | None = None
    egress_validation_path: Path | None = None


class Extraction(StrictModel):
    max_parse_seconds: int = Field(default=15, gt=0)
    max_pdf_pages: int = Field(default=200, gt=0)
    max_text_chars: int = Field(default=300000, gt=0)
    window_chars: int = Field(default=6000, gt=0)
    window_overlap_chars: int = Field(default=500, ge=0)
    max_windows_per_action: int = Field(default=6, gt=0)
    max_memory_bytes: int = Field(default=536870912, gt=0)
    max_input_bytes: int = Field(default=8000000, gt=0)
    max_total_render_bytes: int = Field(default=50331648, gt=0)

    @model_validator(mode="after")
    def check_window(self) -> "Extraction":
        if self.window_overlap_chars >= self.window_chars:
            raise ValueError("window overlap must be less than window size")
        return self


class Audit(StrictModel):
    max_schema_retries: Literal[1] = 1
    max_transport_retries: int = Field(default=2, ge=0, le=2)


class Stopping(StrictModel):
    saturation_effective_rounds: int = Field(default=3, gt=0)
    max_no_progress_actions: int = Field(default=3, gt=0)
    max_revision_cycles: int = Field(default=2, ge=0)
    human_review_deadline_seconds: int = Field(default=86400, gt=0)


class Research(StrictModel):
    execution: Execution
    runtime: Runtime = Field(default_factory=Runtime)
    budget: Budget = Field(default_factory=Budget)
    model: Model
    search: Search
    fetch: Fetch
    extraction: Extraction = Field(default_factory=Extraction)
    audit: Audit = Field(default_factory=Audit)
    stopping: Stopping = Field(default_factory=Stopping)
    visual: "Visual" = Field(default_factory=lambda: Visual())


class ResearchConfig(StrictModel):
    research: Research

    @model_validator(mode="after")
    def check_modes(self) -> "ResearchConfig":
        r = self.research
        if r.visual.enabled and (r.model.backend != "chat_json" or r.model.profile_path is None):
            raise ValueError("visual requires a chat_json backend and an explicit vision profile")
        if r.execution.mode == "replay":
            if (r.model.backend, r.search.provider, r.fetch.mode) != ("replay", "replay", "replay") or r.execution.fixture_dir is None:
                raise ValueError("replay requires replay model/search/fetch and fixture_dir")
        elif r.execution.mode == "assisted":
            if r.model.backend not in {"external", "chat_json"} or (r.search.provider, r.fetch.mode) != ("external", "external") or r.execution.fixture_dir is not None:
                raise ValueError("assisted requires external or chat_json model and external search/fetch without fixtures")
            if r.model.backend == "chat_json" and not all((r.model.base_url, r.model.model, r.model.api_key_env.strip())):
                raise ValueError("chat_json requires base_url, model and api_key_env")
            if r.budget.enforcement != "calls_time":
                raise ValueError("assisted usage is unavailable; use calls_time")
        else:
            if (r.model.backend, r.search.provider, r.fetch.mode) != ("chat_json", "serper", "egress_proxy"):
                raise ValueError("live requires chat_json/serper/egress_proxy")
            if r.execution.fixture_dir is not None or not all((r.model.base_url, r.model.model, r.model.profile_path, r.fetch.egress_proxy, r.fetch.egress_validation_path)):
                raise ValueError("live requires model profile, endpoint, and validated proxy")
        return self


class Visual(StrictModel):
    enabled: bool = False
    max_figures_per_action: int = Field(default=3, ge=1, le=3)
    max_images_per_figure: int = Field(default=2, ge=1, le=6)
    dpi: Literal[150] = 150
    detail_dpi: Literal[300] = 300
    max_image_pixels: int = Field(default=6000000, gt=0, le=6000000)
    max_image_bytes: int = Field(default=8388608, gt=0, le=8388608)


Research.model_rebuild()
ResearchConfig.model_rebuild()


def load_config(path: Path) -> ResearchConfig:
    path = path.resolve()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    for section, name in (("execution", "fixture_dir"), ("model", "profile_path"), ("fetch", "egress_validation_path")):
        section_data = data.get("research", {}).get(section, {})
        if name in section_data:
            section_data[name] = (path.parent / section_data[name]).resolve()
    return ResearchConfig.model_validate(data, strict=True)
