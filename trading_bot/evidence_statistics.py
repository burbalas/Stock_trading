"""Descriptive uncertainty for *paired* strategy and benchmark equity curves.

This module never submits orders or selects a strategy.  Equity must already be
net of simulated costs, denominated in the same currency, and free of external
cash flows.  Block bootstrap intervals are conditional on the supplied history
and an approximate stationary-return assumption; they do not prove a future
trading edge or correct data, universe, execution, or strategy-selection bias.
"""
from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any


def _field(point: Any, name: str, default: Any = None) -> Any:
    return point.get(name, default) if isinstance(point, Mapping) else getattr(point, name, default)


def _number(value: Any, description: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{description} must be a finite number, not a boolean.')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{description} must be a finite number.') from exc
    if not math.isfinite(result):
        raise ValueError(f'{description} must be finite.')
    return result


def _curve(points: Sequence[Any], name: str) -> tuple[list[date], list[float], list[float] | None]:
    dates: list[date] = []
    equity: list[float] = []
    exposure: list[float] = []
    exposure_complete = True
    for point in points:
        raw_date = _field(point, 'date')
        if isinstance(raw_date, datetime):
            raise ValueError(f'{name} requires session dates, not intraday datetimes.')
        try:
            session = raw_date if isinstance(raw_date, date) else date.fromisoformat(raw_date)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'{name} has an invalid session date.') from exc
        if dates and session <= dates[-1]:
            raise ValueError(f'{name} session dates must be strictly increasing and unique.')
        value = _number(_field(point, 'equity'), f'{name} equity')
        if value <= 0:
            raise ValueError(f'{name} equity must stay strictly positive; zero equity is not a valid return.')
        invested = _field(point, 'positions_value', _field(point, 'invested'))
        if invested is None:
            exposure_complete = False
        else:
            invested_value = _number(invested, f'{name} invested value')
            if invested_value < 0:
                raise ValueError(f'{name} invested value cannot be negative.')
            ratio = invested_value / value
            if not math.isfinite(ratio):
                raise ValueError(f'{name} exposure must be finite.')
            exposure.append(ratio)
        dates.append(session)
        equity.append(value)
    if len(dates) < 2:
        raise ValueError(f'{name} requires at least two equity observations.')
    return dates, equity, exposure if exposure_complete else None


def _log_returns(equity: list[float]) -> list[float]:
    # Subtract logs instead of dividing equity values: extreme finite endpoints
    # must not overflow into an apparently valid infinite simple return.
    logs = [math.log(value) for value in equity]
    result = [right - left for left, right in zip(logs, logs[1:])]
    for value in result:
        try:
            simple = math.expm1(value)
        except OverflowError as exc:
            raise ValueError('Equity changes imply a non-finite daily return.') from exc
        if not math.isfinite(simple) or simple <= -1:
            raise ValueError('Daily returns must be finite and strictly greater than -100%.')
    return result


def _growth_pct(log_return: float) -> float:
    try:
        result = math.expm1(log_return) * 100
    except OverflowError as exc:
        raise ValueError('Return horizon overflows finite percentage reporting.') from exc
    if not math.isfinite(result):
        raise ValueError('Return horizon overflows finite percentage reporting.')
    return result


def _drawdown_pct(equity: list[float]) -> float:
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst * 100


def _percentile(sorted_values: list[float], fraction: float) -> float:
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _interval(values: list[float], confidence: float) -> dict[str, float]:
    ordered = sorted(values)
    tail = (1 - confidence) / 2
    return {'lower': _percentile(ordered, tail), 'upper': _percentile(ordered, 1 - tail)}


def _prefix(values: list[float]) -> list[float]:
    result = [0.0]
    for value in values + values:
        result.append(result[-1] + value)
    return result


