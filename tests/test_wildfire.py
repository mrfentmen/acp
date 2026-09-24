"""Tests for the wildfire ACP agent: NIFC/WFIGS reader, routing, skills, permissions.

    python3 tests/test_wildfire.py

No network: the reader runs against injected payloads and the agent against a FakeData.
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
WILDFIRE_DIR = REPO_ROOT / "agents" / "wildfire"
for path in (str(REPO_ROOT), str(WILDFIRE_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    DEFAULT_RADIUS_MILES,
    WildfireAgent,
    name_from_text,
    place_from_text,
    route,
    state_from_text,
)
from data import (  # noqa: E402
    DATASET,
    FIELDS,
    INCIDENTS_PATH,
    MAX_ROWS,
    WildfireData,
    WildfireError,
    escape_literal,
    haversine_miles,
    to_iso,
)

#: Real shapes, copied from the live WFIGS layer on 2026-09-22.
TIMBER = {
    "attributes": {
        "IncidentName": "Timber", "IncidentSize": 12345.6, "PercentContained": 35.0,
        "FireDiscoveryDateTime": 1757000000000, "IncidentTypeCategory": "WF", "POOState": "US-CO",
        "POOCounty": "Larimer", "FireCause": "Human", "GACC": "Rocky Mountain",
        "IncidentManagementOrganization": "Type 3 Team", "UniqueFireIdentifier": "2026-CO-RMA-000123",
        "ModifiedOnDateTime_dt": 1757100000000,
    },
    "geometry": {"x": -105.5, "y": 40.6},
}
RIDGE = {
    "attributes": {
        "IncidentName": "Ridge", "IncidentSize": 100.0, "PercentContained": 90.0,
        "FireDiscoveryDateTime": 1757000000000, "IncidentTypeCategory": "WF", "POOState": "US-CA",
        "POOCounty": "Ventura", "FireCause": "Lightning", "GACC": "South Ops",
        "IncidentManagementOrganization": None, "UniqueFireIdentifier": "2026-CA-SOC-000456",
        "ModifiedOnDateTime_dt": None,
    },
    "geometry": {"x": -119.2, "y": 34.4},
}
#: A neighbour of New York City: 8.5 miles out, so distances are exercised in both directions.
HUDSON = {
    "attributes": {
        "IncidentName": "Hudson", "IncidentSize": 12.0, "PercentContained": 100.0,
        "FireDiscoveryDateTime": 1757000000000, "IncidentTypeCategory": "RX", "POOState": "US-NY",
        "POOCounty": "Orange", "FireCause": None, "GACC": "Eastern",
        "IncidentManagementOrganization": None, "UniqueFireIdentifier": "2026-NY-EAS-000789",
        "ModifiedOnDateTime_dt": 1757000000000,
    },
    "geometry": {"x": -73.9, "y": 40.8},
}
#: Some rows arrive with no geometry; they cannot have a distance and must be dropped.
GEOMETRYLESS = {
    "attributes": {
        "IncidentName": "NoPoint", "IncidentSize": 500.0, "PercentContained": 10.0,
        "FireDiscoveryDateTime": 1757000000000, "IncidentTypeCategory": "WF", "POOState": "US-NV",
        "POOCounty": "Elko", "FireCause": "Unknown", "GACC": "Great Basin",
        "IncidentManagementOrganization": None, "UniqueFireIdentifier": "2026-NV-GBA-000111",
        "ModifiedOnDateTime_dt": None,
    },
}

NYC = (40.71, -74.01)


def fetch_from(payloads, calls=None):
    """A WildfireData `fetch` that dispatches on path, so data.py runs for real."""

    def fetch(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        payload = payloads[path]
        return payload() if callable(payload) else payload

    return fetch


class FakeData(WildfireData):
    """Same interface as WildfireData, no network."""

    def __init__(self, active=None, near=None, summary=None, lookup=None, raise_error: bool = False):
        self._active = active if active is not None else {
            "dataset": DATASET, "where": "POOState = 'US-CO'", "count": 2, "acres": 12445.6,
            "incidents": [_row(TIMBER), _row(RIDGE)],
        }
        self._near = near if near is not None else {
            "dataset": DATASET, "origin": {"latitude": NYC[0], "longitude": NYC[1]},
            "radius_miles": 100.0, "count": 1, "acres": 12.0,
            "incidents": [{**_row(HUDSON), "distance_miles": 8.5}],
        }
        self._summary = summary if summary is not None else {
            "dataset": DATASET, "scope": "the United States", "count": 3, "acres": 12957.6,
            "uncontained": 2, "biggest": [_row(TIMBER), _row(GEOMETRYLESS), _row(RIDGE)],
            "by_state": [{"state": "CO", "count": 1, "acres": 12345.6},
                         {"state": "NV", "count": 1, "acres": 500.0},
                         {"state": "CA", "count": 1, "acres": 100.0}],
            "page_limit": MAX_ROWS, "page_full": False,
        }
        self._lookup = lookup if lookup is not None else {
            "dataset": DATASET, "query": "Timber", "count": 1, "incidents": [_row(TIMBER)],
        }
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def incidents(self, state=None, min_acres=None, limit=10, uncontained=False):
        if self.raise_error:
            raise WildfireError("WFIGS layer offline")
        self.calls.append({"kind": "incidents", "state": state, "min_acres": min_acres,
                           "limit": limit, "uncontained": uncontained})
        return dict(self._active)

    def near(self, lat, lon, radius_miles=100, limit=10):
        if self.raise_error:
            raise WildfireError("WFIGS layer offline")
        self.calls.append({"kind": "near", "lat": lat, "lon": lon,
                           "radius_miles": radius_miles, "limit": limit})
        return dict(self._near)

    def summary(self, state=None):
        if self.raise_error:
            raise WildfireError("WFIGS layer offline")
        self.calls.append({"kind": "summary", "state": state})
        return dict(self._summary)

    def lookup(self, name, limit=5):
        if self.raise_error:
            raise WildfireError("WFIGS layer offline")
        self.calls.append({"kind": "lookup", "name": name, "limit": limit})
        return dict(self._lookup)


def _row(feature: dict) -> dict:
    """The reader's row shape, built by the real reader so the tests cannot drift from it."""
    return WildfireData._row(feature)


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


