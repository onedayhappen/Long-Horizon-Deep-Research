"""E8 integration with Controller roles, budgets, immutable entities and reuse."""
from __future__ import annotations

import base64
import asyncio
import importlib.metadata
import json
import re
import secrets
import struct

from .budget import BudgetDenied
from .jsonio import canonical, digest
from .models import EntityRef, ParsedBlock, ParsedDocument, RoleImage, EvidenceAuditVerdict, CounterAuditVerdict
from .visual_models import (DocumentMap, FigureArtifact, FigureReadPlan, RenderedImage, VisualAudit,
    VisualClaimProposal, VisualError, VisualLocator, VisionProbeResult, WorkerConfig, check_bbox)


def role_images(store, hashes):
    result = []
    for sha in hashes:
        data = store.read_blob(sha)
        if len(data) < 24 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise VisualError('invalid_image: expected preserved PNG')
        width, height = struct.unpack('>II', data[16:24])
        result.append(RoleImage(blob_hash=sha, data_base64=base64.b64encode(data).decode(), width=width, height=height))
    return result


def verify_visual_evidence(store, evidence):
    """Validate the exact Figure/DocumentMap/raw/image closure, including imported IDs."""
    from .reuse import entity
    locator = VisualLocator.model_validate(evidence['locator'])
    row = entity(store, locator.figure_version_id)
    if row['kind'] != 'figure':
        raise VisualError('invalid_visual_locator: wrong figure kind')
    artifact = FigureArtifact.model_validate(row['payload']['artifact'])
    document_row = entity(store, row['payload']['document_map_version_id'])
    if document_row['kind'] != 'document_map':
        raise VisualError('invalid_visual_locator: missing document map')
    document = DocumentMap.model_validate(document_row['payload']['document'])
    if (locator.raw_hash != artifact.snapshot_ref or locator.raw_hash != document.raw_hash
        or locator.image_blob_hashes != [i.blob_hash for i in artifact.images]
        or locator.page_regions != artifact.page_regions or locator.caption_refs != artifact.caption_refs):
        raise VisualError('invalid_visual_locator: image/region binding mismatch')
    if not set(artifact.caption_refs + artifact.mention_refs) <= {b.id for b in document.blocks}:
        raise VisualError('invalid_visual_locator: missing caption/body context')
    store.read_blob(locator.raw_hash)
    images = role_images(store, locator.image_blob_hashes)
    for expected, actual in zip(artifact.images, images):
        if (expected.width, expected.height) != (actual.width, actual.height):
            raise VisualError('invalid_visual_locator: image dimensions changed')
    pages = {r.page for r in artifact.page_regions}
    refs = set(artifact.caption_refs + artifact.mention_refs)
    pages.update(b.page for b in document.blocks if b.id in refs)
    context = [b.model_dump() for b in document.blocks if b.page in pages or b.id in refs]
    return artifact, context


