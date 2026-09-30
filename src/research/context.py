"""Persist each independent role request without importing other conversations."""
from __future__ import annotations

import uuid

from .jsonio import canonical, digest


class ContextBuilder:
    @staticmethod
    def build(request, attempt_id, attempt_no):
        # Callers construct role-specific data packets from committed state. The
        # packet stores the exact data sent, not a second LLM-written summary.
        data = request.data_packet
        if request.role in {'auditor.evidence', 'auditor.counter_entailment'}:
            prohibited = {'historical_audit', 'historical_verdict', 'research_digest_json', 'budget_json', 'conversation', 'messages'}
            if prohibited.intersection(data):
                raise ValueError('independent evidence review received prohibited history')
        return dict(packet_id=str(uuid.uuid4()), attempt_id=attempt_id, attempt_no=attempt_no,
                    role=request.role, action_id=request.logical_action_key, objective=request.response_schema_id,
                    contract_version=request.contract_version, input_manifest_hash=request.input_manifest_hash,
                    selected_refs={key: value for key, value in data.items() if key.endswith(('_ids', '_refs', '_version_id'))},
                    omitted_refs=[], data_packet=data, context_budget={'max_output_tokens': request.max_output_tokens},
                    system_template_hash=request.system_template_hash)

    @classmethod
    def save(cls, store, request, attempt_id, owner, generation):
        attempt_no = store.db.execute('SELECT attempt_no FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()[0]
        packet = cls.build(request, attempt_id, attempt_no)
        with store.transaction() as db:
            store.check_lease(db, owner, generation)
            db.execute('INSERT INTO context_packets VALUES(?,?,?,?,?)',
                       (packet['packet_id'], attempt_id, request.input_manifest_hash, digest(packet), canonical(packet).decode()))
        return packet
