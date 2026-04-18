import logging
from dataclasses import dataclass
from datetime import date

import requests

logger = logging.getLogger(__name__)

TIMELINE_URL = (
    "https://weather.visualcrossing.com"
    "/VisualCrossingWebServices/rest/services/timeline"
)


@dataclass
class WeatherForecast:
    city: str
    forecast_date: date
    precip_prob: float      # 0.0 – 1.0
    temp_max_c: float
    temp_min_c: float
    conditions: str


class WeatherClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._cache: dict[tuple[str, date], WeatherForecast] = {}

    def get_forecast(self, city: str, target_date: date) -> WeatherForecast:
        key = (city, target_date)
        if key in self._cache:
            return self._cache[key]

        date_str = target_date.strftime("%Y-%m-%d")
        url = f"{TIMELINE_URL}/{city}/{date_str}/{date_str}"
        resp = requests.get(
            url,
            params={"unitGroup": "metric", "key": self._key,
                    "contentType": "json", "include": "days"},
            timeout=10,
        )
        resp.raise_for_status()
        day = resp.json()["days"][0]
        precip_prob = (day.get("precipprob") or 0.0) / 100.0
        logger.debug("Forecast %s %s: precip=%.0f%%", city, date_str, precip_prob * 100)
        result = WeatherForecast(
            city=city,
            forecast_date=target_date,
            precip_prob=precip_prob,
            temp_max_c=day.get("tempmax", 0.0),
            temp_min_c=day.get("tempmin", 0.0),
            conditions=day.get("conditions", ""),
        )
        self._cache[key] = result
        return result
