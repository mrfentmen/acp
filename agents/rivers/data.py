"""Read-only reader for USGS NWIS real-time stream gauges (keyless).

Self-contained on purpose: this repo has no dependency on the a2a repo, so the transport
and its small cache live here. Verified live on 2026-09-24 03:00 UTC: Clear Creek at Golden
(06719505) read 61.6 ft3/s and 3.74 ft at 2026-09-23T20:45:00-06:00, a three-site request
returned six series in one response, and a Denver-area bounding box held 22 active
real-time stream sites.

The quirks, all handled here:

1. Two different services with two different formats. The site service answers tab-delimited
   RDB (``format=json`` is rejected with HTTP 400) and the instantaneous-values service
   answers JSON. Both are keyless and both need a User-Agent that identifies you.
2. An RDB file is comment lines, then a header line, then a *format* line made of width
   tokens like ``5s``/``16s``, then rows. The format line looks like data and has to be
   skipped, and every field arrives padded to its declared width.
3. ``-999999`` (and an empty field) is USGS's "no value", not a number; it is read as None.
4. A site that USGS does not know and a site that exists but publishes no real-time values
   both come back as HTTP 200 with an empty ``timeSeries`` list. The site file is what
   tells them apart, so it is only fetched when the values request comes back empty.
5. Each value carries its own qualifier list with the code *and* USGS's own description
   (``P`` → "Provisional data subject to revision."), so the reader never has to invent one.
6. The services are load-sensitive, and they fail differently per filter and per size. A
   whole-state values request is refused (HTTP 503 on ``stateCd``), a values read over a
   28-mile bbox is refused the same way, and the site service's ``siteStatus=active`` or
   ``hasDataTypeCd=iv`` filters on a bbox go from two seconds to a 30-second timeout run by
   run. A plain bbox + ``siteType`` read is fast and steady, so "what is near this place" is
   one plain bbox read followed by *one* multi-site values request for the nearest
   ``PROBE_LIMIT`` candidates. Which gauge is actually reporting is decided by the values
   response, never assumed from the site file. A 502/503/504 or a dropped connection is
   retried.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime
from http.client import IncompleteRead
from urllib import error as urlerror
from urllib import parse, request

SITE_PATH = "/nwis/site/"
IV_PATH = "/nwis/iv/"

BASE_URL = "https://waterservices.usgs.gov"
DATASET_SITE = "waterservices.usgs.gov/nwis/site"
DATASET_IV = "waterservices.usgs.gov/nwis/iv"
DEFAULT_USER_AGENT = "acp-rivers/1.0 (+https://github.com/mrfentmen/acp)"

#: Surface-water sites that carry a real-time stream gauge: streams, canals, ditches, tides.
SITE_TYPES = "ST,ST-CA,ST-DCH,ST-TS"

#: Parameter code -> (key, label, unit words, unit code), in report order.
PARAMETERS = {
    "00060": ("discharge", "Discharge", "cubic feet per second", "ft3/s"),
    "00065": ("gage_height", "Gage height", "feet", "ft"),
    "00010": ("water_temperature", "Water temperature", "degrees Celsius", "degC"),
}

#: What a "right now" answer asks for.
READ_PARAMETERS = ("00060", "00065", "00010")

#: Units the service actually prints, mapped to the words an answer uses.
UNIT_WORDS = {
    "ft3/s": "cubic feet per second",
    "ft": "feet",
    "degC": "degrees Celsius",
    "degF": "degrees Fahrenheit",
    "in": "inches",
}

#: The USGS "no value" code and the empty field, both read as None.
NO_VALUE = "-999999"

#: The services answer 502/503/504 under load, so a read is retried a few times.
RETRY_STATUSES = (502, 503, 504)
RETRY_ATTEMPTS = 4
RETRY_BACKOFF = 0.5

MAX_RADIUS_MILES = 100.0
DEFAULT_RADIUS_MILES = 25.0
MAX_SITES = 10
DEFAULT_SITES = 5
MAX_HOURS = 72
DEFAULT_HOURS = 24
#: A "latest reading" window; short on purpose, the answer only reports the last point.
LATEST_HOURS = 2

#: The site file barely changes, so it is cached for longer than the values.
SITE_TTL = 3600.0

#: Places people ask about, with coordinates. Reference geography shipped with this agent:
#: USGS gauges are in the United States and its territories, so this list is US-only.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "buffalo": (42.89, -78.88), "albany": (42.65, -73.76),
    "rochester": (43.16, -77.61), "hartford": (41.77, -72.68), "boston": (42.36, -71.06),
    "philadelphia": (39.95, -75.17), "pittsburgh": (40.44, -80.00), "harrisburg": (40.27, -76.88),
    "baltimore": (39.29, -76.61), "washington": (38.91, -77.04), "richmond": (37.54, -77.44),
    "charlotte": (35.23, -80.84), "raleigh": (35.78, -78.64), "atlanta": (33.75, -84.39),
    "nashville": (36.16, -86.78), "memphis": (35.15, -90.05), "knoxville": (35.96, -83.92),
    "birmingham": (33.52, -86.80), "jacksonville": (30.33, -81.66), "orlando": (28.54, -81.38),
    "tampa": (27.95, -82.46), "miami": (25.76, -80.19), "new orleans": (29.95, -90.07),
    "baton rouge": (30.45, -91.19), "columbus": (39.96, -83.00), "cleveland": (41.50, -81.69),
    "cincinnati": (39.10, -84.51), "detroit": (42.33, -83.05), "indianapolis": (39.77, -86.16),
    "chicago": (41.88, -87.63), "milwaukee": (43.04, -87.91), "madison": (43.07, -89.40),
    "minneapolis": (44.98, -93.27), "st paul": (44.95, -93.09), "des moines": (41.59, -93.62),
    "omaha": (41.26, -95.93), "kansas city": (39.10, -94.58), "st louis": (38.63, -90.20),
    "springfield": (37.21, -93.29), "oklahoma city": (35.47, -97.52), "tulsa": (36.15, -95.99),
    "dallas": (32.78, -96.80), "fort worth": (32.76, -97.33), "austin": (30.27, -97.74),
    "san antonio": (29.42, -98.49), "houston": (29.76, -95.37), "el paso": (31.76, -106.49),
    "denver": (39.74, -104.99), "golden": (39.76, -105.22), "boulder": (40.01, -105.27),
    "fort collins": (40.59, -105.08), "colorado springs": (38.83, -104.82), "pueblo": (38.25, -104.61),
    "grand junction": (39.06, -108.55), "durango": (37.28, -107.88), "steamboat springs": (40.48, -106.83),
    "salt lake city": (40.76, -111.89), "provo": (40.23, -111.66), "ogden": (41.22, -111.97),
    "boise": (43.62, -116.20), "missoula": (46.87, -113.99), "bozeman": (45.68, -111.04),
    "billings": (45.78, -108.50), "helena": (46.59, -112.04), "cheyenne": (41.14, -104.82),
    "casper": (42.85, -106.32), "jackson": (43.48, -110.76), "rapid city": (44.08, -103.23),
    "fargo": (46.88, -96.79), "sioux falls": (43.55, -96.70), "bismarck": (46.81, -100.78),
    "albuquerque": (35.08, -106.65), "santa fe": (35.69, -105.94), "las cruces": (32.31, -106.78),
    "phoenix": (33.45, -112.07), "tucson": (32.22, -110.97), "flagstaff": (35.20, -111.65),
    "las vegas": (36.17, -115.14), "reno": (39.53, -119.81), "elko": (40.83, -115.76),
    "los angeles": (34.05, -118.24), "san diego": (32.72, -117.16), "santa barbara": (34.42, -119.70),
    "fresno": (36.74, -119.79), "sacramento": (38.58, -121.49), "san francisco": (37.77, -122.42),
    "san jose": (37.34, -121.89), "oakland": (37.80, -122.27), "redding": (40.59, -122.39),
    "portland": (45.52, -122.68), "eugene": (44.05, -123.09), "salem": (44.94, -123.04),
    "medford": (42.33, -122.87), "bend": (44.06, -121.31), "seattle": (47.61, -122.33),
    "spokane": (47.66, -117.43), "tacoma": (47.25, -122.44), "yakima": (46.60, -120.51),
    "wenatchee": (47.42, -120.31), "anchorage": (61.22, -149.90), "fairbanks": (64.84, -147.72),
    "juneau": (58.30, -134.42), "honolulu": (21.31, -157.86), "san juan": (18.47, -66.11),
}

#: The bbox padding added around a radius so the box never clips a gauge on the edge.
BBOX_MARGIN_MILES = 3.0

#: How many candidates go into one multi-site values request, and how many such requests the
#: probe may make. A quiet gauge at the top of the list must not hide a reporting one just
#: behind it, but the service refuses (or times out on) oversized `sites` lists, so the probe
#: walks the nearest candidates in chunks and stops as soon as it has the answer.
PROBE_LIMIT = 20
PROBE_CHUNKS = 3


class RiversError(RuntimeError):
    """A USGS water service could not be read."""


class RiversBusy(RiversError):
    """The service is busy or unreachable: the same read is worth trying again.

    Kept separate from a plain RiversError so a rejected request (a 400) is never retried
    while a 502/503/504, a timeout or a dropped connection is.
    """


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles (mean Earth radius 3958.8 mi)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(a)))


def parse_rdb(text: str) -> list[dict]:
    """USGS tab-delimited output -> one dict per row, in the file's own column order.

    Comment lines (``#``) and the width-format line are skipped, and every field is
    stripped of the padding that RDB adds.
    """
    rows: list[dict] = []
    header: list[str] | None = None
    for line in str(text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if header is None:
            header = [field.strip() for field in fields]
            continue
        # The line under the header declares field widths: "5s 15s 50s ...".
        if all(re.fullmatch(r"\d+[a-z]+", field.strip()) for field in fields) and fields:
            continue
        rows.append({name: fields[index].strip() if index < len(fields) else ""
                     for index, name in enumerate(header)})
    return rows


def rdb_number(value):
    """An RDB field as a float, with USGS's no-value codes read as None."""
    text = str(value or "").strip()
    if not text or text in (NO_VALUE, f"{NO_VALUE}.0"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return None if number == float(NO_VALUE) else number


def parse_sites(text: str) -> list[dict]:
    """A site file -> [{site_number, site_name, latitude, longitude}], no-value rows dropped.

    The key is ``site_name`` so a site-file row and a values record describe a gauge the same
    way. A row without coordinates is dropped: a gauge that cannot be placed on the map cannot
    have a distance, and guessing one is not an option.
    """
    sites: list[dict] = []
    for row in parse_rdb(text):
        site_number = str(row.get("site_no") or "").strip()
        if not site_number or not site_number.isdigit():
            continue
        latitude = rdb_number(row.get("dec_lat_va"))
        longitude = rdb_number(row.get("dec_long_va"))
        if latitude is None or longitude is None:
            continue
        sites.append({
            "site_number": site_number,
            "site_name": " ".join(str(row.get("station_nm") or "").split()),
            "latitude": latitude,
            "longitude": longitude,
        })
    return sites


def _point_time(stamp: str):
    """USGS timestamps carry offsets (``2026-09-23T20:45:00.000-06:00``); sort on the real time."""
    try:
        return datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return datetime.min


def _latest(points: list[dict]) -> dict | None:
    return max(points, key=lambda point: _point_time(point["time"])) if points else None


def _earliest(points: list[dict]) -> dict | None:
    return min(points, key=lambda point: _point_time(point["time"])) if points else None


class RiversData:
    """Real-time USGS stream-gauge reads: one site, the gauges near a place, and trends."""

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, user_agent: str | None = None, sleep=None) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("RIVERS_BASE_URL") or BASE_URL).rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get("RIVERS_CACHE_TTL", "300"))
        self.timeout = float(timeout if timeout is not None else env.get("RIVERS_HTTP_TIMEOUT", "30"))
        self.user_agent = user_agent or env.get("RIVERS_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._sleep = sleep or time.sleep
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.RLock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, path: str, params: dict) -> str:
        """One HTTP attempt: the body as text, or a classified failure.

        A 502/503/504, a timeout or a dropped connection is a RiversBusy (so _query retries
        it); any other status is a plain RiversError, because retrying a 400 would not help.
        The JSON body is decoded by the caller's format.
        """
        query = parse.urlencode(params) if params else ""
        full = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                try:
                    return resp.read().decode("utf-8", "replace")
                except IncompleteRead as exc:
                    # USGS pipes some responses chunked and closes without the final chunk.
                    # What already arrived is the whole document, so it is kept rather than
                    # turning a good answer into a failure.
                    if exc.partial:
                        return bytes(exc.partial).decode("utf-8", "replace")
                    raise RiversBusy(f"USGS water services closed the response early for {path}") from exc
        except urlerror.HTTPError as exc:
            if exc.code in RETRY_STATUSES:
                raise RiversBusy(self._http_error(exc, path)) from exc
            raise RiversError(self._http_error(exc, path)) from exc
        except RiversError:
            raise
        except Exception as exc:  # urllib raises many types; callers see one
            raise RiversBusy(f"USGS water services request failed: {exc}") from exc

    @staticmethod
    def _http_error(exc, path: str) -> str:
        """A readable reason: an HTML error report is replaced by what the status means."""
        detail = ""
        try:
            detail = " ".join(exc.read().decode("utf-8", "replace").split())
        except Exception:  # the body is optional
            detail = ""
        note = f"USGS water services answered HTTP {exc.code} for {path}"
        if detail and not detail.startswith("<"):
            return f"{note}: {detail[:200]}"
        return f"{note} — the service is busy or unavailable; try again in a moment"

    def _query(self, path: str, params: dict, ttl: float | None = None) -> str:
        """One cached read, retrying the failures that are the service's own (RiversBusy)."""
        key = f"{path}:{json.dumps(params, sort_keys=True)}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]  # type: ignore[return-value]
        failure: RiversBusy | None = None
        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                self._sleep(RETRY_BACKOFF * attempt)
            try:
                body = self._fetch(path, params)
            except RiversBusy as exc:
                failure = exc
                continue
            if not isinstance(body, str):
                raise RiversError("USGS water services returned an unexpected payload")
            if body.lstrip().startswith("<"):
                # Both services answer a bad request with an HTML error report, which is a
                # failure the caller must hear about rather than an empty result set.
                raise RiversError(f"USGS water services rejected the request to {path}")
            with self._lock:
                self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), body)
            return body
        raise failure or RiversBusy(f"USGS water services could not be read for {path}")

    def _sites_in_box(self, south: float, west: float, north: float, east: float) -> list[dict]:
        """Every stream site in a box, by type only — the one filter combination that holds up.

        ``siteStatus=active`` answers 503 on a bbox and ``hasDataTypeCd=iv`` times out on the
        same box, so neither is sent: the values response is what decides which gauges report.
        """
        body = self._query(SITE_PATH, {
            "format": "rdb",
            "bBox": f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}",
            "siteType": SITE_TYPES,
        }, ttl=SITE_TTL)
        return parse_sites(body)

    def _sites_by_number(self, site_numbers: list[str]) -> list[dict]:
        body = self._query(SITE_PATH, {
            "format": "rdb",
            "sites": ",".join(site_numbers),
            "siteType": SITE_TYPES,
        }, ttl=SITE_TTL)
        return parse_sites(body)

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_site_number(value, name: str = "site number") -> str:
        """USGS site numbers are 8 to 15 digits (names are a separate read, not a guess)."""
        text = re.sub(r"[^\d]", "", str(value or ""))
        if not text.isdigit() or not 8 <= len(text) <= 15:
            raise ValueError(f"a USGS {name} is 8 to 15 digits, like 06719505")
        return text

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
            raise ValueError("a point must look like 39.74,-104.99")
        return RiversData.check_lat(parts[0]), RiversData.check_lon(parts[1])

    @staticmethod
    def check_radius(value, name: str = "radius") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not 1 <= number <= MAX_RADIUS_MILES:
            raise ValueError(f"{name} must be between 1 and {MAX_RADIUS_MILES:g} miles")
        return number

    @staticmethod
    def check_hours(value, name: str = "hours") -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number <= MAX_HOURS:
            raise ValueError(f"{name} must be between 1 and {MAX_HOURS}")
        return number

    @staticmethod
    def check_limit(value, name: str = "count") -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number <= MAX_SITES:
            raise ValueError(f"{name} must be between 1 and {MAX_SITES}")
        return number

    @staticmethod
    def city(name: str) -> tuple[float, float, str] | None:
        """A known place from the agent's own US-only list -> (lat, lon, label)."""
        key = " ".join(str(name or "").lower().strip().split())
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return lat, lon, key.title()
        for known, coords in CITY_COORDS.items():
            if key and len(key) >= 4 and (key in known or known in key):
                return coords[0], coords[1], known.title()
        return None

    # -- reads -------------------------------------------------------------

    def _read_values(self, site_numbers: list[str], hours: int) -> dict:
        """One multi-site values request -> {site_number: record} plus the service's read time."""
        numbers = [self.check_site_number(number) for number in site_numbers]
        if not numbers:
            raise ValueError("at least one site number is needed")
        hours = self.check_hours(hours)
        body = self._query(IV_PATH, {
            "format": "json",
            "sites": ",".join(numbers),
            "parameterCd": ",".join(READ_PARAMETERS),
            "period": f"PT{hours}H",
        })
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise RiversError("USGS sent a values response that is not JSON") from exc
        block = payload.get("value") if isinstance(payload, dict) else None
        series_list = (block or {}).get("timeSeries") if isinstance(block, dict) else None
        if series_list is None:
            series_list = []
        if not isinstance(series_list, list):
            raise RiversError("USGS sent a values response with an unexpected shape")
        records: dict[str, dict] = {}
        for series in series_list:
            if not isinstance(series, dict):
                continue
            source = series.get("sourceInfo") or {}
            codes = source.get("siteCode") or []
            site_number = str((codes[0] or {}).get("value") if codes else "").strip()
            if not site_number:
                continue
            variable = series.get("variable") or {}
            variable_codes = variable.get("variableCode") or []
            code = str((variable_codes[0] or {}).get("value") if variable_codes else "").strip()
            parameter = PARAMETERS.get(code)
            if not parameter:
                continue
            values = series.get("values") or []
            first_block = values[0] if values and isinstance(values[0], dict) else {}
            points = []
            for entry in first_block.get("value") or []:
                if not isinstance(entry, dict):
                    continue
                number = rdb_number(entry.get("value"))
                if number is None:
                    continue
                points.append({"time": str(entry.get("dateTime") or ""), "value": number})
            record = records.setdefault(site_number, {
                "site_number": site_number,
                "site_name": " ".join(str(source.get("siteName") or "").split()),
                "latitude": ((source.get("geoLocation") or {}).get("geogLocation") or {}).get("latitude"),
                "longitude": ((source.get("geoLocation") or {}).get("geogLocation") or {}).get("longitude"),
                "series": {},
            })
            if not record["site_name"] or record["latitude"] is None:
                record["site_name"] = record["site_name"] or " ".join(str(source.get("siteName") or "").split())
                geo = (source.get("geoLocation") or {}).get("geogLocation") or {}
                record["latitude"] = record["latitude"] if record["latitude"] is not None else geo.get("latitude")
                record["longitude"] = record["longitude"] if record["longitude"] is not None else geo.get("longitude")
            if not points:
                # A series whose every value is the no-data code is a gauge that published
                # nothing usable in the window: it must not look like a reading of zero.
                continue
            # USGS descriptions arrive with the codes, so the answer never invents one.
            qualifiers = [{"code": str(q.get("qualifierCode") or ""),
                           "description": " ".join(str(q.get("qualifierDescription") or "").split())}
                          for q in first_block.get("qualifier") or [] if isinstance(q, dict)]
            record["series"][parameter[0]] = {
                "parameter_code": code,
                "label": parameter[1],
                "unit_code": (variable.get("unit") or {}).get("unitCode") or parameter[3],
                "points": points,
                "latest": _latest(points),
                "earliest": _earliest(points),
                "qualifiers": qualifiers,
            }
        return {"records": records, "read_at": self._read_at(payload), "site_numbers": numbers,
                "hours": hours}

    @staticmethod
    def _read_at(payload) -> str | None:
        """The service's own request timestamp, from queryInfo's requestDT note."""
        for note in ((payload.get("value") or {}).get("queryInfo") or {}).get("note") or []:
            if isinstance(note, dict) and note.get("title") == "requestDT":
                return str(note.get("value") or "") or None
        return None

    def read_site(self, site_number, hours: int = LATEST_HOURS) -> dict:
        """One gauge: its name, its latest reading(s), and whether USGS knows the site at all."""
        number = self.check_site_number(site_number)
        read = self._read_values([number], hours)
        record = read["records"].get(number)
        if record and record["series"]:
            return {"found": True, "known": True, "site_number": number,
                    "site_name": record["site_name"], "latitude": record["latitude"],
                    "longitude": record["longitude"], "series": record["series"],
                    "read_at": read["read_at"], "hours": read["hours"]}
        # Empty values: the site file is what separates "no such site" from "quiet gauge".
        known = None
        for site in self._sites_by_number([number]):
            if site["site_number"] == number:
                known = site
                break
        return {
            "found": False,
            "known": bool(known),
            "site_number": number,
            "site_name": (known or {}).get("site_name") or (record or {}).get("site_name") or "",
            "latitude": (known or {}).get("latitude"),
            "longitude": (known or {}).get("longitude"),
            "series": {},
            "read_at": read["read_at"],
            "hours": read["hours"],
        }

    def nearest_sites(self, latitude, longitude, radius_miles=DEFAULT_RADIUS_MILES,
                      limit: int = DEFAULT_SITES) -> dict:
        """Stream sites within a radius of a point, nearest first — before any values read."""
        lat = self.check_lat(latitude)
        lon = self.check_lon(longitude)
        radius = self.check_radius(radius_miles)
        limit = self.check_limit(limit)
        reach = radius + BBOX_MARGIN_MILES
        dlat = reach / 69.0
        dlon = min(180.0, reach / max(0.01, 69.0 * math.cos(math.radians(lat))))
        sites = self._sites_in_box(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
        found = []
        for site in sites:
            miles = distance_miles(lat, lon, site["latitude"], site["longitude"])
            if miles <= radius:
                found.append({**site, "miles": round(miles, 1)})
        found.sort(key=lambda site: site["miles"])
        return {"latitude": lat, "longitude": lon, "radius_miles": radius,
                "sites_in_box": len(sites), "candidates": found, "sites": found[:limit],
                "found": len(found), "limit": limit}

    def gauges_near(self, latitude, longitude, radius_miles=DEFAULT_RADIUS_MILES,
                    limit: int = DEFAULT_SITES, hours: int = LATEST_HOURS) -> dict:
        """The nearest reporting gauges to a point: one site-file read plus a values probe.

        The values probe walks the nearest candidates in chunks of PROBE_LIMIT and stops as
        soon as `limit` gauges have reported, up to PROBE_CHUNKS chunks. A gauge that is
        listed but silent is never shown with an invented reading: it is counted as silent.
        """
        near = self.nearest_sites(latitude, longitude, radius_miles, limit)
        candidates = near["candidates"]
        readings: list[dict] = []
        silent: list[dict] = []
        checked = 0
        read_at = None
        for chunk in range(PROBE_CHUNKS):
            if len(readings) >= limit:
                break
            probe = candidates[checked:checked + PROBE_LIMIT]
            if not probe:
                break
            read = self._read_values([site["site_number"] for site in probe], hours)
            checked += len(probe)
            read_at = read["read_at"] or read_at
            for site in probe:
                series = (read["records"].get(site["site_number"]) or {}).get("series") or {}
                (readings if series else silent).append({**site, "series": series})
        return {**near, "readings": readings[:limit], "silent": silent, "checked": checked,
                "read_at": read_at}

    def trend(self, site_number, hours: int = DEFAULT_HOURS) -> dict:
        """How a gauge's discharge (or stage, if that is all it reports) moved over a window."""
        hours = self.check_hours(hours)
        read = self.read_site(site_number, hours=hours)
        if not read["found"]:
            return read
        series = read["series"]
        key = "discharge" if "discharge" in series else ("gage_height" if "gage_height" in series else None)
        if key is None:
            return {**read, "found": False, "reason": "this gauge publishes no discharge or gage height"}
        points = series[key]["points"]
        if len(points) < 2:
            return {**read, "trend": None, "parameter": key, "label": series[key]["label"],
                    "unit_code": series[key]["unit_code"], "points": len(points)}
        first, last = _earliest(points), _latest(points)
        values = [point["value"] for point in points]
        change = round(last["value"] - first["value"], 2)
        percent = round(100.0 * change / first["value"], 1) if first["value"] else None
        if abs(change) < 0.05:
            direction = "flat"
        elif change > 0:
            direction = "rising"
        else:
            direction = "falling"
        return {
            **read,
            "trend": {
                "direction": direction,
                "parameter": key,
                "label": series[key]["label"],
                "unit_code": series[key]["unit_code"],
                "unit_words": UNIT_WORDS.get(series[key]["unit_code"], series[key]["unit_code"]),
                "first": first,
                "last": last,
                "change": change,
                "percent": percent,
                "minimum": min(values),
                "maximum": max(values),
                "readings": len(points),
                "qualifiers": series[key]["qualifiers"],
            },
            "parameter": key,
            "label": series[key]["label"],
            "unit_code": series[key]["unit_code"],
            "points": len(points),
        }


__all__ = [
    "BASE_URL",
    "BBOX_MARGIN_MILES",
    "PROBE_CHUNKS",
    "PROBE_LIMIT",
    "CITY_COORDS",
    "DATASET_IV",
    "DATASET_SITE",
    "DEFAULT_HOURS",
    "DEFAULT_RADIUS_MILES",
    "DEFAULT_SITES",
    "IV_PATH",
    "LATEST_HOURS",
    "MAX_HOURS",
    "MAX_RADIUS_MILES",
    "MAX_SITES",
    "NO_VALUE",
    "PARAMETERS",
    "READ_PARAMETERS",
    "SITE_PATH",
    "SITE_TTL",
    "SITE_TYPES",
    "UNIT_WORDS",
    "RiversBusy",
    "RiversData",
    "RiversError",
    "distance_miles",
    "parse_rdb",
    "parse_sites",
    "rdb_number",
]
