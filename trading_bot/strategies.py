from __future__ import annotations

from abc import ABC, abstractmethod
from math import sqrt
from statistics import median

from .models import Candle, Signal, Side


class Strategy(ABC):
    @abstractmethod
    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        """Generate a signal from candles available up to the current point."""

    def generate_many(self, histories: dict[str, list[Candle]]) -> list[Signal]:
        """Generate signals with optional access to cross-sectional context."""
        return [
            self.generate(symbol, history)
            for symbol, history in sorted(histories.items())
            if history
        ]


def generate_strategy_signals(strategy, histories: dict[str, list[Candle]]) -> list[Signal]:
    generate_many = getattr(strategy, "generate_many", None)
    if callable(generate_many):
        return generate_many(histories)
    return [
        strategy.generate(symbol, history)
        for symbol, history in sorted(histories.items())
        if history
    ]


class EnsembleStrategy(Strategy):
    def __init__(self, strategies: list[Strategy]) -> None:
        if not strategies:
            raise ValueError("At least one strategy is required.")
        self.strategies = strategies

    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        signals = [strategy.generate(symbol, history) for strategy in self.strategies]
        active = [signal for signal in signals if signal.side is not Side.HOLD]
        if not active:
            return Signal(symbol=symbol, side=Side.HOLD, confidence=0.0, reason="ensemble: no active signals")

        buy_score = sum(signal.confidence for signal in active if signal.side is Side.BUY)
        sell_score = sum(signal.confidence for signal in active if signal.side is Side.SELL)
        side = Side.BUY if buy_score > sell_score else Side.SELL
        aligned = [signal for signal in active if signal.side is side]
        opposed = [signal for signal in active if signal.side is not side]
        confidence = sum(signal.confidence for signal in aligned) / len(aligned)
        confidence += 0.05 * max(0, len(aligned) - 1)
        confidence -= 0.08 * len(opposed)
        confidence = max(0.0, min(0.95, confidence))
        reasons = [signal.reason for signal in aligned]
        return Signal(symbol=symbol, side=side, confidence=confidence, reason="ensemble: " + " | ".join(reasons))


class MovingAverageCrossStrategy(Strategy):
    def __init__(self, short_window: int = 10, long_window: int = 30) -> None:
        if short_window >= long_window:
            raise ValueError("short_window must be smaller than long_window.")
        self.short_window = short_window
        self.long_window = long_window

    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        if len(history) < self.long_window + 1:
            return Signal(symbol=symbol, side=Side.HOLD, confidence=0.0, reason="not enough history")

        previous = history[:-1]
        prev_short = self._average(previous[-self.short_window :])
        prev_long = self._average(previous[-self.long_window :])
        current_short = self._average(history[-self.short_window :])
        current_long = self._average(history[-self.long_window :])

        gap = abs(current_short - current_long) / max(history[-1].close, 0.01)
        confidence = min(0.90, 0.55 + gap * 8)

        if prev_short <= prev_long and current_short > current_long:
            return Signal(symbol=symbol, side=Side.BUY, confidence=confidence, reason="bullish MA cross")
        if prev_short >= prev_long and current_short < current_long:
            return Signal(symbol=symbol, side=Side.SELL, confidence=confidence, reason="bearish MA cross")

        return Signal(symbol=symbol, side=Side.HOLD, confidence=0.0, reason="no crossover")

    @staticmethod
    def _average(candles: list[Candle]) -> float:
        return sum(candle.close for candle in candles) / len(candles)


class MomentumStrategy(Strategy):
    def __init__(self, lookback: int = 20, threshold_pct: float = 0.04) -> None:
        self.lookback = lookback
        self.threshold_pct = threshold_pct

    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        if len(history) < self.lookback + 1:
            return Signal(symbol=symbol, side=Side.HOLD, confidence=0.0, reason="not enough momentum history")

        start = history[-self.lookback - 1].close
        end = history[-1].close
        move = (end - start) / start
        confidence = min(0.90, 0.50 + abs(move) * 3)

        if move >= self.threshold_pct:
            return Signal(symbol=symbol, side=Side.BUY, confidence=confidence, reason=f"{self.lookback}-bar momentum up")
        if move <= -self.threshold_pct:
            return Signal(symbol=symbol, side=Side.SELL, confidence=confidence, reason=f"{self.lookback}-bar momentum down")
        return Signal(symbol=symbol, side=Side.HOLD, confidence=0.0, reason="momentum below threshold")


