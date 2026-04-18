import json
import logging
import os
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

        edge = forecast.precip_prob - market.yes_price
        if abs(edge) < (self._min_edge + self._edge_adjustment):
            return None

        side = "yes" if edge > 0 else "no"
        market_price = market.yes_price if side == "yes" else market.no_price
        return TradeSignal(
            market=market,
            side=side,
            edge=edge,
            forecast_prob=forecast.precip_prob,
            market_price=market_price,
        )

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
