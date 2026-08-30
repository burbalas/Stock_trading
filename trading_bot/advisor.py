from __future__ import annotations

from typing import Protocol

from .models import Candle, Signal, Side


class Advisor(Protocol):
    def review(self, signal: Signal, history: list[Candle]) -> Signal:
        """Review or adjust a strategy signal."""


class HeuristicAdvisor:
    """Small local advisor that behaves like a replaceable AI decision layer."""

    def review(self, signal: Signal, history: list[Candle]) -> Signal:
        if signal.side is Side.HOLD or len(history) < 5:
            return signal

        recent = history[-5:]
        closes = [candle.close for candle in recent]
        momentum = (closes[-1] - closes[0]) / closes[0]
        volume_avg = sum(candle.volume for candle in recent[:-1]) / max(1, len(recent) - 1)
        volume_boost = recent[-1].volume > volume_avg * 1.15

        adjusted = signal.confidence
        notes = [signal.reason]

        if signal.side is Side.BUY and momentum > 0:
            adjusted += 0.08
            notes.append("positive 5-candle momentum")
        elif signal.side is Side.SELL and momentum < 0:
            adjusted += 0.08
            notes.append("negative 5-candle momentum")
        else:
            adjusted -= 0.05
            notes.append("momentum does not confirm")

        if volume_boost:
            adjusted += 0.03
            notes.append("volume confirmation")

        return Signal(
            symbol=signal.symbol,
            side=signal.side,
            confidence=max(0.0, min(1.0, adjusted)),
            reason="; ".join(notes),
        )
