"""Explicitly configured live pilot, isolated from the paper desktop application.

Nothing in this module transfers funds or promotes a paper account. Check/status
are read-only at the broker. Only the explicit run command permits broker writes.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from .alpaca_integration import (
    AlpacaMarketDataProvider, AlpacaPaperBroker, _configure_request_timeouts,
    _load_alpaca_modules, _load_local_env,
)
from .journal import TradeJournal
from .models import Order, PortfolioSnapshot, Side
from .paper_app import _read, _write
from .runtime_paths import canonical_runtime_directory, default_paper_directory, runtime_home
from .time_utils import MARKET_TZ


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PROJECT / 'live-policy.json'
KNOWN_CASH_INCOME = {'DIV', 'DIVNRA', 'DIVFEE', 'INT', 'INTNRA', 'INTTW', 'FEE', 'PTC', 'PTR', 'CGD'}
EXTERNAL_CASH = {'CSD', 'CSW', 'ACATC'}


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a finite positive number.')
    return float(value)


def _number(value, name):
    if isinstance(value, bool):
        raise RuntimeError(f'Invalid broker {name}.')
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f'Missing broker {name}.') from exc
    if not math.isfinite(result):
        raise RuntimeError(f'Invalid broker {name}.')
    return result


def live_directory():
    return canonical_runtime_directory(runtime_home() / 'live')


@dataclass(frozen=True)
class LivePolicy:
    account_id: str
    capital_limit_eur: float
    max_total_loss_eur: float
    max_daily_loss_eur: float
    max_order_eur: float
    enabled: bool = False
    entry_limit_bps: float = 5.0
    feed: str = 'iex'

    def __post_init__(self):
        try:
            if str(UUID(self.account_id)) != self.account_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError('A lowercase, exact live account UUID is required.') from exc
        if not isinstance(self.enabled, bool):
            raise ValueError('enabled must be a boolean.')
        for name in ('capital_limit_eur', 'max_total_loss_eur', 'max_daily_loss_eur', 'max_order_eur'):
            _positive(getattr(self, name), name)
        if self.capital_limit_eur > 5000 or self.max_total_loss_eur > 1000:
            raise ValueError('Policy exceeds the user limits: EUR 5,000 capital / EUR 1,000 loss.')
        if not self.max_daily_loss_eur <= self.max_total_loss_eur <= self.capital_limit_eur:
            raise ValueError('Require daily loss <= total loss <= capital limit.')
        if self.max_order_eur > self.capital_limit_eur * 0.10:
            raise ValueError('A single entry cannot exceed 10% of configured capital.')
        if (isinstance(self.entry_limit_bps, bool) or not isinstance(self.entry_limit_bps, (int, float))
                or not math.isfinite(self.entry_limit_bps) or not 0 <= self.entry_limit_bps <= 25):
            raise ValueError('entry_limit_bps must be finite, between 0 and 25.')
        if self.feed not in {'iex', 'sip'}:
            raise ValueError('Live quotes must use an explicitly supported iex or sip feed.')

    @classmethod
    def load(cls, path=DEFAULT_POLICY):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f'Duplicate live policy field: {key}')
                result[key] = value
            return result
        try:
            value = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique)
            return cls(**value)
        except FileNotFoundError as exc:
            raise ValueError('Live policy is missing. Copy live-policy.example.json and configure it locally.') from exc

    @property
    def fingerprint(self):
        # Enabling/disabling never changes the financial policy or resets risk.
        values = asdict(self)
        values.pop('enabled')
        return hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _live_keys():
    _load_local_env()
    key, secret = os.getenv('ALPACA_LIVE_API_KEY'), os.getenv('ALPACA_LIVE_SECRET_KEY')
    if not key or not secret or key.startswith('YOUR_') or secret.startswith('YOUR_'):
        raise RuntimeError('Separate ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY are required in the ignored .env.')
    if key == os.getenv('ALPACA_API_KEY') or secret == os.getenv('ALPACA_SECRET_KEY'):
        raise RuntimeError('Live and paper credentials must be separate.')
    return key, secret


class AlpacaLiveMarketDataProvider(AlpacaMarketDataProvider):
    def __init__(self, days=600, feed='iex', exclude_date=None, *, adjustment='split'):
        if adjustment != 'split':
            raise ValueError('The frozen live candidate uses split-adjusted signal history.')
        modules = _load_alpaca_modules()
        key, secret = _live_keys()
        self.days, self.feed, self.exclude_date, self.adjustment = days, feed, exclude_date, adjustment
        self._request_cls = modules['StockBarsRequest']
        self._latest_quote_request = modules['StockLatestQuoteRequest']
        self._timeframe = modules['TimeFrame']
        self._client = _configure_request_timeouts(modules['StockHistoricalDataClient'](key, secret))
        self._client._retry = 0


class AlpacaLiveBroker(AlpacaPaperBroker):
    execution_mode = 'alpaca-live'

    def __init__(self, policy: LivePolicy, *, submit_enabled=False, directory=None, fx=None):
        self.policy = policy
        self.submit_enabled = bool(submit_enabled and policy.enabled)
        if submit_enabled and not policy.enabled:
            raise RuntimeError('Live policy is disabled. No broker writes are permitted.')
        self.directory = canonical_runtime_directory(directory or live_directory())
        paper = canonical_runtime_directory(default_paper_directory())
        if self.directory == paper or paper in self.directory.parents or self.directory in paper.parents:
            raise RuntimeError('Live and paper journals must be separate.')
        self.journal = TradeJournal(self.directory)
        # The daily reference rate is for EUR monitoring, not an executable FX quote.
        from .fx_budget import load_cached_reference_for_risk_only, load_ecb_reference_quote
        self._fx_injected = fx is not None
        self.fx_error = None
        try:
            self.fx = (fx or load_ecb_reference_quote(self.directory / 'fx-reference.json')).validate()
        except (RuntimeError, ValueError, OSError) as exc:
            if fx is not None:
                raise
            self.fx = load_cached_reference_for_risk_only(self.directory / 'fx-reference.json')
            self.fx_error = str(exc)
        if submit_enabled and self.fx_error and not (self.directory / 'live-risk.json').exists():
            raise RuntimeError('Fresh FX reference is required for first live initialization.')
        modules = _load_alpaca_modules()
        key, secret = _live_keys()
        for attr, name in (
            ('_order_side', 'OrderSide'), ('_order_type', 'OrderType'), ('_query_order_status', 'QueryOrderStatus'),
            ('_time_in_force', 'TimeInForce'), ('_get_orders_request', 'GetOrdersRequest'),
            ('_get_calendar_request', 'GetCalendarRequest'), ('_market_order_request', 'MarketOrderRequest'),
            ('_limit_order_request', 'LimitOrderRequest'), ('_replace_order_request', 'ReplaceOrderRequest'),
            ('_stop_order_request', 'StopOrderRequest'),
        ):
            setattr(self, attr, modules[name])
        self._configure_entry_policy(policy.entry_limit_bps)
        # Never reuse the paper URL override or accept an arbitrary live endpoint.
        self._client = _configure_request_timeouts(modules['TradingClient'](key, secret, paper=False))
        self._client._retry = 0
        self.cash, self.positions, self.position_prices, self.fills = 0.0, {}, {}, []
        self.account_id, self.last_submitted_order = None, None
        self.refresh()

    def _validate_account(self, account):
        if str(getattr(account, 'id', '')) != self.policy.account_id:
            raise RuntimeError('Live account differs from the pinned policy account. No state was reset.')
        status = getattr(account, 'status', '')
        if str(getattr(status, 'value', status)).upper() != 'ACTIVE' or getattr(account, 'currency', None) != 'USD':
            raise RuntimeError('This adapter requires an ACTIVE USD Trading API account; EUR/LCT accounts are unsupported.')
        for flag in ('trading_blocked', 'account_blocked', 'trade_suspended_by_user'):
            if getattr(account, flag, None) is not False:
                raise RuntimeError(f'Live account restriction or unknown flag: {flag}.')
        if _number(getattr(account, 'equity', None), 'equity') <= 0:
            raise RuntimeError('Live account has no positive equity.')
        for position in self.positions.values():
            try:
                raw = getattr(position, 'broker_quantity', None)
                quantity = Decimal(str(raw if raw is not None else position.quantity))
                supported = quantity.is_finite() and quantity >= 0 and quantity == quantity.to_integral_value()
            except InvalidOperation:
                supported = False
            if not supported:
                raise RuntimeError('Live whole-share operator cannot manage fractional or short holdings; reconcile them first.')

    def refresh(self):
        super().refresh()
        self._validate_account(self.account)
        from .fx_budget import load_ecb_reference_quote
        try:
            if not getattr(self, '_fx_injected', False):
                self.fx = load_ecb_reference_quote(self.directory / 'fx-reference.json')
            self.fx.validate()
            self.fx_error = None
        except (RuntimeError, ValueError, OSError) as exc:
            # Preserve known USD positions and broker-side protection even when
            # a daily informational FX feed is unavailable. New BUYs fail closed.
            self.fx_error = str(exc)
        available = [_number(getattr(self.account, field, None), field)
                     for field in ('cash', 'buying_power', 'non_marginable_buying_power')]
        fees = max(0.0, _number(getattr(self.account, 'accrued_fees', 0) or 0, 'accrued fees'))
        outbound = max(0.0, _number(getattr(self.account, 'pending_transfer_out', 0) or 0, 'pending transfer'))
        self.cash = max(0.0, min(available) - fees - outbound)
        self.risk_error = None
        if self.submit_enabled:
            try:
                self._observe_risk()
            except Exception as exc:
                # A cash-activity outage must not disable USD exits or existing
                # stop management. No new entry may use an unverified baseline.
                self.risk_error = str(exc)
                print(f'LIVE RISK MONITOR DEGRADED: {exc}. New buys blocked; existing-position exits remain permitted.')
            try:
                _write(self.directory / 'live-health.json', {
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                    'fx_warning': self.fx_error, 'risk_warning': self.risk_error,
                    'new_buys_blocked': bool(self.fx_error or self.risk_error),
                })
            except OSError:
                pass

    def sizing_equity(self, equity):
        return min(equity, float(self.fx.eur_to_usd(self.policy.capital_limit_eur)))

    def snapshot(self, prices, trade_date=None):
        # Buying power is a constraint on new entries, not the account balance.
        return PortfolioSnapshot(trade_date or datetime.now(MARKET_TZ).date(),
                                 _number(self.account.cash, 'cash'),
                                 sum(p.quantity * self.position_prices[s] for s, p in self.positions.items()),
                                 _number(self.account.equity, 'equity'))

    def constrain_buy_quantity(self, symbol, quantity, ask):
        if self.buy_rejection():
            return 0
        limit = self.maximum_entry_price(ask)
        gross = sum(p.quantity * self.position_prices[s] for s, p in self.positions.items())
        budget = min(float(self.fx.eur_to_usd(self.policy.max_order_eur)),
                     max(0.0, float(self.fx.eur_to_usd(self.policy.capital_limit_eur)) - gross),
                     max(0.0, self.cash - self.sizing_equity(float(self.account.equity)) * 0.08))
        return min(quantity, max(0, int(budget // limit)))

    def buy_rejection(self):
        state = _read(self.directory / 'live-risk.json')
        return (getattr(self, 'fx_error', None) or getattr(self, 'risk_error', None) or state.get('review_required')
                or ('Live loss halt is latched.' if state.get('loss_halted') else None))

    def _activities(self, state):
        # Re-read the small non-trade ledger from the baseline so corrections to
        # older cash activities cannot hide behind an incremental cursor.
        rows, seen_tokens, token = [], set(), None
        for _ in range(100):
            query = {'after': state['started_at'], 'direction': 'asc', 'page_size': 100,
                     'category': 'non_trade_activity'}
            if token:
                query['page_token'] = token
            page = self._client.get('/account/activities', query)
            if not isinstance(page, list) or len(page) > 100:
                raise RuntimeError('Invalid or incomplete live account activities response.')
            for row in page:
                if not isinstance(row, dict) or not row.get('id') or not row.get('activity_type'):
                    raise RuntimeError('Malformed account activity; new entries require review.')
            rows.extend(page)
            if len(page) < 100:
                return rows
            token = page[-1]['id']
            if token in seen_tokens:
                raise RuntimeError('Account activity pagination did not advance.')
            seen_tokens.add(token)
        raise RuntimeError('Account activities exceeded the safety pagination bound.')

    def _observe_risk(self):
        with self.journal.lock('live-risk'):
            path = self.directory / 'live-risk.json'
            state = _read(path)
            stamp = datetime.now(timezone.utc)
            equity = _number(self.account.equity, 'equity')
            if not state:
                journal_state = self.journal._read_state()
                if (journal_state.get('live_risk_initialized') or journal_state.get('order_intents')
                        or journal_state.get('submitted_order_keys') or self.journal.fills_path.exists()
                        or self.positions or self.open_orders()):
                    raise RuntimeError('First live initialization requires a flat dedicated account and an empty live journal.')
                anchor = stamp.astimezone(MARKET_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                state = {'account_id': self.policy.account_id, 'policy_sha256': self.policy.fingerprint,
                         'started_at': anchor.astimezone(timezone.utc).isoformat(), 'cash_flows_usd': 0.0,
                         'peak_eur': float(self.fx.usd_to_eur(equity)), 'activity_ids': [], 'activity_fingerprints': {},
                         'day': '', 'daily_peak_eur': float(self.fx.usd_to_eur(equity))}
                # Anchor pre-existing same-day activities without counting them
                # again as new funding. Require an unchanged ledger around the
                # baseline equity read instead of assuming these APIs are atomic.
                def fingerprints(rows):
                    values = {}
                    for row in rows:
                        key = row['id']
                        if key in values:
                            raise RuntimeError('Duplicate account activity identity.')
                        values[key] = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).hexdigest()
                    return values
                for _ in range(3):
                    before = fingerprints(self._activities(state))
                    baseline_account = self._client.get_account()
                    self._validate_account(baseline_account)
                    equity = _number(baseline_account.equity, 'equity')
                    after = fingerprints(self._activities(state))
                    if before == after:
                        state['activity_ids'] = sorted(after)
                        state['activity_fingerprints'] = after
                        state['peak_eur'] = state['daily_peak_eur'] = float(self.fx.usd_to_eur(equity))
                        break
                else:
                    raise RuntimeError('Cash activities changed during live initialization; retry after funding settles.')
            if state.get('account_id') != self.policy.account_id or state.get('policy_sha256') != self.policy.fingerprint:
                raise RuntimeError('Live risk policy/account changed; review the saved baseline before trading.')
            if (not isinstance(state.get('loss_halted', False), bool)
                    or not isinstance(state.get('activity_ids'), list)
                    or any(not isinstance(value, str) or not value for value in state['activity_ids'])
                    or len(set(state['activity_ids'])) != len(state['activity_ids'])
                    or not isinstance(state.get('activity_fingerprints'), dict)
                    or set(state['activity_fingerprints']) != set(state['activity_ids'])
                    or _number(state['peak_eur'], 'saved peak') < 0):
                raise RuntimeError('Live risk baseline is malformed; preserve it and reconcile before trading.')
            _number(state['cash_flows_usd'], 'saved cash flows')
            seen = set(state['activity_ids'])
            activities = self._activities(state)
            observed = set()
            for row in activities:
                observed.add(row['id'])
                fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).hexdigest()
                if row['id'] in seen:
                    if state['activity_fingerprints'].get(row['id']) != fingerprint:
                        state['review_required'] = 'Broker corrected a previously recorded non-trade activity; reconcile cash flows.'
                    continue
                kind = row['activity_type']
                if row.get('status', 'executed') != 'executed':
                    state['review_required'] = 'Non-trade activity is not confirmed executed; review its final cash effect.'
                elif kind in EXTERNAL_CASH:
                    state['cash_flows_usd'] += _number(row.get('net_amount'), 'cash activity amount')
                elif kind not in KNOWN_CASH_INCOME:
                    state['review_required'] = f'Unsupported non-trade activity {kind}; reconcile corporate actions/transfers.'
                else:
                    _number(row.get('net_amount'), 'non-trade cash amount')
                seen.add(row['id'])
                state['activity_fingerprints'][row['id']] = fingerprint
            if not set(state['activity_ids']).issubset(observed):
                state['review_required'] = 'Previously recorded non-trade activity is missing; reconcile account history.'
            if activities:
                # Read equity after the activity list to avoid mixing a pre-flow
                # balance with a newly observed deposit or withdrawal.
                current_account = self._client.get_account()
                self._validate_account(current_account)
                equity = _number(current_account.equity, 'equity')
                state['activity_cursor'] = activities[-1]['id']
            state['activity_ids'] = sorted(seen)
            normalized = float(self.fx.usd_to_eur(equity - state['cash_flows_usd']))
            state['peak_eur'] = max(_number(state['peak_eur'], 'saved peak'), normalized)
            day = stamp.astimezone(MARKET_TZ).date().isoformat()
            if state['day'] != day:
                # Overnight gaps count against today's loss budget.
                state['day'], state['daily_peak_eur'] = day, state.get('normalized_equity_eur', normalized)
            state['daily_peak_eur'] = max(_number(state['daily_peak_eur'], 'saved daily peak'), normalized)
            total_loss = max(0.0, state['peak_eur'] - normalized)
            daily_loss = max(0.0, state['daily_peak_eur'] - normalized)
            if total_loss >= self.policy.max_total_loss_eur or daily_loss >= self.policy.max_daily_loss_eur:
                state['loss_halted'] = True
                self.journal.latch_risk_halt('live EUR loss limit', {
                    'total_loss_eur': total_loss, 'daily_loss_eur': daily_loss,
                    'max_total_loss_eur': self.policy.max_total_loss_eur,
                    'max_daily_loss_eur': self.policy.max_daily_loss_eur,
                })
            state.update(updated_at=stamp.isoformat(), normalized_equity_eur=normalized,
                         total_loss_eur=total_loss, daily_loss_eur=daily_loss,
                         fx_date=self.fx.as_of.isoformat(), usd_per_eur=float(self.fx.usd_per_eur),
                         fx_warning=getattr(self, 'fx_error', None))
            _write(path, state)
            with self.journal.lock():
                journal_state = self.journal._read_state()
                journal_state['live_risk_initialized'] = True
                self.journal._write_state(journal_state)

    def _validate_trading_request(self, operation, *, order=None):
        if not self.submit_enabled or not self.policy.enabled:
            raise RuntimeError('Read-only live broker: order submission, replacement and cancellation are disabled.')
        self.refresh()
        if order and order.side is Side.BUY:
            if self.entry_limit_bps is None:
                raise RuntimeError('Live BUY requires a bounded IOC limit policy.')
            if (self.directory / 'STOP').exists() or (self.directory / 'STOP_BUYS').exists():
                raise RuntimeError('Live operator stop/pause is active.')
            state = _read(self.directory / 'live-risk.json')
            if self.buy_rejection():
                raise RuntimeError(self.buy_rejection())
            if state.get('review_required') or state.get('loss_halted') or self.journal._read_state().get('drawdown_halted'):
                raise RuntimeError(state.get('review_required') or 'Live loss halt is latched; new buys are blocked.')
            clock = self.market_clock()
            since_open = (clock.timestamp.astimezone(MARKET_TZ).hour * 60
                          + clock.timestamp.astimezone(MARKET_TZ).minute - 570)
            if not clock.is_open or since_open < 10 or (clock.next_close - clock.timestamp).total_seconds() <= 900:
                raise RuntimeError('Live entries require regular hours outside opening/closing buffers.')
            if self.constrain_buy_quantity(order.symbol, order.quantity, order.price) < order.quantity:
                raise RuntimeError('Live order exceeds current cash/capital/order limits.')
            asset = self._client.get_asset(order.symbol)
            asset_class = getattr(asset, 'asset_class', '')
            if (not getattr(asset, 'tradable', False)
                    or str(getattr(asset_class, 'value', asset_class)) != 'us_equity'):
                raise RuntimeError('Live entry asset is not a tradable US equity.')
        elif order and order.side is Side.SELL:
            held = self.positions.get(order.symbol)
            if not held or held.quantity < order.quantity:
                raise RuntimeError('Live SELL exceeds the confirmed holding.')

    def after_buy_submission(self):
        # Exercise the same confirmed-stop/replacement/recovery path as paper,
        # before another planned BUY can be submitted.
        from .cli import _alpaca_protect
        args = SimpleNamespace(journal_dir=str(self.directory), submit=True, stop_loss_pct=.10,
                               trailing_stop_pct=.10, _broker_override=self)
        try:
            for attempt in range(5):
                try:
                    _alpaca_protect(args)
                    from .paper_app import protected_symbols
                    positions = [{'symbol': s, 'quantity': p.quantity} for s, p in self.positions.items()]
                    tracked = {row.get('client_order_id') for row in self.journal._read_state().get('protective_orders', {}).values()}
                    orders = []
                    for remote in self.open_orders():
                        def value(name):
                            field = getattr(remote, name, '')
                            return str(getattr(field, 'value', field))
                        orders.append({'symbol': remote.symbol, 'side': value('side'), 'type': value('type'),
                                       'status': value('status'), 'quantity': float(remote.qty),
                                       'filled_quantity': float(remote.filled_qty or 0),
                                       'stop_price': float(remote.stop_price or 0),
                                       'managed': str(remote.client_order_id).startswith('bot-protect-')
                                       or str(remote.client_order_id) in tracked})
                    if set(protected_symbols(positions, orders)) != set(self.positions):
                        raise RuntimeError('Live holding lacks broker-confirmed stop coverage.')
                    return
                except RuntimeError as exc:
                    waiting = any(value in str(exc).lower() for value in (
                        'pending_new', 'pending_replace', 'lacks broker-confirmed stop coverage'))
                    if not waiting or attempt == 4:
                        raise
                    # Look up/confirm the same persisted client ID; never loosen
                    # the stop or issue an independent duplicate during this wait.
                    time.sleep(1.0)
        except Exception as exc:
            self.journal.latch_risk_halt('live entry protection unconfirmed')
            # Do not make an emergency exit depend on another historical-data
            # request in a retried signal cycle. The risk path uses broker marks
            # and preserves the normal stable-ID/cancellation/fill safeguards.
            try:
                self._emergency_risk_exit()
            except Exception as exit_error:
                raise RuntimeError(f'Live entry protection failed: {exc}. Emergency risk exit also requires review: {exit_error}') from exc
            raise RuntimeError(f'Live entry protection failed: {exc}. Emergency risk exit requested; halt remains latched.') from exc

    def _emergency_risk_exit(self):
        from .cli import _run_alpaca_risk_once
        args = candidate_session(self.policy, DEFAULT_POLICY)
        args._broker_override = self
        return _run_alpaca_risk_once(args)

    def cancel_open_sell_orders(self, symbol):
        tracked = {row.get('client_order_id') for row in self.journal._read_state().get('protective_orders', {}).values()}
        for remote in self.open_orders([symbol]):
            side = getattr(remote.side, 'value', remote.side)
            if (str(side).lower() == 'sell' and not str(remote.client_order_id).startswith('bot-')
                    and str(remote.client_order_id) not in tracked):
                raise RuntimeError('External SELL order conflicts with live management; reconcile it before replacement.')
        return super().cancel_open_sell_orders(symbol)


def candidate_session(policy, path):
    from .cli import _resolve_symbols
    return SimpleNamespace(
        command='alpaca-session', symbols=_resolve_symbols([], str(PROJECT / 'universe_popular.txt')),
        symbols_file=None, cash=policy.capital_limit_eur, days=600, feed=policy.feed,
        short_window=20, long_window=100, min_confidence=0.68, strategy='relative-strength', profile='custom',
        max_open_positions=4, max_daily_orders=2, max_position_pct=0.25, max_order_pct=0.10,
        target_cash_pct=0.08, stop_loss_pct=0.10, trailing_stop_pct=0.10,
        slippage_bps=5.0, commission=0.0, max_drawdown_stop_pct=0.15,
        journal_dir=str(live_directory()), allow_closed_submit=False,
        close_buffer_minutes=15.0, open_buffer_minutes=10.0, allow_near_open=False, allow_near_close=False,
        sell_only=False, allow_position_adds=False, buy_cooldown_days=3, submit=True,
        risk_interval_minutes=1.0, signal_interval_minutes=390.0, protective_stop_sync_minutes=5.0,
        no_protective_stops=False, cycles=0, until_close=False, wait_for_open=True, continuous=True,
        max_consecutive_errors=3, shadow_strategy=None, live_policy_path=str(Path(path).resolve()),
        live_policy_fingerprint=policy.fingerprint, capital_limit_eur=policy.capital_limit_eur,
        max_total_loss_eur=policy.max_total_loss_eur, max_daily_loss_eur=policy.max_daily_loss_eur,
        max_order_eur=policy.max_order_eur,
    )


def check(policy):
    from dataclasses import asdict
    from .reconcile import reconcile_positions, sync_broker_orders
    broker = AlpacaLiveBroker(policy, submit_enabled=False)
    sync_broker_orders(broker.journal, broker)
    result = reconcile_positions(broker.journal, broker)
    state = _read(broker.directory / 'live-risk.json')
    session = candidate_session(policy, DEFAULT_POLICY)
    clock = broker.market_clock()
    expected = broker.latest_completed_session()
    try:
        provider = AlpacaLiveMarketDataProvider(days=600, feed=policy.feed)
        data = provider.load(session.symbols)
        missing = []
        affordability = []
        entry_budget = min(float(broker.fx.eur_to_usd(policy.max_order_eur)),
                           min(float(broker.account.equity), float(broker.fx.eur_to_usd(policy.capital_limit_eur))) * .10)
        for symbol in session.symbols:
            rows = [row for row in data.get(symbol, []) if row.date <= expected]
            if len(rows) < 195 or rows[-1].date != expected:
                missing.append(symbol)
                continue
            if rows[-1].close > 0 and broker.maximum_entry_price(rows[-1].close) <= entry_budget:
                affordability.append(symbol)
        quote_errors = {}
        fresh_affordable = []
        if clock.is_open:
            for symbol in affordability:
                try:
                    ask = provider.latest_entry_prices([symbol])[symbol]
                    if broker.maximum_entry_price(ask) <= entry_budget:
                        fresh_affordable.append(symbol)
                except RuntimeError as exc:
                    quote_errors[symbol] = str(exc)
        data_check = {'feed': policy.feed, 'expected_completed_session': expected.isoformat(),
                      'missing_or_insufficient_history': missing,
                      'affordable_at_previous_close': affordability,
                      'entry_budget_usd': entry_budget,
                      'fresh_entry_quotes_checked': bool(clock.is_open), 'quote_errors': quote_errors}
        data_check['affordable_at_fresh_ask'] = fresh_affordable if clock.is_open else None
        data_ok = not missing and bool(fresh_affordable if clock.is_open else affordability)
    except Exception as exc:
        data_check, data_ok = {'error': str(exc)}, False
    initial_ok = bool(state) or not (broker.positions or broker.open_orders() or broker.journal.fills_path.exists()
                                    or broker.journal._read_state().get('live_risk_initialized'))
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(), 'broker_writes_enabled': False,
        'account_suffix': policy.account_id[-6:], 'capital_limit_eur': policy.capital_limit_eur,
        'max_total_loss_eur': policy.max_total_loss_eur, 'max_daily_loss_eur': policy.max_daily_loss_eur,
        'equity_usd': _number(broker.account.equity, 'equity'),
        'equity_eur_reference': float(broker.fx.usd_to_eur(float(broker.account.equity))),
        'usd_per_eur': float(broker.fx.usd_per_eur), 'fx_date': broker.fx.as_of.isoformat(),
        'fx_warning': broker.fx_error,
        'reconciliation_ok': result.ok, 'reconciliation_issues': [asdict(x) for x in result.issues],
        'market_data_check': data_check,
        'technical_checks_passed': bool(result.ok and data_ok and not broker.fx_error and initial_ok),
        'positions': [{'symbol': s, 'quantity': p.quantity} for s, p in broker.positions.items()],
        'new_account_requires_flat_start': not bool(state),
        'strategy_profitability_proven': False, 'configured_for_submission': policy.enabled,
        'risk_state': {k: v for k, v in state.items() if k != 'account_id'},
        'monitor_health': _read(broker.directory / 'live-health.json'),
    }
    _write(broker.directory / 'live-check.json', report)
    return report


def account_details():
    """Read the live account UUID locally without enabling any order path."""
    modules = _load_alpaca_modules()
    key, secret = _live_keys()
    client = _configure_request_timeouts(modules['TradingClient'](key, secret, paper=False))
    client._retry = 0
    try:
        account = client.get_account()
    except Exception as exc:
        raise RuntimeError(f'Live account inspection failed: {exc}') from exc
    status = getattr(account, 'status', '')
    return {'account_id': str(account.id), 'status': str(getattr(status, 'value', status)),
            'currency': account.currency, 'equity': str(account.equity), 'broker_writes_enabled': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Separate, explicitly configured live-money pilot. Default check is read-only.')
    parser.add_argument('action', choices=['account', 'check', 'run', 'status', 'stop', 'pause-buys', 'resume-buys'])
    parser.add_argument('--policy', default=str(DEFAULT_POLICY))
    parser.add_argument('--confirm-account', help='Exact pinned account UUID required for run')
    args = parser.parse_args(argv)
    directory = live_directory()
    if args.action in {'stop', 'pause-buys'}:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ('STOP' if args.action == 'stop' else 'STOP_BUYS')).touch()
        print('Live ' + args.action + ' requested. Existing broker orders remain active.')
        return 0
    if args.action == 'status':
        from .monitoring import operator_status
        print(json.dumps({'runtime_dir': str(directory), 'operator': operator_status(directory),
                          'risk': _read(directory / 'live-risk.json'),
                          'monitor_health': _read(directory / 'live-health.json'),
                          'stop_requested': (directory / 'STOP').exists(),
                          'buys_paused': (directory / 'STOP_BUYS').exists()}, indent=2))
        return 0
    try:
        if args.action == 'account':
            print(json.dumps(account_details(), indent=2))
            return 0
        policy = LivePolicy.load(args.policy)
        if args.action == 'resume-buys':
            if TradeJournal(directory)._read_state().get('drawdown_halted'):
                raise RuntimeError('Live loss halt is latched; it cannot be cleared by Resume buys.')
            (directory / 'STOP_BUYS').unlink(missing_ok=True)
            return 0
        if args.action == 'check':
            report = check(policy)
            print(json.dumps(report, indent=2))
            return 0 if report['technical_checks_passed'] else 2
        if not policy.enabled or args.confirm_account != policy.account_id:
            raise RuntimeError('Run requires enabled policy and --confirm-account matching its exact account UUID.')
        # The live lock is independent of paper and held for the whole operator.
        journal = TradeJournal(directory)
        with TradeJournal(runtime_home() / 'locks').lock('live-execution', timeout=0), journal.lock('execution', timeout=0):
            # Explicit Run clears an earlier completed stop before networking.
            # A new Stop during preflight must win and remain persisted.
            (directory / 'STOP').unlink(missing_ok=True)
            report = check(policy)
            if (directory / 'STOP').exists():
                raise RuntimeError('Live start canceled by Stop during preflight.')
            if not report['technical_checks_passed']:
                raise RuntimeError('Live technical preflight failed. Review live-check.json; no operator was started.')
            AlpacaLiveBroker(policy, submit_enabled=True)
            if (directory / 'STOP').exists():
                raise RuntimeError('Live start canceled by Stop during initialization.')
            from .cli import _run_alpaca_session
            print('LIVE MONEY: frozen whole-share candidate. Loss triggers cannot guarantee realized loss limits.')
            from .paper_app import _keep_system_awake
            with _keep_system_awake() as power:
                if power['power_warning']:
                    print(power['power_warning'])
                _run_alpaca_session(candidate_session(policy, args.policy))
        return 0
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        print(f'Live operation blocked: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
