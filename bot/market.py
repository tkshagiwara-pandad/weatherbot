import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
WEATHER_KEYWORDS = ["rain", "precipitation", "temperature", "snow", "storm", "weather", "humidity"]

# 略称 → Visual Crossing が認識する正式名
CITY_ALIASES: dict[str, str] = {
    "NYC": "New York City",
    "NY": "New York City",
    "LA": "Los Angeles",
    "DC": "Washington DC",
    "SF": "San Francisco",
}

# Polymarket に実際に存在する天気マーケットの都市リスト
# 質問文にこのいずれかが含まれる場合のみ処理する
KNOWN_CITIES: set[str] = {
    "New York City", "New York", "NYC", "NY",
    "Los Angeles", "LA",
    "Chicago",
    "Houston",
    "Miami",
    "Dallas",
    "Seattle",
    "San Francisco", "SF",
    "Boston",
    "Atlanta",
    "Denver",
    "Las Vegas",
    "Phoenix",
    "London",
    "Paris",
    "Tokyo",
    "Sydney",
    "Dubai",
    "Singapore",
    "Hong Kong",
    "Mumbai",
    "Berlin",
    "Toronto",
    "Washington DC", "DC",
}


@dataclass
class WeatherMarket:
    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    city: str
    end_date: str
    volume: float
    active: bool


@dataclass
class BookQuote:
    """Best-of-book snapshot for one outcome token."""
    best_bid: Optional[float]       # None when no bids exist
    best_ask: Optional[float]       # None when no asks exist
    bid_size: float                 # shares at best bid
    ask_size: float                 # shares at best ask


