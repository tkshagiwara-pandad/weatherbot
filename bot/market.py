import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CLOB_URL = "https://clob.polymarket.com"
WEATHER_KEYWORDS = ["rain", "precipitation", "temperature", "snow", "storm", "weather", "humidity"]

CITY_ALIASES: dict[str, Optional[str]] = {
    "NYC": "New York City",
    "NY": "New York City",
    "LA": "Los Angeles",
    "DC": "Washington DC",
    "SF": "San Francisco",
    "UK": None,   # 国レベルは不可
    "US": None,
    "EU": None,
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
        try:
            resp = self._session.get(
                f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=10
            )
            resp.raise_for_status()
            asks = resp.json().get("asks", [])
            return float(asks[0]["price"]) if asks else None
        except Exception as exc:
            logger.warning("Price fetch failed for %s: %s", token_id[:8], exc)
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
        match = re.search(
            r"\bin\s+([A-Z][a-zA-Z\s]+?)(?:\s+on|\s+during|\s+this|\?|$)", question
        )
        if not match:
            return ""
        city = match.group(1).strip()
        # 単語数が多すぎる場合は都市名ではない ("Ukraine before July" など)
        if len(city.split()) > 3:
            return ""
        return CITY_ALIASES.get(city, city) or ""
