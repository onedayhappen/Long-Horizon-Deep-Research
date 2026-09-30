"""Unit-preserving paired measurements; absence of data is never a saving."""
from __future__ import annotations


def ratio(numerator, denominator):
    return None if numerator is None or denominator in (None, 0) else numerator / denominator


def reduction(baseline, reused):
    return None if baseline is None or reused is None else ratio(baseline - reused, baseline)


def paired_metrics(baseline, reused, *, source_cost=None):
    """Input observations carry quality_passed plus separate cost dimensions."""
    equal_quality = baseline.get('quality_passed') is True and reused.get('quality_passed') is True
    costs = {}
    for unit in ('external_calls', 'input_tokens', 'output_tokens', 'elapsed_seconds', 'cost_usd'):
        costs[unit] = {'baseline': baseline.get(unit), 'reused': reused.get(unit),
                       'reduction': reduction(baseline.get(unit), reused.get(unit)) if equal_quality else None}
    return {'quality_equivalent': equal_quality, 'costs': costs, 'source_cost_reference': source_cost,
            'reuse_hit_rate': ratio(reused.get('used_imported_evidence'), reused.get('imported_evidence')),
            'invalid_reuse_rate': ratio(reused.get('incorrect_at_use_bindings'), reused.get('used_bindings')),
            'later_expired_bindings': reused.get('later_expired_bindings'),
            'later_corrected_bindings': reused.get('later_corrected_bindings')}
