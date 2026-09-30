"""Re-index explicitly authored fictional responses against current request packets.

The Controller is used to capture request hashes, not to invent audit verdicts.
This utility runs only while building fixtures, never in ReplayRoleBackend.
"""
import asyncio
import json
import tempfile
from pathlib import Path

from src.research.controller import Controller
from src.research.jsonio import canonical, load, digest
from src.research.models import ResearchContract, RoleResponse
from src.research.storage import Store


def reindex(root):
    root = Path(root)
    manifest = load(root/'fixture_manifest.json')
    original = {}
    for key, path in manifest['roles'].items():
        role, action, _ = key.split('|')
        original[(role, action)] = load(root/path)
    indexed = {}

    class Recorder:
        def __init__(self, fail_once=False):
            self.fail_once = fail_once

        async def generate(self, request):
            role, action = request.role, request.logical_action_key
            if self.fail_once and role == 'auditor.evidence':
                self.fail_once = False
                raise RuntimeError('fixture builder simulates one missing audit response')
            if role == 'planner.next':
                if action == 'plan:0':
                    value = original[(role,'search-1')]
                else:
                    # Fixed fixture scenario: after its single search, propose completion.
                    value = dict(original[(role,'search-1')])
                    value['raw_text'] = canonical(dict(kind='terminate', coverage_refs=request.data_packet['coverage_refs'], proposed_outcome='complete')).decode()
            elif role == 'auditor.question_space':
                value = original[(role,'question_space')]
            elif role == 'auditor.search_bias':
                value = original[(role,'search-bias')]
            elif role == 'auditor.coverage':
                value = original[(role,':'.join(action.split(':')[:2]))]
            elif role == 'writer.section':
                value = original[(role,'write:'+action.split(':')[-1])]
            elif role == 'auditor.report':
                value = original[(role,'report')]
            else:
                value = original[(role,action)]
            key = f'{role}|{action}|{request.input_manifest_hash}'
            path = f'roles/loop-{digest(key)}.json'
            indexed[key] = path
            (root/path).write_bytes(canonical(value))
            return RoleResponse.model_validate(value)

    from src.research.budget import BudgetDenied
    for calls, fail_once in [(120,False),(120,True),(10,False)]:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), namespace_seed=manifest['namespace_seed'])
            store.init_run('fixture-index', 1, 'fixture-index')
            controller = Controller(store, ResearchContract.model_validate(load(root/'contract.json')), root, calls, [80,10,10])
            controller.roles = Recorder(fail_once)
            try:
                try:
                    asyncio.run(controller.run())
                except BudgetDenied:
                    if calls != 10:
                        raise
                except RuntimeError as exc:
                    if str(exc) != 'fixture builder simulates one missing audit response':
                        raise
                    controller.record_stop('failed', str(exc))
                    asyncio.run(controller.run())
            finally:
                controller.close(); store.close()
    manifest['roles'] = indexed
    import os
    temporary = root / 'fixture_manifest.pending.json'
    temporary.write_bytes(canonical(manifest))
    os.replace(temporary, root / 'fixture_manifest.json')