class QueueReader:
    """One side's inbox: an iterator of lines, plus push to add one."""

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


class HelperTests(unittest.TestCase):
    def test_escape_literal_doubles_quotes(self):
        self.assertEqual(escape_literal("US-CO"), "'US-CO'")
        self.assertEqual(escape_literal("O'Brien"), "'O''Brien'")
        self.assertEqual(escape_literal("CA' OR 1=1"), "'CA'' OR 1=1'")
        self.assertEqual(escape_literal(None), "''")

    def test_to_iso_converts_epoch_milliseconds(self):
        self.assertEqual(to_iso(1757000000000), "2025-09-04T15:33:20Z")
        self.assertEqual(to_iso("1757000000000"), "2025-09-04T15:33:20Z")
        self.assertIsNone(to_iso(None))
        self.assertIsNone(to_iso(""))
        self.assertIsNone(to_iso("not a date"))

    def test_haversine_is_a_real_great_circle_distance(self):
        self.assertEqual(haversine_miles(40.71, -74.01, 40.71, -74.01), 0.0)
        self.assertAlmostEqual(haversine_miles(*NYC, 40.8, -73.9), 8.5, places=1)
        # New York to the point in Colorado used by the fixtures.
        self.assertAlmostEqual(haversine_miles(*NYC, 40.6, -105.5), 1641.7, places=0)

    def test_an_uppercase_code_is_a_state(self):
        self.assertEqual(state_from_text("what wildfires are burning in CA right now?"), "CA")

    def test_a_lowercase_code_after_a_preposition_is_a_state(self):
        self.assertEqual(state_from_text("fires in ca"), "CA")
        self.assertEqual(state_from_text("fires across mt"), "MT")

    def test_ordinary_words_are_never_read_as_states(self):
        # "in or out" must not become Oregon, even after a preposition, and a bare
        # "me" must not become Maine.
        self.assertIsNone(state_from_text("is the fire in or out of control"))
        self.assertIsNone(state_from_text("fires for or"))
        self.assertIsNone(state_from_text("show me everything"))

    def test_a_full_state_name_is_a_state(self):
        self.assertEqual(state_from_text("wildfires in California right now"), "CA")
        self.assertEqual(state_from_text("anything burning in new mexico?"), "NM")

    def test_place_reads_the_longest_name(self):
        self.assertEqual(place_from_text("any fires near Denver?"), "denver")
        self.assertEqual(place_from_text("fires near New York"), "new york")
        self.assertIsNone(place_from_text("any fires near the coast?"))

    def test_name_reads_the_incident_after_the_the(self):
        self.assertEqual(name_from_text("tell me about the Timber fire"), "Timber")
        self.assertEqual(name_from_text("search for Ridge incidents"), "Ridge")
        self.assertEqual(name_from_text("look up Bootleg Fire"), "Bootleg")
        self.assertIsNone(name_from_text("what fires are burning right now?"))


