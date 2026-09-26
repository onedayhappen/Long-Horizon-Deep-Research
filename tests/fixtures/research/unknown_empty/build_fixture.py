"""Create hash-indexed responses for the fictional empty-search unknown path."""
import hashlib
import json
import uuid
from pathlib import Path

from lh_harness.research.jsonio import canonical, digest


ROOT = Path(__file__).parent
SEED = "84577ce2-0c06-499a-85c3-14ec8cc3dbb2"
ROLES = {}


def add(role, action, packet, output):
    key = f"{role}|{action}|{digest({'role': role, 'logical_action_key': action, 'contract_version': 1, 'packet': packet})}"
    path = f"roles/{len(ROLES)+1:02d}.json"
    ROLES[key] = path
    (ROOT / path).parent.mkdir(exist_ok=True)
    (ROOT / path).write_bytes(canonical({"raw_text": canonical(output).decode(), "finish_reason": "stop", "provider_request_id": None, "usage": {"input_tokens": 1, "output_tokens": 1, "cost": None, "basis": "provider"}, "model_profile_hash": "replay-fixture-v1"}))


question = "虚构系统 X 是否有独立性能测量？"
add("planner.initialize", "initialize", {"question": question, "requirement_ids": ["R1"]}, {"question_ids": ["R1"], "outline_sections": ["结论", "依据与限制"]})
add("auditor.question_space", "question_space", {"question": question, "question_ids": ["R1"]}, {"review_status": "pass", "dimensions_checked": ["独立测量", "反例", "边界"], "missing_questions": []})
add("planner.next", "search-1", {"requirement_ids": ["R1"], "question_ids": ["R1"]}, {"kind": "search", "question_id": "R1", "query_plan": [{"query_id": "q1", "question_id": "R1", "text": "虚构系统 X 独立性能测量", "strategy_family": "independent", "language": "zh-CN", "country_code": None, "source_class_targets": ["primary_study"], "max_results": 10}], "acceptance_check_ids": ["R1.support", "R1.challenge"], "max_external_calls": 10})
add("auditor.search_bias", "search-bias", {"question_ids": ["R1"], "query_ids": ["q1"]}, {"review_status": "pass", "source_classes_seen": [], "query_families_seen": ["independent"], "blind_spots": []})
add("auditor.coverage", "coverage:R1", {"requirement_id": "R1", "passed_check_ids": ["R1.challenge"], "missing_check_ids": ["R1.support"]}, {"requirement_id": "R1", "disposition": "bounded_unknown", "passed_check_ids": ["R1.challenge"], "missing_check_ids": ["R1.support"], "claim_version_ids": [], "investigation_refs": ["q1"]})
coverage_id = str(uuid.uuid5(uuid.UUID(SEED), "coverage:R1:1"))
add("writer.section", "write:0", {"section_id": "section-1", "section_name": "结论", "claim_version_ids": []}, {"section_id": "section-1", "revision": 0, "outline_version": 1, "paragraphs": [{"id": "p1", "sentences": [{"id": "s1", "text": "在约定的检索范围内，尚无足够证据判断虚构系统 X 是否有独立性能测量。", "fact_ids": ["f1"]}]}], "facts": [{"kind": "bounded_unknown", "id": "f1", "requirement_ids": ["R1"], "coverage_assessment_version_id": coverage_id, "investigation_refs": ["q1"]}], "open_questions": ["未来可扩大检索范围"]})
add("writer.section", "write:1", {"section_id": "section-2", "section_name": "依据与限制", "claim_version_ids": []}, {"section_id": "section-2", "revision": 0, "outline_version": 1, "paragraphs": [{"id": "p2", "sentences": [{"id": "s2", "text": "独立来源查询成功返回空结果；这不能证明测量不存在。", "fact_ids": []}]}], "facts": [], "open_questions": []})
report = "\n\n".join(["## 结论", "在约定的检索范围内，尚无足够证据判断虚构系统 X 是否有独立性能测量。 [调查记录:q1]", "## 依据与限制", "独立来源查询成功返回空结果；这不能证明测量不存在。"] ) + "\n"
report_hash = hashlib.sha256(report.encode()).hexdigest()
add("auditor.report", "report", {"report_hash": report_hash, "fact_ids": ["f1"], "section_ids": ["section-1", "section-2"]}, {"report_hash": report_hash, "findings": [], "checked_fact_ids": ["f1"], "checked_section_ids": ["section-1", "section-2"], "answered_requirement_ids": ["R1"]})
(ROOT / "fixture_manifest.json").write_bytes(canonical({"schema_version": 1, "fixed_start": "2026-09-01T00:00:00Z", "namespace_seed": SEED, "sources": {}, "roles": ROLES}))
print(report_hash)

from scripts.reindex_research_fixture import reindex
reindex(ROOT)
