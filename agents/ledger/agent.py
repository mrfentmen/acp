"""Ledger — US Treasury fiscal data inside your editor.

The third non-coding ACP agent in this repo, and the first public-finance agent of any
kind: it answers what the US government owes, what it pays in interest, what its cash
balance is, what it says foreign currencies are worth, and what it is auctioning —
straight from the Treasury's own Fiscal Data API.

Deterministic on purpose: routing is rules, every number comes from the Treasury, and
nothing is guessed. It reports a plan, opens one tool call per lookup, asks permission
before the first read, streams the answer, and closes the tool call with a one-line
summary of the dataset it read.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (  # noqa: E402
    AUCTION_SECURITY_TYPES,
    DATASET_DEBT,
    LedgerData,
    LedgerError,
)

HELP = (
    "I read the US Treasury's Fiscal Data API. Ask me:\n"
    "  • how big is the national debt right now?\n"
    "  • what is the average interest rate on Treasury bills?\n"
    "  • what exchange rate does the Treasury use for Japan?\n"
    "  • how much cash is in the Treasury General Account?\n"
    "  • what Treasury auctions are coming up?\n"
    "Every number is the Treasury's own, read live, and I say which dataset it came from. "
    "I am not a financial adviser."
)

PERMISSION_KEY = "ledger-read-public-treasury"

SKILLS = ("debt-outstanding", "interest-rates", "exchange-rates", "cash-balance", "auctions", "help")

#: Words the router recognises and the name the Treasury dataset uses.
SECURITY_WORDS = {
    "bill": "Bill", "bills": "Bill", "t-bill": "Bill", "tbills": "Bill",
    "note": "Note", "notes": "Note", "t-note": "Note",
    "bond": "Bond", "bonds": "Bond", "t-bond": "Bond",
    "tips": "TIPS", "inflation-protected": "TIPS",
    "frn": "FRN", "floating rate": "FRN",
    "cmb": "CMB", "cash management": "CMB",
}
#: For the average-rate dataset the Treasury calls them "Treasury Bills" and so on.
INTEREST_WORDS = {"bills": "Bills", "bill": "Bills", "notes": "Notes", "note": "Notes",
                  "bonds": "Bonds", "bond": "Bonds", "tips": "TIPS", "frn": "FRN"}
#: What a caller may ask for by name, for the honest "no rows" message.
INTEREST_CHOICES = "Bills, Notes, Bonds, TIPS, FRN or total"

#: A handful of currencies people ask about by name rather than by country.
CURRENCY_COUNTRIES = {
    "yen": "Japan", "euro": "Euro Zone", "pound": "United Kingdom", "sterling": "United Kingdom",
    "peso": "Mexico", "rupee": "India", "yuan": "China", "renminbi": "China",
    "won": "Korea", "real": "Brazil", "rand": "South Africa", "lira": "Turkey",
    "krona": "Sweden", "franc": "Switzerland", "zloty": "Poland", "baht": "Thailand",
    "ringgit": "Malaysia", "rupiah": "Indonesia", "shekel": "Israel", "dollar": "Australia",
}

_DEBT_WORDS = re.compile(r"\b(debt|owe|owing|borrow(?:ed|ing)?|deficit|national debt|debt to the penny)\b",
                         re.IGNORECASE)
_INTEREST_WORDS = re.compile(r"\b(interest rate|interest rates|average rate|paying|yield|yields)\b", re.IGNORECASE)
_EXCHANGE_WORDS = re.compile(r"\b(exchange rate|exchange rates|foreign exchange|forex|currency|currencies)\b",
                             re.IGNORECASE)
_CASH_WORDS = re.compile(r"\b(cash balance|how much cash|cash on hand|general account|tga|operating cash)\b",
                         re.IGNORECASE)
_AUCTION_WORDS = re.compile(r"\b(auction|auctions|auctioned|upcoming sale|issuance|issuing)\b", re.IGNORECASE)
_SECURITY_RE = re.compile(
    r"\b(bills?|notes?|bonds?|t-?bills?|t-?notes?|t-?bonds?|tips|frn|floating rate|cmb|cash management)\b",
    re.IGNORECASE,
)
_COUNTRY_RE = re.compile(
    r"\b(?:for|of|against|vs\.?|versus|in)\s+((?:[A-Z][\w.'\-]+(?:\s+(?:and|of|the|[A-Z][\w.'\-]+)){0,3})"
    r"|[A-Za-z]{3,20}-[A-Za-z]{3,20})"
)
_FOR_COUNTRY_RE = re.compile(r"\bfor\s+([a-z][a-z .'\-]{2,30})", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\b(?:last|latest|top|first)\s+(\d{1,3})\b", re.IGNORECASE)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    security_match = _SECURITY_RE.search(text)
    security = security_match.group(1).lower().replace("-", " ") if security_match else None
    limit_match = _LIMIT_RE.search(text)
    limit = int(limit_match.group(1)) if limit_match else None

    if _DEBT_WORDS.search(text):
        return "debt-outstanding", {}

    if _AUCTION_WORDS.search(text):
        params: dict = {}
        if security:
            lookup = SECURITY_WORDS.get(security) or SECURITY_WORDS.get(security + "s")
            if lookup in AUCTION_SECURITY_TYPES:
                params["security_type"] = lookup
        if limit:
            params["limit"] = limit
        return "auctions", params

    if _CASH_WORDS.search(text):
        return "cash-balance", {}

    if _INTEREST_WORDS.search(text):
        params = {}
        if security and security in INTEREST_WORDS:
            params["security"] = INTEREST_WORDS[security]
        return "interest-rates", params

    if _EXCHANGE_WORDS.search(text):
        params = {}
        country = None
        match = _COUNTRY_RE.search(text)
        if match:
            country = match.group(1).strip()
        else:
            lowered_match = _FOR_COUNTRY_RE.search(text)
            if lowered_match:
                words = []
                for word in lowered_match.group(1).split():
                    lowered_word = word.lower()
                    if not words and lowered_word in ("the", "a", "an"):
                        continue  # "for the yen" is about the yen
                    if lowered_word in _COUNTRY_STOPWORDS:
                        break
                    words.append(word)
                country = " ".join(words[:3]).strip()
        if country:
            params["country"] = _country_name(country)
            params["country_label"] = country
        if limit:
            params["limit"] = limit
        return "exchange-rates", params

    if security and security in INTEREST_WORDS:
        return "interest-rates", {"security": INTEREST_WORDS[security]}
    return "help", {}


#: Words that end a captured country phrase rather than belong to it.
_COUNTRY_STOPWORDS = frozenset(
    {"the", "a", "an", "today", "now", "this", "quarter", "please", "currency", "rate", "rates"}
)


def _country_name(phrase: str) -> str:
    """'the yen' -> Japan, 'Japanese yen' -> Japan, 'Germany' -> Germany."""
    words = [word for word in phrase.strip().split() if word.lower() not in ("the", "a", "an")]
    text = " ".join(words).strip()
    if not text:
        return phrase.strip()
    lowered = text.lower()
    if lowered in CURRENCY_COUNTRIES:
        return CURRENCY_COUNTRIES[lowered]
    last = words[-1].lower()
    if last in CURRENCY_COUNTRIES:
        return CURRENCY_COUNTRIES[last]
    return text.title() if text.islower() or text.isupper() else text


def _money(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "not published"


def _trillions(value: float | None) -> str:
    return f"${value / 1_000_000_000_000:,.2f} trillion" if value is not None else "not published"


def _millions(value: float | None) -> str:
    if value is None:
        return "not published"
    if abs(value) >= 1000:
        return f"${value:,.0f} million (${value / 1000:,.2f} billion)"
    return f"${value:,.0f} million"


class LedgerAgent(AcpAgent):
    name = "ledger"
    title = "Ledger — US Treasury fiscal data"
    version = "1.0.0"

    def __init__(self, connection=None, data: LedgerData | None = None) -> None:
        super().__init__(connection)
        self.data = data or LedgerData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Ledger session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the Treasury's own dataset", "medium"),
            ("Answer with the dataset id, record date and units", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("The Treasury publishes this data daily (debt) and monthly (interest rates).")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read the Treasury Fiscal Data API for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Ledger to read the public US Treasury Fiscal Data API?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the Treasury's public data before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (LedgerError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the Treasury dataset: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a single HTTP read; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "debt-outstanding":
            return self._debt()
        if skill == "interest-rates":
            return self._interest(params.get("security"))
        if skill == "exchange-rates":
            return self._exchange(params.get("country"), params.get("country_label"), params.get("limit"))
        if skill == "cash-balance":
            return self._cash()
        if skill == "auctions":
            return self._auctions(params.get("security_type"), params.get("limit"))
        return HELP, {"summary": "help", "dataset": None}

    def _debt(self) -> tuple[str, dict]:
        result = self.data.debt_to_penny()
        latest = result.get("latest")
        if not latest or latest.get("total") is None:
            return (
                "The Treasury's debt-to-the-penny dataset answered but published no rows.",
                {"summary": f"{DATASET_DEBT}: no rows", "dataset": DATASET_DEBT, "data_date": result.get("data_date")},
            )
        change = result.get("change")
        direction = ""
        if change is not None:
            direction = f", {'up' if change >= 0 else 'down'} {_money(abs(change))} on the previous business day"
        previous = result.get("previous") or {}
        answer = (
            f"The US total public debt outstanding was {_money(latest['total'])} "
            f"({_trillions(latest['total'])}) on {latest['record_date']}{direction}.\n"
            f"  • Held by the public: {_money(latest.get('public'))}\n"
            f"  • Intragovernmental holdings: {_money(latest.get('intragov'))}\n"
            + (f"  • Previous business day ({previous.get('record_date')}): {_money(previous.get('total'))}\n"
               if previous.get('record_date') else "")
            + f"\nSource: {DATASET_DEBT} (read live; {result.get('total_count'):,} daily rows published). "
            "This is the Treasury's own figure, not my estimate."
        )
        return answer, {
            "summary": f"{DATASET_DEBT}: {latest['total']:.2f} on {latest['record_date']}",
            "dataset": DATASET_DEBT,
            "record_date": latest["record_date"],
            "total": latest["total"],
            "public": latest.get("public"),
            "intragov": latest.get("intragov"),
            "change": change,
            "data_date": result.get("data_date"),
        }

    def _interest(self, security: str | None) -> tuple[str, dict]:
        result = self.data.average_interest_rates(security)
        rates = result.get("rates") or []
        if not rates:
            return (
                f"The Treasury published no average interest rate for {security or 'Treasury securities'}. "
                f"Try {INTEREST_CHOICES}.",
                {"summary": f"{result['dataset']}: no rows for {security or 'all'}", "dataset": result["dataset"]},
            )
        listing = "\n".join(
            f"  • {row.get('security')}: {row.get('rate')}%" + (f" ({row.get('security_type')})" if row.get("security_type") else "")
            for row in rates
        )
        answer = (
            f"The Treasury's average interest rate for {result['for']} as of {result['record_date']}:\n"
            f"{listing}\n\nSource: {result['dataset']} (read live; published monthly, "
            f"{result.get('total_count'):,} rows in total). Rates are the average paid on outstanding securities."
        )
        return answer, {
            "summary": f"{result['dataset']}: {len(rates)} rate(s) for {result['for']} on {result['record_date']}",
            "dataset": result["dataset"],
            "for": result["for"],
            "record_date": result["record_date"],
            "rates": rates,
            "data_date": result.get("data_date"),
        }

    def _exchange(self, country: str | None, label: str | None, limit) -> tuple[str, dict]:
        result = self.data.exchange_rates(country, limit=int(limit) if limit else 15)
        rates = result.get("rates") or []
        name = label or result["for"]
        if not rates:
            return (
                f"The Treasury published no exchange rate for “{name}”. It reports rates quarterly for "
                "the currencies it transacts in; try a country name such as Japan, or a pair such as Japan-Yen.",
                {"summary": f"{result['dataset']}: no rows for {name}", "dataset": result["dataset"]},
            )
        listing = "\n".join(
            f"  • {row.get('country')} ({row.get('currency')}): {row.get('rate_per_dollar')} per US dollar"
            for row in rates[:10]
        )
        answer = (
            f"The Treasury's official rates of exchange for {name} as of {result['record_date']} "
            f"(for reporting, not trading):\n{listing}\n\n"
            f"Source: {result['dataset']} (read live; published quarterly, {result.get('total_count'):,} rows). "
            "These are the rates US agencies must use to report foreign currency."
        )
        return answer, {
            "summary": f"{result['dataset']}: {len(rates)} rate(s) for {name} on {result['record_date']}",
            "dataset": result["dataset"],
            "for": name,
            "record_date": result["record_date"],
            "rates": rates,
            "data_date": result.get("data_date"),
        }

    def _cash(self) -> tuple[str, dict]:
        result = self.data.cash_balance()
        accounts = result.get("accounts") or []
        if not accounts:
            return (
                "The Treasury's operating cash balance dataset answered but published no rows.",
                {"summary": f"{result['dataset']}: no rows", "dataset": result["dataset"]},
            )
        listing = "\n".join(
            f"  • {row.get('account')}: {_millions(row.get('open_today'))}"
            + (f" (month to date {_millions(row.get('open_month'))})" if row.get("open_month") is not None else "")
            for row in accounts
            if row.get("open_today") is not None
        )
        total = next((row for row in accounts if "Treasury General Account" in (row.get("account") or "")
                      and row.get("open_today") is not None), None)
        headline = (
            f"The Treasury General Account opened at {_millions(total['open_today'])} on "
            f"{result['record_date']}.\n" if total else ""
        )
        answer = (
            f"{headline}{listing}\n\n"
            f"Source: {result['dataset']} (read live, in {result['units']}; "
            f"{result.get('total_count'):,} rows across the Daily Treasury Statement)."
        )
        return answer, {
            "summary": f"{result['dataset']}: {len(accounts)} account(s) on {result['record_date']}",
            "dataset": result["dataset"],
            "record_date": result["record_date"],
            "accounts": accounts,
            "units": result.get("units"),
        }

    def _auctions(self, security_type: str | None, limit) -> tuple[str, dict]:
        result = self.data.auctions(limit=int(limit) if limit else 10, security_type=security_type)
        auctions = result.get("auctions") or []
        if not auctions:
            return (
                f"The Treasury's auction dataset published no {security_type or ''} auctions. "
                "Try Bill, Note, Bond, TIPS, FRN or CMB.",
                {"summary": f"{result['dataset']}: no rows", "dataset": result["dataset"]},
            )
        listing = "\n".join(
            f"  • {row.get('auction_date')} — {row.get('security_type')} {row.get('security_term')} "
            f"(CUSIP {row.get('cusip')}, issues {row.get('issue_date')}, matures {row.get('maturity_date')})"
            + (f" offering {_money(row.get('offering_amt'))}" if row.get("offering_amt") else "")
            + (f", bid-to-cover {row['bid_to_cover']}" if row.get("bid_to_cover") else "")
            for row in auctions[:10]
        )
        answer = (
            f"{len(auctions)} Treasury auction(s) for {result['for']}, most recent auction date first:\n{listing}\n\n"
            f"Source: {result['dataset']} (read live; {result.get('total_count'):,} auction rows). "
            "Dates and amounts are the Treasury's."
        )
        return answer, {
            "summary": f"{result['dataset']}: {len(auctions)} auction(s) for {result['for']}",
            "dataset": result["dataset"],
            "for": result["for"],
            "auctions": auctions,
            "data_date": result.get("data_date"),
        }


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        LedgerAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
