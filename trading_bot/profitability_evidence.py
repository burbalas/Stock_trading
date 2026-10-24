"""Frozen raw-price profitability diagnostics. Offline: cannot place orders.

Input archives are immutable; every run is written to a new output directory.
The October diagnostic is separate from the prospective October 23 trial.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path

from .data import CsvMarketDataProvider, StaticMarketDataProvider
from .evidence_statistics import analyze_paired_equity
from .models import PortfolioSnapshot, Side
from .profitability_study import STOCKS, ETFS, candidates
from .research_accounting import CorporateActionLedger, CorporateActionResearchEngine, DividendEvent, SplitEvent
from .research_archive import immutable_write
from .research_benchmarks import run_passive_raw, run_cash_reference, average_invested_fraction, ex_post_exposure_reference

PLAN_FILES = ('PROFITABILITY_EVIDENCE_PLAN_2026-10-22.md', 'PROFITABILITY_EVIDENCE_PLAN_ADDENDUM_2026-10-22.md')
SCENARIOS = (('base_5bps_fee0', 5, 0), ('stress_20bps_fee0', 20, 0), ('fee_sensitivity_20bps_fee1', 20, 1))
START = date(2026, 9, 15)
END = date(2026, 10, 21)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_archive(directory: Path):
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('collection_succeeded') is not True:
        raise ValueError('Incomplete research archive cannot be evaluated.')
    for name, entry in manifest['artifacts'].items():
        if Path(name).name != name or digest(directory / name) != entry['sha256']:
            raise ValueError('Research archive integrity check failed.')
    symbols = sorted(set(STOCKS + ETFS))
    data = {mode: CsvMarketDataProvider(directory / f'bars_{mode}.csv').load(symbols)
            for mode in ('raw', 'split', 'all')}
    actions = json.loads((directory / 'corporate_actions.json').read_text(encoding='utf-8'))
    supplement_path = directory / 'corporate_actions_supplement.json'
    extra = None
    if supplement_path.exists():
        extra = json.loads(supplement_path.read_text(encoding='utf-8'))
        if extra['pagination']['terminal_page_without_next_token'] is not True:
            raise ValueError('Incomplete action supplement.')
        for kind, rows in extra['actions'].items():
            original = {row['id']: row for row in actions.get(kind, [])}
            for row in rows:
                if row['id'] in original and original[row['id']] != row:
                    raise ValueError('Conflicting corporate-action records.')
                original[row['id']] = row
            actions[kind] = list(original.values())
    with (directory / 'fx_usd_per_eur.csv').open(newline='', encoding='utf-8') as handle:
        fx = {date.fromisoformat(row['date']): float(row['usd_per_eur']) for row in csv.DictReader(handle)}
    return manifest, data, actions, fx, extra


def normalized_ledger(actions, data, *, start, end):
    """Require economic dates, not process dates; ignore warm-up cash dividends."""
    splits = []
    dividends = []
    for kind, rows in actions.items():
        for row in rows:
            symbol = row.get('symbol')
            # Merger/name-change records use participant roles rather than a
            # single symbol. Never silently discard an unsupported action that
            # touches either side of the configured universe.
            participants = {value for key, value in row.items()
                            if (key == 'symbol' or key.endswith('_symbol')) and isinstance(value, str)}
            if not participants.intersection(data):
                continue
            if kind in ('forward_splits', 'reverse_splits', 'cash_dividends'):
                effective = row.get('ex_date')
            elif kind == 'name_changes':
                # This API's name-change schema supplies process_date only.
                effective = row.get('effective_date') or row.get('process_date')
            else:
                effective = row.get('effective_date') or row.get('ex_date')
            if not effective:
                raise ValueError('An action lacks its required economic effective/ex-date; process date cannot substitute.')
            day = date.fromisoformat(effective)
            if kind in ('forward_splits', 'reverse_splits') and day <= end:
                ratio = Fraction(str(row['new_rate'])) / Fraction(str(row['old_rate']))
                splits.append(SplitEvent(symbol, day, ratio.numerator, ratio.denominator))
            elif kind == 'cash_dividends' and start <= day <= end:
                if not row.get('payable_date') or row.get('foreign') is True:
                    raise ValueError('Dividend requires verified USD amount and payment date.')
                dividends.append(DividendEvent(symbol, day, date.fromisoformat(row['payable_date']), float(row['rate'])))
            elif kind not in ('forward_splits', 'reverse_splits', 'cash_dividends') and start <= day <= end:
                raise ValueError(f'Unsupported evaluation-period action: {kind} {sorted(participants.intersection(data))} {day}')
    first = min(bar.date for rows in data.values() for bar in rows)
    ledger = CorporateActionLedger(first, end,
        'Archived Alpaca actions including future process dates; raw/split/all factor reconciliation; completeness is conditional on these checks.',
        True, tuple(splits), tuple(dividends))
    ledger.validate(data)
    return ledger


def audit_adjustments(modes, ledger, *, start, end):
    """Reconcile split history and material dividend-factor jumps, not prices for fills.

    Two-decimal vendor bars imply small factor noise. The fixed USD0.03
    tolerance is a data reconciliation bound, not a dividend-estimation rule.
    Missing events are rejected instead of inferring cash from rounded prices.
    """
    checks = []
    distributions = {(row.symbol, row.ex_date): row for row in ledger.dividends}
    matched = set()
    for symbol, raw in modes['raw'].items():
        split = modes['split'][symbol]
        total = modes['all'][symbol]
        if [bar.date for bar in raw] != [bar.date for bar in split] or [bar.date for bar in raw] != [bar.date for bar in total]:
            raise ValueError('Adjustment modes must have identical dates.')
        future_factor = 1.0
        split_by_date = {row.effective_date: row for row in ledger.splits if row.symbol == symbol}
        for raw_bar, split_bar in reversed(list(zip(raw, split))):
            for field in ('open', 'high', 'low', 'close'):
                if abs(getattr(raw_bar, field) * future_factor - getattr(split_bar, field)) > .03:
                    raise ValueError(f'Unresolved split adjustment: {symbol} {raw_bar.date}')
            if raw_bar.date in split_by_date:
                future_factor /= float(split_by_date[raw_bar.date].ratio)
        for index in range(1, len(raw)):
            day = raw[index].date
            if not start <= day <= end:
                continue
            before = total[index - 1].close / split[index - 1].close
            after = total[index].close / split[index].close
            implied = split[index - 1].close * (1 - before / after)
            action = distributions.get((symbol, day))
            if abs(implied) > .03 or action is not None:
                if action is None or abs(implied - action.amount_per_share) > .03:
                    raise ValueError(f'Unresolved cash adjustment: {symbol} {day}; no assumed zero dividend.')
                matched.add((symbol, day))
                checks.append({'symbol': symbol, 'ex_date': day, 'verified_amount': action.amount_per_share,
                               'rounded_bar_implied_amount': implied, 'pay_date': action.pay_date})
    if matched != set(distributions):
        raise ValueError('Not every dividend reconciles to an observed factor change.')
    grids = [{bar.date for bar in rows if start <= bar.date <= end} for rows in modes['raw'].values()]
    if not grids or any(grid != grids[0] for grid in grids) or not grids[0]:
        raise ValueError('Symbol-specific missing evaluation sessions.')
    return {'status': 'reconciled', 'dividend_factor_checks': checks,
            'dividend_tolerance_usd': .03, 'split_price_tolerance_usd': .03,
            'sessions': len(grids[0]), 'independent_exchange_calendar_verified': False,
            'caveat': 'Reconciliation cannot exclude common provider omissions, very small omitted distributions, revisions or historical publication delays.'}


def rate_for(day, fx):
    available = [stamp for stamp in fx if stamp <= day]
    if not available:
        raise ValueError('No past dated FX reference for an equity observation.')
    stamp = max(available)
    if (day - stamp).days > 7:
        raise ValueError('FX reference is more than seven calendar days stale.')
    return stamp, fx[stamp]


def euro_curve(snapshots, fx):
    return [PortfolioSnapshot(row.date, row.cash / rate_for(row.date, fx)[1],
                              row.positions_value / rate_for(row.date, fx)[1], row.equity / rate_for(row.date, fx)[1])
            for row in snapshots]


def summarize(result, data, ledger, fx, *, slippage, commission):
    snapshots = result.snapshots
    first, last = snapshots[0], snapshots[-1]
    shares = defaultdict(int)
    entered = {}
    durations = []
    # Interleave effective splits with fills for terminal quantities and holding periods.
    events = [(row.effective_date, 0, row) for row in ledger.splits if first.date <= row.effective_date <= last.date]
    events += [(row.date, 1, row) for row in result.fills]
    closed = 0
    for day, kind, row in sorted(events, key=lambda event: (event[0], event[1])):
        if kind == 0:
            exact = Fraction(shares[row.symbol]) * row.ratio
            if exact.denominator != 1:
                raise ValueError('Fractional split cannot be silently rounded in terminal holdings.')
            shares[row.symbol] = exact.numerator
        elif row.side is Side.BUY:
            entered.setdefault(row.symbol, day)
            shares[row.symbol] += row.quantity
        else:
            shares[row.symbol] -= row.quantity
            if shares[row.symbol] == 0:
                closed += 1
                durations.append((day - entered.pop(row.symbol)).days)
    held = {symbol: quantity for symbol, quantity in shares.items() if quantity > 0}
    values = {symbol: quantity * next(bar.close for bar in data[symbol] if bar.date == last.date)
              for symbol, quantity in held.items()}
    liquidation_cost = sum(value * slippage / 10000 + commission for value in values.values())
    peak = first.equity
    worst = 0.0
    for row in snapshots:
        peak = max(peak, row.equity)
        worst = min(worst, (row.equity / peak - 1) * 100)
    eur = euro_curve(snapshots, fx)
    rejections = getattr(result, 'decision_rejections', ())
    unpaid = sum(row.amount for row in getattr(result, 'unpaid_dividends', ()))
    fees = len(result.fills) * commission
    fee_eur = sum(commission / rate_for(fill.date, fx)[1] for fill in result.fills)
    slip = slippage / 10000
    slippage_cost = sum(fill.value * slip / (1 + slip if fill.side is Side.BUY else 1 - slip)
                        for fill in result.fills)
    first_fill_day = min((fill.date for fill in result.fills), default=None)
    return {
        'observations': len(snapshots), 'return_sessions': len(snapshots) - 1,
        'initial_usd': first.equity, 'final_usd': last.equity, 'pnl_usd': last.equity - first.equity,
        'return_usd_pct': (last.equity / first.equity - 1) * 100,
        'initial_eur_mark': eur[0].equity, 'final_eur_mark': eur[-1].equity,
        'pnl_eur_mark': eur[-1].equity - eur[0].equity,
        'return_eur_pct': (eur[-1].equity / eur[0].equity - 1) * 100,
        'max_drawdown_usd_pct': worst,
        'average_exposure_pct': average_invested_fraction(result) * 100,
        'maximum_exposure_pct': max(row.positions_value / row.equity for row in snapshots) * 100,
        'final_exposure_pct': last.positions_value / last.equity * 100,
        'flat_sessions': sum(row.positions_value == 0 for row in snapshots),
        'entries': sum(row.side is Side.BUY for row in result.fills),
        'exits': sum(row.side is Side.SELL for row in result.fills),
        'closed_round_trips': closed, 'closed_holding_calendar_days': durations,
        'open_holding_calendar_days': {symbol: (last.date - entered[symbol]).days for symbol in held},
        'turnover_notional_usd': sum(row.value for row in result.fills),
        'fees_usd': fees, 'fees_eur_dated': fee_eur,
        'modeled_slippage_cost_usd': slippage_cost,
        'modeled_total_execution_cost_usd': slippage_cost + fees,
        'cash_after_first_execution_session_usd': next((row.cash for row in snapshots if row.date == first_fill_day), first.cash),
        'investable_reference_informative': bool(result.fills),
        'ending_cash_usd': last.cash, 'ending_held_shares': held,
        'unpaid_dividend_assets_usd': unpaid,
        'paid_dividend_cash_usd': sum(row.amount for row in getattr(result, 'corporate_cash_flows', ()) if row.kind == 'dividend'),
        'terminal_exit_cost_usd': liquidation_cost,
        'terminal_liquidation_sensitivity_return_usd_pct': ((last.equity - liquidation_cost) / first.equity - 1) * 100,
        'terminal_liquidation_sensitivity_note': 'Last close less assumed exit costs; unpaid dividends remain receivables, not immediately available cash. No fill is asserted.',
        'rejection_events': len(rejections),
        'rejections_by_reason': dict(Counter(reason.split(': ', 1)[-1] for _, reason in rejections)),
        'affordability_rejection_events': sum('round' in reason or 'affordability' in reason for _, reason in rejections),
        'circuit_latched_sessions': len(getattr(result, 'circuit_dates', ())),
    }


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def save_json(path, value):
    content = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=_json_default) + '\n').encode('utf-8')
    return immutable_write(path, content)


def run_study(archive: Path, output: Path, *, start=START, end=END, reference_fx=1.1234):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new empty output directory; completed evidence is never overwritten.')
    manifest, all_modes, actions, fx, extra = load_archive(archive)
    modes = {mode: {symbol: [row for row in rows if row.date <= end] for symbol, rows in data.items()}
             for mode, data in all_modes.items()}
    raw = modes['raw']
    if any(rows[-1].date != end for rows in raw.values()):
        raise ValueError('Archive must include the complete requested end session for every symbol.')
    ledger = normalized_ledger(actions, raw, start=start, end=end)
    audit = audit_adjustments(modes, ledger, start=start, end=end)
    base, strategy, advisor = candidates(.15)['relative_strength_189']
    payload = {
        'schema_version': 1, 'recorded_at': datetime.now(timezone.utc),
        'evaluation_kind': 'post_selection_historical_diagnostic' if start == START else 'prospective_daily_bar_replay',
        'start': start, 'end': end, 'profitability_proven': False, 'live_promotion_authorized': False,
        'reference_fx': {'usd_per_eur': reference_fx, 'reference_date': '2026-10-21',
                         'purpose': 'Common constant USD starting amounts; EUR daily marks use dated past ECB rates. Initial EUR marks therefore need not equal budget labels.'},
        'archive_manifest_sha256': digest(archive / 'manifest.json'),
        'supplement_sha256': digest(archive / 'corporate_actions_supplement.json') if extra else None,
        'plans_sha256': {name: digest(Path(name)) for name in PLAN_FILES},
        'code_sha256': {path.name: digest(path) for path in Path(__file__).parent.glob('*.py')},
        'config': asdict(base), 'strategy': {'class': type(strategy).__name__, 'parameters': vars(strategy)},
        'corporate_actions': asdict(ledger), 'data_audit': audit,
        'rows': [], 'comparisons': [], 'artifacts': {},
        'limitations': [
            'No future profits are proven. The historical diagnostic is already partly observed; selection and repeated examination are not corrected.',
            'Current 15-stock universe and asof symbol mapping have hindsight and survivorship bias; historical action publication timestamps are not verified.',
            'Daily next-open fills do not reproduce the paper operator opening delay, IEX signals, quote caps, IOC failures, latency, partial fills or live protection.',
            'Slippage and zero/$1 commissions are scenarios, not verified account charges. Withholding, other taxes, executable FX costs, interest and market impact are not included.',
            '15% account circuit differs from the disabled EUR250 total/EUR50 daily/EUR250 order pilot policy; no funded-policy validation is asserted.',
            'Cash has zero USD interest and carries currency risk in EUR. Passive holdings have effectively disabled risk stops.',
            'A minimum 60 sessions AND20 closed round trips supports an operational review only; 252 return sessions is a separate descriptive duration checkpoint.',
            'No additional entry-delay scenario is implemented. Conservative previous-high bar stops cannot resolve every intrabar execution path.',
            'Paired intervals are descriptive conditional on short data, not selection-adjusted significance or a probability of future profit.',
        ],
    }
    observations = []
    for budget in (2500, 5000):
        for scenario, slippage, commission in SCENARIOS:
            cash = budget * reference_fx
            config = replace(base, starting_cash=cash, slippage_bps=slippage, commission_per_trade=commission)
            selected = {symbol: raw[symbol] for symbol in config.symbols}
            active_ledger = replace(ledger, splits=tuple(row for row in ledger.splits if row.symbol in selected),
                                    dividends=tuple(row for row in ledger.dividends if row.symbol in selected))
            active = CorporateActionResearchEngine(config, StaticMarketDataProvider(selected), deepcopy(strategy),
                deepcopy(advisor), active_ledger, trade_start_date=start).run()
            spy = run_passive_raw(raw, ledger, start=start, starting_cash=cash, symbols=['SPY'], fraction=.4,
                                  slippage_bps=slippage, commission=commission)
            basket = run_passive_raw(raw, ledger, start=start, starting_cash=cash, symbols=STOCKS, fraction=.4,
                                     slippage_bps=slippage, commission=commission)
            cash_run = run_cash_reference(raw, start=start, starting_cash=cash)
            full_spy = run_passive_raw(raw, ledger, start=start, starting_cash=cash, symbols=['SPY'], fraction=1,
                                       slippage_bps=slippage, commission=commission)
            matched = ex_post_exposure_reference(full_spy, candidate_average_exposure=average_invested_fraction(active))
            runs = {'candidate': active, 'SPY_40pct_whole_share': spy, 'stocks_40pct_whole_share': basket,
                    'USD_cash_zero_interest': cash_run}
            for name, result in runs.items():
                row = {'budget_eur_label': budget, 'scenario': scenario, 'name': name,
                       **summarize(result, raw, ledger, fx, slippage=slippage, commission=commission)}
                payload['rows'].append(row)
                artifact = f'{budget}_{scenario}_{name}.json'
                content = {'summary': row, 'snapshots': [asdict(item) for item in result.snapshots],
                           'fills': [asdict(item) for item in result.fills],
                           'assumptions': getattr(result, 'assumptions', ()),
                           'unpaid_dividends': [asdict(item) for item in getattr(result, 'unpaid_dividends', ())],
                           'corporate_cash_flows': [asdict(item) for item in getattr(result, 'corporate_cash_flows', ())],
                           'decision_rejections': getattr(result, 'decision_rejections', ())}
                payload['artifacts'][artifact] = save_json(output / artifact, content)
            references = {name: result for name, result in runs.items() if name != 'candidate'}
            references['SPY_ex_post_exposure_matched_analytical'] = matched
            matched_file = f'{budget}_{scenario}_ex_post_reference.json'
            payload['artifacts'][matched_file] = save_json(output / matched_file, asdict(matched))
            for name, result in references.items():
                stats = (analyze_paired_equity(active.snapshots, result.snapshots, benchmark_name=name, costs_included=True)
                         if len(active.snapshots) >= 2 else {'benchmark': name, 'observations': 1,
                             'return_sessions': 0, 'evidence_screen_passed': False,
                             'diagnostics': ['No paired return yet; first observation is the flat baseline.']})
                payload['comparisons'].append({'budget_eur_label': budget, 'scenario': scenario, **stats})
            exits = 0
            for candidate_mark, benchmark_mark in zip(active.snapshots, spy.snapshots):
                exits = sum(fill.side is Side.SELL and fill.date <= candidate_mark.date for fill in active.fills)
                observations.append({'date': candidate_mark.date.isoformat(), 'budget_eur': budget, 'scenario': scenario,
                    'candidate_equity': candidate_mark.equity, 'benchmark_equity': benchmark_mark.equity,
                    'closed_round_trips': exits, 'candidate_invested': candidate_mark.positions_value,
                    'benchmark_invested': benchmark_mark.positions_value})
            print(f'{budget} EUR label / {scenario}: candidate {payload["rows"][-4]["return_usd_pct"]:.3f}% USD', flush=True)
    payload['artifacts']['paired_observations.json'] = save_json(output / 'paired_observations.json', observations)
    save_json(output / 'results.json', payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=Path('var/evidence_2026-10-22/data'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_study(args.archive, args.output)
    print(json.dumps({'output': str(args.output), 'sessions': result['data_audit']['sessions'], 'profitability_proven': False}))


if __name__ == '__main__':
    main()
