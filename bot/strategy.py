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

    def evaluate(self, market: WeatherMarket, forecast: WeatherForecast) -> Optional[TradeSignal]:
        if market.volume < self._min_volume or not market.active:
            return None
        if self._MONTHLY_RE.search(market.question):
            return None

        forecast_prob, _ = self._forecast_prob(market, forecast)
        if forecast_prob is None:
            return None
        edge = forecast_prob - market.yes_price
        if abs(edge) < (self._min_edge + self._edge_adjustment):
            return None

        side = "yes" if edge > 0 else "no"
        market_price = market.yes_price if side == "yes" else market.no_price
        if market_price < 0.02:  # no realistic ask/bid — token has no liquidity
            return None
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
        """Return (forecast_prob, edge, is_temp) — volume check done by caller.
        Returns None for market types the model cannot evaluate."""
        if self._MONTHLY_RE.search(market.question):
            return None
        forecast_prob, is_temp = self._forecast_prob(market, forecast)
        if forecast_prob is None:
            return None
        edge = forecast_prob - market.yes_price
        return forecast_prob, edge, is_temp

    # ------------------------------------------------------------------
    # Temperature market helpers
    # ------------------------------------------------------------------

    # "between 48°F and 49°F"  →  groups: (48, F, 49)
    _TEMP_RANGE_AND = re.compile(
        r"between\s+(\d+(?:\.\d+)?)\s*°?\s*([FC])\s+and\s+(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    # "between 59-60°F" or "between 50–51 °F"  →  groups: (59, 60, F)
    _TEMP_RANGE_DASH = re.compile(
        r"between\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*°?\s*([FC])\b",
        re.IGNORECASE,
    )
    # Single threshold: "80°F", "30.5°C", "75 degrees F"
    _TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°\s*|degrees?\s+)([FC])\b", re.IGNORECASE)
    # Monthly accumulation market: ends with "in April", "in March 2026", etc.
    _MONTHLY_RE = re.compile(
        r"\bin\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
        r"|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s*(?:\d{4}\s*)?\??\s*$",
        re.IGNORECASE,
    )
    # Binary daily rain/precip: must mention rain/precip AND have a specific date
    _PRECIP_RAIN_RE = re.compile(r"\b(?:rain(?:fall)?|precipitation)\b", re.IGNORECASE)
    _PRECIP_DATE_RE = re.compile(
        r"\bon\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
        r"|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+\d{1,2}\b",
        re.IGNORECASE,
    )
    # Patterns that disqualify a precip market from binary daily rain model
    _PRECIP_SKIP_RE = re.compile(
        r"\b(?:snow(?:fall)?|blizzard|hurricane|typhoon|tornado|flood|drought"
        r"|first|last|record|total|average|monthly|seasonal|season|annual)\b"
        r"|\d+\s*(?:mm|cm|inch(?:es)?)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _parse_temp_market(question: str) -> Optional[tuple]:
        """Return tagged tuple for temp markets, else None.

        Range:      ("range", lo_c, hi_c, use_max)
        Directional: ("dir",  direction, threshold_c, use_max)
        """
        q = question.lower()
        if not any(kw in q for kw in ("temperature", "degrees", "°f", "°c")):
            return None

        use_max = "low" not in q or "high" in q

        # Range: "between 48°F and 49°F"
        m = Strategy._TEMP_RANGE_AND.search(question)
        if m:
            v1, unit, v2 = float(m.group(1)), m.group(2).upper(), float(m.group(3))
            f = lambda v: (v - 32) * 5 / 9 if unit == "F" else v
            lo_c, hi_c = f(v1), f(v2)
            if hi_c - lo_c < 1.5:
                return None  # too narrow for logistic model (σ=3°C)
            return ("range", lo_c, hi_c, use_max)

        # Range: "between 59-60°F"
        m = Strategy._TEMP_RANGE_DASH.search(question)
        if m:
            v1, v2, unit = float(m.group(1)), float(m.group(2)), m.group(3).upper()
            f = lambda v: (v - 32) * 5 / 9 if unit == "F" else v
            lo_c, hi_c = f(v1), f(v2)
            if hi_c - lo_c < 1.5:
                return None  # too narrow for logistic model (σ=3°C)
            return ("range", lo_c, hi_c, use_max)

        # Directional or exact temperature
        m = Strategy._TEMP_RE.search(question)
        if not m:
            return None
        value, unit = float(m.group(1)), m.group(2).upper()
        threshold_c = (value - 32) * 5 / 9 if unit == "F" else value

        if re.search(r"\b(below|under|drop|fall|lower)\b|or less", q):
            return ("dir", "below", threshold_c, use_max)
        if re.search(r"\b(higher|exceed|above|over)\b|or more|at least", q):
            return ("dir", "above", threshold_c, use_max)
        # No directional word → exact single-degree market; too narrow to model
        return None

    _TEMP_KEYWORDS = frozenset(("temperature", "degrees", "°f", "°c"))

    def _forecast_prob(self, market: WeatherMarket, forecast: WeatherForecast) -> tuple[Optional[float], bool]:
        """Return (forecast_prob, is_temp_market).

        Returns (None, *) for any market the model cannot evaluate; callers
        must treat None as "skip this market".  Only two types are modelled:
          1. Directional / wide-range temperature markets → logistic(σ=3°C)
          2. Binary daily rain/precip markets with a specific date → precip_prob
        All other markets (snowfall events, amounts, monthly, exact temps, etc.)
        return None and are silently skipped.
        """
        parsed = self._parse_temp_market(market.question)
        if parsed is None:
            q = market.question.lower()
            if any(kw in q for kw in self._TEMP_KEYWORDS):
                return None, True   # temperature market but unmodelable
            # Only accept binary daily rain/precip with a concrete date
            if (self._PRECIP_RAIN_RE.search(market.question)
                    and self._PRECIP_DATE_RE.search(market.question)
                    and not self._PRECIP_SKIP_RE.search(market.question)):
                return forecast.precip_prob, False
            return None, False      # unrecognised market type — skip

        if parsed[0] == "range":
            _, lo_c, hi_c, use_max = parsed
            raw_temp = forecast.temp_max_c if use_max else forecast.temp_min_c
            return self._temp_range_prob(raw_temp, lo_c, hi_c), True

        _, direction, threshold_c, use_max = parsed
        raw_temp = forecast.temp_max_c if use_max else forecast.temp_min_c
        return self._temp_prob(raw_temp, threshold_c, direction), True

    @staticmethod
    def _temp_prob(forecast_temp_c: float, threshold_c: float, direction: str, sigma: float = 3.0) -> float:
        """P(temp exceeds/falls below threshold) via logistic function."""
        p = 1.0 / (1.0 + math.exp(-(forecast_temp_c - threshold_c) / sigma))
        return p if direction == "above" else 1.0 - p

    @staticmethod
    def _temp_range_prob(forecast_temp_c: float, lo_c: float, hi_c: float, sigma: float = 3.0) -> float:
        """P(lo <= temp <= hi) via logistic distribution."""
        p_above_lo = 1.0 / (1.0 + math.exp(-(forecast_temp_c - lo_c) / sigma))
        p_above_hi = 1.0 / (1.0 + math.exp(-(forecast_temp_c - hi_c) / sigma))
        return max(0.0, p_above_lo - p_above_hi)

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
