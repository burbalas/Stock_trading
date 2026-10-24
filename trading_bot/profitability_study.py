"""Offline, prespecified strategy comparison. This module cannot submit orders."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path

from .advisor import HeuristicAdvisor
from .config import BotConfig
from .data import CsvMarketDataProvider, StaticMarketDataProvider
from .engine import RunResult, TradingEngine
from .models import Fill, PortfolioSnapshot, Side
from .research import metrics
from .strategies import EnsembleStrategy, MeanReversionStrategy, MomentumStrategy, MovingAverageCrossStrategy, RegimeRelativeStrengthStrategy
from .trend_research import MonthlyTrendStrategy


STOCKS = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AVGO', 'AMD', 'NFLX', 'JPM', 'V', 'MA', 'COST', 'WMT']
ETFS = ['SPY', 'QQQ', 'IWM', 'EFA', 'EEM', 'IEF', 'TLT', 'GLD']
PERIODS = {'development': (date(2020, 1, 1), date(2022, 12, 31)),
           'validation': (date(2023, 1, 1), date(2024, 12, 31)),
           'final': (date(2025, 1, 1), date(2026, 9, 14))}


class PassThroughAdvisor:
    def review(self, signal, history):
        return signal


def candidates(max_drawdown):
    base = BotConfig(symbols=STOCKS, starting_cash=100000, short_window=5, long_window=20,
                     min_confidence=.65, max_open_positions=4, max_daily_orders=2,
                     max_position_pct=.25, max_order_pct=.10, target_cash_pct=.08,
                     stop_loss_pct=.10, trailing_stop_pct=.03, max_drawdown_stop_pct=max_drawdown)
    ensemble = EnsembleStrategy([MovingAverageCrossStrategy(5, 20), MomentumStrategy(20), MeanReversionStrategy(20)])
    monthly = replace(base, symbols=['SPY'], max_position_pct=.40, max_order_pct=.40, max_open_positions=1,
                      max_daily_orders=1, stop_loss_pct=.20, trailing_stop_pct=.20)
    rotation = replace(monthly, symbols=ETFS, max_position_pct=.40/3, max_order_pct=.40/3, max_open_positions=3, max_daily_orders=3)
    return {
        'legacy_ensemble_3pct_trail': (base, ensemble, HeuristicAdvisor()),
        'ensemble_10pct_trail': (replace(base, trailing_stop_pct=.10), ensemble, HeuristicAdvisor()),
        'legacy_without_advisor': (base, ensemble, PassThroughAdvisor()),
        'momentum_63': (replace(base, trailing_stop_pct=.10, long_window=63, min_confidence=.58), MomentumStrategy(63, .04), HeuristicAdvisor()),
        'relative_strength_189': (replace(base, trailing_stop_pct=.10, short_window=20, long_window=100, min_confidence=.68), RegimeRelativeStrengthStrategy(), HeuristicAdvisor()),
        'spy_monthly_trend': (monthly, MonthlyTrendStrategy(), PassThroughAdvisor()),
        'etf_monthly_rotation': (rotation, MonthlyTrendStrategy(top_k=3), PassThroughAdvisor()),
    }


def describe(result):
    stats = metrics(result)
    snapshots = result.snapshots
    years = (snapshots[-1].date - snapshots[0].date).days / 365.25
    return {**asdict(stats), 'cagr_pct': ((1 + stats.return_pct / 100) ** (1 / years) - 1) * 100 if years else 0,
            'avg_exposure_pct': sum(s.positions_value / s.equity for s in snapshots) / len(snapshots) * 100,
            'stop_exits': sum(f.reason in {'stop loss', 'trailing stop'} for f in result.fills),
            'circuit_exits': sum(f.reason == 'account drawdown stop' for f in result.fills),
            'first_circuit_exit': next((f.date.isoformat() for f in result.fills if f.reason == 'account drawdown stop'), None),
            'flat_sessions_pct': sum(s.positions_value == 0 for s in snapshots) / len(snapshots) * 100,
            'traded_notional_multiple': sum(f.value for f in result.fills) / snapshots[0].equity,
            'start': snapshots[0].date.isoformat(), 'end': snapshots[-1].date.isoformat()}


def run_candidate(data, specification, period, slippage):
    base, strategy, advisor = specification
    start, end = period
    subset = {s: [bar for bar in data[s] if bar.date <= end] for s in base.symbols}
    config = replace(base, slippage_bps=slippage)
    result = TradingEngine(config, StaticMarketDataProvider(subset), deepcopy(strategy), deepcopy(advisor), trade_start_date=start).run()
    return describe(result)


def passive(data, symbols, period, fraction, slippage):
    start, end = period
    dates = sorted({bar.date for s in symbols for bar in data[s] if start <= bar.date <= end})
    if len(dates) < 2:
        raise ValueError('Passive evaluation requires at least two sessions.')
    index = {s: {bar.date: bar for bar in data[s]} for s in symbols}
    quantities = {}
    cash = 100000.0
    snapshots = [PortfolioSnapshot(dates[0], cash, 0, cash)]
    entry_date = dates[1]
    fills = []
    for s in symbols:
        if entry_date not in index[s]:
            raise ValueError(f'Missing benchmark entry bar for {s}.')
        price = index[s][entry_date].open * (1 + slippage / 10000)
        allocation = 100000 * fraction / len(symbols)
        quantities[s] = allocation / price
        cash -= allocation
        fills.append(Fill(entry_date, s, Side.BUY, quantities[s], price, allocation, 'passive entry'))
    prices = {}
    for day in dates[1:]:
        for s in symbols:
            if day in index[s]:
                prices[s] = index[s][day].close
        value = sum(quantities[s] * prices[s] for s in symbols)
        snapshots.append(PortfolioSnapshot(day, cash, value, cash + value))
    return describe(RunResult(snapshots, fills))


def select_candidate(results, max_drawdown):
    """Selection only reads development and validation, even if later rows exist."""
    dev = {r['candidate']: r for r in results if r['period'] == 'development' and r['slippage_bps'] == 5}
    validation = {r['candidate']: r for r in results if r['period'] == 'validation' and r['slippage_bps'] == 5}
    legacy = validation['legacy_ensemble_3pct_trail']
    eligible = [name for name in dev if name in validation and dev[name]['return_pct'] > 0
                and validation[name]['return_pct'] > max(0, legacy['return_pct'])
                and min(dev[name]['max_drawdown_pct'], validation[name]['max_drawdown_pct']) >= -max_drawdown * 100]
    scores = {name: .3 * dev[name]['cagr_pct'] + .7 * validation[name]['cagr_pct']
              - .5 * abs(min(dev[name]['max_drawdown_pct'], validation[name]['max_drawdown_pct'])) for name in eligible}
    return (max(scores, key=scores.get) if scores else None), scores


def compare(csv_path, output, max_drawdown=.15):
    output.mkdir(parents=True, exist_ok=True)
    specifications = candidates(max_drawdown)
    data = CsvMarketDataProvider(csv_path).load(sorted(set(STOCKS + ETFS)))
    fingerprint = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    source_path = csv_path.parent / 'source.json'
    source = json.loads(source_path.read_text(encoding='utf-8')) if source_path.exists() else {}
    if source.get('sha256') != fingerprint or source.get('adjustment') != 'all':
        raise ValueError('The frozen data must match a source.json hash and explicitly use all adjustments.')
    settings = {name: {'config': asdict(spec[0]), 'strategy': type(spec[1]).__name__, 'parameters': vars(spec[1]), 'advisor': type(spec[2]).__name__}
                for name, spec in specifications.items()}
    results = []
    for period in ('development', 'validation'):
        for name, spec in specifications.items():
            row = {'candidate': name, 'period': period, 'slippage_bps': 5, **run_candidate(data, spec, PERIODS[period], 5)}
            results.append(row)
            print(f"{period:12} {name:28} return={row['return_pct']:7.2f}% dd={row['max_drawdown_pct']:7.2f}%", flush=True)
    selected, selection_scores = select_candidate(results, max_drawdown)
    selection = {'selected': selected, 'scores': selection_scores, 'data_sha256': fingerprint,
                 'selection_basis': 'positive development and validation, beats legacy validation, both drawdowns within configured limit; 30% development CAGR + 70% validation CAGR - 50% worst drawdown magnitude',
                 'profitability_proven': False, 'final_segment_used_for_selection': False,
                 'recorded_at': datetime.now(timezone.utc).isoformat()}
    # Persist the choice before any final-period result is calculated.
    (output / 'selection_before_final.json').write_text(json.dumps(selection, indent=2) + '\n', encoding='utf-8')
    print('Frozen selection before final evaluation:', selected, flush=True)
    for slippage in (5, 20):
        for name, spec in specifications.items():
            row = {'candidate': name, 'period': 'final', 'slippage_bps': slippage, **run_candidate(data, spec, PERIODS['final'], slippage)}
            results.append(row)
            print(f"final {slippage:2}bps {name:28} return={row['return_pct']:7.2f}% dd={row['max_drawdown_pct']:7.2f}%", flush=True)
    benchmarks = []
    for period, dates in PERIODS.items():
        for name, symbols, fraction in [('SPY_100pct', ['SPY'], 1), ('SPY_40pct', ['SPY'], .4), ('stocks_40pct', STOCKS, .4), ('ETFs_40pct', ETFS, .4)]:
            benchmarks.append({'candidate': name, 'period': period, 'slippage_bps': 5, **passive(data, symbols, dates, fraction, 5)})
    code_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
    payload = {'source': source, 'selection': selection, 'code_sha256': code_hashes, 'max_drawdown_threshold': max_drawdown, 'candidates': settings,
               'results': results, 'benchmarks': benchmarks,
               'limitations': ['Exploratory, historical periods may have been seen in earlier research.', 'Present-day stock universe has survivorship/selection bias.',
                               'All-adjusted OHLC is an approximation to total return, not literal tradable historical prices.',
                               'Passive benchmarks have no account circuit and may exceed the requested risk budget; compare return and drawdown together.',
                               '40% benchmark is initially capital-matched; actual exposures drift.',
                               'Monthly signals use the existing order planner; unavailable cash/slots can defer a rotation to a later review.',
                               'Circuit stops remain latched through each period; separate periods reset accounts.',
                               'Uninvested cash earns zero; no taxes, borrow, market impact or exact spread model.']}
    (output / 'results.json').write_text(json.dumps(payload, indent=2, default=lambda item: {'class': type(item).__name__, **vars(item)}) + '\n', encoding='utf-8')
    with (output / 'results.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]))
        writer.writeheader(); writer.writerows(results + benchmarks)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-drawdown', type=float, default=.15)
    args = parser.parse_args()
    compare(args.csv, args.output, args.max_drawdown)


if __name__ == '__main__':
    main()