def _bootstrap(candidate_logs: list[float], benchmark_logs: list[float], *, block_length: int,
               samples: int, seed: int, confidence: float) -> dict[str, Any]:
    """Circular moving blocks, same indices for both sides of every pair.

    Prefix sums avoid materializing resampled daily histories.  A block can wrap
    from the last observation to the first; the final block is truncated so
    every replicate contains exactly the original number of return sessions.
    """
    count = len(candidate_logs)
    excess_logs = [a - b for a, b in zip(candidate_logs, benchmark_logs)]
    excess_simple = [math.expm1(a) - math.expm1(b) for a, b in zip(candidate_logs, benchmark_logs)]
    log_prefix = _prefix(excess_logs)
    simple_prefix = _prefix(excess_simple)
    rng = random.Random(seed)
    log_sums: list[float] = []
    mean_simple_bps: list[float] = []
    for _ in range(samples):
        remaining = count
        log_sum = simple_sum = 0.0
        while remaining:
            start = rng.randrange(count)
            size = min(block_length, remaining)
            log_sum += log_prefix[start + size] - log_prefix[start]
            simple_sum += simple_prefix[start + size] - simple_prefix[start]
            remaining -= size
        log_sums.append(log_sum)
        mean_simple_bps.append(simple_sum / count * 10000)
    mean_log_bps = [value / count * 10000 for value in log_sums]
    return {
        'block_length': block_length,
        'effective_blocks_per_replicate': count / block_length,
        'bootstrap_samples': samples,
        'seed': seed,
        'confidence_level': confidence,
        'mean_daily_simple_excess_bps_ci': _interval(mean_simple_bps, confidence),
        'mean_daily_log_excess_bps_ci': _interval(mean_log_bps, confidence),
        'relative_wealth_gain_pct_ci': _interval([_growth_pct(value) for value in log_sums], confidence),
        'annualized_relative_wealth_gain_pct_ci': _interval(
            [_growth_pct(value / count * 252) for value in log_sums], confidence) if count >= 252 else None,
    }


