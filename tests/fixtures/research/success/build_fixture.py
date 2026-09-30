"""Hash-index fixed human-readable fictional role responses; no policy oracle is run."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from src.research.extract import parse_document, locate
from src.research.jsonio import canonical, digest


ROOT = Path(__file__).parent
SEED = "36f7f96e-8b53-43c9-a29a-690d350cbb23"
SOURCE = ROOT / "sources/official.html"
URL = "https://example.org/official-export"
RAW = SOURCE.read_bytes()
PARSED, TEXT = parse_document(RAW, "text/html")
EXCERPT = "虚构产品 A 官方声明支持导出 CSV 文件。"
LOCATOR = locate(TEXT, EXCERPT)
CLAIM_VERSION_ID = str(uuid.uuid5(uuid.UUID(SEED), "claim:official-export:1"))
EVIDENCE_VERSION_ID = str(uuid.uuid5(uuid.UUID(SEED), "evidence:q1.1.e1:1"))
ROLES = {}


def add(role: str, action: str, packet: dict, output: dict) -> None:
    manifest_hash = digest({"role": role, "logical_action_key": action, "contract_version": 1, "packet": packet})
    key = f"{role}|{action}|{manifest_hash}"
    path = f"roles/{len(ROLES)+1:02d}.json"
    ROLES[key] = path
    (ROOT / path).parent.mkdir(exist_ok=True)
    (ROOT / path).write_bytes(canonical({"raw_text": canonical(output).decode(), "finish_reason": "stop", "provider_request_id": None, "usage": {"input_tokens": 1, "output_tokens": 1, "cost": None, "basis": "provider"}, "model_profile_hash": "replay-fixture-v1"}))


QUESTION = "截至指定日期，虚构产品 A 的官方资料是否声明支持导出？"
add("planner.initialize", "initialize", {"question": QUESTION, "requirement_ids": ["R1"]}, {"question_ids": ["R1"], "outline_sections": ["结论", "依据与限制"]})
add("auditor.question_space", "question_space", {"question": QUESTION, "question_ids": ["R1"]}, {"review_status": "pass", "dimensions_checked": ["对象", "归因", "时间"], "missing_questions": []})
add("planner.next", "search-1", {"requirement_ids": ["R1"], "question_ids": ["R1"]}, {"kind": "search", "question_id": "R1", "query_plan": [{"query_id": "q1", "question_id": "R1", "text": "虚构产品 A 官方 导出 CSV", "strategy_family": "primary", "language": "zh-CN", "country_code": None, "source_class_targets": ["primary_official"], "max_results": 10}], "acceptance_check_ids": ["R1.support", "R1.attribution"], "max_external_calls": 10})
add("researcher.extract", "extract:q1.1", {"source_id": "q1.1", "text_blob_hash": PARSED.text_blob_hash, "title": "虚构产品 A 功能说明"}, {"candidates": [{"claim_id": "official-export", "claim_text": "虚构产品 A 官方声明支持导出 CSV 文件。", "claim_kind": "attributed", "requirement_ids": ["R1"], "excerpt": EXCERPT, "source_class": "primary_official", "observation_root": "official-doc"}]})
AUDIT_PACKET = {"claim_version_id": CLAIM_VERSION_ID, "evidence_id": "q1.1.e1", "excerpt_hash": LOCATOR.quote_hash, "source_class": "primary_official", "url": URL}
add("auditor.evidence", "forward:q1.1.e1", AUDIT_PACKET, {"verdict": "supported", "reason": "原文直接包含官方声明", "source_class_verified": True})
add("auditor.counter_entailment", "reverse:q1.1.e1", AUDIT_PACKET, {"verdict": "no_objection", "reason": "未将官方声明写成独立验证"})
add("auditor.search_bias", "search-bias", {"question_ids": ["R1"], "query_ids": ["q1"]}, {"review_status": "pass", "source_classes_seen": ["primary_official"], "query_families_seen": ["primary"], "blind_spots": []})
add("auditor.coverage", "coverage:R1", {"requirement_id": "R1", "passed_check_ids": ["R1.attribution", "R1.support"], "missing_check_ids": []}, {"requirement_id": "R1", "disposition": "satisfied", "passed_check_ids": ["R1.attribution", "R1.support"], "missing_check_ids": [], "claim_version_ids": [CLAIM_VERSION_ID], "investigation_refs": []})
add("writer.section", "write:0", {"section_id": "section-1", "section_name": "结论", "claim_version_ids": [CLAIM_VERSION_ID]}, {"section_id": "section-1", "revision": 0, "outline_version": 1, "paragraphs": [{"id": "p1", "sentences": [{"id": "s1", "text": "虚构产品 A 的官方资料声明支持导出 CSV 文件。", "fact_ids": ["f1"]}]}], "facts": [{"kind": "factual", "id": "f1", "requirement_ids": ["R1"], "claim_version_ids": [CLAIM_VERSION_ID], "evidence_ids": ["q1.1.e1"]}], "open_questions": []})
add("writer.section", "write:1", {"section_id": "section-2", "section_name": "依据与限制", "claim_version_ids": [CLAIM_VERSION_ID]}, {"section_id": "section-2", "revision": 0, "outline_version": 1, "paragraphs": [{"id": "p2", "sentences": [{"id": "s2", "text": "这是一项官方声明，不能据此认定该功能已获独立验证。", "fact_ids": []}]}], "facts": [], "open_questions": []})
REPORT = "\n\n".join(["## 结论", "虚构产品 A 的官方资料声明支持导出 CSV 文件。 [^q1.1.e1]", "## 依据与限制", "这是一项官方声明，不能据此认定该功能已获独立验证。", f"[^q1.1.e1]: local snapshot q1.1, text SHA-256 {LOCATOR.text_blob_hash}, span {LOCATOR.start}:{LOCATOR.end}"]) + "\n"
REPORT_HASH = hashlib.sha256(REPORT.encode()).hexdigest()
add("auditor.report", "report", {"report_hash": REPORT_HASH, "fact_ids": ["f1"], "section_ids": ["section-1", "section-2"]}, {"report_hash": REPORT_HASH, "findings": [], "checked_fact_ids": ["f1"], "checked_section_ids": ["section-1", "section-2"], "answered_requirement_ids": ["R1"]})
manifest = {"schema_version": 1, "fixed_start": "2026-09-01T00:00:00Z", "namespace_seed": SEED, "sources": {URL: {"path": "sources/official.html", "mime": "text/html", "sha256": hashlib.sha256(RAW).hexdigest()}}, "roles": ROLES}
(ROOT / "fixture_manifest.json").write_bytes(canonical(manifest))
print(REPORT_HASH)

from scripts.reindex_research_fixture import reindex
reindex(ROOT)
