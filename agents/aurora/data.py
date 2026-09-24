"""Read-only reader for NOAA's Space Weather Prediction Center (SWPC) products.

Self-contained on purpose: this repo has no dependency on the a2a repo, so the transport
and its cache live here. Every endpoint is keyless and was verified live on 2026-09-24
02:30 UTC:

  /json/planetary_k_index_1m.json                 estimated_kp 2.67 (kp_index 3) — right now
  /products/noaa-planetary-k-index.json           3-hourly Kp, 56 rows (~7 days)
  /products/noaa-planetary-k-index-forecast.json  81 rows mixing observed, estimated and predicted
  /json/ovation_aurora_latest.json                65,160 [longitude, latitude, probability] cells

The quirks, all handled here:

1. Both Kp files are bare JSON arrays, and the 1-minute file keeps its latest value under
   `estimated_kp` (`kp_index` is the same number rounded, `kp` is a label like "3M").
2. The forecast file mixes three kinds of row in one list — `observed`, `estimated` and
   `predicted` — and it starts about a week in the past, so only "predicted" rows on days
   that have not happened yet are a forecast.
3. The 1-minute feed is finer than the 3-hourly index it estimates from, so a "right now"
   answer reports both instead of pretending they are the same number.
4. OVATION is a ~900 KB grid covering the whole planet at one-degree steps, so it is cached
   hard and only the neighbourhood of the asked-for point is read. Its longitudes run 0-359
   while callers speak -180..180, so longitudes are normalised before the search.
5. A probability is for aurora *overhead* at that grid cell. It says nothing about clouds.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import parse, request

BASE_URL = "https://services.swpc.noaa.gov"
DEFAULT_USER_AGENT = "acp-aurora/1.0 (+https://github.com/mrfentmen/acp)"

KP_1M = "/json/planetary_k_index_1m.json"
KP_3H = "/products/noaa-planetary-k-index.json"
KP_FORECAST = "/products/noaa-planetary-k-index-forecast.json"
OVATION = "/json/ovation_aurora_latest.json"

DATASET_KP_1M = "swpc.noaa.gov/planetary-k-index-1m"
DATASET_KP = "swpc.noaa.gov/planetary-k-index"
DATASET_FORECAST = "swpc.noaa.gov/planetary-k-index-forecast"
DATASET_OVATION = "swpc.noaa.gov/ovation-aurora"

#: NOAA's geomagnetic storm scale: Kp 5 and up is a storm, graded G1-G5.
STORM_SCALE = {
    5: "G1 (minor)",
    6: "G2 (moderate)",
    7: "G3 (strong)",
    8: "G4 (severe)",
    9: "G5 (extreme)",
}

MAX_DAYS = 3
DEFAULT_AURORA_RADIUS = 2.0

#: Coordinates for places people ask about, so callers need not know their latitude.
#: Reference geography shipped with this agent, not data from NOAA. High-latitude places
#: are included because the aurora is a high-latitude phenomenon.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "boston": (42.36, -71.06), "philadelphia": (39.95, -75.17),
    "washington": (38.91, -77.04), "atlanta": (33.75, -84.39), "chicago": (41.88, -87.63),
    "detroit": (42.33, -83.05), "minneapolis": (44.98, -93.27), "st louis": (38.63, -90.20),
    "kansas city": (39.10, -94.58), "cleveland": (41.50, -81.69), "pittsburgh": (40.44, -79.99),
    "buffalo": (42.89, -78.88), "burlington": (44.48, -73.21), "denver": (39.74, -104.99),
    "salt lake city": (40.76, -111.89), "boise": (43.62, -116.20), "billings": (45.78, -108.50),
    "fargo": (46.88, -96.79), "duluth": (46.79, -92.10), "seattle": (47.61, -122.33),
    "portland": (45.52, -122.68), "san francisco": (37.77, -122.42), "los angeles": (34.05, -118.24),
    "phoenix": (33.45, -112.07), "dallas": (32.78, -96.80), "miami": (25.76, -80.19),
    "honolulu": (21.31, -157.86), "anchorage": (61.22, -149.90), "fairbanks": (64.84, -147.72),
    "toronto": (43.65, -79.38), "montreal": (45.50, -73.57), "vancouver": (49.28, -123.12),
    "winnipeg": (49.90, -97.14), "saskatoon": (52.13, -106.67), "yellowknife": (62.45, -114.37),
    "reykjavik": (64.15, -21.94), "tromso": (69.65, -18.96), "tromsø": (69.65, -18.96),
    "oslo": (59.91, 10.75),
    "stockholm": (59.33, 18.07), "helsinki": (60.17, 24.94), "copenhagen": (55.68, 12.57),
    "edinburgh": (55.95, -3.19), "dublin": (53.35, -6.26), "london": (51.51, -0.13),
    "berlin": (52.52, 13.40), "warsaw": (52.23, 21.01), "kyiv": (50.45, 30.52),
    "moscow": (55.76, 37.62), "murmansk": (68.97, 33.08), "tokyo": (35.68, 139.69),
    "beijing": (39.90, 116.41), "sapporo": (43.06, 141.35), "sydney": (-33.87, 151.21),
    "melbourne": (-37.81, 144.96), "auckland": (-36.85, 174.76), "dunedin": (-45.87, 170.50),
    "cape town": (-33.92, 18.42), "buenos aires": (-34.60, -58.38), "ushuaia": (-54.80, -68.30),
}


class SpaceWeatherError(RuntimeError):
    """A SWPC product could not be read."""


def kp_band(kp) -> str:
    """Plain words for a Kp value, using NOAA's own storm grading from 5 up."""
    if kp is None:
        return "unknown"
    value = float(kp)
    if value >= 5:
        return STORM_SCALE.get(int(min(round(value), 9)), "G5 (extreme)")
    if value >= 4:
        return "active"
    if value >= 3:
        return "unsettled"
    return "quiet"


