"""Transparent ETF strategy candidates for historical research only.

This module is deliberately absent from the live strategy factory. Its rules
are hypotheses to evaluate, not evidence that the strategy will be profitable.
"""

from __future__ import annotations

from math import isfinite

from .models import Candle, Side, Signal
from .strategies import Strategy


class MonthlyTrendStrategy(Strategy):
    """Review a long-only trend rule after the month's first supplied session.

    A close above its simple moving average is eligible for a BUY. With top_k
    set, only the strongest positive-momentum eligible symbols are selected;
    the other sufficiently observed symbols receive SELL. Momentum compares
    the close momentum_window sessions ago with the close skip_recent sessions
    ago: default 126-to-21-session momentum. No ranking is applied by default.

    The review uses completed supplied candles, never a future exchange
    calendar. The caller must supply ordered histories of completed sessions.
    Signals are evaluated after the first available session in a new month;
    TradingEngine executes accepted orders at the following session's open.
    Other days emit HOLD. A BUY is emitted on every eligible monthly review,
    so an earlier stop can re-enter at a later review without needing a cross.

    This is a selection rule, not an exact portfolio rebalance. Position sizes,
    exits caused by stops and available cash are controlled by the engine. A
    rejected monthly BUY waits until the next monthly review: the existing
    portfolio planner does not pre-spend pending sale proceeds or free occupied
    position slots. Restarting with the same history reproduces the same
    signal, because the strategy has no mutable evaluation state.

    Confidence is a fixed routing value, not a calibrated profit probability.
    """

    def __init__(
        self,
        trend_window: int = 200,
        momentum_window: int = 126,
        skip_recent: int = 21,
        top_k: int | None = None,
        confidence: float = 0.90,
    ) -> None:
        for name, value in (("trend_window", trend_window), ("momentum_window", momentum_window)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if isinstance(skip_recent, bool) or not isinstance(skip_recent, int) or not 0 <= skip_recent < momentum_window:
            raise ValueError("skip_recent must be an integer from zero to momentum_window - 1.")
        if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0):
            raise ValueError("top_k must be a positive integer or None.")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not isfinite(confidence) or not 0 < confidence <= 1:
            raise ValueError("confidence must be a finite number greater than zero and at most one.")
        self.trend_window = trend_window
        self.momentum_window = momentum_window
        self.skip_recent = skip_recent
        self.top_k = top_k
        self.confidence = confidence

    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        return self.generate_many({symbol: history})[0]

    def generate_many(self, histories: dict[str, list[Candle]]) -> list[Signal]:
        signals = {
            symbol: Signal(symbol, Side.HOLD, 0.0, "monthly trend: insufficient history")
            for symbol in sorted(histories)
        }
        observed = [history for history in histories.values() if history]
        if not observed:
            return list(signals.values())

        current_date = max(history[-1].date for history in observed)
        preceding_dates = [
            history[-2].date if history[-1].date == current_date else history[-1].date
            for history in observed
            if history[-1].date != current_date or len(history) >= 2
        ]
        if not preceding_dates:
            return list(signals.values())
        previous_date = max(preceding_dates)
        review = (current_date.year, current_date.month) != (previous_date.year, previous_date.month)
        required = max(2, self.trend_window, self.momentum_window + 1 if self.top_k is not None else 0)
        eligible: list[tuple[str, float]] = []
        evaluated: set[str] = set()

        for symbol, history in histories.items():
            if not history or history[-1].date != current_date:
                signals[symbol] = Signal(symbol, Side.HOLD, 0.0, "monthly trend: missing current session")
                continue
            if len(history) < required:
                continue
            if not review:
                signals[symbol] = Signal(symbol, Side.HOLD, 0.0, "monthly trend: waiting for monthly review")
                continue

            closes = [candle.close for candle in history[-required:]]
            if any(not isfinite(value) or value <= 0 for value in closes):
                signals[symbol] = Signal(symbol, Side.HOLD, 0.0, "monthly trend: invalid prices")
                continue
            evaluated.add(symbol)
            trend_average = sum(closes[-self.trend_window:]) / self.trend_window
            momentum = 0.0
            if self.top_k is not None:
                momentum = closes[-self.skip_recent - 1] / closes[-self.momentum_window - 1] - 1
            if closes[-1] > trend_average and (self.top_k is None or momentum > 0):
                eligible.append((symbol, momentum))

        # Stable ticker ordering makes tied rankings independent of dict order.
        eligible.sort(key=lambda item: (-item[1], item[0]))
        selected = {symbol for symbol, _ in (eligible if self.top_k is None else eligible[:self.top_k])}
        for symbol in evaluated:
            if symbol in selected:
                signals[symbol] = Signal(symbol, Side.BUY, self.confidence, "monthly trend: selected above moving average")
            else:
                signals[symbol] = Signal(symbol, Side.SELL, self.confidence, "monthly trend: outside eligible selection")
        return list(signals.values())
