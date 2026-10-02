"""Serial replay/assisted controller; persist external results before domain commit."""
from __future__ import annotations

import asyncio
import json
import uuid
import time
from pathlib import Path
from typing import TypeVar

from pydantic import TypeAdapter

from .budget import BudgetManager
from .extract import locate, parse_document, verify_locator
from .jsonio import canonical, digest, loads
from .models import (
    BoundedUnknownFact, CandidateEvidenceBundle, CounterAuditVerdict,
    CoverageAssessment, CoverageProposal, DraftSection, EvidenceAuditVerdict,
    FetchRequest, FactualFact, QuestionSpaceAudit,
    QuerySpec, ReportAudit, ResearchContract, RoleRequest, SearchBatch, FetchResult,
    SearchAction, SearchBiasVerdict, StopDecision, GateResult, OutlineState,
)
from .policy import QueryOutcome, challenge_satisfied, coverage_gates
from .prompts import template as role_template
from .replay import ReplayFetchProvider, ReplayRoleBackend, ReplaySearchProvider
from .storage import Store
from .outline import ordered_nodes
from .loop import ResearchLoopMixin, ResearchStopped, ResearchReopened
from .reuse_runtime import ReuseRuntimeMixin
from .storage import now
from .visual_runtime import VisualRuntimeMixin


class IncompleteResearch(RuntimeError):
    pass


T = TypeVar("T")