class MeanReversionStrategy(Strategy):
    def __init__(self, window: int = 20, threshold_pct: float = 0.06) -> None:
        self.window = window
        self.threshold_pct = threshold_pct

    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        if len(history) < self.window:
            return Signal(symbol=symbol, side=Side.HOLD, confidence=0.0, reason="not enough reversion history")

        average = self._average(history[-self.window :])
        latest = history[-1].close
        distance = (latest - average) / average
        confidence = min(0.85, 0.50 + abs(distance) * 3)

        if distance <= -self.threshold_pct:
            return Signal(symbol=symbol, side=Side.BUY, confidence=confidence, reason="price below rolling average")
        if distance >= self.threshold_pct:
            return Signal(symbol=symbol, side=Side.SELL, confidence=confidence, reason="price above rolling average")
        return Signal(symbol=symbol, side=Side.HOLD, confidence=0.0, reason="price near rolling average")

    @staticmethod
    def _average(candles: list[Candle]) -> float:
        return sum(candle.close for candle in candles) / len(candles)


class RegimeRelativeStrengthStrategy(Strategy):
    """Ranks a universe by volatility-adjusted momentum in healthy regimes."""

    def __init__(
        self,
        momentum_window: int = 189,
        trend_window: int = 100,
        volatility_window: int = 20,
        skip_recent: int = 5,
        top_n: int = 4,
        min_breadth: float = 0.60,
    ) -> None:
        if min(momentum_window, trend_window, volatility_window, top_n) <= 0:
            raise ValueError("Strategy windows and top_n must be positive.")
        if skip_recent < 0:
            raise ValueError("skip_recent cannot be negative.")
        if not 0 < min_breadth <= 1:
            raise ValueError("min_breadth must be between 0 and 1.")
        self.momentum_window = momentum_window
        self.trend_window = trend_window
        self.volatility_window = volatility_window
        self.skip_recent = skip_recent
        self.top_n = top_n
        self.min_breadth = min_breadth

    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        return Signal(
            symbol=symbol,
            side=Side.HOLD,
            confidence=0.0,
            reason="relative strength requires cross-sectional context",
        )

    def generate_many(self, histories: dict[str, list[Candle]]) -> list[Signal]:
        metrics: dict[str, tuple[float, float, bool]] = {}
        required = max(
            self.trend_window,
            self.momentum_window + self.skip_recent + 1,
            self.volatility_window + 1,
        )

        for symbol, history in histories.items():
            if len(history) < required:
                continue
            closes = [candle.close for candle in history]
            momentum_end_index = -self.skip_recent - 1 if self.skip_recent else -1
            momentum_start_index = -(self.momentum_window + self.skip_recent + 1)
            start = closes[momentum_start_index]
            end = closes[momentum_end_index]
            if start <= 0:
                continue
            momentum = end / start - 1
            recent = closes[-(self.volatility_window + 1) :]
            daily_returns = [current / previous - 1 for previous, current in zip(recent, recent[1:]) if previous > 0]
            volatility = self._annualized_volatility(daily_returns)
            score = momentum / max(volatility, 0.08)
            trend_average = sum(closes[-self.trend_window :]) / self.trend_window
            metrics[symbol] = (score, momentum, closes[-1] > trend_average)

        minimum_universe = max(3, self.top_n)
        if len(metrics) < minimum_universe:
            return [
                Signal(symbol, Side.HOLD, 0.0, "not enough relative-strength history")
                for symbol in sorted(histories)
            ]

        breadth = sum(1 for _, _, above_trend in metrics.values() if above_trend) / len(metrics)
        market_momentum = median(momentum for _, momentum, _ in metrics.values())
        regime_on = breadth >= self.min_breadth and market_momentum > 0
        eligible = sorted(
            (
                (symbol, score)
                for symbol, (score, momentum, above_trend) in metrics.items()
                if above_trend and momentum > 0 and score > 0
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        selected = {symbol: rank for rank, (symbol, _) in enumerate(eligible[: self.top_n])}

        signals: list[Signal] = []
        for symbol in sorted(histories):
            if symbol not in metrics:
                signals.append(Signal(symbol, Side.HOLD, 0.0, "not enough relative-strength history"))
                continue
            score, momentum, above_trend = metrics[symbol]
            if not regime_on:
                signals.append(Signal(symbol, Side.SELL, 0.90, "market regime is risk-off"))
            elif symbol in selected:
                rank = selected[symbol]
                rank_boost = 0.18 * (self.top_n - rank) / self.top_n
                confidence = min(0.95, 0.70 + rank_boost + min(0.07, max(0.0, score) * 0.03))
                signals.append(
                    Signal(symbol, Side.BUY, confidence, "regime on; top cross-sectional relative strength")
                )
            elif not above_trend or momentum <= 0:
                signals.append(Signal(symbol, Side.SELL, 0.76, "trend or momentum failed"))
            else:
                signals.append(Signal(symbol, Side.HOLD, 0.0, "positive trend but outside leadership group"))
        return signals

    @staticmethod
    def _annualized_volatility(returns: list[float]) -> float:
        if len(returns) < 2:
            return 0.0
        average = sum(returns) / len(returns)
        variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
        return sqrt(variance) * sqrt(252)
