"""Read-only reader for the NHTSA safety APIs and the NHTSA vPIC VIN decoder.

Self-contained on purpose: this repo has no dependency on the a2a repo, so the
transport and its small cache live here. Two keyless NHTSA hosts, verified live on
2026-09-22:

  api.nhtsa.gov
    /recalls/recallsByVehicle?make=&model=&modelYear=        safety recalls for a vehicle
    /complaints/complaintsByVehicle?make=&model=&modelYear=  owner complaints for a vehicle
    /products/vehicle/models?make=&modelYear=&issueType=r    models a make sold in a year
  vpic.nhtsa.dot.gov
    /api/vehicles/DecodeVinValues/{vin}?format=json          VIN -> make, model, year, plant, engine

The quirks, all handled here:

1. An unknown make or model answers HTTP 400 with an empty result set. That is "no
   data", not a failure, so it becomes an empty read with a message — not an error.
2. The two services spell the row count differently: recalls use `Count`, complaints
   use `count`. Both are read, and the length of the list is the fallback.
3. Dates arrive as DD/MM/YYYY, not ISO. They are passed through exactly as published
   and parsed only when the rows have to be sorted.
4. vPIC always answers with one row of about 154 fields, most of them empty strings,
   plus an ErrorText that says whether the VIN was decoded completely. Only a curated
   set of fields is kept.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from urllib import error as urlerror
from urllib import parse, request

NHTSA_BASE_URL = "https://api.nhtsa.gov"
VPIC_BASE_URL = "https://vpic.nhtsa.dot.gov"

RECALLS_PATH = "/recalls/recallsByVehicle"
COMPLAINTS_PATH = "/complaints/complaintsByVehicle"
MODELS_PATH = "/products/vehicle/models"
VIN_PATH = "/api/vehicles/DecodeVinValues/{vin}"

DATASET_RECALLS = "nhtsa.gov/recallsByVehicle"
DATASET_COMPLAINTS = "nhtsa.gov/complaintsByVehicle"
DATASET_MODELS = "nhtsa.gov/products/vehicle/models"
DATASET_VIN = "vpic.nhtsa.dot.gov/DecodeVinValues"

DEFAULT_USER_AGENT = "acp-vehicles/1.0 (+https://github.com/mrfentmen/acp)"

#: Fields kept per answer, in artifact order.
RECALL_FIELDS = ("NHTSACampaignNumber", "Component", "ReportReceivedDate", "Consequence", "Remedy",
                 "Summary", "parkIt", "parkOutSide", "overTheAirUpdate", "Manufacturer")
COMPLAINT_FIELDS = ("odiNumber", "dateComplaintFiled", "dateOfIncident", "components", "crash", "fire",
                    "numberOfInjuries", "numberOfDeaths", "summary", "vin")
VIN_FIELDS = ("Make", "Model", "ModelYear", "Series", "Trim", "BodyClass", "VehicleType", "DriveType",
              "EngineCylinders", "DisplacementL", "EngineHP", "FuelTypePrimary", "TransmissionStyle",
              "Doors", "GVWR", "Manufacturer", "PlantCity", "PlantCountry", "PlantState",
              "SeatBeltsAll", "AirBagLocFront", "ErrorCode", "ErrorText")

#: The VIN alphabet leaves out I, O and Q so they are not confused with 1 and 0;
#: vPIC also accepts * where a digit is unknown, and partial VINs down to 11 characters.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9*]{11,17}$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")

#: The oldest model year worth asking NHTSA about.
MIN_YEAR = 1949


class VehicleError(RuntimeError):
    """The NHTSA service could not be read."""


def _text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _flag(value) -> bool | None:
    """NHTSA sends recall flags as the strings 'Y'/'N'/'2' or leaves them empty."""
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text in ("Y", "YES", "1", "TRUE"):
        return True
    if text in ("N", "NO", "0", "FALSE"):
        return False
    return True  # '2' and anything else the agency uses means the campaign covers it


def _date_sort_key(value, day_first: bool) -> tuple:
    """A published date -> a sortable tuple. Unparseable dates sort last.

    NHTSA publishes recalls day-first (25/03/2021 exists) and complaints month-first
    (08/30/2026 exists). A component larger than 12 settles which way a date runs; when
    both are 12 or less the row's dataset decides via `day_first`.
    """
    match = _DATE_RE.match(str(value or "").strip())
    if not match:
        return (0, 0, 0)
    first, second, year = (int(part) for part in match.groups())
    if first > 12:
        day, month = first, second
    elif second > 12:
        day, month = second, first
    elif day_first:
        day, month = first, second
    else:
        month, day = first, second
    return (year, month, day)


class VehicleData:
    """NHTSA recalls, complaints, models and VIN decoding: injectable fetch, cached, threadsafe."""

    def __init__(self, fetch=None, base_url: str | None = None, vpic_url: str | None = None,
                 cache_ttl: float | None = None, timeout: float | None = None,
                 user_agent: str | None = None) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("VEHICLES_BASE_URL") or NHTSA_BASE_URL).rstrip("/")
        self.vpic_url = (vpic_url or env.get("VEHICLES_VPIC_URL") or VPIC_BASE_URL).rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get("VEHICLES_CACHE_TTL", "1800"))
        self.timeout = float(timeout if timeout is not None else env.get("VEHICLES_HTTP_TIMEOUT", "20"))
        self.user_agent = user_agent or env.get("VEHICLES_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.RLock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, base: str, path: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{base}{path}" + (f"?{query}" if query else "")
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # the body is optional
                body = ""
            if exc.code == 400:
                # NHTSA says 400 for "no such make/model", with an empty result set.
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("results") == []:
                    return parsed
            raise VehicleError(f"NHTSA answered {exc.code}{': ' + body if body else ''}") from exc
        except Exception as exc:  # urllib raises many types; callers see one
            raise VehicleError(f"NHTSA request failed: {exc}") from exc

    def _get(self, key: str, base: str, path: str, params: dict, ttl: float | None = None) -> dict:
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(base, path, params)
        if not isinstance(payload, dict):
            raise VehicleError("NHTSA returned an unexpected payload")
        # api.nhtsa.gov spells it 'results'; vpic.nhtsa.dot.gov spells it 'Results'.
        rows = payload.get("results")
        if rows is None:
            rows = payload.get("Results")
        if rows is None:
            raise VehicleError("NHTSA returned a payload with no results array")
        result = {
            "rows": rows,
            "count": int(payload.get("Count") or payload.get("count") or len(rows)),
        }
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), result)
        return result

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_positive(value, name: str = "limit", maximum: int = 100) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return number

    @staticmethod
    def check_text(value, name: str = "value") -> str:
        text = " ".join(str(value or "").split())
        if not 2 <= len(text) <= 40 or not re.search(r"[A-Za-z]", text):
            raise ValueError(f"{name} must be 2-40 characters of letters")
        return text

    @staticmethod
    def check_year(value, name: str = "year") -> int:
        try:
            year = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a four-digit year") from None
        ceiling = datetime.now().year + 1
        if not MIN_YEAR <= year <= ceiling:
            raise ValueError(f"{name} must be between {MIN_YEAR} and {ceiling}")
        return year

    @staticmethod
    def check_vin(value) -> str:
        vin = "".join(str(value or "").split()).upper()
        if not _VIN_RE.match(vin):
            raise ValueError("a VIN must be 11-17 letters and digits (I, O and Q are never used; "
                             "* stands for an unknown digit)")
        return vin

    @staticmethod
    def check_model(value, name: str = "model") -> str:
        """Model names are freer than makes: 'CR-V', 'F-150', 'Model 3' all exist."""
        text = " ".join(str(value or "").split())
        if not 1 <= len(text) <= 40:
            raise ValueError(f"{name} must be 1-40 characters")
        return text

    # -- reads -------------------------------------------------------------

    def recalls(self, make: str, model: str, year) -> dict:
        """Every safety recall NHTSA has for one make, model and model year."""
        make = self.check_text(make, "make")
        model = self.check_model(model)
        year = self.check_year(year)
        params = {"make": make, "model": model, "modelYear": str(year)}
        key = f"recalls:{json.dumps(params, sort_keys=True)}"
        page = self._get(key, self.base_url, RECALLS_PATH, params)
        rows = [row for row in page["rows"] if isinstance(row, dict)]
        recalls = [
            {
                "campaign": _text(row.get("NHTSACampaignNumber")),
                "component": _text(row.get("Component")),
                "reported": _text(row.get("ReportReceivedDate")),
                "consequence": _text(row.get("Consequence")),
                "remedy": _text(row.get("Remedy")),
                "park_it": _flag(row.get("parkIt")),
                "park_outside": _flag(row.get("parkOutSide")),
                "over_the_air_update": _flag(row.get("overTheAirUpdate")),
                "manufacturer": _text(row.get("Manufacturer")),
                "dataset": DATASET_RECALLS,
            }
            for row in rows
        ]
        recalls.sort(key=lambda item: _date_sort_key(item.get("reported"), day_first=True), reverse=True)
        return {
            "dataset": DATASET_RECALLS,
            "make": make.title(),
            "model": model.upper() if model.islower() else model,
            "year": year,
            "count": page["count"] if rows else 0,
            "recalls": recalls,
            "park_it_flags": sorted({item["component"] or "" for item in recalls if item["park_it"]}),
        }

    def complaints(self, make: str, model: str, year, limit: int = 10, recent: int = 5) -> dict:
        """Owner complaints NHTSA received, plus a tally of the harm they report."""
        make = self.check_text(make, "make")
        model = self.check_model(model)
        year = self.check_year(year)
        limit = self.check_positive(limit, "limit", maximum=100)
        recent = self.check_positive(recent, "recent", maximum=25)
        params = {"make": make, "model": model, "modelYear": str(year)}
        key = f"complaints:{json.dumps(params, sort_keys=True)}"
        page = self._get(key, self.base_url, COMPLAINTS_PATH, params)
        rows = [row for row in page["rows"] if isinstance(row, dict)]
        rows.sort(key=lambda row: _date_sort_key(row.get("dateComplaintFiled"), day_first=False), reverse=True)
        kept = rows[:limit]
        components: dict[str, int] = {}
        for row in rows:
            for part in str(row.get("components") or "").split(","):
                name = part.strip()
                if name:
                    components[name] = components.get(name, 0) + 1
        top = sorted(components.items(), key=lambda item: (-item[1], item[0]))[:5]
        return {
            "dataset": DATASET_COMPLAINTS,
            "make": make.title(),
            "model": model.upper() if model.islower() else model,
            "year": year,
            "count": page["count"] if rows else 0,
            "injuries": sum(int(row.get("numberOfInjuries") or 0) for row in rows),
            "deaths": sum(int(row.get("numberOfDeaths") or 0) for row in rows),
            "crashes": sum(1 for row in rows if str(row.get("crash")).lower() in ("true", "1", "yes")),
            "fires": sum(1 for row in rows if str(row.get("fire")).lower() in ("true", "1", "yes")),
            "top_components": [{"component": name, "complaints": count} for name, count in top],
            "recent": [
                {
                    "odi_number": row.get("odiNumber"),
                    "filed": _text(row.get("dateComplaintFiled")),
                    "incident": _text(row.get("dateOfIncident")),
                    "component": _text(row.get("components")),
                    "crash": str(row.get("crash")).lower() in ("true", "1", "yes"),
                    "fire": str(row.get("fire")).lower() in ("true", "1", "yes"),
                    "injuries": int(row.get("numberOfInjuries") or 0),
                    "deaths": int(row.get("numberOfDeaths") or 0),
                    "summary": _text(row.get("summary")),
                    "dataset": DATASET_COMPLAINTS,
                }
                for row in kept[:recent]
            ],
        }

    def models(self, make: str, year, limit: int = 25) -> dict:
        """Every model a make sold in a model year, according to NHTSA's own list."""
        make = self.check_text(make, "make")
        year = self.check_year(year)
        limit = self.check_positive(limit, "limit", maximum=100)
        params = {"make": make, "modelYear": str(year), "issueType": "r"}
        key = f"models:{json.dumps(params, sort_keys=True)}"
        page = self._get(key, self.base_url, MODELS_PATH, params)
        names: list[str] = []
        seen: set[str] = set()
        for row in page["rows"]:
            if not isinstance(row, dict):
                continue
            name = _text(row.get("model"))
            # NHTSA files the same model under different casing, so keep the first spelling.
            if name and name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
        return {
            "dataset": DATASET_MODELS,
            "make": make.title(),
            "year": year,
            "count": len(names),
            "models": names[:limit],
            "truncated": len(names) > limit,
        }

    def decode_vin(self, vin: str) -> dict:
        """VIN -> the curated identity of the vehicle, straight from NHTSA's decoder."""
        vin = self.check_vin(vin)
        path = VIN_PATH.format(vin=vin)
        key = f"vin:{vin}"
        page = self._get(key, self.vpic_url, path, {"format": "json"}, ttl=min(self.cache_ttl, 86400))
        rows = [row for row in page["rows"] if isinstance(row, dict)]
        row = rows[0] if rows else {}
        vehicle = {field: _text(row.get(field)) for field in VIN_FIELDS if field not in ("ErrorCode", "ErrorText")}
        vehicle = {field: value for field, value in vehicle.items() if value}
        error_code = _text(row.get("ErrorCode")) or ""
        codes = [code for code in error_code.split(",") if code.strip()]
        return {
            "dataset": DATASET_VIN,
            "vin": vin,
            "vehicle": vehicle,
            "error_code": error_code or None,
            "error_text": _text(row.get("ErrorText")),
            # vPIC uses code 0 for a clean decode; anything else means some field was unknown.
            "fully_decoded": codes == ["0"],
            "fields_decoded": len(vehicle),
        }
