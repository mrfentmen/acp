"""Tests for the ledger ACP agent: Treasury data reader, routing, skills, permissions.

    python3 tests/test_ledger.py
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = REPO_ROOT / "agents" / "ledger"
for path in (str(REPO_ROOT), str(LEDGER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    CURRENCY_COUNTRIES,
    LedgerAgent,
    route,
)
from data import (  # noqa: E402
    AUCTIONS_PATH,
    CASH_PATH,
    SECURITY_DESCRIPTIONS,
    DATASET_AUCTIONS,
    DATASET_CASH,
    DATASET_DEBT,
    DATASET_EXCHANGE,
    DATASET_INTEREST,
    DEBT_PATH,
    EXCHANGE_PATH,
    INTEREST_PATH,
    LedgerData,
    LedgerError,
    _number,
)

DEBT = {
    "dataset": DATASET_DEBT,
    "data_date": "2026-09-18",
    "total_count": 8396,
    "latest": {"record_date": "2026-09-18", "total": 40101500516712.09,
               "public": 32401926526743.72, "intragov": 7699573989968.37},
    "previous": {"record_date": "2026-09-17", "total": 40093343468150.50},
    "change": 8157048561.59,
}

INTEREST = {
    "dataset": DATASET_INTEREST,
    "data_date": "2026-08-31",
    "total_count": 5009,
    "for": "Treasury Bills",
    "record_date": "2026-08-31",
    "rates": [{"security_type": "Marketable", "security": "Treasury Bills", "rate": 3.788,
               "dataset": DATASET_INTEREST}],
}

EXCHANGE = {
    "dataset": DATASET_EXCHANGE,
    "data_date": "2026-06-30",
    "total_count": 104,
    "for": "Japan",
    "record_date": "2026-06-30",
    "rates": [{"country": "Japan", "currency": "Yen", "rate_per_dollar": 162.38,
               "effective_date": "2026-06-30", "dataset": DATASET_EXCHANGE}],
}

CASH = {
    "dataset": DATASET_CASH,
    "data_date": "2026-09-18",
    "total_count": 16626,
    "record_date": "2026-09-18",
    "units": "millions of dollars",
    "accounts": [
        {"account": "Treasury General Account (TGA) Opening Balance", "open_today": 972675.0,
         "open_month": 1023554.0, "open_fiscal_year": 873000.0, "dataset": DATASET_CASH},
        {"account": "Federal Reserve Account", "open_today": 0.0, "open_month": 0.0,
         "open_fiscal_year": 0.0, "dataset": DATASET_CASH},
    ],
}

AUCTIONS = {
    "dataset": DATASET_AUCTIONS,
    "data_date": "2026-09-30",
    "total_count": 11127,
    "for": "all security types",
    "auctions": [
        {"security_type": "Note", "security_term": "7-Year", "cusip": "91282CRM5",
         "auction_date": "2026-09-24", "issue_date": "2026-09-30", "maturity_date": "2033-09-30",
         "offering_amt": 44000000000.0, "bid_to_cover": 2.45, "high_yield": 4.12,
         "high_discount_rate": None, "dataset": DATASET_AUCTIONS},
    ],
}


class FakeData(LedgerData):
    """Same interface as LedgerData, no network."""

    def __init__(self, debt=DEBT, interest=INTEREST, exchange=EXCHANGE, cash=CASH, auctions=AUCTIONS,
                 raise_error: bool = False):
        self._debt = debt
        self._interest = interest
        self._exchange = exchange
        self._cash = cash
        self._auctions = auctions
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def debt_to_penny(self):
        if self.raise_error:
            raise LedgerError("Treasury dataset offline")
        self.calls.append({"kind": "debt"})
        return dict(self._debt)

    def average_interest_rates(self, security=None):
        if self.raise_error:
            raise LedgerError("Treasury dataset offline")
        self.calls.append({"kind": "interest", "security": security})
        return {**self._interest, "for": security or "all Treasury securities"}

    def exchange_rates(self, country_or_currency=None, limit=15):
        if self.raise_error:
            raise LedgerError("Treasury dataset offline")
        self.calls.append({"kind": "exchange", "country": country_or_currency, "limit": limit})
        return dict(self._exchange)

    def cash_balance(self):
        if self.raise_error:
            raise LedgerError("Treasury dataset offline")
        self.calls.append({"kind": "cash"})
        return dict(self._cash)

    def auctions(self, limit=10, security_type=None):
        if self.raise_error:
            raise LedgerError("Treasury dataset offline")
        self.calls.append({"kind": "auctions", "limit": limit, "security_type": security_type})
        return {**self._auctions, "for": security_type or "all security types"}


class QueueReader:
    def __init__(self):
        self._items: queue.Queue = queue.Queue()

    def push(self, text):
        self._items.put(text)

    def close(self):
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if item is None:
            raise StopIteration
        return item


class WiredWriter:
    def __init__(self, reader):
        self.reader = reader

    def write(self, text):
        self.reader.push(text)

    def flush(self):
        pass

    def close(self):
        self.reader.close()


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


def fetch_from(payloads, calls=None):
    """A LedgerData `fetch` that dispatches on path, so data.py runs for real."""

    def fetch(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        payload = payloads[path]
        return payload() if callable(payload) else payload

    return fetch


def page(rows, total=None, data_date="2026-09-18"):
    return {"data": rows, "meta": {"total-count": total if total is not None else len(rows),
                                   "dataDate": data_date}}


class HelperTests(unittest.TestCase):
    def test_number_handles_treasury_strings(self):
        self.assertEqual(_number("40101500516712.09"), 40101500516712.09)
        self.assertEqual(_number("1,234.5"), 1234.5)
        self.assertIsNone(_number("null"))
        self.assertIsNone(_number(""))
        self.assertIsNone(_number(None))
        self.assertIsNone(_number("not a number"))

    def test_check_helpers(self):
        self.assertEqual(LedgerData.check_positive("5"), 5)
        with self.assertRaises(ValueError):
            LedgerData.check_positive("0")
        with self.assertRaises(ValueError):
            LedgerData.check_positive("200", maximum=100)
        self.assertEqual(LedgerData.check_date("2026-09-18"), "2026-09-18")
        with self.assertRaises(ValueError):
            LedgerData.check_date("18/09/2026")
        self.assertEqual(LedgerData.check_text("Treasury Bills"), "Treasury Bills")
        with self.assertRaises(ValueError):
            LedgerData.check_text("a:b")  # a colon would break the filter language

    def test_security_description(self):
        self.assertEqual(LedgerData.security_description("bills"), "Treasury Bills")
        self.assertEqual(LedgerData.security_description("BOND"), "Treasury Bonds")
        self.assertEqual(LedgerData.security_description("Treasury Notes"), "Treasury Notes")
        self.assertEqual(LedgerData.security_description("FRN"), "Treasury Floating Rate Notes (FRN)")
        self.assertEqual(LedgerData.security_description("tips"),
                         "Treasury Inflation-Protected Securities (TIPS)")
        self.assertEqual(LedgerData.security_description("total"), "Total Interest-bearing Debt")

    def test_security_descriptions_match_what_the_treasury_publishes(self):
        # Copied from the live avg_interest_rates rows on 2026-09-22.
        published = {
            "Treasury Bills", "Treasury Notes", "Treasury Bonds",
            "Treasury Floating Rate Notes (FRN)", "Treasury Inflation-Protected Securities (TIPS)",
            "Total Marketable", "Total Non-marketable", "Total Interest-bearing Debt",
            "Government Account Series", "United States Savings Securities",
        }
        for value in SECURITY_DESCRIPTIONS.values():
            self.assertIn(value, published)


class ReaderTests(unittest.TestCase):
    def test_debt_to_penny_parses_and_diffs(self):
        rows = [
            {"record_date": "2026-09-18", "tot_pub_debt_out_amt": "40101500516712.09",
             "debt_held_public_amt": "32401926526743.72", "intragov_hold_amt": "7699573989968.37"},
            {"record_date": "2026-09-17", "tot_pub_debt_out_amt": "40093343468150.50",
             "debt_held_public_amt": "32390000000000.00", "intragov_hold_amt": "7690000000000.00"},
        ]
        data = LedgerData(fetch=fetch_from({DEBT_PATH: page(rows, total=8396)}))
        result = data.debt_to_penny()
        self.assertEqual(result["latest"]["total"], 40101500516712.09)
        self.assertEqual(result["latest"]["record_date"], "2026-09-18")
        self.assertAlmostEqual(result["change"], 8157048561.59, places=2)
        self.assertEqual(result["total_count"], 8396)
        self.assertEqual(result["dataset"], DATASET_DEBT)

    def test_debt_with_a_single_row_has_no_change(self):
        rows = [{"record_date": "2026-09-18", "tot_pub_debt_out_amt": "40101500516712.09"}]
        data = LedgerData(fetch=fetch_from({DEBT_PATH: page(rows)}))
        result = data.debt_to_penny()
        self.assertIsNone(result["change"])
        self.assertIsNone(result["previous"])

    def test_average_interest_rates_filters_and_takes_the_latest_month(self):
        rows = [
            {"record_date": "2026-08-31", "security_type_desc": "Marketable", "security_desc": "Treasury Bills",
             "avg_interest_rate_amt": "3.788"},
            {"record_date": "2026-08-31", "security_type_desc": "Marketable", "security_desc": "Treasury Notes",
             "avg_interest_rate_amt": "3.022"},
            {"record_date": "2026-07-31", "security_type_desc": "Marketable", "security_desc": "Treasury Bills",
             "avg_interest_rate_amt": "3.795"},
        ]
        calls = []

        def fetch(path, params):
            calls.append((path, dict(params)))
            wanted = (params.get("filter") or "").removeprefix("security_desc:eq:")
            matched = [row for row in rows if not wanted or row["security_desc"] == wanted]
            return page(matched)

        data = LedgerData(fetch=fetch)
        result = data.average_interest_rates("bills")
        path, params = calls[0]
        self.assertEqual(path, INTEREST_PATH)
        self.assertEqual(params["filter"], "security_desc:eq:Treasury Bills")
        self.assertEqual(params["sort"], "-record_date")
        self.assertEqual(len(result["rates"]), 1)
        self.assertEqual(result["rates"][0]["rate"], 3.788)
        self.assertEqual(result["record_date"], "2026-08-31")

        unfiltered = data.average_interest_rates()
        self.assertEqual(len(unfiltered["rates"]), 2)
        self.assertEqual(unfiltered["for"], "all Treasury securities")

    def test_exchange_rates_by_country_and_by_pair(self):
        rows = [
            {"record_date": "2026-06-30", "country": "Japan", "currency": "Yen",
             "country_currency_desc": "Japan-Yen", "exchange_rate": "162.38",
             "effective_date": "2026-06-30"},
        ]
        calls = []
        data = LedgerData(fetch=fetch_from({EXCHANGE_PATH: page(rows, total=104)}, calls))
        result = data.exchange_rates("Japan")
        self.assertEqual(calls[0][1]["filter"], "country:eq:Japan")
        self.assertEqual(result["rates"][0]["rate_per_dollar"], 162.38)

        data.exchange_rates("Japan-Yen")
        self.assertEqual(calls[1][1]["filter"], "country_currency_desc:eq:Japan-Yen")

    def test_cash_balance_groups_by_the_latest_date(self):
        rows = [
            {"record_date": "2026-09-18", "account_type": "Treasury General Account (TGA) Opening Balance",
             "open_today_bal": "972675", "open_month_bal": "1023554", "open_fiscal_year_bal": "873000"},
            {"record_date": "2026-09-18", "account_type": "Federal Reserve Account", "open_today_bal": "0",
             "open_month_bal": "0", "open_fiscal_year_bal": "0"},
            {"record_date": "2026-09-17", "account_type": "Treasury General Account (TGA) Opening Balance",
             "open_today_bal": "910000", "open_month_bal": "1000000", "open_fiscal_year_bal": "800000"},
        ]
        data = LedgerData(fetch=fetch_from({CASH_PATH: page(rows, total=16626)}))
        result = data.cash_balance()
        self.assertEqual(result["record_date"], "2026-09-18")
        self.assertEqual(len(result["accounts"]), 2)
        self.assertEqual(result["accounts"][0]["open_today"], 972675.0)
        self.assertEqual(result["units"], "millions of dollars")

    def test_auctions_pass_the_security_filter(self):
        rows = [{"record_date": "2026-09-30", "cusip": "91282CRM5", "security_type": "Note",
                 "security_term": "7-Year", "auction_date": "2026-09-24", "issue_date": "2026-09-30",
                 "maturity_date": "2033-09-30", "offering_amt": "44000000000",
                 "bid_to_cover_ratio": "2.45", "high_yield": "4.12"}]
        calls = []
        data = LedgerData(fetch=fetch_from({AUCTIONS_PATH: page(rows, total=11127)}, calls))
        result = data.auctions(limit=5, security_type="Note")
        path, params = calls[0]
        self.assertEqual(path, AUCTIONS_PATH)
        self.assertEqual(params["filter"], "security_type:eq:Note")
        self.assertEqual(params["sort"], "-auction_date")
        self.assertEqual(params["page[size]"], "5")
        self.assertEqual(result["auctions"][0]["offering_amt"], 44000000000.0)

    def test_missing_data_array_is_an_error(self):
        data = LedgerData(fetch=fetch_from({DEBT_PATH: {"meta": {"total-count": 0}}}))
        with self.assertRaises(LedgerError):
            data.debt_to_penny()

    def test_http_error_becomes_a_ledger_error_without_the_url(self):
        import urllib.error

        error = urllib.error.HTTPError("https://api.fiscaldata.treasury.gov/x", 400, "Bad Request", {},
                                      mock.Mock(read=lambda: b'{"message":"bad filter"}'))
        with mock.patch("data.request.urlopen", side_effect=error):
            data = LedgerData()
            with self.assertRaises(LedgerError) as caught:
                data.debt_to_penny()
        message = str(caught.exception)
        self.assertIn("Treasury answered 400", message)
        self.assertIn("bad filter", message)


class RouteTests(unittest.TestCase):
    def test_debt(self):
        self.assertEqual(route("how big is the national debt right now?")[0], "debt-outstanding")
        self.assertEqual(route("what do we owe?")[0], "debt-outstanding")
        self.assertEqual(route("debt to the penny")[0], "debt-outstanding")

    def test_interest(self):
        skill, params = route("what is the average interest rate on Treasury bills?")
        self.assertEqual(skill, "interest-rates")
        self.assertEqual(params["security"], "Bills")
        self.assertEqual(route("what are we paying on bonds?")[0], "interest-rates")

    def test_exchange(self):
        skill, params = route("what exchange rate does the Treasury use for Japan?")
        self.assertEqual(skill, "exchange-rates")
        self.assertEqual(params["country"], "Japan")
        self.assertEqual(params["country_label"], "Japan")
        skill, params = route("what is the exchange rate for the yen?")
        self.assertEqual(params["country"], "Japan")
        self.assertEqual(params["country_label"], "yen")
        self.assertEqual(route("what is the exchange rate for euro?")[1]["country"], "Euro Zone")
        self.assertEqual(route("exchange rate for japan?")[1]["country"], "Japan")
        self.assertNotIn("country", route("what exchange rates does the Treasury publish?")[1])

    def test_cash(self):
        self.assertEqual(route("how much cash is in the Treasury General Account?")[0], "cash-balance")
        self.assertEqual(route("what is the TGA balance?")[0], "cash-balance")

    def test_auctions(self):
        skill, params = route("what Treasury auctions are coming up?")
        self.assertEqual(skill, "auctions")
        self.assertNotIn("security_type", params)
        skill, params = route("when is the next 10-year note auction?")
        self.assertEqual(skill, "auctions")
        self.assertEqual(params["security_type"], "Note")

    def test_limits(self):
        self.assertEqual(route("show me the last 5 auctions")[1]["limit"], 5)

    def test_help_and_unknown(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("what is the weather today?")[0], "help")

    def test_currency_map_covers_the_common_ones(self):
        self.assertEqual(CURRENCY_COUNTRIES["yen"], "Japan")
        self.assertEqual(CURRENCY_COUNTRIES["euro"], "Euro Zone")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = LedgerAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_debt_answer_cites_the_dataset_and_the_date(self):
        result, client = self.turn("how big is the national debt right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("$40,101,500,516,712.09", result["text"])
        self.assertIn("$40.10 trillion", result["text"])
        self.assertIn(DATASET_DEBT, result["text"])
        self.assertIn("2026-09-18", result["text"])
        self.assertIn("up $8,157,048,561.59", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "debt-outstanding")
        self.assertEqual(tools[0]["kind"], "fetch")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("how big is the national debt right now?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_interest_answer(self):
        result, _ = self.turn("what is the average interest rate on Treasury bills?")
        self.assertIn("3.788%", result["text"])
        self.assertIn(DATASET_INTEREST, result["text"])
        self.assertIn("2026-08-31", result["text"])

    def test_exchange_answer(self):
        result, _ = self.turn("what exchange rate does the Treasury use for Japan?")
        self.assertIn("162.38", result["text"])
        self.assertIn(DATASET_EXCHANGE, result["text"])
        self.assertIn("Japan", result["text"])

    def test_cash_answer_names_the_account_and_units(self):
        result, _ = self.turn("how much cash is in the Treasury General Account?")
        self.assertIn("Treasury General Account", result["text"])
        self.assertIn("$972,675 million", result["text"])
        self.assertIn("millions of dollars", result["text"])
        self.assertIn(DATASET_CASH, result["text"])

    def test_auctions_answer(self):
        result, _ = self.turn("what Treasury auctions are coming up?")
        self.assertIn("91282CRM5", result["text"])
        self.assertIn("7-Year", result["text"])
        self.assertIn(DATASET_AUCTIONS, result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("national debt", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_unknown_question_gets_help_not_a_guess(self):
        result, _ = self.turn("what is the weather today?")
        self.assertIn("I read the US Treasury", result["text"])

    def test_permission_denied(self):
        result, _ = self.turn("how big is the national debt?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_dataset_failure_is_reported(self):
        result, _ = self.turn("how big is the national debt?", data=FakeData(raise_error=True))
        self.assertIn("could not read the Treasury dataset", result["text"])
        statuses = [update.get("status") for update in result["updates"] if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_empty_rates_are_reported_honestly(self):
        empty = {"dataset": DATASET_INTEREST, "data_date": None, "total_count": 0,
                 "for": "Treasury Bonds", "record_date": None, "rates": []}
        result, _ = self.turn("what is the average interest rate on Treasury bonds?",
                              data=FakeData(interest=empty))
        self.assertIn("published no average interest rate", result["text"])
        self.assertIn("Try Bills, Notes, Bonds, TIPS, FRN or total", result["text"])


if __name__ == "__main__":
    unittest.main()
