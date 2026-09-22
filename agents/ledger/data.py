"""Read-only reader for the US Treasury Fiscal Data API.

Self-contained on purpose: this repo has no dependency on the a2a repo, so the
transport and its small cache live here. Every endpoint is keyless and was verified
live on 2026-09-22:

  /v2/accounting/od/debt_to_penny                     total public debt, daily
  /v2/accounting/od/avg_interest_rates                average rates on Treasury securities, monthly
  /v1/accounting/od/rates_of_exchange                 Treasury's official quarterly exchange rates
  /v1/accounting/dts/operating_cash_balance           the Treasury General Account, business days
  /v1/accounting/od/auctions_query                    auction results and upcoming auctions

The service has its own query language and its own quirks, all handled here:

1. `filter=field:eq:value` is how you narrow a dataset — not a free-text search.
2. `sort=-record_date` sorts descending, and `page[size]=N` is the page size.
3. Numbers arrive as strings ("40101500516712.09"), and missing values arrive as
   the literal string "null", so everything numeric is parsed defensively.
4. Rows are stamped with `meta.total-count` and `meta.dataDate`, which are carried
   into every answer so a caller can see how much of the dataset was read and when
   the Treasury last updated it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib import error as urlerror
from urllib import parse, request

BASE_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"

DEBT_PATH = "/v2/accounting/od/debt_to_penny"
INTEREST_PATH = "/v2/accounting/od/avg_interest_rates"
EXCHANGE_PATH = "/v1/accounting/od/rates_of_exchange"
CASH_PATH = "/v1/accounting/dts/operating_cash_balance"
AUCTIONS_PATH = "/v1/accounting/od/auctions_query"

DATASET_DEBT = "fiscaldata.treasury.gov/debt_to_penny"
DATASET_INTEREST = "fiscaldata.treasury.gov/avg_interest_rates"
DATASET_EXCHANGE = "fiscaldata.treasury.gov/rates_of_exchange"
DATASET_CASH = "fiscaldata.treasury.gov/dts/operating_cash_balance"
DATASET_AUCTIONS = "fiscaldata.treasury.gov/auctions_query"

MAX_PAGE = 100
DEFAULT_USER_AGENT = "acp-ledger/1.0 (+https://github.com/mrfentmen/acp)"

#: Fields kept per dataset, in artifact order.
DEBT_FIELDS = ("record_date", "tot_pub_debt_out_amt", "debt_held_public_amt", "intragov_hold_amt")
INTEREST_FIELDS = ("record_date", "security_type_desc", "security_desc", "avg_interest_rate_amt")
EXCHANGE_FIELDS = ("record_date", "country", "currency", "country_currency_desc", "exchange_rate", "effective_date")
CASH_FIELDS = ("record_date", "account_type", "open_today_bal", "open_month_bal", "open_fiscal_year_bal")
AUCTION_FIELDS = ("record_date", "cusip", "security_type", "security_term", "auction_date", "issue_date",
                  "maturity_date", "offering_amt", "total_accepted", "high_yield", "high_discount_rate",
                  "bid_to_cover_ratio")

#: Short words mapped to the exact security_desc values the Treasury publishes
#: (checked against the live dataset on 2026-09-22).
SECURITY_DESCRIPTIONS = {
    "bill": "Treasury Bills", "bills": "Treasury Bills", "t-bill": "Treasury Bills",
    "note": "Treasury Notes", "notes": "Treasury Notes", "t-note": "Treasury Notes",
    "bond": "Treasury Bonds", "bonds": "Treasury Bonds", "t-bond": "Treasury Bonds",
    "tips": "Treasury Inflation-Protected Securities (TIPS)",
    "inflation protected": "Treasury Inflation-Protected Securities (TIPS)",
    "frn": "Treasury Floating Rate Notes (FRN)",
    "floating rate": "Treasury Floating Rate Notes (FRN)",
    "total": "Total Interest-bearing Debt",
    "marketable": "Total Marketable",
    "non marketable": "Total Non-marketable",
    "savings": "United States Savings Securities",
    "government account": "Government Account Series",
}
SECURITY_TYPES = ("Bills", "Notes", "Bonds", "TIPS", "FRN")
AUCTION_SECURITY_TYPES = ("Bill", "Note", "Bond", "TIPS", "FRN", "CMB")

#: Path -> dataset id, so a page can label itself without the caller passing one.
DATASET_FOR_PATH = {
    DEBT_PATH: DATASET_DEBT,
    INTEREST_PATH: DATASET_INTEREST,
    EXCHANGE_PATH: DATASET_EXCHANGE,
    CASH_PATH: DATASET_CASH,
    AUCTIONS_PATH: DATASET_AUCTIONS,
}


class LedgerError(RuntimeError):
    """The Treasury API could not be read."""


def _number(value) -> float | None:
    """Treasury sends numbers as strings, and missing values as the string 'null'."""
    if value in (None, "", "null", "None"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _iso(value) -> str | None:
    text = str(value or "").strip()
    return text or None


class LedgerData:
    """Treasury datasets over HTTP: injectable fetch, cached per query, threadsafe."""

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, user_agent: str | None = None) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("LEDGER_BASE_URL") or BASE_URL).rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get("LEDGER_CACHE_TTL", "900"))
        self.timeout = float(timeout if timeout is not None else env.get("LEDGER_HTTP_TIMEOUT", "20"))
        self.user_agent = user_agent or env.get("LEDGER_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.RLock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, path: str, params: dict):
        query = parse.urlencode(params, doseq=True) if params else ""
        full = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # the body is optional
                detail = ""
            raise LedgerError(f"Treasury answered {exc.code}{': ' + detail if detail else ''}") from exc
        except Exception as exc:  # urllib raises many types; callers see one
            raise LedgerError(f"Treasury request failed: {exc}") from exc

    def _page(self, path: str, fields: tuple[str, ...], page_size: int = 20, sort: str = "-record_date",
              filter_: str | None = None, ttl: float | None = None) -> dict:
        size = max(1, min(int(page_size), MAX_PAGE))
        params: dict[str, str] = {"sort": sort, "page[size]": str(size)}
        if filter_:
            params["filter"] = filter_
        key = f"{path}:{json.dumps(params, sort_keys=True)}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(path, params)
        if not isinstance(payload, dict):
            raise LedgerError("Treasury returned an unexpected payload")
        rows = payload.get("data")
        if rows is None:
            raise LedgerError("Treasury returned a payload with no data array")
        meta = payload.get("meta") or {}
        result = {
            "rows": [{field: row[field] for field in fields if field in row} for row in rows],
            "total_count": int(meta.get("total-count") or len(rows)),
            "data_date": meta.get("dataDate"),
            "dataset": DATASET_FOR_PATH.get(path, path),
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
    def check_date(value, name: str = "date") -> str:
        text = str(value).strip()
        parts = text.split("-")
        if len(parts) != 3 or not all(part.isdigit() for part in parts) or len(parts[0]) != 4:
            raise ValueError(f"{name} must look like 2026-09-18")
        return text

    @staticmethod
    def check_text(value, name: str = "value") -> str:
        """Fiscal Data filters are field:eq:value, so the value may not contain a colon."""
        text = " ".join(str(value).split())
        if not 2 <= len(text) <= 60 or ":" in text:
            raise ValueError(f"{name} must be 2-60 characters without a colon")
        return text

    @staticmethod
    def security_description(name: str) -> str:
        """'bills' -> 'Treasury Bills'; an already-precise name passes through unchanged."""
        text = LedgerData.check_text(name, "security")
        lowered = text.lower().strip()
        if lowered in SECURITY_DESCRIPTIONS:
            return SECURITY_DESCRIPTIONS[lowered]
        for exact in SECURITY_DESCRIPTIONS.values():
            if lowered == exact.lower():
                return exact
        return text

    # -- reads -------------------------------------------------------------

    def debt_to_penny(self) -> dict:
        """Total public debt outstanding, with the previous business day's figure."""
        page = self._page(DEBT_PATH, DEBT_FIELDS, page_size=2, ttl=min(self.cache_ttl, 3600))
        if not page["rows"]:
            return {"dataset": DATASET_DEBT, "data_date": page["data_date"], "rows": [], "latest": None,
                    "previous": None, "change": None, "total_count": page["total_count"]}
        latest = page["rows"][0]
        previous = page["rows"][1] if len(page["rows"]) > 1 else None
        total = _number(latest.get("tot_pub_debt_out_amt"))
        prior = _number((previous or {}).get("tot_pub_debt_out_amt"))
        return {
            "dataset": DATASET_DEBT,
            "data_date": page["data_date"],
            "total_count": page["total_count"],
            "latest": {
                "record_date": _iso(latest.get("record_date")),
                "total": total,
                "public": _number(latest.get("debt_held_public_amt")),
                "intragov": _number(latest.get("intragov_hold_amt")),
            },
            "previous": {
                "record_date": _iso((previous or {}).get("record_date")),
                "total": prior,
            } if previous else None,
            "change": (round(total - prior, 2) if total is not None and prior is not None else None),
        }

    def average_interest_rates(self, security: str | None = None) -> dict:
        """Latest month's average interest rates, optionally for one security description."""
        filter_ = None
        label = "all Treasury securities"
        if security:
            description = self.security_description(security)
            filter_ = f"security_desc:eq:{description}"
            label = description
        page = self._page(INTEREST_PATH, INTEREST_FIELDS, page_size=30, filter_=filter_,
                          ttl=min(self.cache_ttl, 3600))
        rows = page["rows"]
        latest_date = _iso(rows[0].get("record_date")) if rows else None
        latest = [row for row in rows if _iso(row.get("record_date")) == latest_date]
        return {
            "dataset": DATASET_INTEREST,
            "data_date": page["data_date"],
            "total_count": page["total_count"],
            "for": label,
            "filter": filter_,
            "record_date": latest_date,
            "rates": [
                {
                    "security_type": row.get("security_type_desc"),
                    "security": row.get("security_desc"),
                    "rate": _number(row.get("avg_interest_rate_amt")),
                    "dataset": DATASET_INTEREST,
                }
                for row in latest
            ],
        }

    def exchange_rates(self, country_or_currency: str | None = None, limit: int = 15) -> dict:
        """Treasury's official quarterly rates of exchange."""
        filter_ = None
        label = "all reported countries"
        if country_or_currency:
            value = self.check_text(country_or_currency, "country or currency")
            if "-" in value:
                filter_ = f"country_currency_desc:eq:{value}"
                label = value
            else:
                filter_ = f"country:eq:{value}"
                label = value
        page = self._page(EXCHANGE_PATH, EXCHANGE_FIELDS, page_size=self.check_positive(limit),
                          filter_=filter_, ttl=min(self.cache_ttl, 3600))
        rows = page["rows"]
        latest_date = _iso(rows[0].get("record_date")) if rows else None
        latest = [row for row in rows if _iso(row.get("record_date")) == latest_date]
        return {
            "dataset": DATASET_EXCHANGE,
            "data_date": page["data_date"],
            "total_count": page["total_count"],
            "for": label,
            "filter": filter_,
            "record_date": latest_date,
            "rates": [
                {
                    "country": row.get("country"),
                    "currency": row.get("currency"),
                    "rate_per_dollar": _number(row.get("exchange_rate")),
                    "effective_date": _iso(row.get("effective_date")),
                    "dataset": DATASET_EXCHANGE,
                }
                for row in latest
            ],
        }

    def cash_balance(self) -> dict:
        """The Treasury General Account: how much cash the government holds."""
        page = self._page(CASH_PATH, CASH_FIELDS, page_size=30, ttl=min(self.cache_ttl, 3600))
        rows = page["rows"]
        latest_date = _iso(rows[0].get("record_date")) if rows else None
        latest = [row for row in rows if _iso(row.get("record_date")) == latest_date]
        entries = [
            {
                "account": row.get("account_type"),
                "open_today": _number(row.get("open_today_bal")),
                "open_month": _number(row.get("open_month_bal")),
                "open_fiscal_year": _number(row.get("open_fiscal_year_bal")),
                "dataset": DATASET_CASH,
            }
            for row in latest
        ]
        return {
            "dataset": DATASET_CASH,
            "data_date": page["data_date"],
            "total_count": page["total_count"],
            "record_date": latest_date,
            "accounts": entries,
            "units": "millions of dollars",
        }

    def auctions(self, limit: int = 10, security_type: str | None = None) -> dict:
        """Auction results and upcoming auctions, most recent auction date first."""
        filter_ = None
        label = "all security types"
        if security_type:
            value = self.check_text(security_type, "security type")
            filter_ = f"security_type:eq:{value}"
            label = value
        page = self._page(AUCTIONS_PATH, AUCTION_FIELDS, page_size=self.check_positive(limit),
                          sort="-auction_date", filter_=filter_, ttl=min(self.cache_ttl, 3600))
        return {
            "dataset": DATASET_AUCTIONS,
            "data_date": page["data_date"],
            "total_count": page["total_count"],
            "for": label,
            "filter": filter_,
            "auctions": [
                {
                    "security_type": row.get("security_type"),
                    "security_term": row.get("security_term"),
                    "cusip": row.get("cusip"),
                    "auction_date": _iso(row.get("auction_date")),
                    "issue_date": _iso(row.get("issue_date")),
                    "maturity_date": _iso(row.get("maturity_date")),
                    "offering_amt": _number(row.get("offering_amt")),
                    "bid_to_cover": _number(row.get("bid_to_cover_ratio")),
                    "high_yield": _number(row.get("high_yield")),
                    "high_discount_rate": _number(row.get("high_discount_rate")),
                    "dataset": DATASET_AUCTIONS,
                }
                for row in page["rows"]
            ],
        }