class Controller(VisualRuntimeMixin, ReuseRuntimeMixin, ResearchLoopMixin):
    def __init__(self, store: Store, contract: ResearchContract, fixture_dir: Path | None, max_calls: int, pools: list[int], *, external_providers: tuple | None = None, max_rounds: int = 20, max_duration_seconds: int = 3600, max_no_progress_actions: int = 3, saturation_effective_rounds: int = 3, max_revision_cycles: int = 2, max_output_tokens: int = 4096, api_model_name: str | None = None, research_config=None):
        self.max_output_tokens = max_output_tokens
        self.api_model_name = api_model_name
        self.clock = now
        self.source_validator = None
        self.max_rounds = max_rounds
        self.max_duration_seconds = max_duration_seconds
        self.max_no_progress_actions = max_no_progress_actions
        self.saturation_effective_rounds = saturation_effective_rounds
        self.max_revision_cycles = max_revision_cycles
        self.last_tick = time.monotonic()
        self.store = store
        self.contract = contract
        self.fixture_dir = fixture_dir
        self.assisted = external_providers is not None
        if external_providers is not None:
            self.roles, self.search, self.fetch = external_providers
        else:
            self.roles = ReplayRoleBackend(fixture_dir)
            self.search = ReplaySearchProvider(fixture_dir)
            self.fetch = ReplayFetchProvider(fixture_dir)
            from .jsonio import load
            from .models import utc
            fixed = load(fixture_dir / 'fixture_manifest.json').get('fixed_start')
            if fixed:
                self.clock = lambda: utc(fixed)
        self.budget = BudgetManager(store, max_calls, pools)
        self.owner = str(uuid.uuid4())
        self.generation = store.acquire(self.owner)
        from .config import Visual, Extraction
        self.visual_config = research_config.visual if research_config else Visual()
        self.extraction_config = research_config.extraction if research_config else Extraction()

    def close(self) -> None:
        self.tick(enforce=False)
        self.store.release(self.owner, self.generation)

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            self.store.heartbeat(self.owner, self.generation)
            self.tick(enforce=False)

    def record_stop(self, outcome: str, reason: str) -> None:
        version = self.store.run()["state_version"]
        unresolved = [r['id'] for r in self.heads('coverage') if r['payload']['disposition'] not in {'satisfied','bounded_unknown'}]
        unresolved += [r['id'] for r in self.open_report_gaps()]
        stop = StopDecision(outcome=outcome, evaluated_state_version=version, terminal_state_version=version + 1, input_manifest_hash=digest({"outcome": outcome, "reason": reason, "state_version": version}), gate_results=[GateResult(code="execution", status="fail", reason=reason)], unresolved_ids=unresolved, budget_snapshot=self.usage_snapshot(), **self.stop_context())
        receipt = self.commit(f"stop:{outcome}:{version}", stop.model_dump(mode="json"), [("stop", f"stop:{version}", stop.model_dump(mode="json"))])
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            db.execute("UPDATE runs SET phase='terminal',lifecycle_status=?,research_outcome=?,current_stop_version_id=?", ("failed" if outcome == "failed" else "incomplete", outcome, receipt["refs"][0]["version_id"]))
        (self.store.run_dir / "stop.json").write_bytes(canonical(stop.model_dump(mode="json")))

    async def role(self, role: str, action_key: str, packet: dict[str, str | int | bool | list[str]], result_type: type[T] | TypeAdapter, *, image_hashes=None) -> T:
        self.tick()
        packet = dict(packet, contract_json=self.contract.model_dump_json())
        if image_hashes:
            packet['visual_image_hashes'] = list(image_hashes)
        images = []
        # A logical step owns its immutable request. Resume uses that exact packet,
        # even if a crash occurred after part of the step committed its results.
        request_dir = self.store.run_dir / 'role-inputs'
        request_dir.mkdir(exist_ok=True)
        request_path = request_dir / (digest({'role': role, 'key': action_key}) + '.json')
        from .jsonio import load
        if request_path.exists():
            saved = load(request_path, max_bytes=4_000_000)
            if saved['role'] != role or saved['key'] != action_key or saved['contract_version'] != self.contract.version:
                raise RuntimeError('saved role input identity mismatch')
            if ResearchContract.model_validate_json(saved['packet'].get('contract_json', '{}')) != self.contract:
                raise RuntimeError('saved role input contract changed without a new version')
            packet = saved['packet']
        else:
            import os
            temp = request_path.with_suffix('.tmp')
            temp.write_bytes(canonical({'role': role, 'key': action_key, 'contract_version': self.contract.version, 'packet': packet}))
            os.replace(temp, request_path)
        if packet.get('visual_image_hashes'):
            from .visual_runtime import role_images
            images = role_images(self.store, packet['visual_image_hashes'])
        manifest_hash = digest({"role": role, "logical_action_key": action_key, "contract_version": self.contract.version, "packet": packet})
        action_id = f"role:{action_key}"
        row = self.store.db.execute("SELECT result_json,input_manifest_hash FROM actions WHERE action_id=?", (action_id,)).fetchone()
        if row and row['input_manifest_hash'] != manifest_hash:
            raise RuntimeError('cached role input manifest mismatch')
        if row and row["result_json"]:
            raw_response = json.loads(row["result_json"])
        else:
            template = f"{role}.v1"
            template_hash = digest(role_template(role))
            request = RoleRequest(role=role, logical_action_key=action_key, input_manifest_hash=manifest_hash, contract_version=self.contract.version, policy_version=1, system_template_id=template, system_template_hash=template_hash, data_packet=packet, response_schema_id=getattr(result_type, "__name__", "ResearchAction"), response_schema_version=1, max_output_tokens=self.max_output_tokens, images=images)
            attempt = self.budget.reserve(action_id, "role", manifest_hash, "report_audit" if role == "auditor.report" else "writing" if role == "writer.section" else "research", self.owner, self.generation)
            response = None
            try:
                from .context import ContextBuilder
                ContextBuilder.save(self.store, request, attempt, self.owner, self.generation)
                response = await self.roles.generate(request)
                raw_response = loads(response.raw_text)
                self.budget.settle(attempt, response.usage)
                with self.store.transaction() as db:
                    self.store.check_lease(db, self.owner, self.generation)
                    db.execute("UPDATE actions SET result_json=?,result_hash=? WHERE action_id=?", (canonical(raw_response).decode(), digest(raw_response), action_id))
            except Exception as exc:
                from .models import Usage
                self.budget.settle(attempt, response.usage if response is not None else getattr(exc, 'usage', None) or Usage(basis="unknown"), error=type(exc).__name__)
                raise
        if isinstance(result_type, TypeAdapter):
            return result_type.validate_python(raw_response, strict=True)
        return result_type.model_validate(raw_response, strict=True)

    def commit(self, key: str, payload: object, entities: list[tuple[str, str, object]] = ()) -> dict[str, object]:
        return self.store.commit_proposal(key, key, self.store.run()["state_version"], digest(payload), payload, self.owner, self.generation, entities)

    def phase(self, value: str) -> None:
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            db.execute("UPDATE runs SET phase=?, lifecycle_status='active', research_outcome=NULL, current_stop_version_id=NULL", (value,))

    async def research_action(self, action: SearchAction, outline: OutlineState) -> None:
        requirements = [r.id for r in self.contract.requirements]
        if not set(q.question_id for q in action.query_plan) <= set(outline.question_ids):
            raise IncompleteResearch("unknown question in search action")
        outcomes: list[QueryOutcome] = []
        hits = []
        for query in action.query_plan:
            self.commit(f"query-definition:{query.query_id}", query.model_dump(mode="json"), [("query", query.query_id, query.model_dump(mode="json"))])
            action_id = f"search:{query.query_id}"
            row = self.store.db.execute("SELECT result_json FROM actions WHERE action_id=? AND state IN ('result_saved','committed')", (action_id,)).fetchone()
            if row and row["result_json"]:
                batch = SearchBatch.model_validate(json.loads(row["result_json"]), strict=True)
            else:
                attempt = self.budget.reserve(action_id, "search", digest(query.model_dump(mode="json")), "research", self.owner, self.generation)
                try:
                    batch = await self.search.search(query)
                    from .models import Usage
                    self.budget.settle(attempt, batch.usage, error=batch.error if batch.status == "error" else None)
                    with self.store.transaction() as db:
                        db.execute("UPDATE actions SET state='result_saved',result_json=?,result_hash=? WHERE action_id=?", (batch.model_dump_json(), digest(batch.model_dump(mode="json")), action_id))
                except Exception as exc:
                    from .models import Usage
                    self.budget.settle(attempt, Usage(basis="unknown"), error=type(exc).__name__)
                    raise
            outcomes.append(QueryOutcome(query.strategy_family, "failed" if batch.status == "error" else batch.status))
            self.commit(f"search:{query.query_id}", batch.model_dump(mode="json"), [("query_result", query.query_id, batch.model_dump(mode="json"))])
            hits.extend(batch.hits)

        evidence_by_requirement: dict[str, list[dict[str, object]]] = {rid: [] for rid in requirements}
        for hit in hits:
            action_id = f"fetch:{hit.hit_id}"
            row = self.store.db.execute("SELECT result_json FROM actions WHERE action_id=? AND state IN ('result_saved','committed')", (action_id,)).fetchone()
            if row and row["result_json"]:
                fetched = FetchResult.model_validate(json.loads(row["result_json"]), strict=True)
                raw = self.store.read_blob(fetched.blob_hash)
            else:
                attempt = self.budget.reserve(action_id, "fetch", digest({"url": hit.url}), "research", self.owner, self.generation)
                try:
                    fetched, raw = await self.fetch.fetch(FetchRequest(source_id=hit.hit_id, url=hit.url))
                    from .models import Usage
                    self.budget.settle(attempt, Usage(basis="unknown"))
                    self.store.put_blob(raw)
                    with self.store.transaction() as db:
                        db.execute("UPDATE actions SET state='result_saved',result_json=?,result_hash=? WHERE action_id=?", (fetched.model_dump_json(), digest(fetched.model_dump(mode="json")), action_id))
                except Exception as exc:
                    from .models import Usage
                    self.budget.settle(attempt, Usage(basis="unknown"), error=type(exc).__name__)
                    raise
            raw_hash = self.store.put_blob(raw)
            parsed, text, document = await self.parse_source(raw, fetched.mime or "")
            text_hash = self.store.put_blob(text.encode("utf-8"))
            if raw_hash != fetched.blob_hash or text_hash != parsed.text_blob_hash:
                raise RuntimeError("snapshot hash mismatch")
            packet = {"source_id": hit.hit_id, "text_blob_hash": text_hash, "title": hit.title}
            packet.update(document_text=text, source_url=hit.url)
            bundle = await self.role("researcher.extract", f"extract:{hit.hit_id}", packet, CandidateEvidenceBundle)
            snapshot = {"source_id": hit.hit_id, "url": hit.url, "raw_hash": raw_hash, "text_hash": text_hash, "fetched_at": fetched.fetched_at, "parser": parsed.parser_id}
            if hit.published_at:
                snapshot['published_at'] = hit.published_at
            self.commit(f"snapshot:{hit.hit_id}", snapshot, [("source", hit.hit_id, {"url": hit.url}), ("snapshot", hit.hit_id, snapshot)])
            for index, candidate in enumerate(bundle.candidates):
                if not set(candidate.requirement_ids) <= set(requirements):
                    raise RuntimeError("candidate refers to unknown requirement")
                locator = locate(text, candidate.excerpt)
                if not verify_locator(text, locator, candidate.excerpt):
                    raise RuntimeError("invalid evidence locator")
                evidence_id = f"{hit.hit_id}.e{index+1}"
                evidence = {"snapshot_id": hit.hit_id, "excerpt": candidate.excerpt, "locator": locator.model_dump(mode="json"), "source_class": candidate.source_class, "observation_root": candidate.observation_root, "validity": "active"}
                if document is not None:
                    evidence['observation_root'] = raw_hash
                claim = {"text": candidate.claim_text, "kind": candidate.claim_kind, "requirement_ids": candidate.requirement_ids, "validity": "current"}
                receipt = self.commit(f"candidate:{evidence_id}", {"claim": claim, "evidence": evidence}, [("evidence", evidence_id, evidence), ("claim", candidate.claim_id, claim)])
                refs = {item["kind"]: item for item in receipt["refs"]}
                audit_packet = {"claim_version_id": refs["claim"]["version_id"], "evidence_id": evidence_id, "excerpt_hash": locator.quote_hash, "source_class": candidate.source_class, "url": hit.url}
                audit_packet.update(claim_text=candidate.claim_text, excerpt=candidate.excerpt,
                    source_context=text[max(0, locator.start-1500):locator.end+1500])
                forward = await self.role("auditor.evidence", f"forward:{evidence_id}", audit_packet, EvidenceAuditVerdict)
                reverse = await self.role("auditor.counter_entailment", f"reverse:{evidence_id}", audit_packet, CounterAuditVerdict)
                audit_payload = {"forward": forward.model_dump(), "reverse": reverse.model_dump(), "input_manifest_hash": digest(audit_packet)}
                audit_receipt = self.commit(f"audit:{evidence_id}", audit_payload, [("audit", evidence_id, audit_payload)])
                qualified = forward.verdict == "supported" and reverse.verdict == "no_objection"
                if qualified:
                    from .models import EntityRef
                    self.store.add_link(f"link:{evidence_id}", EntityRef.model_validate(refs["claim"]), EntityRef.model_validate(refs["evidence"]), EntityRef.model_validate(audit_receipt["refs"][0]), "supports", self.owner, self.generation)
                for rid in candidate.requirement_ids:
                    evidence_by_requirement[rid].append({"claim_ref": refs["claim"], "evidence_ref": refs["evidence"], "source_class": candidate.source_class, "source_class_verified": forward.source_class_verified, "kind": candidate.claim_kind, "qualified": qualified})
            if document is not None:
                await self.research_figures(action, hit, document)
            elif self.visual_config.enabled and fetched.mime == 'text/html':
                from .visual_extract import discover_html_figures
                figures = discover_html_figures(raw, hit.url)
                if figures:
                    requirements_for_action = [r.id for r in self.contract.requirements if any(
                        check.check_id in action.acceptance_check_ids for check in r.acceptance_checks)]
                    self.visual_gap('html:' + raw_hash, requirements_for_action,
                                    'requires_validated_image_fetch: ' + canonical(figures).decode())


    async def assess(self, outline: OutlineState, question_audit: QuestionSpaceAudit):
        contract = self.contract
        evidence_by_requirement = self.evidence_rows()
        queries = [QuerySpec.model_validate(row['payload']) for row in self.heads('query')]
        batches = {row['id']: row['payload'] for row in self.heads('query_result')}
        outcomes = [QueryOutcome(q.strategy_family, 'failed' if batches[q.query_id]['status'] == 'error' else batches[q.query_id]['status']) for q in queries]
        token = digest({'outline': outline.model_dump(mode='json'), 'research': self.research_digest()})
        bias = await self.role("auditor.search_bias", f"search-bias:{token}", {"question_ids": outline.question_ids, "query_ids": [q.query_id for q in queries], "research_digest_json": canonical(self.research_digest()).decode()}, SearchBiasVerdict)
        if not set(bias.query_families_seen) <= {q.strategy_family for q, outcome in zip(queries, outcomes) if outcome.status in {"hit", "empty"}}:
            raise RuntimeError("search bias audit invents a query family")
        if not set(bias.source_classes_seen) <= {str(row["source_class"]) for rows in evidence_by_requirement.values() for row in rows}:
            raise RuntimeError("search bias audit invents a source class")
        self.commit(f"search-bias-audit:{token}", bias.model_dump(mode="json"), [("audit", "search-bias", bias.model_dump(mode="json"))])
        assessments = []
        assessment_refs: dict[str, str] = {}
        for requirement in contract.requirements:
            rows = evidence_by_requirement[requirement.id]
            passed: set[str] = set()
            for check in requirement.acceptance_checks:
                if check.stage != "research":
                    continue
                if check.code == "has_current_support":
                    matched = [r for r in rows if r["qualified"] and r["kind"] in check.params.claim_kinds]
                    if len({r["claim_ref"]["version_id"] for r in matched}) >= check.params.min_claims:
                        passed.add(check.check_id)
                elif check.code == "source_attribution":
                    if any(r["qualified"] and r["source_class_verified"] and r["source_class"] == check.params.required_class for r in rows):
                        passed.add(check.check_id)
                elif check.code == "challenge_attempted":
                    if challenge_satisfied([o for q, o in zip(queries, outcomes) if q.question_id == requirement.id], set(check.params.families), check.params.min_successful_queries):
                        passed.add(check.check_id)
                elif check.code == 'freshness':
                    from .models import utc
                    for evidence_row in rows:
                        evidence = self.head('evidence', evidence_row['evidence_ref']['id'])
                        snapshot = self.head('snapshot', evidence['payload']['snapshot_id'])
                        basis = snapshot['payload'].get(check.params.basis)
                        if basis and 0 <= (utc(contract.as_of) - utc(basis)).total_seconds() <= check.params.max_age_days * 86400:
                            passed.add(check.check_id)
                            break
            research_checks = {c.check_id for c in requirement.acceptance_checks if c.stage == "research"}
            visual_gaps = [g for g in self.heads('visual_gap') if g['payload']['status'] == 'open'
                           and requirement.id in g['payload']['requirement_ids']]
            if visual_gaps:
                # Unread figures must not be hidden by other text evidence.
                passed -= {c.check_id for c in requirement.acceptance_checks if c.code == 'has_current_support'}
            proposal = await self.role("auditor.coverage", f"coverage:{requirement.id}:{token}", {"requirement_id": requirement.id, "passed_check_ids": sorted(passed), "missing_check_ids": sorted(research_checks - passed), "research_digest_json": canonical(self.research_digest()).decode()}, CoverageProposal)
            if proposal.requirement_id != requirement.id or set(proposal.passed_check_ids) != passed or set(proposal.missing_check_ids) != research_checks - passed:
                raise RuntimeError("coverage proposal contradicts deterministic checks")
            qualified_claim_ids = {str(row["claim_ref"]["version_id"]) for row in rows if row["qualified"]}
            if not set(proposal.claim_version_ids) <= qualified_claim_ids:
                raise RuntimeError("coverage refers to unsupported claim")
            if proposal.disposition == "satisfied" and passed != research_checks:
                raise RuntimeError("unsupported coverage claim")
            if proposal.disposition == 'satisfied' and visual_gaps:
                raise RuntimeError('unresolved visual reading obligation')
            if proposal.disposition == "satisfied":
                for check in requirement.acceptance_checks:
                    if check.code == 'has_current_support' and len(set(proposal.claim_version_ids)) < check.params.min_claims:
                        raise RuntimeError('coverage omits supporting claim versions')
            valid_investigations = {q.query_id for q in queries if q.question_id == requirement.id and batches[q.query_id]['status'] in {'hit','empty'}}
            if not set(proposal.investigation_refs) <= valid_investigations:
                raise RuntimeError('coverage invents investigation references')
            if proposal.disposition == "bounded_unknown" and (not requirement.allow_unknown or not set(requirement.unknown_check_ids) <= passed or not proposal.investigation_refs):
                raise RuntimeError("unsupported bounded unknown")
            for gap_id in proposal.resolved_gap_ids:
                gap = self.head('report_gap',gap_id)
                if not gap or gap['payload']['status']!='open' or requirement.id not in gap['payload']['requirement_ids']:
                    raise RuntimeError('coverage resolves an unknown or unrelated report gap')
                signatures = {digest({'text':' '.join(r['payload']['text'].split()).casefold(),'kind':r['payload']['kind']}) for r in self.heads('claim') if r['version_id'] in qualified_claim_ids}
                new_support = signatures - set(gap['payload']['claim_signatures'])
                investigated = set(proposal.investigation_refs) - set(gap['payload']['query_ids'])
                if not (proposal.disposition=='satisfied' and new_support) and not (proposal.disposition=='bounded_unknown' and investigated):
                    raise RuntimeError('report gap needs new audited support or an accepted new investigation')
                resolved_requirements = set(gap['payload'].get('resolved_requirement_ids',[])) | {requirement.id}
                resolved = dict(gap['payload'], status='resolved' if set(gap['payload']['requirement_ids']) <= resolved_requirements else 'open',
                    resolved_requirement_ids=sorted(resolved_requirements), resolved_by=f'coverage:{requirement.id}:{token}')
                self.commit(f'resolve-gap:{gap_id}:{requirement.id}:{token}',resolved,[('report_gap',gap_id,resolved)])
            assessment = CoverageAssessment(requirement_id=requirement.id, stage="research", contract_version=contract.version, input_manifest_hash=digest({"requirement": requirement.id, "passed": sorted(passed), "research": token}), disposition=proposal.disposition, passed_check_ids=sorted(passed), missing_check_ids=sorted(research_checks - passed), claim_version_ids=proposal.claim_version_ids, audit_id=f"coverage:{requirement.id}:{token}")
            receipt = self.commit(f"coverage:{requirement.id}:{token}", assessment.model_dump(mode="json"), [("coverage", requirement.id, assessment.model_dump(mode="json"))])
            assessment_refs[requirement.id] = receipt["refs"][0]["version_id"]
            assessments.append(assessment)

        gates = coverage_gates(contract, assessments, question_audit_passed=question_audit.review_status == "pass", search_bias_passed=bias.review_status == "pass", coverage_audit_passed=len(assessments) == len(contract.requirements))
        return assessments, assessment_refs, bias, gates

    async def write_report(self, outline: OutlineState, assessments, assessment_refs) -> str:
        contract = self.contract
        freeze = self.head('freeze','outline')['payload']
        if freeze['outline_version'] != outline.outline_version or set(freeze['coverage_refs']) != set(assessment_refs.values()):
            raise RuntimeError('writing input does not match frozen research versions')
        evidence_by_requirement = self.evidence_rows()
        self.phase("writing")
        sections = []
        for index, node in enumerate(ordered_nodes(outline)):
            section_name = node.title
            revision = self.store.db.execute('SELECT COALESCE(MAX(revision),0) FROM revision_jobs WHERE section_id=? AND contract_version=?', (node.id,contract.version)).fetchone()[0]
            material = self.section_material(node)
            allowed_claims = {r['version_id'] for r in material['claims']}
            allowed_evidence = {r['id'] for r in material['evidence']}
            previous = self.head('draft',node.id)
            if previous and previous['payload']['outline_version']==outline.outline_version and previous['payload']['revision']==revision:
                section = DraftSection.model_validate(previous['payload'])
            else:
                key = f"write:{outline.outline_version}:{index}" + (f":revision:{revision}" if revision else '')
                if self.store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone():
                    key += ':material:' + digest(material)
                feedback = self.head('audit','report')
                packet = {"section_id": node.id, "section_name": section_name, "outline_version": outline.outline_version, "revision": revision,
                    "node_json": node.model_dump_json(), "section_material_json": canonical(material).decode(), "claim_version_ids": sorted(allowed_claims)}
                if revision:
                    packet['report_feedback_json'] = canonical(feedback['payload'] if feedback else {}).decode()
                section = await self.role("writer.section", key, packet, DraftSection)
            if section.section_id != node.id or section.outline_version != outline.outline_version or section.revision != revision:
                raise RuntimeError("writer section mismatch")
            for fact in section.facts:
                if isinstance(fact, FactualFact):
                    if not set(fact.claim_version_ids) <= allowed_claims or not set(fact.evidence_ids) <= allowed_evidence:
                        raise RuntimeError('writer cites evidence outside its section material')
                    for claim_id in fact.claim_version_ids:
                        linked = {r['evidence_ref']['id'] for rows in evidence_by_requirement.values() for r in rows if r['claim_ref']['version_id']==claim_id}
                        if not linked.intersection(fact.evidence_ids):
                            raise RuntimeError('writer claim/evidence citation pair is not supported')
            draft_key = f"draft:{outline.outline_version}:{section.section_id}:{revision}"
            if self.store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone():
                draft_key += ':' + digest(section.model_dump(mode='json'))
            self.commit(draft_key, section.model_dump(mode="json"), [("draft", section.section_id, section.model_dump(mode="json"))])
            sections.append((section_name, section))
        valid_claim_ids = {str(r["claim_ref"]["version_id"]) for rows in evidence_by_requirement.values() for r in rows if r["qualified"]}
        valid_evidence_ids = {str(r["evidence_ref"]["id"]) for rows in evidence_by_requirement.values() for r in rows if r["qualified"]}
        for _, section in sections:
            fact_ids = {fact.id for fact in section.facts}
            for paragraph in section.paragraphs:
                for sentence in paragraph.sentences:
                    if not set(sentence.fact_ids) <= fact_ids:
                        raise RuntimeError("sentence cites missing fact")
            for fact in section.facts:
                if isinstance(fact, FactualFact) and (not set(fact.claim_version_ids) <= valid_claim_ids or not set(fact.evidence_ids) <= valid_evidence_ids):
                    raise RuntimeError("draft cites unapproved evidence")
                if isinstance(fact, BoundedUnknownFact) and fact.coverage_assessment_version_id != assessment_refs.get(fact.requirement_ids[0]):
                    raise RuntimeError("draft cites wrong unknown assessment")
        lines = []
        if self.assisted:
            provenance = (
                f"> 运行方式：由 {self.api_model_name} API 承担规划、提取、审核与写作；搜索与网页抓取由外部辅助通道提供。各角色使用分阶段数据包调用同一模型，不构成独立模型交叉审核。"
                if self.api_model_name else
                "> 运行方式：由当前 Codex 会话承担各角色推理和资料检索，未配置模型或搜索 API key。各角色基于分阶段数据包审阅，但未进行独立会话或独立模型交叉审核。本报告是有人工监督的研究运行，不构成自动联网后端的验收。"
            )
            lines.extend([f"# {contract.question}",
                provenance,
                f"范围与信息截止：{contract.as_of}；官方资料解读，不包含本地性能实测。"])
        for title, section in sections:
            nodes = {n.id:n for n in outline.nodes}
            depth, parent = 2, nodes[section.section_id].parent_id
            while parent is not None:
                depth += 1
                parent = nodes[parent].parent_id
            lines.append(f"{'#' * min(depth,6)} {title}")
            for paragraph in section.paragraphs:
                sentences = []
                for sentence in paragraph.sentences:
                    refs = []
                    for fact in section.facts:
                        if fact.id in sentence.fact_ids and isinstance(fact, FactualFact):
                            refs.extend(fact.evidence_ids)
                    investigations = [ref for fact in section.facts if fact.id in sentence.fact_ids and isinstance(fact, BoundedUnknownFact) for ref in fact.investigation_refs]
                    suffix = "".join(f" [^{ref}]" for ref in sorted(set(refs))) + "".join(f" [调查记录:{ref}]" for ref in sorted(set(investigations)))
                    sentences.append(sentence.text + suffix)
                lines.append("".join(sentences))
        for evidence_id in sorted(valid_evidence_ids):
            row = self.store.db.execute("SELECT payload_json FROM entity_versions WHERE kind='evidence' AND id=? ORDER BY version DESC LIMIT 1", (evidence_id,)).fetchone()
            evidence = json.loads(row[0])
            if evidence['locator']['kind'] == 'pdf_region':
                from .visual_runtime import verify_visual_evidence
                artifact, _ = verify_visual_evidence(self.store, evidence)
                citation = (f"PDF SHA-256 {artifact.snapshot_ref}, "
                    f"pages {sorted({r.page for r in artifact.page_regions})}, {artifact.figure_label}; "
                    + '; '.join(evidence['observation']['conditions']) + '; '
                    + ' '.join(f'[图像预览](blobs/{i.blob_hash})' for i in artifact.images))
            else:
                citation = f"local snapshot {evidence['snapshot_id']}, text SHA-256 {evidence['locator']['text_blob_hash']}, span {evidence['locator']['start']}:{evidence['locator']['end']}"
            if self.assisted:
                snapshot_row = self.store.db.execute("SELECT payload_json FROM entity_versions WHERE kind='snapshot' AND id=? ORDER BY version DESC LIMIT 1", (evidence['snapshot_id'],)).fetchone()
                snapshot = json.loads(snapshot_row[0])
                citation = f"[原始资料]({snapshot['url']})；[本地原始快照](blobs/{snapshot['raw_hash']})；" + citation
            lines.append(f"[^{evidence_id}]: {citation}")
        report = "\n\n".join(lines) + "\n"
        if len(report) > contract.output_contract.max_characters:
            raise RuntimeError("report exceeds contract length")
        report_hash = self.store.put_blob(report.encode("utf-8"))
        self.phase("report_auditing")
        report_packet = {"report_hash": report_hash, "fact_ids": [f.id for _, s in sections for f in s.facts], "section_ids": [s.section_id for _, s in sections]}
        report_packet["report_markdown"] = report
        report_packet["sections_json"] = canonical([section.model_dump(mode="json") for _, section in sections]).decode()
        report_packet["research_digest_json"] = canonical(self.research_digest()).decode()
        revision_manifest = digest([(s.section_id,s.revision) for _,s in sections])
        audit_key = f"report:{outline.outline_version}:{revision_manifest}"
        if self.store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone():
            audit_key += ':reuse:' + digest(report_packet)
        report_audit = await self.role("auditor.report", audit_key, report_packet, ReportAudit)
        if report_audit.report_hash != report_hash or set(report_audit.checked_fact_ids) != {f.id for _, s in sections for f in s.facts} or set(report_audit.checked_section_ids) != {s.section_id for _, s in sections}:
            raise IncompleteResearch("report audit failed")
        self.commit(f"report-audit:{audit_key}", report_audit.model_dump(mode="json"), [("audit", "report", report_audit.model_dump(mode="json"))])
        if report_audit.findings:
            affected = set()
            reopen = False
            known_sections = {s.section_id for _,s in sections}
            for finding in report_audit.findings:
                if finding.repair_kind in {'research','collect_evidence','new_evidence','revise_outline'}:
                    reopen = True
                matches = {s.section_id for _,s in sections if finding.fact_id in {f.id for f in s.facts}}
                matches |= set(finding.refs) & known_sections
                affected |= matches or known_sections
            revisions = {s.section_id:s.revision for _,s in sections}
            if any(revisions[sid] >= self.max_revision_cycles for sid in affected):
                raise ResearchStopped('incomplete_report_audit_loop','section_revision_limit')
            with self.store.transaction() as db:
                self.store.check_lease(db,self.owner,self.generation)
                for sid in sorted(affected):
                    job_id = digest({'section':sid,'contract':contract.version,'audit':audit_key})
                    db.execute('INSERT OR IGNORE INTO revision_jobs VALUES(?,?,?,?,?)',
                        (job_id,sid,contract.version,audit_key,revisions[sid]+1))
            if reopen:
                signatures = sorted({digest({'text':' '.join(r['payload']['text'].split()).casefold(),'kind':r['payload']['kind']}) for r in self.heads('claim')})
                requirements = sorted({rid for n in outline.nodes if n.id in affected for rid in n.requirement_ids})
                gap_id = 'report-gap:' + digest(audit_key)
                gap = dict(status='open', requirement_ids=requirements, section_ids=sorted(affected),
                    planner_step=self.head('loop','controller')['payload']['step'],
                    findings=[f.model_dump(mode='json') for f in report_audit.findings],
                    claim_signatures=signatures, query_ids=[q['id'] for q in self.heads('query')])
                self.commit(f'open-gap:{gap_id}',gap,[('report_gap',gap_id,gap)])
                with self.store.transaction() as db:
                    self.store.check_lease(db,self.owner,self.generation)
                    for sid in affected:
                        db.execute("UPDATE entity_heads SET validity='stale' WHERE kind='draft' AND id=?",(sid,))
                    db.execute("UPDATE entity_heads SET validity='stale' WHERE kind='freeze' AND id='outline'")
                raise ResearchReopened('report audit reopened research: '+gap_id)
            return await self.write_report(outline,assessments,assessment_refs)
        final_gates = coverage_gates(contract, assessments, question_audit_passed=freeze['question_space_passed'], search_bias_passed=freeze['search_bias_passed'], coverage_audit_passed=freeze['coverage_audit_passed'], report_audit_passed=True, answered_requirement_ids=set(report_audit.answered_requirement_ids))
        if not final_gates.delivery_ready:
            raise IncompleteResearch("delivery gate failed")
        outcome = "complete_with_limitations" if self.assisted or any(a.disposition == "bounded_unknown" for a in assessments) else "complete"
        version = self.store.run()["state_version"]
        self.assert_reuse_delivery()
        version = self.store.run()['state_version']
        stop = StopDecision(outcome=outcome, evaluated_state_version=version, terminal_state_version=version+1, input_manifest_hash=digest({"report_hash": report_hash, "state_version": version}), gate_results=[GateResult(code="research", status="pass", reason="all research gates passed"), GateResult(code="delivery", status="pass", reason="report audit passed")], unresolved_ids=[], budget_snapshot=self.usage_snapshot(), report_hash=report_hash, **self.stop_context())
        stop_key = 'stop:' + digest(stop.model_dump(mode='json')) if self.store.db.execute('SELECT 1 FROM reuse_imports LIMIT 1').fetchone() else 'stop'
        receipt = self.commit(stop_key, stop.model_dump(mode="json"), [("stop", "final", stop.model_dump(mode="json"))])
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            db.execute("UPDATE runs SET phase='terminal',lifecycle_status='complete',research_outcome=?,current_stop_version_id=?", (outcome, receipt["refs"][0]["version_id"]))
        (self.store.run_dir / "report.md").write_bytes(report.encode("utf-8"))
        (self.store.run_dir / "stop.json").write_bytes(canonical(stop.model_dump(mode="json")))
        from .export import export_artifacts, archive_delivery
        export_artifacts(self.store)
        archive_delivery(self.store)
        return outcome
