import json
import logging
import statistics
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"

# JMA・ECMWF・GFS（NOAA）・ICON（DWD）の4モデルアンサンブル
# モデルごとにデータがない変数は自動スキップされる
_ENSEMBLE_MODELS = ("jma_seamless", "ecmwf_ifs025", "gfs_seamless", "icon_seamless")

# Polymarket の天気マーケットは公式観測点（主に空港）で解決される。
# 市街中心ではなく ICAO ステーション座標を使うことで解決値に近づける。
CITY_COORDS: dict[str, tuple[float, float]] = {
    "New York City": (40.7831, -73.9712),   # Central Park (KNYC)
    "New York":      (40.7831, -73.9712),
    "Los Angeles":   (33.9425, -118.4081),  # KLAX
    "Chicago":       (41.9786, -87.9048),   # KORD
    "Houston":       (29.9902, -95.3368),   # KIAH
    "Miami":         (25.7959, -80.2870),   # KMIA
    "Dallas":        (32.8998, -97.0403),   # KDFW
    "Seattle":       (47.4502, -122.3088),  # KSEA
    "San Francisco": (37.6213, -122.3790),  # KSFO
    "Boston":        (42.3656, -71.0096),   # KBOS
    "Atlanta":       (33.6407, -84.4277),   # KATL
    "Denver":        (39.8561, -104.6737),  # KDEN
    "Las Vegas":     (36.0840, -115.1537),  # KLAS
    "Phoenix":       (33.4342, -112.0116),  # KPHX
    "Washington DC": (38.8512, -77.0402),   # KDCA
    "London":        (51.4700, -0.4543),    # EGLL
    "Paris":         (49.0097, 2.5479),     # LFPG
    "Tokyo":         (35.5494, 139.7798),   # RJTT (Haneda)
    "Sydney":        (-33.9399, 151.1753),  # YSSY
    "Dubai":         (25.2528, 55.3644),    # OMDB
    "Singapore":     (1.3644, 103.9915),    # WSSS
    "Hong Kong":     (22.3080, 113.9185),   # VHHH
    "Mumbai":        (19.0896, 72.8656),    # VABB
    "Berlin":        (52.3667, 13.5033),    # EDDB
    "Toronto":       (43.6777, -79.6248),   # CYYZ
    "Sao Paulo":     (-23.4356, -46.4731),  # SBGR
    "São Paulo":     (-23.4356, -46.4731),  # SBGR
    "Madrid":        (40.4936, -3.5668),    # LEMD
    "Austin":        (30.1945, -97.6699),   # KAUS
    "Seoul":         (37.5583, 126.7906),   # RKSS (Gimpo)
    "Lucknow":       (26.7606, 80.8893),    # VILK
    "Warsaw":        (52.1657, 20.9671),    # EPWA
    "Mexico City":   (19.4363, -99.0721),   # MMMX
    "Buenos Aires":  (-34.8222, -58.5358),  # SAEZ
    "Ankara":        (40.1281, 32.9951),    # LTAC
    "Munich":        (48.3537, 11.7750),    # EDDM
    "Shanghai":      (31.1443, 121.8083),   # ZSPD
    "Milan":         (45.4455, 9.2768),     # LIML
    "Beijing":       (40.0799, 116.5849),   # ZBAA
    "Amsterdam":     (52.3086, 4.7639),     # EHAM
    "Wellington":    (-41.3272, 174.8050),  # NZWN
    "Taipei":        (25.0777, 121.2328),   # RCTP
    "Wuhan":         (30.7838, 114.2081),   # ZHHH
    "Moscow":        (55.9726, 37.4146),    # UUEE
    "Istanbul":      (41.2761, 28.7519),    # LTFM
    "Tel Aviv":      (32.0114, 34.8867),    # LLBG
    "Chongqing":     (29.7192, 106.6419),   # ZUCK
    "Shenzhen":      (22.6393, 113.8107),   # ZGSZ
    "Chengdu":       (30.5785, 103.9472),   # ZUUU
    "Busan":         (35.1795, 128.9382),   # RKPK
    "Helsinki":      (60.3172, 24.9633),    # EFHK
    "Lagos":         (6.5774, 3.3215),      # DNMM
    "Kuala Lumpur":  (2.7456, 101.7099),    # WMKK
    "Cape Town":     (-33.9648, 18.6017),   # FACT
    "Panama City":   (9.0714, -79.3835),    # MPTO
    "Jakarta":       (-6.1275, 106.6537),   # WIHH
    "Guangzhou":     (23.3924, 113.2990),   # ZGGG
    "Karachi":       (24.9065, 67.1609),    # OPKC
    "Jeddah":        (21.6796, 39.1565),    # OEJN
    "Manila":        (14.5086, 121.0194),   # RPLL
    "Bangkok":       (13.6811, 100.7473),   # VTBS
}


