"""Read-only hazard data reader for the hazards agent.

Self-contained on purpose: this repo has no dependency on the a2a repo, so the NWS
and USGS transports live here with one small cache. Both upstreams are keyless and
were verified live on 2026-09-22.

  https://api.weather.gov/alerts/active                NWS active alerts (386 nationwide that day)
  https://api.weather.gov/alerts/active/count          NWS live counts
  https://earthquake.usgs.gov/fdsnws/event/1/query     USGS earthquake catalog (GeoJSON)

Both agencies ask for a contactable User-Agent; set HAZARDS_USER_AGENT before running
this anywhere public.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib import parse, request

NWS_BASE_URL = "https://api.weather.gov"
USGS_BASE_URL = "https://earthquake.usgs.gov"

NWS_DATASET = "api.weather.gov/alerts/active"
USGS_DATASET = "earthquake.usgs.gov/fdsnws/event/1"

DEFAULT_USER_AGENT = "acp-hazards/1.0 (+https://github.com/mrfentmen/acp)"

ALERT_FIELDS = (
    "id", "areaDesc", "event", "severity", "certainty", "urgency",
    "onset", "ends", "headline", "instruction", "senderName",
)
SEVERITY_ORDER = ("Extreme", "Severe", "Moderate", "Minor", "Unknown")

#: Two-letter NWS area codes (states, DC and territories) mapped to readable names.
STATE_CODES = {
    code: name
    for code, name in (
        ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"), ("CA", "California"),
        ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"),
        ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
        ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"),
        ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
        ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
        ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
        ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"), ("OR", "Oregon"),
        ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
        ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
        ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
        ("PR", "Puerto Rico"), ("VI", "Virgin Islands"), ("GU", "Guam"), ("AS", "American Samoa"),
        ("MP", "Northern Mariana Islands"),
    )
}

_QUAKE_FIELDS = (
    "id", "magnitude", "place", "time", "depth_km", "latitude", "longitude",
    "tsunami", "alert", "significance", "detail_url",
)

_AREA_RE = re.compile(r"^[A-Z]{2}$")
_POINT_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


class HazardDataError(RuntimeError):
    """An upstream hazard feed could not be read."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ms_to_iso(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return (
            datetime.fromtimestamp(int(value) / 1000, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return None


class HazardData:
    """NWS alerts + USGS earthquakes, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 60.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("HAZARDS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("HAZARDS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("HAZARDS_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise HazardDataError(f"hazard feed request failed: {exc}") from exc

    def _get(self, url: str, params: dict, ttl: float | None = None):
        key = f"{url}:{json.dumps({k: str(v) for k, v in sorted(params.items())})}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, params)
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_state(value: str) -> str:
        state = str(value).strip().upper()
        if not _AREA_RE.match(state):
            raise ValueError("a state is two letters, for example NY")
        return state

    @staticmethod
    def check_point(value: str) -> str:
        match = _POINT_RE.match(str(value))
        if not match:
            raise ValueError("a point is 'lat,lon', for example 40.71,-74.01")
        latitude, longitude = float(match.group(1)), float(match.group(2))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        return f"{round(latitude, 4)},{round(longitude, 4)}"

    @staticmethod
    def check_magnitude(value) -> float:
        try:
            magnitude = float(value)
        except (TypeError, ValueError):
            raise ValueError("a magnitude must be a number") from None
        if not -1.0 <= magnitude <= 10.0:
            raise ValueError("a magnitude must be between -1 and 10")
        return round(magnitude, 1)

    @staticmethod
    def check_hours(value) -> float:
        try:
            hours = float(value)
        except (TypeError, ValueError):
            raise ValueError("hours must be a number") from None
        if not 0.25 <= hours <= 8760:
            raise ValueError("hours must be between 0.25 (15 minutes) and 8760 (one year)")
        return hours

    @staticmethod
    def check_radius_km(value) -> float:
        try:
            radius = float(value)
        except (TypeError, ValueError):
            raise ValueError("a radius must be a number of kilometres") from None
        if not 1 <= radius <= 20000:
            raise ValueError("a radius must be between 1 and 20000 km")
        return radius

    @staticmethod
    def place_label(state: str | None = None, point: str | None = None) -> str:
        if state:
            return STATE_CODES.get(state, state)
        if point:
            return f"the point {point}"
        return "the United States"

    # -- NWS ---------------------------------------------------------------

    def weather_alerts(self, state: str | None = None, point: str | None = None, severity: str | None = None,
                       event_contains: str | None = None, limit: int = 20) -> list[dict]:
        params: dict[str, str] = {}
        if state:
            params["area"] = self.check_state(state)
        if point:
            params["point"] = self.check_point(point)
        if severity:
            wanted = str(severity).strip().title()
            if wanted not in SEVERITY_ORDER:
                raise ValueError(f"severity must be one of: {', '.join(SEVERITY_ORDER)}")
            params["severity"] = wanted
        payload = self._get(f"{NWS_BASE_URL}/alerts/active", params, ttl=min(self.cache_ttl, 60))
        features = payload.get("features") if isinstance(payload, dict) else None
        if features is None:
            raise HazardDataError("NWS returned an unexpected payload (no features)")
        alerts = [
            {field: (feature.get("properties") or {})[field]
             for field in ALERT_FIELDS if field in (feature.get("properties") or {})}
            for feature in features
        ]
        if event_contains:
            needle = str(event_contains).strip().lower()
            alerts = [alert for alert in alerts if needle in (alert.get("event") or "").lower()]
        alerts.sort(key=lambda alert: (
            SEVERITY_ORDER.index(alert["severity"]) if alert.get("severity") in SEVERITY_ORDER else len(SEVERITY_ORDER),
            alert.get("onset") or "",
        ))
        return alerts[: max(1, min(int(limit), 100))]

    def alert_counts(self) -> dict:
        payload = self._get(f"{NWS_BASE_URL}/alerts/active/count", {}, ttl=min(self.cache_ttl, 120))
        if not isinstance(payload, dict) or "total" not in payload:
            raise HazardDataError("NWS returned an unexpected count payload")
        return {
            "total": int(payload.get("total", 0)),
            "land": int(payload.get("land", 0)),
            "marine": int(payload.get("marine", 0)),
            "areas": payload.get("areas") or {},
        }

    # -- USGS --------------------------------------------------------------

    def recent_quakes(self, min_magnitude: float = 2.5, hours: float = 24, limit: int = 10) -> list[dict]:
        params = {
            "format": "geojson",
            "starttime": self._starttime(hours),
            "minmagnitude": f"{self.check_magnitude(min_magnitude)}",
            "orderby": "time",
            "limit": str(max(1, min(int(limit), 100))),
        }
        return self._quakes(f"{USGS_BASE_URL}/fdsnws/event/1/query", params)

    def quakes_near(self, point: str, radius_km: float = 300, min_magnitude: float = 2.0,
                    hours: float = 720, limit: int = 10) -> list[dict]:
        latitude, longitude = self.check_point(point).split(",")
        params = {
            "format": "geojson",
            "latitude": latitude,
            "longitude": longitude,
            "maxradiuskm": f"{self.check_radius_km(radius_km)}",
            "starttime": self._starttime(hours),
            "minmagnitude": f"{self.check_magnitude(min_magnitude)}",
            "orderby": "time",
            "limit": str(max(1, min(int(limit), 100))),
        }
        return self._quakes(f"{USGS_BASE_URL}/fdsnws/event/1/query", params)

    def quake_counts(self) -> dict:
        return {
            "last_24h_m1.0": self._quake_count(1.0, 24),
            "last_24h_m4.5": self._quake_count(4.5, 24),
            "last_7d_m6.0": self._quake_count(6.0, 168),
        }

    def _quake_count(self, min_magnitude: float, hours: float) -> int:
        params = {
            "format": "geojson",
            "starttime": self._starttime(hours),
            "minmagnitude": f"{self.check_magnitude(min_magnitude)}",
        }
        payload = self._get(f"{USGS_BASE_URL}/fdsnws/event/1/count", params, ttl=min(self.cache_ttl, 120))
        if not isinstance(payload, dict) or "count" not in payload:
            raise HazardDataError("USGS returned an unexpected count payload")
        return int(payload["count"])

    def _quakes(self, url: str, params: dict) -> list[dict]:
        payload = self._get(url, params, ttl=min(self.cache_ttl, 60))
        features = payload.get("features") if isinstance(payload, dict) else None
        if features is None:
            raise HazardDataError("USGS returned an unexpected payload (no features)")
        return [self._one_quake(feature) for feature in features]

    def _starttime(self, hours: float) -> str:
        start = datetime.now(timezone.utc) - timedelta(hours=self.check_hours(hours))
        return start.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _one_quake(feature: dict) -> dict:
        props = feature.get("properties") or {}
        coordinates = (feature.get("geometry") or {}).get("coordinates") or []
        row = {
            "id": feature.get("id") or "",
            "magnitude": props.get("mag"),
            "place": props.get("place"),
            "time": _ms_to_iso(props.get("time")),
            "depth_km": coordinates[2] if len(coordinates) > 2 else None,
            "latitude": coordinates[1] if len(coordinates) > 1 else None,
            "longitude": coordinates[0] if len(coordinates) > 0 else None,
            "tsunami": props.get("tsunami"),
            "alert": props.get("alert"),
            "significance": props.get("sig"),
            "detail_url": props.get("url"),
        }
        return {field: row[field] for field in _QUAKE_FIELDS}

    # -- provenance --------------------------------------------------------

    def freshness(self, dataset_key: str) -> str:
        """Both feeds are live, so freshness is the moment we read them."""
        if dataset_key not in ("alerts", "quakes"):
            raise ValueError("dataset_key must be 'alerts' or 'quakes'")
        return utc_now_iso()