class PolymarketClient:
    def __init__(self, min_volume_24h: float = 50.0):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "weatherbot/1.0"
        self._min_volume_24h = min_volume_24h

    def get_weather_markets(self) -> list[WeatherMarket]:
        """Fetch active weather markets from Gamma API, filtered by liquidity."""
        markets: list[WeatherMarket] = []
        limit = 100
        offset = 0

        while True:
            resp = self._session.get(
                f"{GAMMA_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "tag": "weather",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()

            # Gamma API may wrap results; normalise to a list
            if isinstance(raw, dict):
                page = raw.get("data", raw.get("markets", raw.get("results", [])))
            else:
                page = raw

            if not page:
                break

            if offset == 0:
                sample = page[0] if page else {}
                logger.info(
                    "Gamma API first page: %d markets | sample keys: %s | sample question: %s",
                    len(page), list(sample.keys()), sample.get("question", "")[:80],
                )
                logger.info("Gamma API sample outcomes: %s | liquidity: %s",
                            sample.get("outcomes"), sample.get("liquidity"))

            for m in page:
                if not any(kw in m.get("question", "").lower() for kw in WEATHER_KEYWORDS):
                    continue
                liq = float(m.get("liquidity") or m.get("volume24hr") or 0)
                if liq < self._min_volume_24h:
                    continue
                parsed = self._parse_gamma_market(m)
                if parsed:
                    markets.append(parsed)

            if len(page) < limit:
                break
            offset += limit

        logger.info(
            "Found %d weather markets (Gamma API, vol24h≥$%.0f)",
            len(markets), self._min_volume_24h,
        )
        return markets

    def get_best_ask(self, token_id: str) -> Optional[float]:
        q = self.get_book_quote(token_id)
        return q.best_ask if q else None

    def get_market_outcome(self, condition_id: str) -> Optional[str]:
        """Return "yes" or "no" if the market is resolved, else None."""
        try:
            resp = self._session.get(
                f"{CLOB_URL}/markets/{condition_id}", timeout=10
            )
            resp.raise_for_status()
            m = resp.json()
            if m.get("active", True):
                return None
            for t in m.get("tokens", []):
                if abs(float(t.get("price", 0)) - 1.0) < 0.01:
                    outcome = t.get("outcome", "").lower()
                    return outcome if outcome in ("yes", "no") else None
        except Exception as exc:
            logger.debug("Outcome fetch failed for %s: %s", condition_id[:8], exc)
        return None

    def get_book_quote(self, token_id: str) -> Optional[BookQuote]:
        try:
            resp = self._session.get(
                f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=10
            )
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            return BookQuote(
                best_bid=float(bids[0]["price"]) if bids else None,
                best_ask=float(asks[0]["price"]) if asks else None,
                bid_size=float(bids[0]["size"]) if bids else 0.0,
                ask_size=float(asks[0]["size"]) if asks else 0.0,
            )
        except Exception as exc:
            logger.warning("Book fetch failed for %s: %s", token_id[:8], exc)
            return None

    # ------------------------------------------------------------------
    def _parse_gamma_market(self, m: dict) -> Optional[WeatherMarket]:
        import json as _json
        try:
            # Token IDs: Gamma API encodes them as a JSON string or list
            raw_ids = m.get("clobTokenIds") or m.get("tokens", [])
            if isinstance(raw_ids, str):
                token_ids: list = _json.loads(raw_ids)
            elif isinstance(raw_ids, list) and raw_ids and isinstance(raw_ids[0], dict):
                # CLOB-style: [{"token_id": ..., "outcome": ...}, ...]
                yes_t = next((t for t in raw_ids if t.get("outcome") == "Yes"), raw_ids[0])
                no_t  = next((t for t in raw_ids if t.get("outcome") == "No"),  raw_ids[1])
                return WeatherMarket(
                    market_id=m["conditionId"],
                    question=m.get("question", ""),
                    yes_token_id=yes_t["token_id"],
                    no_token_id=no_t["token_id"],
                    yes_price=float(yes_t.get("price", 0.5)),
                    no_price=float(no_t.get("price", 0.5)),
                    city=self._extract_city(m.get("question", "")),
                    end_date=m.get("endDate", ""),
                    volume=float(m.get("liquidity") or m.get("volume24hr") or 0),
                    active=bool(m.get("active", False)),
                )
            else:
                token_ids = list(raw_ids)

            if len(token_ids) < 2:
                return None

            # Prices: Gamma API encodes as JSON string or list of strings
            raw_prices = m.get("outcomePrices", ["0.5", "0.5"])
            if isinstance(raw_prices, str):
                prices: list = _json.loads(raw_prices)
            else:
                prices = list(raw_prices)

            return WeatherMarket(
                market_id=m["conditionId"],
                question=m.get("question", ""),
                yes_token_id=str(token_ids[0]),
                no_token_id=str(token_ids[1]),
                yes_price=float(prices[0]) if prices else 0.5,
                no_price=float(prices[1]) if len(prices) > 1 else 0.5,
                city=self._extract_city(m.get("question", "")),
                end_date=m.get("endDate", ""),
                volume=float(m.get("liquidity") or m.get("volume24hr") or 0),
                active=bool(m.get("active", False)),
            )
        except (KeyError, ValueError, TypeError, _json.JSONDecodeError):
            return None

    def _parse_market(self, m: dict) -> Optional[WeatherMarket]:
        try:
            tokens = m.get("tokens", [])
            if len(tokens) < 2:
                return None
            yes = next((t for t in tokens if t.get("outcome") == "Yes"), tokens[0])
            no = next((t for t in tokens if t.get("outcome") == "No"), tokens[1])
            return WeatherMarket(
                market_id=m["condition_id"],
                question=m.get("question", ""),
                yes_token_id=yes["token_id"],
                no_token_id=no["token_id"],
                yes_price=float(yes.get("price", 0.5)),
                no_price=float(no.get("price", 0.5)),
                city=self._extract_city(m.get("question", "")),
                end_date=m.get("end_date_iso", ""),
                volume=float(m.get("volume", 0)),
                active=bool(m.get("active", False)),
            )
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _extract_city(question: str) -> str:
        # 既知の都市名が質問文に含まれているか直接照合（長い名前を優先）
        for city in sorted(KNOWN_CITIES, key=len, reverse=True):
            if re.search(rf"\b{re.escape(city)}\b", question, re.IGNORECASE):
                return CITY_ALIASES.get(city, city)
        return ""