class ReaderTests(unittest.TestCase):
    def test_check_helpers(self):
        self.assertEqual(WildfireData.check_acres("1,234.5"), 1234.5)
        with self.assertRaises(ValueError):
            WildfireData.check_acres("-1")
        self.assertEqual(WildfireData.check_positive("5"), 5)
        with self.assertRaises(ValueError):
            WildfireData.check_positive("0")
        with self.assertRaises(ValueError):
            WildfireData.check_positive("101")
        self.assertEqual(WildfireData.check_lat("40.71"), 40.71)
        with self.assertRaises(ValueError):
            WildfireData.check_lat("91")
        self.assertEqual(WildfireData.check_lon("-105.5"), -105.5)
        with self.assertRaises(ValueError):
            WildfireData.check_lon("-181")
        self.assertEqual(WildfireData.check_point("40.71, -74.01"), (40.71, -74.01))
        with self.assertRaises(ValueError):
            WildfireData.check_point("40.71")

    def test_incidents_builds_an_escaped_state_and_size_clause(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [TIMBER, RIDGE]}}, calls))
        result = data.incidents(state="ca", min_acres=1000, limit=5)
        path, params = calls[0]
        self.assertEqual(path, INCIDENTS_PATH)
        self.assertEqual(params["where"], "POOState = 'US-CA' AND IncidentSize >= 1000.0")
        self.assertEqual(params["orderByFields"], "IncidentSize DESC")
        self.assertEqual(params["resultRecordCount"], "5")
        self.assertEqual(params["returnGeometry"], "true")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["acres"], 12445.6)
        self.assertEqual(result["incidents"][0]["name"], "Timber")
        self.assertEqual(result["incidents"][0]["state"], "CO")
        self.assertEqual(result["incidents"][0]["latitude"], 40.6)
        self.assertEqual(result["incidents"][0]["discovered"], "2025-09-04T15:33:20Z")
        self.assertEqual(result["incidents"][1]["percent_contained"], 90.0)
        self.assertEqual(result["incidents"][1]["last_updated"], None)
        self.assertEqual(result["dataset"], DATASET)

    def test_incidents_without_filters_asks_for_everything(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": []}}, calls))
        result = data.incidents()
        self.assertEqual(calls[0][1]["where"], "1=1")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["acres"], 0)

    def test_uncontained_adds_the_half_contained_clause(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [TIMBER]}}, calls))
        data.incidents(uncontained=True)
        self.assertEqual(calls[0][1]["where"], "PercentContained <= 50")

    def test_a_state_cannot_break_out_of_the_where_clause(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": []}}, calls))
        data.incidents(state="CA' OR 1=1")
        # The quote is doubled, so the whole thing stays one string literal.
        self.assertEqual(calls[0][1]["where"], "POOState = 'US-CA'' OR 1=1'")

    def test_incidents_reports_the_type_in_words(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [TIMBER, HUDSON]}}, calls))
        result = data.incidents()
        self.assertEqual(result["incidents"][0]["type_name"], "wildfire")
        self.assertEqual(result["incidents"][1]["type_name"], "prescribed fire")

    def test_near_asks_for_a_statute_mile_radius_around_the_point(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [HUDSON]}}, calls))
        result = data.near(*NYC, radius_miles=50)
        path, params = calls[0]
        self.assertEqual(params["where"], "1=1")
        self.assertEqual(params["geometry"], "-74.01,40.71")  # lon,lat, as ArcGIS wants
        self.assertEqual(params["units"], "esriSRUnit_StatuteMile")
        self.assertEqual(params["distance"], "50.0")
        self.assertEqual(params["inSR"], "4326")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["origin"], {"latitude": 40.71, "longitude": -74.01})
        self.assertEqual(result["incidents"][0]["distance_miles"], 8.5)

    def test_near_sorts_nearest_first_and_drops_rows_without_a_point(self):
        payload = {"features": [TIMBER, HUDSON, GEOMETRYLESS]}
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: payload}))
        result = data.near(*NYC, radius_miles=2000, limit=5)
        self.assertEqual([row["name"] for row in result["incidents"]], ["Hudson", "Timber"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["acres"], 12357.6)

    def test_near_honours_the_cap(self):
        payload = {"features": [TIMBER, HUDSON, GEOMETRYLESS]}
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: payload}))
        result = data.near(*NYC, radius_miles=2000, limit=1)
        self.assertEqual([row["name"] for row in result["incidents"]], ["Hudson"])

    def test_summary_ranks_states_by_acres(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [TIMBER, RIDGE, HUDSON]}}, calls))
        result = data.summary()
        self.assertEqual(calls[0][1]["returnGeometry"], "false")
        self.assertEqual(calls[0][1]["resultRecordCount"], str(MAX_ROWS))
        self.assertEqual(result["scope"], "the United States")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["acres"], 12457.6)
        self.assertEqual(result["uncontained"], 1)  # only Timber is under half contained
        self.assertEqual([entry["state"] for entry in result["by_state"]], ["CO", "CA", "NY"])
        self.assertEqual(result["by_state"][0]["acres"], 12345.6)
        self.assertFalse(result["page_full"])

    def test_summary_for_a_state_scopes_to_it(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [RIDGE]}}, calls))
        result = data.summary(state="ca")
        self.assertEqual(calls[0][1]["where"], "POOState = 'US-CA'")
        self.assertEqual(result["scope"], "CA")
        self.assertEqual(result["by_state"], [])
        self.assertEqual(result["count"], 1)

    def test_summary_flags_a_full_page(self):
        features = [{"attributes": dict(TIMBER["attributes"]), "geometry": None} for _ in range(MAX_ROWS)]
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": features}}))
        result = data.summary()
        self.assertTrue(result["page_full"])
        self.assertEqual(result["page_limit"], MAX_ROWS)

    def test_lookup_wildcards_and_escapes_the_name(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [TIMBER]}}, calls))
        result = data.lookup("timber")
        self.assertEqual(calls[0][1]["where"], "UPPER(IncidentName) LIKE '%TIMBER%'")
        self.assertEqual(result["query"], "timber")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["incidents"][0]["id"], "2026-CO-RMA-000123")

    def test_lookup_rejects_an_unusable_name(self):
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": []}}))
        with self.assertRaises(ValueError):
            data.lookup("x")
        with self.assertRaises(ValueError):
            data.lookup("f" * 61)

    def test_arcgis_error_body_is_a_value_error_not_a_read(self):
        payload = {"error": {"code": 400, "message": "Invalid field name: bogus"}}
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: payload}))
        with self.assertRaises(ValueError) as caught:
            data.incidents()
        self.assertIn("Invalid field name", str(caught.exception))

    def test_a_payload_without_features_is_a_reader_error(self):
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"objectIdFieldName": "OBJECTID"}}))
        with self.assertRaises(WildfireError):
            data.incidents()

    def test_a_non_dict_payload_is_a_reader_error(self):
        data = WildfireData(fetch=lambda path, params: ["not", "a", "dict"])
        with self.assertRaises(WildfireError):
            data.incidents()

    def test_an_http_error_is_reported_without_the_url(self):
        import urllib.error

        error = urllib.error.HTTPError("https://services3.arcgis.com/x", 503, "Service Unavailable", {},
                                       mock.Mock(read=lambda: b"busy"))
        with mock.patch("data.request.urlopen", side_effect=error):
            data = WildfireData()
            with self.assertRaises(WildfireError) as caught:
                data.incidents()
        message = str(caught.exception)
        self.assertIn("answered 503", message)
        self.assertNotIn("services3.arcgis.com", message)

    def test_an_offline_service_is_reported_as_a_reader_error(self):
        with mock.patch("data.request.urlopen", side_effect=OSError("no route to host")):
            data = WildfireData()
            with self.assertRaises(WildfireError) as caught:
                data.incidents()
        self.assertIn("wildfire request failed", str(caught.exception))

    def test_a_query_is_cached_by_its_parameters(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": [TIMBER]}}, calls))
        data.incidents(state="co")
        data.incidents(state="co")
        data.incidents(state="ca")
        self.assertEqual(len(calls), 2)  # the repeat of CO is served from the cache

    def test_every_request_carries_the_field_list(self):
        calls = []
        data = WildfireData(fetch=fetch_from({INCIDENTS_PATH: {"features": []}}, calls))
        data.incidents()
        self.assertEqual(calls[0][1]["outFields"].split(","), list(FIELDS))
        self.assertEqual(calls[0][1]["f"], "json")


class RouteTests(unittest.TestCase):
    def test_a_state_question_lists_active_fires(self):
        skill, params = route("what wildfires are burning in California right now?")
        self.assertEqual(skill, "wildfire-active")
        self.assertEqual(params["state"], "CA")

    def test_a_size_filter_is_read_as_acres(self):
        skill, params = route("any fires over 5,000 acres in Idaho?")
        self.assertEqual(skill, "wildfire-active")
        self.assertEqual(params["min_acres"], 5000.0)

    def test_a_size_phrase_is_never_read_as_a_point(self):
        # "5,000" looks like "lat,lon" to a naive regex; it is a size, and the state still wins.
        skill, params = route("any fires over 5,000 acres in Idaho?")
        self.assertEqual(skill, "wildfire-active")
        self.assertEqual(params["state"], "ID")
        self.assertNotIn("point", params)

    def test_a_point_with_a_zero_longitude_is_still_a_point(self):
        # Greenwich is at -0.13; a leading zero there is a coordinate, not a thousands group.
        skill, params = route("any fires near 51.51,-0.13?")
        self.assertEqual(skill, "wildfire-near")
        self.assertEqual(params["point"], "51.51,-0.13")

    def test_a_thousands_separated_size_is_never_a_point(self):
        skill, params = route("anything burning near 12,345 acres?")
        self.assertNotIn("point", params)
        self.assertEqual(params["min_acres"], 12345.0)
        self.assertEqual(skill, "wildfire-active")

    def test_a_uncontained_question_sets_the_flag(self):
        skill, params = route("show me the uncontained fires in Montana")
        self.assertEqual(skill, "wildfire-active")
        self.assertTrue(params["uncontained"])
        self.assertEqual(params["state"], "MT")

    def test_near_with_a_radius_and_a_city(self):
        skill, params = route("any fires within 150 miles of Denver?")
        self.assertEqual(skill, "wildfire-near")
        self.assertEqual(params["place"], "denver")
        self.assertEqual(params["radius_miles"], 150.0)

    def test_near_with_a_point(self):
        skill, params = route("anything burning near 39.74,-104.99?")
        self.assertEqual(skill, "wildfire-near")
        self.assertEqual(params["point"], "39.74,-104.99")
        self.assertEqual(params["radius_miles"], DEFAULT_RADIUS_MILES)

    def test_near_by_observation_word(self):
        self.assertEqual(route("is there a fire close by?")[0], "wildfire-near")

    def test_an_unknown_place_is_carried_through_for_the_refusal(self):
        skill, params = route("any fires near Gotham?")
        self.assertEqual(skill, "wildfire-near")
        self.assertEqual(params["place"], "gotham")

    def test_the_word_after_near_me_is_not_a_place(self):
        skill, params = route("is anything burning near me right now?")
        self.assertEqual(skill, "wildfire-near")
        self.assertNotIn("place", params)

    def test_a_summary_question(self):
        skill, params = route("how much fire is burning in the country right now?")
        self.assertEqual(skill, "wildfire-summary")
        self.assertEqual(params, {})
        state, _ = route("how many acres are burning in Oregon?")
        self.assertEqual(state, "wildfire-summary")

    def test_a_lookup_question(self):
        skill, params = route("tell me about the Timber fire")
        self.assertEqual(skill, "wildfire-lookup")
        self.assertEqual(params["name"], "Timber")

    def test_help_and_empty(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_an_unrecognised_question_defaults_to_active_fires(self):
        skill, params = route("what is burning?")
        self.assertEqual(skill, "wildfire-active")
        self.assertEqual(params, {})


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = WildfireAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_an_active_answer_cites_the_layer_and_the_acreage(self):
        result, client = self.turn("what wildfires are burning in Colorado?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Timber", result["text"])
        self.assertIn("12,346 acres", result["text"])
        self.assertIn("35% contained", result["text"])
        self.assertIn(DATASET, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "wildfire-active")
        self.assertEqual(tools[0]["kind"], "fetch")
        self.assertEqual(len(client.permission_requests), 1)

    def test_a_near_answer_lists_real_distances_and_says_what_distance_is_not(self):
        data = FakeData()
        result, _ = self.turn("any fires within 100 miles of New York?", data=data)
        self.assertIn("8.5 miles away", result["text"])
        self.assertIn("great-circle miles", result["text"])
        self.assertIn("not an evacuation notice", result["text"])
        self.assertEqual(data.calls[-1]["kind"], "near")
        self.assertEqual(data.calls[-1]["radius_miles"], 100.0)

    def test_a_summary_answer_gives_totals_and_a_state_ranking(self):
        result, _ = self.turn("how much fire is burning in the country right now?")
        self.assertIn("3 active wildfire(s)", result["text"])
        self.assertIn("12,958 acres", result["text"])
        self.assertIn("Still under half contained: 2", result["text"])
        self.assertIn("CO 12,346 acres (1)", result["text"])

    def test_a_lookup_answer_adds_cause_type_and_location(self):
        result, _ = self.turn("tell me about the Timber fire")
        self.assertIn("Timber", result["text"])
        self.assertIn("cause: Human", result["text"])
        self.assertIn("type: wildfire", result["text"])
        self.assertIn("location: 40.6,-105.5", result["text"])
        self.assertIn("2026-CO-RMA-000123", result["text"])

    def test_an_unknown_place_is_refused_without_reading_the_layer(self):
        data = FakeData()
        result, _ = self.turn("any fires near Gotham?", data=data)
        self.assertIn("I do not know the place 'gotham'", result["text"])
        self.assertIn("will not guess coordinates", result["text"])
        self.assertEqual(data.calls, [])

    def test_a_quiet_layer_is_stated_honestly(self):
        empty = {"dataset": DATASET, "where": "POOState = 'US-WY'", "count": 0, "acres": 0,
                 "incidents": []}
        result, _ = self.turn("what fires are burning in Wyoming?", data=FakeData(active=empty))
        self.assertIn("No active wildfire matches", result["text"])
        self.assertIn("right now", result["text"])

    def test_a_lookup_with_no_name_asks_for_one(self):
        data = FakeData()
        result, _ = self.turn("find the fire", data=data)
        self.assertIn("Which incident?", result["text"])
        self.assertIn("as the agencies spell it", result["text"])
        # Nothing is read from the layer until it knows what to look for.
        self.assertEqual(data.calls, [])

    def test_a_near_question_with_no_place_asks_where(self):
        data = FakeData()
        result, _ = self.turn("is anything burning close by?", data=data)
        self.assertIn("Tell me where to look", result["text"])
        self.assertEqual(data.calls, [])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("wildfire", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("what wildfires are burning in Colorado?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_layer_failure_is_reported_in_the_tool_call(self):
        result, _ = self.turn("what wildfires are burning in Colorado?", data=FakeData(raise_error=True))
        self.assertIn("could not read the wildfire layer", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what wildfires are burning in Colorado?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)


if __name__ == "__main__":
    unittest.main()