@dataclass
class WeatherForecast:
    city: str
    forecast_date: date
    precip_prob: float      # 0.0 – 1.0
    temp_max_c: float
    temp_min_c: float
    conditions: str
    weather_code: int = 0   # WMO code (71-77, 85-86 = snow)
    temp_std_c: float = 0.0 # アンサンブルモデル間の標準偏差（不確実性の指標）


# WMO weather code → 短い説明
_WMO_CODES: dict[int, str] = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ hail",
}


class WeatherClient:
    """Open-Meteo forecast client（JMA + ECMWF アンサンブル）。"""

    def __init__(self, forecast_log: str = "forecast_log.jsonl",
                 accuracy_log: str = "accuracy_log.jsonl"):
        self._cache: dict[tuple[str, date], Optional[WeatherForecast]] = {}
        self._forecast_log = forecast_log
        self._accuracy_log = accuracy_log
        self._logged: set[tuple[str, date]] = set()  # 重複ログ防止

    def prefetch(self, city_dates: set[tuple[str, date]]):
        """スキャン開始前に unique な (都市, 日付) を一括取得。失敗もキャッシュする。"""
        todo = city_dates - self._cache.keys()
        logger.info("Fetching weather for %d unique city×date pairs...", len(todo))
        for city, dt in sorted(todo):
            try:
                self.get_forecast(city, dt)
            except Exception as exc:
                logger.warning("Weather fetch failed %s %s: %s", city, dt, exc)
                self._cache[(city, dt)] = None

    def get_forecast(self, city: str, target_date: date) -> Optional[WeatherForecast]:
        key = (city, target_date)
        if key in self._cache:
            return self._cache[key]

        coords = CITY_COORDS.get(city)
        if coords is None:
            logger.debug("No coordinates for city: %s", city)
            self._cache[key] = None
            return None

        lat, lon = coords
        date_str = target_date.strftime("%Y-%m-%d")
        resp = requests.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min,weather_code",
                "models": ",".join(_ENSEMBLE_MODELS),
                "start_date": date_str,
                "end_date": date_str,
                "timezone": "auto",
            },
            timeout=20,
        )
        resp.raise_for_status()
        daily = resp.json().get("daily", {})

        # model → (tmax, tmin, pp, wc)
        model_data: dict[str, tuple] = {}
        for model in _ENSEMBLE_MODELS:
            tmax = daily.get(f"temperature_2m_max_{model}", [None])[0]
            tmin = daily.get(f"temperature_2m_min_{model}", [None])[0]
            pp   = daily.get(f"precipitation_probability_max_{model}", [None])[0]
            wc   = daily.get(f"weather_code_{model}", [None])[0]
            if tmax is not None:
                model_data[model] = (float(tmax),
                                     float(tmin) if tmin is not None else None,
                                     float(pp) if pp is not None else None,
                                     int(wc) if wc is not None else None)
                logger.debug("  model=%-20s  tmax=%.1f°C  tmin=%s",
                             model, float(tmax),
                             f"{float(tmin):.1f}°C" if tmin is not None else "N/A")

        if not model_data:
            logger.warning("No ensemble data for %s %s", city, date_str)
            self._cache[key] = None
            return None

        tmax_pairs  = [(m, d[0]) for m, d in model_data.items()]
        tmin_pairs  = [(m, d[1]) for m, d in model_data.items() if d[1] is not None]
        pp_pairs    = [(m, d[2]) for m, d in model_data.items() if d[2] is not None]
        wcs         = [d[3] for d in model_data.values() if d[3] is not None]
        tmaxes      = [v for _, v in tmax_pairs]

        temp_max  = statistics.mean(tmaxes)
        temp_min  = statistics.mean([v for _, v in tmin_pairs]) if tmin_pairs else temp_max - 8.0
        temp_std  = statistics.pstdev(tmaxes) if len(tmaxes) > 1 else 0.0
        precip_prob = max(v for _, v in pp_pairs) / 100.0 if pp_pairs else 0.0
        code      = max(set(wcs), key=wcs.count) if wcs else 0
        conditions = _WMO_CODES.get(code, f"code_{code}")

        logger.debug("Forecast %s %s: tmax=%.1f±%.1f°C precip=%.0f%% %s",
                     city, date_str, temp_max, temp_std, precip_prob * 100, conditions)
        result = WeatherForecast(
            city=city, forecast_date=target_date,
            precip_prob=precip_prob,
            temp_max_c=temp_max, temp_min_c=temp_min,
            conditions=conditions,
            weather_code=int(code),
            temp_std_c=temp_std,
        )
        self._cache[key] = result
        self._log_forecast(city, target_date, result,
                           {m: d[0] for m, d in model_data.items()})
        return result

    def _log_forecast(self, city: str, target_date: date, f: WeatherForecast,
                      model_tmax: dict[str, float]):
        key = (city, target_date)
        if key in self._logged:
            return
        self._logged.add(key)
        record = {
            "city": city,
            "target_date": target_date.isoformat(),
            "forecast_max_c": round(f.temp_max_c, 2),
            "forecast_min_c": round(f.temp_min_c, 2),
            "precip_prob": round(f.precip_prob, 3),
            "model_tmax": {m: round(v, 2) for m, v in model_tmax.items()},
        }
        try:
            with open(self._forecast_log, "a") as fp:
                fp.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning("Failed to write forecast log: %s", exc)

    def record_actuals(self, _city_dates: set[tuple[str, date]] = None):
        """過去日付の実測値を archive API から取得して accuracy_log.jsonl に記録する。
        forecast_log.jsonl に記録済みで target_date < today のものを対象とする。"""
        today = date.today()
        past: set[tuple[str, date]] = set()
        try:
            with open(self._forecast_log) as fp:
                for line in fp:
                    r = json.loads(line)
                    d = date.fromisoformat(r["target_date"])
                    if d < today:
                        past.add((r["city"], d))
        except FileNotFoundError:
            return
        if not past:
            return

        # すでに記録済みの (city, date) を読み込んでスキップ
        recorded: set[tuple[str, str]] = set()
        try:
            with open(self._accuracy_log) as fp:
                for line in fp:
                    r = json.loads(line)
                    recorded.add((r["city"], r["target_date"]))
        except FileNotFoundError:
            pass

        for city, target_date in sorted(past):
            if (city, target_date.isoformat()) in recorded:
                continue
            coords = CITY_COORDS.get(city)
            if coords is None:
                continue
            lat, lon = coords
            date_str = target_date.isoformat()
            try:
                resp = requests.get(
                    ARCHIVE_URL,
                    params={
                        "latitude": lat, "longitude": lon,
                        "daily": "temperature_2m_max,temperature_2m_min",
                        "start_date": date_str, "end_date": date_str,
                        "timezone": "auto",
                    },
                    timeout=20,
                )
                resp.raise_for_status()
                daily = resp.json().get("daily", {})
                actual_max = daily.get("temperature_2m_max", [None])[0]
                actual_min = daily.get("temperature_2m_min", [None])[0]
                if actual_max is None:
                    continue

                # 対応する予報レコードを探す
                forecast_max = forecast_min = None
                model_tmax: dict = {}
                try:
                    with open(self._forecast_log) as fp:
                        for line in fp:
                            r = json.loads(line)
                            if r["city"] == city and r["target_date"] == date_str:
                                forecast_max = r["forecast_max_c"]
                                forecast_min = r["forecast_min_c"]
                                model_tmax = r.get("model_tmax", {})
                                break
                except FileNotFoundError:
                    pass

                record = {
                    "city": city,
                    "target_date": date_str,
                    "actual_max_c": round(float(actual_max), 2),
                    "actual_min_c": round(float(actual_min), 2),
                    "forecast_max_c": forecast_max,
                    "forecast_min_c": forecast_min,
                    "error_max_c": round(float(actual_max) - forecast_max, 2) if forecast_max is not None else None,
                    "model_tmax": model_tmax,
                }
                with open(self._accuracy_log, "a") as fp:
                    fp.write(json.dumps(record) + "\n")
                logger.info("Accuracy logged: %s %s  actual=%.1f°C  forecast=%s°C  error=%s°C",
                            city, date_str, float(actual_max),
                            forecast_max, record["error_max_c"])
            except Exception as exc:
                logger.debug("Archive fetch failed %s %s: %s", city, date_str, exc)