def is_storm(kp) -> bool:
    return kp is not None and float(kp) >= 5


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class SpaceWeatherData:
    """SWPC products over HTTP: injectable fetch, cached per product, threadsafe."""

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, user_agent: str | None = None) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("AURORA_BASE_URL") or BASE_URL).rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get("AURORA_CACHE_TTL", "300"))
        self.timeout = float(timeout if timeout is not None else env.get("AURORA_HTTP_TIMEOUT", "30"))
        self.user_agent = user_agent or env.get("AURORA_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.RLock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, path: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            raise SpaceWeatherError(f"SWPC answered {exc.code} for {path.split('/')[-1]}") from exc
        except Exception as exc:  # urllib raises many types; callers see one
            raise SpaceWeatherError(f"space-weather request failed: {exc}") from exc

    def _query(self, path: str, ttl: float | None = None):
        now = time.time()
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(path, {})
        if not isinstance(payload, (dict, list)):
            raise SpaceWeatherError(f"SWPC returned an unexpected payload for {path.split('/')[-1]}")
        with self._lock:
            self._cache[path] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    @staticmethod
    def _rows(path: str, payload) -> list[dict]:
        if not isinstance(payload, list):
            raise SpaceWeatherError(f"SWPC returned no rows for {path.split('/')[-1]}")
        return [row for row in payload if isinstance(row, dict)]

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_lat(value, name: str = "latitude") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -90 <= number <= 90:
            raise ValueError(f"{name} must be between -90 and 90")
        return number

    @staticmethod
    def check_lon(value, name: str = "longitude") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -180 <= number <= 180:
            raise ValueError(f"{name} must be between -180 and 180")
        return number

    @staticmethod
    def check_point(value) -> tuple[float, float]:
        text = re.sub(r"\s+", "", str(value or ""))
        parts = text.split(",")
        if len(parts) != 2:
            raise ValueError("a point must look like 64.84,-147.72")
        return SpaceWeatherData.check_lat(parts[0]), SpaceWeatherData.check_lon(parts[1])

    @staticmethod
    def check_days(value, name: str = "days") -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number <= MAX_DAYS:
            raise ValueError(f"{name} must be between 1 and {MAX_DAYS}")
        return number

    @staticmethod
    def city(name: str) -> tuple[float, float, str] | None:
        """A known place from the agent's own list -> (lat, lon, label)."""
        key = " ".join(str(name or "").lower().replace("-", " ").split())
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return lat, lon, key.title()
        for known, coords in CITY_COORDS.items():
            if key and (key in known or known in key):
                return coords[0], coords[1], known.title()
        return None

    # -- reads -------------------------------------------------------------

    def kp_rows(self, limit: int = 8) -> list[dict]:
        """The most recent 3-hourly Kp rows (8 rows is 24 hours)."""
        rows = self._rows(KP_3H, self._query(KP_3H, ttl=min(self.cache_ttl, 300)))
        return rows[-max(1, int(limit)):]

    def kp_now(self) -> dict:
        """The current 1-minute estimated Kp, with the latest 3-hourly index alongside it."""
        minute = self._rows(KP_1M, self._query(KP_1M, ttl=min(self.cache_ttl, 120)))
        latest = minute[-1] if minute else {}
        estimated = latest.get("estimated_kp")
        kp = float(estimated) if estimated is not None else None
        rows = self.kp_rows(limit=1)
        latest_3h = rows[-1] if rows else {}
        return {
            "dataset": DATASET_KP_1M,
            "time_tag": latest.get("time_tag"),
            "estimated_kp": kp,
            "kp_index": latest.get("kp_index"),
            "band": kp_band(kp),
            "storm": is_storm(kp),
            "three_hourly": {
                "dataset": DATASET_KP,
                "time_tag": latest_3h.get("time_tag"),
                "kp": latest_3h.get("Kp"),
                "a_running": latest_3h.get("a_running"),
                "station_count": latest_3h.get("station_count"),
            },
        }

    def recent(self, hours: int = 24) -> dict:
        """Current conditions plus the peak of the last `hours` (3-hourly rows)."""
        rows = self.kp_rows(limit=max(1, int(hours) // 3))
        values = [float(row.get("Kp") or 0) for row in rows]
        peak = max(values, default=None)
        peak_time = next((row.get("time_tag") for row in reversed(rows)
                          if float(row.get("Kp") or 0) >= (peak or 0)), None)
        return {
            "now": self.kp_now(),
            "window_hours": hours,
            "peak_kp": peak,
            "peak_band": kp_band(peak),
            "peak_time_tag": peak_time,
            "rows": [{"time_tag": row.get("time_tag"), "kp": row.get("Kp"),
                      "a_running": row.get("a_running")} for row in rows],
            "dataset": DATASET_KP,
        }

    def forecast(self, days: int = MAX_DAYS) -> dict:
        """Predicted Kp for the days still to come, grouped by UTC day, each day's peak."""
        days = self.check_days(days)
        rows = self._rows(KP_FORECAST, self._query(KP_FORECAST, ttl=min(self.cache_ttl, 900)))
        predicted = [row for row in rows if str(row.get("observed") or "").lower() == "predicted"]
        today = today_utc()
        by_day: dict[str, dict] = {}
        for row in predicted:
            day = str(row.get("time_tag") or "")[:10]
            if not day or day < today:  # the file also carries a week of the past
                continue
            kp = float(row.get("kp") or 0)
            entry = by_day.setdefault(day, {"date": day, "max_kp": kp, "rows": []})
            entry["rows"].append({"time_tag": row.get("time_tag"), "kp": kp})
            entry["max_kp"] = max(entry["max_kp"], kp)
        ordered = [by_day[day] for day in sorted(by_day)][:days]
        for entry in ordered:
            entry["band"] = kp_band(entry["max_kp"])
            entry["storm"] = is_storm(entry["max_kp"])
        peak = max((entry["max_kp"] for entry in ordered), default=None)
        return {
            "dataset": DATASET_FORECAST,
            "days": ordered,
            "predicted_rows": len(predicted),
            "other_rows": len(rows) - len(predicted),
            "peak_kp": peak,
            "peak_band": kp_band(peak),
            "storm_days": [entry["date"] for entry in ordered if entry["storm"]],
        }

    def aurora_probability(self, lat, lon, radius: float = DEFAULT_AURORA_RADIUS) -> dict:
        """The OVATION model's aurora probability at (or nearest to) a point, right now."""
        lat = self.check_lat(lat)
        lon = self.check_lon(lon)
        radius = self.check_lat(radius, "radius")
        grid = self._query(OVATION, ttl=min(self.cache_ttl, 900))
        coords = grid.get("coordinates") if isinstance(grid, dict) else None
        if not coords:
            raise SpaceWeatherError("the OVATION grid was empty")
        lon_norm = lon % 360  # the grid runs 0-359; callers speak -180..180
        best: tuple[float, int, int, int] | None = None  # squared distance, lon, lat, probability
        near: list[int] = []
        for cell in coords:
            if not isinstance(cell, list) or len(cell) < 3:
                continue
            lon_step, lat_step, probability = cell[0], cell[1], cell[2]
            if abs(lat_step - lat) > radius:
                continue
            raw = abs(lon_step - lon_norm)
            delta = min(raw, 360 - raw)
            if delta > radius:
                continue
            near.append(int(probability))
            distance = (lat_step - lat) ** 2 + delta ** 2
            if best is None or distance < best[0]:
                best = (distance, int(lon_step), int(lat_step), int(probability))
        if best is None:
            raise SpaceWeatherError("the OVATION grid had no cell near that point")
        return {
            "dataset": DATASET_OVATION,
            "observation_time": grid.get("Observation Time"),
            "forecast_time": grid.get("Forecast Time"),
            "probability": best[3],
            "nearest_cell": {"latitude": best[2], "longitude": best[1]},
            "radius_degrees": radius,
            "max_probability_nearby": max(near) if near else best[3],
            "cells_read": len(coords),
        }


__all__ = [
    "BASE_URL",
    "CITY_COORDS",
    "DATASET_FORECAST",
    "DATASET_KP",
    "DATASET_KP_1M",
    "DATASET_OVATION",
    "DEFAULT_AURORA_RADIUS",
    "KP_1M",
    "KP_3H",
    "KP_FORECAST",
    "MAX_DAYS",
    "OVATION",
    "STORM_SCALE",
    "SpaceWeatherData",
    "SpaceWeatherError",
    "is_storm",
    "kp_band",
    "today_utc",
]
