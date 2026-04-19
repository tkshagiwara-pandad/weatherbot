import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
WEATHER_KEYWORDS = ["rain", "precipitation", "temperature", "snow", "storm", "weather", "humidity"]
_WEATHER_KW_RE = re.compile(
    # rain/snow: left-boundary only to catch rainfall, snowfall, etc.
    # other keywords: full word boundary (they don't form compounds)
    r"\brain\w*|\bsnow\w*"
    r"|\b(?:precipitation|temperature|storm|weather|humidity)\b",
    re.IGNORECASE,
)

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
    def __init__(self, max_spread: float = 0.20):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "weatherbot/1.0"
        self._max_spread = max_spread

    def get_weather_markets(self) -> list[WeatherMarket]:
        """Fetch active weather markets from Gamma API.

        Pre-filters using Gamma's spread field to avoid calling /book for
        illiquid markets (spread ≥ 1.0 = ghost book).
        """
        markets: list[WeatherMarket] = []
        total_seen = weather_seen = 0
        limit = 100
        offset = 0

        while True:
            resp = self._session.get(
                f"{GAMMA_URL}/markets",
                params={"active": "true", "closed": "false", "limit": limit, "offset": offset},
                timeout=15,
            )
            resp.raise_for_status()
            page: list = resp.json()
            if not isinstance(page, list) or not page:
                break

            total_seen += len(page)
            for m in page:
                if not _WEATHER_KW_RE.search(m.get("question", "")):
                    continue
                weather_seen += 1
                # Use Gamma's pre-computed spread as a fast liquidity pre-filter
                spread = float(m.get("spread") or 1.0)
                if spread > self._max_spread:
                    continue
                parsed = self._parse_gamma_market(m)
                if parsed:
                    markets.append(parsed)

            if len(page) < limit:
                break
            offset += limit

        logger.info(
            "Found %d liquid weather markets (spread≤%.0f%%) from %d weather / %d total",
            len(markets), self._max_spread * 100, weather_seen, total_seen,
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
    @staticmethod
    def _load_json_field(value) -> list:
        """Gamma API encodes some list fields as JSON strings; normalise to list."""
        import json as _json
        if isinstance(value, str):
            return _json.loads(value)
        return list(value) if value else []

    def _parse_gamma_market(self, m: dict) -> Optional[WeatherMarket]:
        try:
            token_ids = self._load_json_field(m.get("clobTokenIds") or [])
            if len(token_ids) < 2:
                return None
            prices = self._load_json_field(m.get("outcomePrices") or ["0.5", "0.5"])
            return WeatherMarket(
                market_id=m["conditionId"],
                question=m.get("question", ""),
                yes_token_id=str(token_ids[0]),
                no_token_id=str(token_ids[1]),
                yes_price=float(prices[0]) if prices else 0.5,
                no_price=float(prices[1]) if len(prices) > 1 else 0.5,
                city=self._extract_city(m.get("question", "")),
                end_date=m.get("endDateIso") or m.get("endDate", ""),
                volume=float(m.get("liquidityClob") or m.get("volume24hr") or 0),
                active=bool(m.get("active", False)),
            )
        except (KeyError, ValueError, TypeError, Exception):
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
