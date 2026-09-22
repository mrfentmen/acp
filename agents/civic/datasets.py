"""Read-only NYC Open Data reader for the civic agent.

Deliberately self-contained (this repo has no dependency on the a2a repo): one
Socrata transport, a small cache, and the four queries the agent needs. Datasets
verified live 2026-09-22.

  erm2-nwe9  311 Service Requests 2020–present (~22.5M rows)
  aq7i-eu5q  FloodNet street flooding events
  bkwf-xfky  Drinking water distribution monitoring (~172K rows)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib import parse, request

BASE_URL = "https://data.cityofnewyork.us"

DATASETS = {
    "311": ("erm2-nwe9", "311 Service Requests from 2020 to Present"),
    "311_sensors": ("kb2e-tjy3", "FloodNet: Sensor Deployment Metadata"),
    "flood": ("aq7i-eu5q", "FloodNet: Street Flooding Events Measured by FloodNet Sensors"),
    "water": ("bkwf-xfky", "Drinking Water Quality Distribution Monitoring Data"),
}

COMPLAINT_FIELDS = (
    "unique_key", "complaint_type", "descriptor", "status", "created_date", "closed_date",
    "resolution_description", "street_name", "incident_zip", "borough", "agency_name",
)
FLOOD_FIELDS = ("sensor_name", "flood_start_time", "flood_end_time", "max_depth_inches", "duration_mins")
WATER_FIELDS = (
    "sample_number", "sample_date", "sample_time", "sample_site", "sample_class",
    "residual_free_chlorine_mg_l", "turbidity_ntu",
    "coliform_quanti_tray_mpn_100ml", "e_coli_quanti_tray_mpn_100ml",
)

_ZIP_RE = re.compile(r"^\d{5}$")
_KEY_RE = re.compile(r"^\d{6,9}$")
_SITE_RE = re.compile(r"^[A-Za-z0-9]{3,10}$")


class CivicDataError(RuntimeError):
    """The public dataset could not be read."""


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip().lstrip("<"))
    except ValueError:
        return None


def _mpn(value):
    """Quanti-Tray MPN: '<1' means not detected."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.startswith("<"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


