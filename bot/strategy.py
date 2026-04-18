import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from bot.market import WeatherMarket
from bot.weather import WeatherForecast

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    market: WeatherMarket
    side: str           # "yes" or "no"
    edge: float
    forecast_prob: float
    market_price: float


@dataclass
class TradeRecord:
    timestamp: str
    market_id: str
    question: str
    side: str
    forecast_prob: float
    market_price: float
    edge: float
    amount_usdc: float
    resolved_outcome: Optional[str] = None  # "yes" / "no" after settlement
    pnl: Optional[float] = None


class Strategy:
    def __init__(
        self,
        min_edge: float = 0.08,
        trade_amount_usdc: float = 10.0,
        min_volume_usdc: float = 1000.0,
        log_path: str = "trades.jsonl",
    ):
        self._min_edge = min_edge
        self._trade_amount = trade_amount_usdc
        self._min_volume = min_volume_usdc
        self._log_path = log_path
        self._edge_adjustment = 0.0     # raised when win-rate is low

    def _forecast_prob(self, market: WeatherMarket, forecast: WeatherForecast) -> tuple[float, bool]:
        """Return (forecast_prob, is_temp_market)."""
        parsed = self._parse_temp_market(market.question)
        if parsed:
            threshold_c, direction, use_max = parsed
            raw_temp = forecast.temp_max_c if use_max else forecast.temp_min_c
            return self._temp_prob(raw_temp, threshold_c, direction), True
        return forecast.precip_prob, False

    def evaluate(self, market: WeatherMarket, forecast: WeatherForecast) -> Optional[TradeSignal]:
        if market.volume < self._min_volume or not market.active:
            return None

        forecast_prob, _ = self._forecast_prob(market, forecast)
        edge = forecast_prob - market.yes_price
        if abs(edge) < (self._min_edge + self._edge_adjustment):
            return None

        side = "yes" if edge > 0 else "no"
        market_price = market.yes_price if side == "yes" else market.no_price
        return TradeSignal(
            market=market,
            side=side,
            edge=edge,
            forecast_prob=forecast_prob,
            market_price=market_price,
        )

    def top_candidates(
        self,
        market: WeatherMarket,
        forecast: WeatherForecast,
    ) -> Optional[tuple[float, float, bool]]:
        """Return (forecast_prob, edge, is_temp) for any evaluated market, ignoring edge threshold."""
        if market.volume < self._min_volume or not market.active:
            return None
        forecast_prob, is_temp = self._forecast_prob(market, forecast)
        edge = forecast_prob - market.yes_price
        return forecast_prob, edge, is_temp

    # ------------------------------------------------------------------
    # Temperature market helpers
    # ------------------------------------------------------------------

    # Matches "80°F", "30.5°C", "75 degrees F", "75 F" etc.
    _TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°\s*|degrees?\s+)([FC])\b", re.IGNORECASE)

    @staticmethod
    def _parse_temp_market(
        question: str,
    ) -> Optional[tuple[float, str, bool]]:
        """Return (threshold_c, direction, use_max) for temp markets, else None."""
        q = question.lower()
        if not any(kw in q for kw in ("temperature", "degrees", "°f", "°c")):
            return None

        m = Strategy._TEMP_RE.search(question)
        if not m:
            return None

        value = float(m.group(1))
        unit = m.group(2).upper()
        threshold_c = (value - 32) * 5 / 9 if unit == "F" else value

        direction = "below" if any(w in q for w in ("below", "under", "drop", "fall")) else "above"
        use_max = "low" not in q or "high" in q

        return threshold_c, direction, use_max

    @staticmethod
    def _temp_prob(forecast_temp_c: float, threshold_c: float, direction: str, sigma: float = 3.0) -> float:
        """Logistic estimate of P(temp exceeds/falls below threshold).

        sigma=3°C represents typical short-range forecast uncertainty.
        """
        p = 1.0 / (1.0 + math.exp(-(forecast_temp_c - threshold_c) / sigma))
        return p if direction == "above" else 1.0 - p

    def log_trade(self, signal: TradeSignal, amount_usdc: float):
        record = TradeRecord(
            timestamp=datetime.utcnow().isoformat(),
            market_id=signal.market.market_id,
            question=signal.market.question,
            side=signal.side,
            forecast_prob=signal.forecast_prob,
            market_price=signal.market_price,
            edge=signal.edge,
            amount_usdc=amount_usdc,
        )
        with open(self._log_path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def self_learn(self):
        """Adjust edge threshold from resolved trade history (min 20 samples)."""
        if not os.path.exists(self._log_path):
            return

        resolved = []
        with open(self._log_path) as f:
            for line in f:
                r = json.loads(line)
                if r.get("resolved_outcome"):
                    resolved.append(r)

        if len(resolved) < 20:
            return

        win_rate = sum(1 for r in resolved if r["side"] == r["resolved_outcome"]) / len(resolved)

        # win_rate < 55% → raise threshold (more selective)
        # win_rate > 65% → lower threshold slightly (more aggressive)
        if win_rate < 0.55:
            self._edge_adjustment = min(self._edge_adjustment + 0.01, 0.10)
        elif win_rate > 0.65:
            self._edge_adjustment = max(self._edge_adjustment - 0.005, -0.03)

        logger.info(
            "Self-learn: win_rate=%.1f%% trades=%d edge_adj=%+.3f",
            win_rate * 100, len(resolved), self._edge_adjustment,
        )