def analyze_paired_equity(candidate: Sequence[Any], benchmark: Sequence[Any], *,
                         candidate_name: str = 'candidate', benchmark_name: str = 'benchmark',
                         seed: int = 1729, bootstrap_samples: int = 10000,
                         block_lengths: tuple[int, ...] = (5, 20), minimum_sessions: int = 252,
                         confidence: float = .95, currency: str = 'USD',
                         costs_included: bool | None = None) -> dict[str, Any]:
    """Return JSON-safe paired diagnostics without repairing missing observations.

    ``candidate`` and ``benchmark`` contain mappings or objects with ``date`` and
    ``equity``. Optional ``positions_value`` (or ``invested``) is a currency
    value, not a weight. Both curves must share every date and initial capital.
    The first observation is the initial baseline, so N observations give N-1
    return sessions. ``costs_included`` is a caller assertion, not verified here.

    ``relative_wealth_gain_pct`` means 100 * (candidate_growth /
    benchmark_growth - 1), not the percentage-point difference in total returns.
    The evidence screen is a prespecified descriptive checkpoint, never proof.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError('seed must be an integer.')
    if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int) or not 200 <= bootstrap_samples <= 100000:
        raise ValueError('bootstrap_samples must be an integer between 200 and 100000.')
    if isinstance(minimum_sessions, bool) or not isinstance(minimum_sessions, int) or minimum_sessions < 1:
        raise ValueError('minimum_sessions must be a positive integer.')
    if not block_lengths or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in block_lengths):
        raise ValueError('block_lengths must contain positive integers.')
    if len(set(block_lengths)) != len(block_lengths):
        raise ValueError('block_lengths cannot contain duplicates.')
    confidence = _number(confidence, 'confidence')
    if not .8 <= confidence < 1:
        raise ValueError('confidence must be at least .8 and below 1.')
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or not currency.isupper():
        raise ValueError('currency must be a three-letter uppercase currency code shared by both curves.')
    if costs_included is not None and not isinstance(costs_included, bool):
        raise ValueError('costs_included must be True, False, or None.')
    candidate_dates, candidate_equity, candidate_exposure = _curve(candidate, candidate_name)
    benchmark_dates, benchmark_equity, benchmark_exposure = _curve(benchmark, benchmark_name)
    if candidate_dates != benchmark_dates:
        raise ValueError('Paired curves must have exactly matching session dates; no padding or date intersection is performed.')
    if not math.isclose(candidate_equity[0], benchmark_equity[0], rel_tol=1e-10, abs_tol=0):
        raise ValueError('Paired curves must start with the same capital in the same currency.')
    candidate_logs = _log_returns(candidate_equity)
    benchmark_logs = _log_returns(benchmark_equity)
    count = len(candidate_logs)
    excess_logs = [a - b for a, b in zip(candidate_logs, benchmark_logs)]
    total_candidate = math.fsum(candidate_logs)
    total_benchmark = math.fsum(benchmark_logs)
    split = count // 2
    halves = []
    for label, start, end in [('first_half', 0, split), ('second_half', split, count)]:
        if start == end:
            continue
        halves.append({
            'name': label, 'return_sessions': end - start,
            'first_return_date': candidate_dates[start + 1].isoformat(),
            'last_return_date': candidate_dates[end].isoformat(),
            'candidate_return_pct': _growth_pct(math.fsum(candidate_logs[start:end])),
            'benchmark_return_pct': _growth_pct(math.fsum(benchmark_logs[start:end])),
            'relative_wealth_gain_pct': _growth_pct(math.fsum(excess_logs[start:end])),
        })
    intervals = [_bootstrap(candidate_logs, benchmark_logs, block_length=length,
                            samples=bootstrap_samples, seed=seed + length, confidence=confidence)
                 for length in block_lengths if length <= count]
    diagnostics: list[str] = []
    if count < minimum_sessions:
        diagnostics.append(f'Only {count} return sessions; the prespecified checkpoint is {minimum_sessions}.')
    if costs_included is not True:
        diagnostics.append('Net-of-cost equity was not asserted by the caller.')
    if any(length > count for length in block_lengths):
        diagnostics.append('At least one block sensitivity exceeds the available return history and was skipped.')
    if any(item['effective_blocks_per_replicate'] < 10 for item in intervals):
        diagnostics.append('At least one block sensitivity has fewer than ten blocks of history; interval precision is limited.')
    if any(item['relative_wealth_gain_pct_ci']['lower'] <= 0 for item in intervals):
        diagnostics.append('At least one paired block interval includes zero or negative relative wealth gain.')
    if len(halves) != 2 or any(item['relative_wealth_gain_pct'] <= 0 for item in halves):
        diagnostics.append('Relative wealth gain was not positive in both chronological halves.')
    if total_candidate <= 0:
        diagnostics.append('The candidate did not grow initial capital over the observed period.')
    passed = not diagnostics and len(intervals) == len(block_lengths)
    return {
        'candidate': candidate_name, 'benchmark': benchmark_name, 'currency': currency,
        'start': candidate_dates[0].isoformat(), 'end': candidate_dates[-1].isoformat(),
        'observations': len(candidate_dates), 'return_sessions': count,
        'initial_capital': candidate_equity[0], 'costs_included_asserted': costs_included,
        'minimum_sessions_checkpoint': minimum_sessions,
        'candidate_return_pct': _growth_pct(total_candidate),
        'benchmark_return_pct': _growth_pct(total_benchmark),
        'total_return_difference_percentage_points': _growth_pct(total_candidate) - _growth_pct(total_benchmark),
        'relative_wealth_gain_pct': _growth_pct(math.fsum(excess_logs)),
        'mean_daily_simple_excess_bps': math.fsum(math.expm1(a) - math.expm1(b)
                                                for a, b in zip(candidate_logs, benchmark_logs)) / count * 10000,
        'mean_daily_log_excess_bps': math.fsum(excess_logs) / count * 10000,
        'candidate_max_drawdown_pct': _drawdown_pct(candidate_equity),
        'benchmark_max_drawdown_pct': _drawdown_pct(benchmark_equity),
        'candidate_average_exposure_pct': None if candidate_exposure is None else math.fsum(candidate_exposure) / len(candidate_exposure) * 100,
        'benchmark_average_exposure_pct': None if benchmark_exposure is None else math.fsum(benchmark_exposure) / len(benchmark_exposure) * 100,
        'chronological_halves': halves,
        'bootstrap': {'method': 'paired circular moving-block percentile bootstrap',
                      'base_seed': seed, 'annualization_sessions': 252, 'sensitivities': intervals},
        'evidence_screen_passed': passed,
        'diagnostics': diagnostics,
        'limitations': [
            'Passing this descriptive screen does not prove profitability or predict future returns.',
            'Equity must already include all costs in one currency and contain no external cash flows; these assumptions are not verified by this module.',
            'Intervals are conditional on the observed history and approximate stationarity; circular blocks join the end of the sample to its beginning.',
            'Block lengths are sensitivity checks, not a search for the most favorable interval. No bootstrap positive fraction is a probability of future profit.',
            'Strategy selection, repeated examination, survivorship, corporate actions, execution errors, and omitted costs are not corrected by this bootstrap.',
            'Relative wealth is candidate growth divided by benchmark growth; it differs from a percentage-point return difference. Annualization assumes 252 return sessions and is a scale conversion, not a forecast.',
        ],
    }