class CivicData:
    def __init__(self, fetch=None, cache_ttl: float = 60.0, timeout: float = 20.0, app_token: str | None = None) -> None:
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.app_token = app_token if app_token is not None else (os.environ.get("CIVIC_APP_TOKEN") or None)
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        headers = {"Accept": "application/json"}
        if self.app_token:
            headers["X-App-Token"] = self.app_token
        req = request.Request(full, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise CivicDataError(f"NYC Open Data request failed: {exc}") from exc

    def _cached(self, key: str, ttl: float, producer):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = producer()
        with self._lock:
            self._cache[key] = (now + ttl, value)
        return value

    def _rows(self, dataset: str, params: dict, ttl: float | None = None) -> list[dict]:
        def produce():
            body = self._fetch(f"{BASE_URL}/resource/{dataset}.json", params)
            if isinstance(body, dict) and "error" in body:
                raise CivicDataError(f"NYC Open Data error: {body['error']}")
            if not isinstance(body, list):
                raise CivicDataError("NYC Open Data returned an unexpected payload")
            return body

        key = f"{dataset}:{json.dumps(params, sort_keys=True)}"
        return self._cached(key, ttl if ttl is not None else min(self.cache_ttl, 60), produce)

    def freshness(self, dataset_key: str) -> str | None:
        dataset = DATASETS[dataset_key][0]

        def produce():
            body = self._fetch(f"{BASE_URL}/api/views/{dataset}.json", {})
            epoch = body.get("rowsUpdatedAt") if isinstance(body, dict) else None
            if not epoch:
                return None
            return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        return self._cached(f"freshness:{dataset}", 3600, produce)

    @staticmethod
    def _pick(row: dict, fields: tuple[str, ...], dataset: str) -> dict:
        summary = {field: row[field] for field in fields if field in row}
        summary["dataset"] = dataset
        return summary

    # -- queries -----------------------------------------------------------

    def complaint(self, unique_key: str) -> dict | None:
        key = str(unique_key).strip()
        if not _KEY_RE.match(key):
            raise ValueError("a 311 complaint key is 6–9 digits")
        dataset = DATASETS["311"][0]
        rows = self._rows(dataset, {"$where": f"unique_key='{key}'", "$limit": "1"}, ttl=30)
        return self._pick(rows[0], COMPLAINT_FIELDS, dataset) if rows else None

    def complaints_near(self, zip_code: str, days: int = 30, limit: int = 10) -> list[dict]:
        value = str(zip_code).strip()
        if not _ZIP_RE.match(value):
            raise ValueError("a NYC ZIP code is 5 digits")
        days = max(1, min(int(days), 365))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        dataset = DATASETS["311"][0]
        rows = self._rows(
            dataset,
            {
                "$where": f"incident_zip='{value}' AND created_date > '{cutoff}'",
                "$order": "created_date DESC",
                "$limit": str(max(1, min(int(limit), 50))),
            },
            ttl=30,
        )
        return [self._pick(row, COMPLAINT_FIELDS, dataset) for row in rows]

    def flood_sensor_ids(self, zip_code: str) -> list[str]:
        """FloodNet sensor ids deployed in a ZIP code (from the deployment metadata)."""
        value = str(zip_code).strip()
        if not _ZIP_RE.match(value):
            raise ValueError("a NYC ZIP code is 5 digits")
        rows = self._rows(DATASETS["311_sensors"][0], {"$where": f"zipcode='{value}'", "$limit": "200"}, ttl=3600)
        return [row["sensor_id"] for row in rows if row.get("sensor_id")]

    def flood_recent(self, hours: int = 72, zip_code: str | None = None, limit: int = 10) -> list[dict]:
        hours = max(1, min(int(hours), 24 * 365))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        where = [f"flood_start_time > '{cutoff}'"]
        if zip_code is not None:
            sensor_ids = self.flood_sensor_ids(zip_code)
            if not sensor_ids:
                return []  # no sensors deployed there: no events either
            where.append("sensor_id in (" + ", ".join(f"'{sid}'" for sid in sensor_ids[:200]) + ")")
        dataset = DATASETS["flood"][0]
        rows = self._rows(
            dataset,
            {"$where": " AND ".join(where), "$order": "flood_start_time DESC", "$limit": str(max(1, min(int(limit), 50)))},
            ttl=60,
        )
        return [self._pick(row, FLOOD_FIELDS, dataset) for row in rows]

    def water_quality(self, site: str, days: int = 180) -> tuple[list[dict], dict]:
        value = str(site).strip()
        if not _SITE_RE.match(value):
            raise ValueError("a DEP sample site looks like '55450' or '1S03A'")
        days = max(1, min(int(days), 730))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        dataset = DATASETS["water"][0]
        rows = self._rows(
            dataset,
            {
                "$where": f"sample_site='{value}' AND sample_date > '{cutoff}'",
                "$order": "sample_date DESC, sample_time DESC",
                "$limit": "300",
            },
            ttl=120,
        )
        samples = [self._pick(row, WATER_FIELDS, dataset) for row in rows]
        return samples, summarize_samples(samples)


def summarize_samples(samples: list[dict]) -> dict:
    """Detection + chemistry summary. Pure function of the sample rows."""
    chlorine = [v for v in (_number(row.get("residual_free_chlorine_mg_l")) for row in samples) if v is not None]
    turbidity = [v for v in (_number(row.get("turbidity_ntu")) for row in samples) if v is not None]
    coliform = [v for v in (_mpn(row.get("coliform_quanti_tray_mpn_100ml")) for row in samples) if v is not None]
    e_coli = [v for v in (_mpn(row.get("e_coli_quanti_tray_mpn_100ml")) for row in samples) if v is not None]
    return {
        "samples": len(samples),
        "first_sample_date": samples[-1]["sample_date"][:10] if samples and samples[-1].get("sample_date") else None,
        "last_sample_date": samples[0]["sample_date"][:10] if samples and samples[0].get("sample_date") else None,
        "chlorine_mg_l": {
            "min": round(min(chlorine), 3) if chlorine else None,
            "max": round(max(chlorine), 3) if chlorine else None,
        },
        "turbidity_ntu_max": round(max(turbidity), 3) if turbidity else None,
        "coliform_detections": sum(1 for v in coliform if v > 0),
        "coliform_samples": len(coliform),
        "e_coli_detections": sum(1 for v in e_coli if v > 0),
        "e_coli_samples": len(e_coli),
    }