class VisualRuntimeMixin:
    async def visual_role(self, role, key, packet, result_type, *, image_hashes=None, protected_keys=()):
        from pydantic import ValidationError
        try:
            return await self.role(role, key, packet, result_type, image_hashes=image_hashes)
        except (ValidationError, json.JSONDecodeError) as exc:
            error = str(exc)
        except ValueError as exc:
            if 'duplicate JSON key' not in str(exc):
                raise
            error = str(exc)
        repair_key = key + ':repair1'
        self.visual_reserve_roles([repair_key, *protected_keys])
        return await self.role(role, repair_key, dict(packet, validation_error=error[:2000]), result_type, image_hashes=image_hashes)
    def visual_reserve_roles(self, keys):
        missing = sum(not self.store.db.execute('SELECT 1 FROM actions WHERE action_id=? AND result_json IS NOT NULL',
                                               ('role:' + key,)).fetchone() for key in keys)
        self.visual_reserve(missing)
    def visual_reserve(self, count):
        self.tick()
        if self.budget.snapshot()['remaining']['research'] < count:
            raise BudgetDenied('visual_audit_reserve: Researcher and both audits need budget')
        if self.budget.action_scope:
            scope, limit = self.budget.action_scope
            used = sum(json.loads(row[0])['scope'] == scope for row in self.store.db.execute(
                "SELECT payload_json FROM events WHERE kind='action_budget_call'"))
            if used + count > limit:
                raise BudgetDenied('visual_audit_reserve: SearchAction call limit')

    async def visual_capability(self):
        if not self.visual_config.enabled:
            raise VisualError('visual_disabled: no text-only fallback')
        profile = getattr(self.roles, 'vision_profile', None)
        if profile is None:
            raise VisualError('unsupported_image_input: backend has no vision profile')
        from .prompts import template
        profile_data = {'profile': profile.model_dump(), 'visual': self.visual_config.model_dump(),
            'prompts': {role: digest(template(role)) for role in ('vision.probe', 'researcher.inspect_figures',
                'researcher.read_figure', 'auditor.visual', 'auditor.visual_counter')}}
        self.commit('visual-manifest', profile_data, [('visual_manifest', 'profile', profile_data)])
        if self.head('visual_capability', 'probe'):
            return
        from .visual_probe import challenge_png
        challenge = self.head('visual_challenge', 'probe')
        if challenge is None:
            self.visual_reserve(1)
            marker = str(secrets.randbelow(900000) + 100000)
            blob = self.store.put_blob(challenge_png(marker))
            data = {'marker': marker, 'image_hash': blob, 'profile_hash': digest(profile_data)}
            self.commit('visual-challenge', data, [('visual_challenge', 'probe', data)])
            challenge = self.head('visual_challenge', 'probe')
        data = challenge['payload']
        response = await self.role('vision.probe', 'visual-probe', {'goal': 'Transcribe the six digits in the attached image.'},
                                   VisionProbeResult, image_hashes=[data['image_hash']])
        if response.marker != data['marker']:
            raise VisualError('vision_capability_failed: actual-image probe failed')
        self.commit('visual-capability', data, [('visual_capability', 'probe', data)])

    async def parse_source(self, raw, mime):
        if not self.visual_config.enabled or mime != 'application/pdf':
            from .extract import parse_document
            parsed, text = parse_document(raw, mime, max_chars=self.extraction_config.max_text_chars,
                                          max_pdf_pages=self.extraction_config.max_pdf_pages)
            return parsed, text, None
        if len(raw) > self.extraction_config.max_input_bytes:
            raise VisualError('input_limit: PDF too large')
        raw_hash = self.store.put_blob(raw)
        document, _ = await self.visual_pdf_job(raw_hash, 'scan')
        document = DocumentMap.model_validate(document)
        blocks, pieces, cursor = [], [], 0
        for block in document.blocks:
            blocks.append(ParsedBlock(id=block.id, page=block.page, start=cursor, end=cursor + len(block.text)))
            pieces.append(block.text)
            cursor += len(block.text) + 1
        text = '\n'.join(pieces)
        sha = self.store.put_blob(text.encode())
        return ParsedDocument(parser_id='pymupdf-map', parser_version=document.parser_version,
                              text_blob_hash=sha, language=None, blocks=blocks), text, document

    async def visual_pdf_job(self, raw_hash, operation, **kwargs):
        from .visual_extract import pdf_job
        config = WorkerConfig(extraction=self.extraction_config, visual=self.visual_config)
        key = digest([raw_hash, operation, kwargs, config.model_dump(), importlib.metadata.version('pymupdf')])
        cached = self.store.db.execute('SELECT payload_json FROM visual_cache WHERE cache_key=?', (key,)).fetchone()
        self.store.read_blob(raw_hash)
        if cached:
            value = json.loads(cached[0])
            for image in value.get('images', []):
                self.store.read_blob(image['blob_hash'])
            return value, {}
        self.tick()
        elapsed = sum(json.loads(row[0])['seconds'] for row in self.store.db.execute(
            "SELECT payload_json FROM events WHERE kind='active_time'"))
        try:
            value, blobs = await asyncio.wait_for(pdf_job(self.store.blob_dir / raw_hash, raw_hash, config, operation, **kwargs),
                                                 timeout=max(0, self.max_duration_seconds - elapsed))
        except asyncio.TimeoutError as exc:
            from .loop import ResearchStopped
            raise ResearchStopped('incomplete_budget', 'time_limit') from exc
        self.tick()
        for data in blobs.values():
            self.store.put_blob(data)
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            db.execute('INSERT OR IGNORE INTO visual_cache VALUES(?,?)', (key, canonical(value).decode()))
        return value, blobs

    def visual_dependency(self, parents, child, reason):
        with self.store.transaction() as db:
            self.store.check_lease(db, self.owner, self.generation)
            for parent in parents:
                db.execute('INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?)', (parent, child, reason))

    def visual_gap(self, key, requirement_ids, reason, status='open'):
        payload = {'requirement_ids': requirement_ids, 'reason': reason, 'status': status}
        self.commit('visual-gap:' + key + ':' + digest(payload), payload, [('visual_gap', key, payload)])

    async def research_figures(self, action, hit, document):
        snapshot = self.head('snapshot', hit.hit_id)
        requirements = [r.id for r in self.contract.requirements if any(
            c.check_id in action.acceptance_check_ids for c in r.acceptance_checks)]
        scope = digest([action.model_dump(mode='json'), snapshot['version_id']])
        if document.status == 'needs_ocr':
            self.visual_gap(scope, requirements, 'needs_ocr: no textual layout/caption association')
            return
        if not document.figures:
            return
        await self.visual_capability()
        map_payload = {'snapshot_id': hit.hit_id, 'document': document.model_dump()}
        receipt = self.commit('document-map:' + snapshot['version_id'], map_payload, [('document_map', scope, map_payload)])
        map_ref = receipt['refs'][0]['version_id']
        self.visual_dependency([snapshot['version_id']], map_ref, 'document_map')
        action_key = digest(action.model_dump(mode='json'))
        used = self.store.db.execute('SELECT count(*) FROM visual_selections WHERE action_id=?', (action_key,)).fetchone()[0]
        # On resume, this source's selections are already counted; its immutable plan is replayed.
        selected_here = sum(row[0].startswith(scope + ':') for row in self.store.db.execute(
            'SELECT figure_key FROM visual_selections WHERE action_id=?', (action_key,)))
        quota = self.visual_config.max_figures_per_action - used + selected_here
        packet = {'question_id': action.question_id, 'requirement_ids': requirements,
            'document_map_json': canonical(document.model_dump()).decode(), 'remaining_figure_quota': quota}
        plan = await self.visual_role('researcher.inspect_figures', 'inspect-figures:' + scope, packet, FigureReadPlan)
        candidates = {f.id: f for f in document.figures}
        chosen = [r.figure_ref for r in plan.reads]
        if (len(chosen) != len(set(chosen)) or len(chosen) > quota
            or set(chosen) & (set(plan.irrelevant) | set(plan.unresolved))
            or set(plan.irrelevant) & set(plan.unresolved)
            or set(chosen) | set(plan.irrelevant) | set(plan.unresolved) != set(candidates)):
            raise VisualError('invalid_figure_plan: account for each candidate once within quota')
        # Explicit body references remain obligations; no keyword-only skip.
        unresolved = dict(plan.unresolved)
        unresolved.update({fid: reason for fid, reason in plan.irrelevant.items() if candidates[fid].mention_refs})
        self.visual_gap(scope, requirements, canonical(unresolved).decode(), 'open' if unresolved else 'resolved')
        for selection in plan.reads:
            candidate = candidates[selection.figure_ref]
            figure_key = scope + ':' + candidate.id
            with self.store.transaction() as db:
                self.store.check_lease(db, self.owner, self.generation)
                db.execute('INSERT OR IGNORE INTO visual_selections VALUES(?,?)', (action_key, figure_key))
                if db.execute('SELECT count(*) FROM visual_selections WHERE action_id=?', (action_key,)).fetchone()[0] > self.visual_config.max_figures_per_action:
                    raise VisualError('figure_limit: SearchAction selected more than allowed')
            regions = [r.model_dump() for r in selection.regions]
            if not regions:
                # Whole-page fallback includes caption pages; association still needs audit.
                pages = sorted({b.page for b in document.blocks if b.id in candidate.caption_refs + candidate.mention_refs})
                regions = [{'page': page} for page in pages]
            try:
                if len(regions) > self.visual_config.max_images_per_figure or sum(r.get('detail', False) for r in regions) > 1:
                    raise VisualError('image_limit: too many regions or detail renders')
                render, _ = await self.visual_pdf_job(document.raw_hash, 'render', regions=regions)
            except VisualError as exc:
                self.visual_gap(figure_key, requirements, str(exc))
                continue
            images = [RenderedImage.model_validate(i) for i in render['images']]
            artifact = FigureArtifact(snapshot_ref=document.raw_hash, figure_label=candidate.figure_label,
                caption_refs=candidate.caption_refs, mention_refs=candidate.mention_refs,
                page_regions=[i.locator for i in images], images=images, render_profile=render['render_profile'],
                extraction_status='located' if candidate.caption_refs or candidate.mention_refs else 'ambiguous')
            payload = {'snapshot_id': hit.hit_id, 'document_map_version_id': map_ref, 'artifact': artifact.model_dump()}
            receipt = self.commit('figure:' + figure_key, payload, [('figure', figure_key, payload)])
            figure_ref = receipt['refs'][0]['version_id']
            self.visual_dependency([snapshot['version_id'], map_ref], figure_ref, 'figure')
            locator = VisualLocator(figure_version_id=figure_ref, raw_hash=document.raw_hash,
                page_regions=artifact.page_regions, image_blob_hashes=[i.blob_hash for i in images], caption_refs=candidate.caption_refs)
            _, context = verify_visual_evidence(self.store, {'locator': locator.model_dump()})
            packet = {'figure_json': canonical(artifact.model_dump()).decode(), 'context_json': canonical(context).decode(),
                'question_id': action.question_id, 'goal': selection.goal, 'requirement_ids': requirements,
                'source_url': hit.url, 'source_title': hit.title}
            self.visual_reserve_roles(['read-figure:' + figure_key, 'visual-forward:' + figure_key, 'visual-reverse:' + figure_key])
            proposal = await self.visual_role('researcher.read_figure', 'read-figure:' + figure_key, packet, VisualClaimProposal,
                image_hashes=locator.image_blob_hashes, protected_keys=['visual-forward:' + figure_key, 'visual-reverse:' + figure_key])
            if not set(proposal.requirement_ids) <= set(requirements):
                raise VisualError('invalid_visual_claim: unknown/out-of-task requirements')
            self.validate_visual_observation(proposal.observation, artifact)
            evidence_id, claim_id = 'visual:' + figure_key, 'visual-claim:' + figure_key
            evidence = {'snapshot_id': hit.hit_id, 'snapshot_version_id': snapshot['version_id'], 'excerpt': '',
                'observation': proposal.observation.model_dump(), 'locator': locator.model_dump(),
                'source_class': proposal.source_class, 'observation_root': document.raw_hash, 'validity': 'active'}
            claim = {'text': proposal.claim_text, 'kind': proposal.claim_kind, 'requirement_ids': proposal.requirement_ids, 'validity': 'current'}
            receipt = self.commit('candidate:' + evidence_id, {'evidence': evidence, 'claim': claim},
                                  [('evidence', evidence_id, evidence), ('claim', claim_id, claim)])
            refs = {r['kind']: r for r in receipt['refs']}
            self.visual_dependency([figure_ref], refs['evidence']['version_id'], 'visual_evidence')
            verdict, images_hash = await self.audit_visual(evidence, claim, figure_key)
            audit_ref = self.commit('visual-audit:' + figure_key, verdict, [('audit', evidence_id, verdict)])['refs'][0]
            self.visual_dependency([refs['evidence']['version_id'], figure_ref], audit_ref['version_id'], 'visual_audit')
            accepted = verdict['forward']['verdict'] == 'supported' and verdict['reverse']['verdict'] == 'no_objection'
            if accepted:
                self.store.add_link('link:' + evidence_id, EntityRef.model_validate(refs['claim']),
                    EntityRef.model_validate(refs['evidence']), EntityRef.model_validate(audit_ref), 'supports', self.owner, self.generation)
            if verdict['conflict']:
                conflict = {'claim_version_ids': [refs['claim']['version_id']], 'evidence_version_ids': [refs['evidence']['version_id']],
                            'severity': 'blocking', 'reason': 'visual_text_conflict'}
                self.commit('visual-conflict:' + figure_key, conflict, [('conflict', figure_key, conflict)])
            self.visual_gap(figure_key, requirements, 'visual_audit' if not accepted else 'audited', 'resolved' if accepted else 'open')

    @staticmethod
    def validate_visual_observation(observation, artifact):
        images = {i.blob_hash: i for i in artifact.images}
        if observation.figure_type == 'table' and observation.numbers:
            raise VisualError('requires_e4: table screenshot numbers cannot be calculated')
        for number in observation.numbers:
            image = images.get(number.image_hash)
            if image is None:
                raise VisualError('invalid_visual_number: label image not attached')
            check_bbox(number.label_bbox, image.width, image.height)
            if number.value not in re.findall(r'(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])', number.label_transcription):
                raise VisualError('unsupported_precision: number not explicitly transcribed')

    async def audit_visual(self, evidence, claim, key, *, requirement=None):
        await self.visual_capability()
        artifact, context = verify_visual_evidence(self.store, evidence)
        hashes = [i.blob_hash for i in artifact.images]
        packet = {'claim_text': claim['text'], 'observation_json': canonical(evidence['observation']).decode(),
            'figure_json': canonical(artifact.model_dump()).decode(), 'context_json': canonical(context).decode(),
            'source_class': evidence['source_class']}
        if requirement:
            packet['requirement_json'] = requirement.model_dump_json()
        self.visual_reserve_roles(['visual-forward:' + key, 'visual-reverse:' + key])
        forward = await self.visual_role('auditor.visual', 'visual-forward:' + key, packet, VisualAudit, image_hashes=hashes,
                                        protected_keys=['visual-reverse:' + key])
        reverse = await self.visual_role('auditor.visual_counter', 'visual-reverse:' + key, packet, VisualAudit, image_hashes=hashes)
        qualified = forward.verdict == reverse.verdict == 'accept' and not evidence['observation']['pending_checks']
        # Project onto existing audit gates; visual details remain immutable alongside them.
        result = {'forward': EvidenceAuditVerdict(verdict='supported' if qualified else 'not_supported',
                    reason='; '.join(forward.findings) or forward.verdict, source_class_verified=forward.source_class_verified).model_dump(),
                  'reverse': CounterAuditVerdict(verdict='no_objection' if qualified else 'found_issue',
                    reason='; '.join(reverse.findings) or reverse.verdict).model_dump(),
                  'visual_forward': forward.model_dump(), 'visual_reverse': reverse.model_dump(),
                  'conflict': 'conflict' in (forward.verdict, reverse.verdict),
                  'image_blob_hashes': hashes, 'input_manifest_hash': digest([packet, hashes])}
        return result, hashes
