import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CLOB_URL = "https://clob.polymarket.com"
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
    def __init__(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "weatherbot/1.0"

    def get_weather_markets(self) -> list[WeatherMarket]:
        markets: list[WeatherMarket] = []
        cursor: Optional[str] = None

        while True:
            params: dict = {"limit": 100, "active": "true", "closed": "false"}
            if cursor:
                params["next_cursor"] = cursor

            resp = self._session.get(f"{CLOB_URL}/markets", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            for m in data.get("data", []):
                if any(kw in m.get("question", "").lower() for kw in WEATHER_KEYWORDS):
                    parsed = self._parse_market(m)
                    if parsed:
                        markets.append(parsed)

            cursor = data.get("next_cursor")
            if not cursor or cursor == "LTE=":
                break

        logger.info("Found %d weather markets", len(markets))
        return markets

    def get_best_ask(self, token_id: str) -> Optional[float]:
        q = self.get_book_quote(token_id)
        return q.best_ask if q else None

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
