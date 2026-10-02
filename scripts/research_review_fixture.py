"""Explicitly permissive mock reviews for fictional protocol fixtures only.

Production code must never import this module. These scripted responses test
state transitions, not a real model's assessment of relevance or completeness.
"""
import json


def conflict_response(packet):
    """Scripted complementary verdicts for existing fictional CSV examples."""
    material = json.loads(packet['material_json'])
    if packet['phase'] == 'verify':
        return dict(checked_claim_version_ids=[i['claim']['version_id'] for i in material],
                    verdict='accept', original_claims_scoped=True, reason='Fictional independent comparison.')
    return dict(conflict_type='complementary', frames=[dict(claim_version_id=i['claim']['version_id'],
        basis=[dict(evidence_version_id=e['version_id'], quote=e['payload'].get('excerpt') or e['payload']['observation']['description'])
               for e in i['evidence']]) for i in material], relations=[], disposition=None,
        reason='Fictional complementary CSV statements.', next_investigation='')


def outline_review_response(packet):
    outline = json.loads(packet['outline_json'])
    material = json.loads(packet['node_material_json'])
    nodes = []
    for node in outline['nodes']:
        if not node['active']:
            continue
        claims = [r['version_id'] for r in material[node['id']]['claims']]
        parent = any(n['active'] and n['parent_id'] == node['id'] for n in outline['nodes'])
        nodes.append(dict(node_id=node['id'], disposition='overview' if parent else 'supported' if claims else 'accepted_unknown',
                          reason='Fictional fixture assumes this chapter is adequate.', claim_version_ids=claims))
    return dict(outline_version=outline['outline_version'], nodes=nodes, missing_dimensions=[],
                unincorporated_summary_ids=[], decision='ready', reason='Scripted fictional review.')
