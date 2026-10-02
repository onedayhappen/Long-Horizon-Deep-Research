"""Bounded, evidence-grounded context for sequential long-form writing."""
from __future__ import annotations

from .jsonio import canonical
from .outline import ordered_nodes


def section_text(section):
    return '\n\n'.join(''.join(s.text for s in p.sentences) for p in section.paragraphs)


def writing_brief(outline, contract, config):
    nodes = ordered_nodes(outline)
    # Reserve space for headings, provenance and citation definitions. The final
    # renderer still enforces the exact contract limit, including those elements.
    target = min(config.target_section_characters,
                 max(1, int(contract.output_contract.max_characters * .65) // len(nodes)),
                 max(1, config.max_output_tokens // 4))
    return {'language': contract.output_contract.language,
            'target_characters': target,
            'target_is_soft': True,
            'outline': [n.model_dump(mode='json') for n in nodes]}


def previous_context(sections, limit):
    """Keep bounded prose for continuity; never carry previous source packets."""
    result = []
    for title, section in reversed(sections):
        if limit <= 0:
            break
        text = section_text(section)
        excerpt = text[-limit:]
        result.append({'section_id': section.section_id, 'title': title,
                       'text': excerpt, 'truncated': len(excerpt) < len(text)})
        limit -= len(excerpt)
    return list(reversed(result))


def source_context(store, material, limit):
    """Retrieve local text around approved spans, without new network calls.

    Surrounding text is context, not an independently approved factual claim.
    PDF-region evidence already carries its verified observation in material.
    """
    result, seen = [], set()
    evidence = material['evidence']
    per_source = min(2400, limit // max(1, len(evidence)))
    for row in evidence:
        locator = row['payload']['locator']
        if locator['kind'] != 'text_span' or per_source <= 0:
            continue
        key = (locator['text_blob_hash'], locator['start'], locator['end'])
        if key in seen:
            continue
        seen.add(key)
        text = store.read_blob(locator['text_blob_hash']).decode('utf-8')
        margin = max(0, (per_source - (locator['end'] - locator['start'])) // 2)
        start = max(0, locator['start'] - margin)
        end = min(len(text), start + per_source)
        result.append({'evidence_id': row['id'], 'snapshot_id': row['payload']['snapshot_id'],
                       'start': start, 'end': end, 'text': text[start:end],
                       'context_only': True})
    return result


def depth_status(section, material, target):
    text = section_text(section)
    # Distinct supported claims, not repeated evidence links, justify expansion.
    claims = {r['version_id'] for r in material['claims']}
    used = {cid for f in section.facts if f.kind == 'factual' for cid in f.claim_version_ids}
    return {'section_id': section.section_id, 'body_characters': len(text),
            'paragraphs': sum(bool(p.sentences) for p in section.paragraphs),
            'target_characters': target, 'available_claims': len(claims),
            'unused_claim_version_ids': sorted(claims - used),
            'needs_expansion': len(claims) >= 3 and len(text) < target // 2,
            'open_questions': section.open_questions}


def writer_packet(store, outline, material, sections, contract, config):
    claims = {r['version_id'] for r in material['claims']}
    evidence = {r['version_id']: r['id'] for r in material['evidence']}
    pairs = sorted({(r['claim_version_id'], evidence[r['evidence_version_id']])
                    for r in store.db.execute("SELECT * FROM evidence_links WHERE relation='supports'")
                    if r['claim_version_id'] in claims and r['evidence_version_id'] in evidence})
    return {
        'writing_brief_json': canonical(writing_brief(outline, contract, config)).decode(),
        'previous_sections_json': canonical(previous_context(sections, config.previous_context_characters)).decode(),
        'source_context_json': canonical(source_context(store, material, config.source_context_characters)).decode(),
        'citation_pairs_json': canonical([{'claim_version_id': c, 'evidence_id': e} for c, e in pairs]).decode(),
        'writing_max_output_tokens': config.max_output_tokens,
    }
