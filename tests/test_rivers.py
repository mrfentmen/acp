"""Tests for the rivers ACP agent: the USGS reader, routing, skills, permissions.

    python3 tests/test_rivers.py

No network: the reader runs against injected payloads and the agent against a FakeData.
"""

from __future__ import annotations

import io
import json
import queue
import sys
import threading
import unittest
from pathlib import Path
from urllib import error as urlerror

REPO_ROOT = Path(__file__).resolve().parents[1]
RIVERS_DIR = REPO_ROOT / "agents" / "rivers"
for path in (str(REPO_ROOT), str(RIVERS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    RiversAgent,
    count_from_text,
    hours_from_text,
    place_from_text,
    point_from_text,
    radius_from_text,
    route,
    site_from_text,
    unknown_place_from_text,
    unknown_places_from_text,
)
from data import (  # noqa: E402
    DATASET_IV,
    DATASET_SITE,
    DEFAULT_HOURS,
    DEFAULT_RADIUS_MILES,
    DEFAULT_SITES,
    IV_PATH,
    LATEST_HOURS,
    MAX_HOURS,
    MAX_RADIUS_MILES,
    MAX_SITES,
    PROBE_LIMIT,
    RETRY_ATTEMPTS,
    SITE_PATH,
    RiversBusy,
    RiversData,
    RiversError,
    distance_miles,
    parse_rdb,
    parse_sites,
    rdb_number,
)

#: Real shapes, copied from the live USGS services on 2026-09-24 04:30 UTC.
SITE_RDB = """#\n# US Geological Survey\n# retrieved: 2026-09-24 00:33:13 -04:00\t(sdas01)\n#\nagency_cd\tsite_no\tstation_nm\tsite_tp_cd\tdec_lat_va\tdec_long_va\thuc_cd\n5s\t15s\t50s\t7s\t16s\t16s\t16s\nUSGS\t06719505\tCLEAR CREEK AT GOLDEN, CO\tST\t39.753056\t-105.234722\t10190004\nUSGS\t06710247\tSOUTH PLATTE RIVER AT ENGLEWOOD, CO\tST\t39.651111\t-105.008889\t10190002\nUSGS\t06710385\tBEAR CREEK AT SHERIDAN, CO\tST\t\t-105.022222\t10190002\n"""

#: One request for two sites: discharge and gage height, one no-data series, one site silent.
IV_BODY = {
    "name": "ns1:timeSeriesResponseType",
    "value": {
        "queryInfo": {
            "queryURL": "http://waterservices.usgs.gov/nwis/iv/",
            "note": [
                {"value": "[ALL:06719505,06710247]", "title": "filter:sites"},
                {"value": "2026-09-24T04:33:13.756Z", "title": "requestDT"},
            ],
        },
        "timeSeries": [
            {
                "name": "USGS:06719505:00060:00000",
                "sourceInfo": {
                    "siteName": "CLEAR CREEK AT GOLDEN, CO",
                    "siteCode": [{"value": "06719505", "network": "NWIS", "agencyCode": "USGS"}],
                    "geoLocation": {"geogLocation": {"latitude": 39.753056, "longitude": -105.234722}},
                },
                "variable": {"variableCode": [{"value": "00060"}], "unit": {"unitCode": "ft3/s"}},
                "values": [{
                    "method": [{"methodID": 210989}],
                    "qualifier": [{"qualifierCode": "P",
                                   "qualifierDescription": "Provisional data subject to revision."}],
                    "value": [
                        {"value": "58.0", "qualifiers": ["P"], "dateTime": "2026-09-23T19:45:00.000-06:00"},
                        {"value": "61.6", "qualifiers": ["P"], "dateTime": "2026-09-23T20:45:00.000-06:00"},
                    ],
                }],
            },
            {
                "name": "USGS:06719505:00065:00000",
                "sourceInfo": {
                    "siteName": "CLEAR CREEK AT GOLDEN, CO",
                    "siteCode": [{"value": "06719505"}],
                    "geoLocation": {"geogLocation": {"latitude": 39.753056, "longitude": -105.234722}},
                },
                "variable": {"variableCode": [{"value": "00065"}], "unit": {"unitCode": "ft"}},
                "values": [{
                    "qualifier": [{"qualifierCode": "P",
                                   "qualifierDescription": "Provisional data subject to revision."}],
                    "value": [{"value": "3.74", "qualifiers": ["P"], "dateTime": "2026-09-23T20:45:00.000-06:00"}],
                }],
            },
            {
                "name": "USGS:06710247:00060:00000",
                "sourceInfo": {
                    "siteName": "SOUTH PLATTE RIVER AT ENGLEWOOD, CO",
                    "siteCode": [{"value": "06710247"}],
                    "geoLocation": {"geogLocation": {"latitude": 39.651111, "longitude": -105.008889}},
                },
                "variable": {"variableCode": [{"value": "00060"}], "unit": {"unitCode": "ft3/s"}},
                "values": [{
                    "qualifier": [{"qualifierCode": "e", "qualifierDescription": "Estimated."}],
                    # Every value is the no-data code: this is not a reading of zero.
                    "value": [{"value": "-999999", "qualifiers": ["e"], "dateTime": "2026-09-23T20:45:00.000-06:00"}],
                }],
            },
            {
                "name": "USGS:06719505:00010:00000",
                "sourceInfo": {
                    "siteName": "CLEAR CREEK AT GOLDEN, CO",
                    "siteCode": [{"value": "06719505"}],
                    "geoLocation": {"geogLocation": {"latitude": 39.753056, "longitude": -105.234722}},
                },
                "variable": {"variableCode": [{"value": "00010"}], "unit": {"unitCode": "degC"}},
                "values": [{
                    "qualifier": [{"qualifierCode": "P",
                                   "qualifierDescription": "Provisional data subject to revision."}],
                    "value": [{"value": "14.2", "qualifiers": ["P"], "dateTime": "2026-09-23T20:45:00.000-06:00"}],
                }],
            },
        ],
    },
}


def fetch_site_file():
    return SITE_RDB


def iv_payload() -> str:
    """A fresh copy of the values response, serialised the way the service sends it."""
    return json.dumps(json.loads(json.dumps(IV_BODY)))


def fetch_by_path(payloads, calls=None):
    """A RiversData `fetch` that dispatches on path, so data.py runs for real."""

    def fetch(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        payload = payloads[path]
        return payload() if callable(payload) else payload

    return fetch


def series_of(read: dict, key: str) -> dict:
    return read["series"][key]


class FakeData(RiversData):
    """Same interface as RiversData, no network."""

    def __init__(self, site=None, near=None, trend=None, raise_error: bool = False,
                 raise_value_error: bool = False):
        self._site = site if site is not None else {
            "found": True, "known": True, "site_number": "06719505",
            "site_name": "CLEAR CREEK AT GOLDEN, CO", "latitude": 39.753056, "longitude": -105.234722,
            "read_at": "2026-09-24T04:33:13.756Z", "hours": LATEST_HOURS,
            "series": {
                "discharge": {"parameter_code": "00060", "label": "Discharge", "unit_code": "ft3/s",
                              "latest": {"time": "2026-09-23T20:45:00.000-06:00", "value": 61.6},
                              "earliest": {"time": "2026-09-23T19:45:00.000-06:00", "value": 58.0},
                              "points": [{"time": "2026-09-23T19:45:00.000-06:00", "value": 58.0},
                                         {"time": "2026-09-23T20:45:00.000-06:00", "value": 61.6}],
                              "qualifiers": [{"code": "P",
                                              "description": "Provisional data subject to revision."}]},
                "gage_height": {"parameter_code": "00065", "label": "Gage height", "unit_code": "ft",
                                "latest": {"time": "2026-09-23T20:45:00.000-06:00", "value": 3.74},
                                "earliest": {"time": "2026-09-23T19:45:00.000-06:00", "value": 3.7},
                                "points": [{"time": "2026-09-23T19:45:00.000-06:00", "value": 3.7},
                                           {"time": "2026-09-23T20:45:00.000-06:00", "value": 3.74}],
                                "qualifiers": [{"code": "P",
                                                "description": "Provisional data subject to revision."}]},
            },
        }
        self._near = near if near is not None else {
            "latitude": 39.74, "longitude": -104.99, "radius_miles": 25.0, "sites_in_box": 604,
            "found": 498, "limit": 5, "checked": 20, "read_at": "2026-09-24T04:33:13.756Z",
            "candidates": [], "silent": [{"site_number": "06711770", "site_name": "DRY GULCH AT DENVER, CO",
                                          "miles": 2.7, "series": {}}],
            "readings": [
                {"site_number": "06713500", "site_name": "CHERRY CREEK AT DENVER, CO.", "miles": 0.6,
                 "latitude": 39.75, "longitude": -104.97, "series": {
                     "discharge": {"label": "Discharge", "unit_code": "ft3/s",
                                   "latest": {"time": "2026-09-23T20:45:00.000-06:00", "value": 33.1},
                                   "points": [{"time": "2026-09-23T20:45:00.000-06:00", "value": 33.1}],
                                   "qualifiers": [{"code": "P",
                                                   "description": "Provisional data subject to revision."}]},
                     "gage_height": {"label": "Gage height", "unit_code": "ft",
                                     "latest": {"time": "2026-09-23T20:45:00.000-06:00", "value": 3.53},
                                     "points": [{"time": "2026-09-23T20:45:00.000-06:00", "value": 3.53}],
                                     "qualifiers": []}}},
                {"site_number": "06711780", "site_name": "LAKEWOOD GULCH AT DENVER, CO", "miles": 2.2,
                 "latitude": 39.73, "longitude": -105.05, "series": {
                     "discharge": {"label": "Discharge", "unit_code": "ft3/s",
                                   "latest": {"time": "2026-09-23T20:45:00.000-06:00", "value": 1.76},
                                   "points": [{"time": "2026-09-23T20:45:00.000-06:00", "value": 1.76}],
                                   "qualifiers": []}}},
            ],
        }
        self._trend = trend if trend is not None else {
            "found": True, "known": True, "site_number": "06719505",
            "site_name": "CLEAR CREEK AT GOLDEN, CO", "latitude": 39.753056, "longitude": -105.234722,
            "read_at": "2026-09-24T04:33:13.756Z", "hours": 24, "parameter": "discharge",
            "label": "Discharge", "unit_code": "ft3/s", "points": 95,
            "trend": {"direction": "rising", "parameter": "discharge", "label": "Discharge",
                      "unit_code": "ft3/s", "unit_words": "cubic feet per second",
                      "first": {"time": "2026-09-22T23:00:00.000-06:00", "value": 52.3},
                      "last": {"time": "2026-09-23T21:45:00.000-06:00", "value": 62.6},
                      "change": 10.3, "percent": 19.7, "minimum": 52.3, "maximum": 63.7,
                      "readings": 95,
                      "qualifiers": [{"code": "P", "description": "Provisional data subject to revision."}]},
        }
        self.raise_error = raise_error
        self.raise_value_error = raise_value_error
        self.calls: list[dict] = []

    def read_site(self, site_number, hours=LATEST_HOURS):
        if self.raise_error:
            raise RiversError("USGS offline")
        if self.raise_value_error:
            raise ValueError("bad site")
        self.calls.append({"kind": "read_site", "site": site_number, "hours": hours})
        return dict(self._site)

    def gauges_near(self, latitude, longitude, radius_miles=DEFAULT_RADIUS_MILES,
                    limit=DEFAULT_SITES, hours=LATEST_HOURS):
        if self.raise_error:
            raise RiversError("USGS offline")
        self.calls.append({"kind": "gauges_near", "lat": latitude, "lon": longitude,
                           "radius": radius_miles, "limit": limit, "hours": hours})
        return dict(self._near)

    def trend(self, site_number, hours=DEFAULT_HOURS):
        if self.raise_error:
            raise RiversError("USGS offline")
        self.calls.append({"kind": "trend", "site": site_number, "hours": hours})
        return dict(self._trend)


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


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


class HelperTests(unittest.TestCase):
    def test_site_numbers_must_be_eight_to_fifteen_digits(self):
        self.assertEqual(RiversData.check_site_number("06719505"), "06719505")
        self.assertEqual(RiversData.check_site_number("USGS 06719505"), "06719505")
        self.assertEqual(RiversData.check_site_number("13018300"), "13018300")
        for bad in ("0671950", "1", "", "not-a-site"):
            with self.assertRaises(ValueError):
                RiversData.check_site_number(bad)

    def test_a_point_keeps_zero_longitudes_and_rejects_bad_ones(self):
        self.assertEqual(RiversData.check_point("39.75,-0.13"), (39.75, -0.13))
        self.assertEqual(RiversData.check_point(" 39.75 , -104.99 "), (39.75, -104.99))
        for bad in ("39.75", "39.75,-104.99,1"):
            with self.assertRaises(ValueError):
                RiversData.check_point(bad)
        with self.assertRaises(ValueError):
            RiversData.check_lat("91")
        with self.assertRaises(ValueError):
            RiversData.check_lon("-181")

    def test_radius_hours_and_count_are_bounded(self):
        self.assertEqual(RiversData.check_radius(str(DEFAULT_RADIUS_MILES)), DEFAULT_RADIUS_MILES)
        self.assertEqual(RiversData.check_hours(str(MAX_HOURS)), MAX_HOURS)
        self.assertEqual(RiversData.check_limit(str(MAX_SITES)), MAX_SITES)
        for bad in ("0", str(MAX_RADIUS_MILES + 1), "far"):
            with self.assertRaises(ValueError):
                RiversData.check_radius(bad)
        for bad in ("0", str(MAX_HOURS + 1), "soon"):
            with self.assertRaises(ValueError):
                RiversData.check_hours(bad)
        for bad in ("0", str(MAX_SITES + 1), "many"):
            with self.assertRaises(ValueError):
                RiversData.check_limit(bad)

    def test_city_lookup_is_case_insensitive_and_never_guesses(self):
        self.assertEqual(RiversData.city("Golden")[2], "Golden")
        self.assertEqual(RiversData.city("denver")[1], -104.99)
        self.assertEqual(RiversData.city("Fort Collins")[2], "Fort Collins")
        self.assertIsNone(RiversData.city("Atlantis"))
        self.assertIsNone(RiversData.city(""))

    def test_distance_is_great_circle_miles(self):
        self.assertEqual(distance_miles(39.74, -104.99, 39.74, -104.99), 0.0)
        miles = distance_miles(39.74, -104.99, 39.76, -105.22)  # Denver -> Golden
        self.assertGreater(miles, 12)
        self.assertLess(miles, 14)

    def test_rdb_skips_comments_and_the_width_line(self):
        rows = parse_rdb(SITE_RDB)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["site_no"], "06719505")
        self.assertEqual(rows[0]["station_nm"], "CLEAR CREEK AT GOLDEN, CO")
        self.assertEqual(rows[2]["dec_lat_va"], "")

    def test_a_short_row_keeps_the_columns_it_has(self):
        rows = parse_rdb("agency_cd\tsite_no\n5s\t15s\nUSGS\t06719505\n")
        self.assertEqual(rows, [{"agency_cd": "USGS", "site_no": "06719505"}])

    def test_no_value_codes_are_none_not_zero(self):
        self.assertIsNone(rdb_number("-999999"))
        self.assertIsNone(rdb_number("-999999.0"))
        self.assertIsNone(rdb_number(""))
        self.assertIsNone(rdb_number(None))
        self.assertEqual(rdb_number("61.6"), 61.6)

    def test_sites_without_coordinates_are_dropped(self):
        sites = parse_sites(SITE_RDB)
        self.assertEqual([site["site_number"] for site in sites], ["06719505", "06710247"])
        self.assertEqual(sites[1]["site_name"], "SOUTH PLATTE RIVER AT ENGLEWOOD, CO")

    def test_place_reads_the_longest_name(self):
        self.assertEqual(place_from_text("gauges near Denver?"), "denver")
        self.assertEqual(place_from_text("river level in Fort Collins"), "fort collins")
        self.assertIsNone(place_from_text("how high is the river?"))

    def test_point_reader_keeps_zero_longitudes(self):
        self.assertEqual(point_from_text("at 51.51,-0.13"), "51.51,-0.13")
        self.assertEqual(point_from_text("at 39.74,-104.99"), "39.74,-104.99")
        self.assertIsNone(point_from_text("a town of 1,000 people"))

    def test_site_reader_ignores_dates_and_reads_usgs_numbers(self):
        self.assertEqual(site_from_text("what is 06719505 reading?"), "06719505")
        self.assertEqual(site_from_text("site USGS 13018300 please"), "13018300")
        self.assertIsNone(site_from_text("how much rain on 20260924?"))
        self.assertIsNone(site_from_text("no number here"))

    def test_radius_hours_and_count_are_read_from_prose(self):
        self.assertEqual(radius_from_text("within 50 miles of Denver"), 50.0)
        self.assertEqual(radius_from_text("within 5 mile"), 5.0)
        self.assertIsNone(radius_from_text("near Denver"))
        self.assertEqual(hours_from_text("over the last 48 hours"), 48)
        self.assertEqual(hours_from_text("in 6 h"), 6)
        self.assertIsNone(hours_from_text("recently"))
        self.assertEqual(count_from_text("the nearest 3 gauges"), 3)
        self.assertEqual(count_from_text("show me 10"), 10)
        self.assertIsNone(count_from_text("which gauges are near here"))

    def test_unknown_places_exclude_places_this_agent_knows(self):
        self.assertEqual(unknown_places_from_text("gauges near Atlantis?"), ["Atlantis"])
        self.assertEqual(unknown_place_from_text("gauges near Atlantis?"), "Atlantis")
        self.assertIsNone(unknown_place_from_text("gauges near Denver?"))


class ReaderTests(unittest.TestCase):
    def read_site_with(self, payloads=None, calls=None, site="06719505", hours=LATEST_HOURS, sleep=None):
        payloads = payloads or {SITE_PATH: fetch_site_file, IV_PATH: iv_payload}
        return RiversData(fetch=fetch_by_path(payloads, calls), sleep=sleep).read_site(site, hours=hours)

    def test_a_single_site_read_is_one_request_and_carries_the_service_timestamps(self):
        calls: list = []
        read = self.read_site_with(calls=calls)
        self.assertTrue(read["found"])
        self.assertTrue(read["known"])
        self.assertEqual(read["site_name"], "CLEAR CREEK AT GOLDEN, CO")
        self.assertEqual(read["read_at"], "2026-09-24T04:33:13.756Z")
        self.assertEqual([path for path, _ in calls], [IV_PATH])
        params = calls[0][1]
        self.assertEqual(params["sites"], "06719505")
        self.assertEqual(params["parameterCd"], "00060,00065,00010")
        self.assertEqual(params["period"], f"PT{LATEST_HOURS}H")

    def test_readings_keep_the_unit_and_usgs_own_qualifier_words(self):
        read = self.read_site_with()
        discharge = series_of(read, "discharge")
        self.assertEqual(discharge["unit_code"], "ft3/s")
        self.assertEqual(discharge["latest"]["value"], 61.6)
        self.assertEqual(discharge["earliest"]["value"], 58.0)
        self.assertEqual(discharge["qualifiers"],
                         [{"code": "P", "description": "Provisional data subject to revision."}])
        self.assertEqual(series_of(read, "gage_height")["latest"]["value"], 3.74)
        self.assertEqual(series_of(read, "water_temperature")["latest"]["value"], 14.2)

    def test_a_series_that_is_all_no_value_is_not_a_reading_of_zero(self):
        read = self.read_site_with()
        self.assertNotIn("temperature", read["series"])
        # 06710247 published only the no-data code, so it is absent rather than zero.
        self.assertEqual(read["site_number"], "06719505")

    def test_a_site_with_no_values_falls_back_to_the_site_file_for_its_name(self):
        empty = {"value": {"queryInfo": {"note": [{"value": "2026-09-24T04:33:13.756Z",
                                                  "title": "requestDT"}]}, "timeSeries": []}}
        calls: list = []
        read = self.read_site_with(
            payloads={SITE_PATH: fetch_site_file, IV_PATH: lambda: json.dumps(empty)},
            calls=calls, site="06719505")
        self.assertFalse(read["found"])
        self.assertTrue(read["known"])
        self.assertEqual(read["site_name"], "CLEAR CREEK AT GOLDEN, CO")
        self.assertEqual([path for path, _ in calls], [IV_PATH, SITE_PATH])

    def test_a_site_usgs_does_not_know_is_reported_as_unknown(self):
        empty = {"value": {"timeSeries": []}}
        read = self.read_site_with(
            payloads={SITE_PATH: lambda: SITE_RDB, IV_PATH: lambda: json.dumps(empty)},
            site="12345678")
        self.assertFalse(read["found"])
        self.assertFalse(read["known"])
        self.assertEqual(read["site_name"], "")

    def test_the_site_file_is_asked_only_for_the_numbers_it_needs(self):
        calls: list = []
        RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file}, calls))._sites_by_number(["06719505"])
        self.assertEqual(calls[0][0], SITE_PATH)
        self.assertEqual(calls[0][1]["sites"], "06719505")

    def test_nearest_sites_filters_by_radius_and_sorts_by_distance(self):
        calls: list = []
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file}, calls))
        near = data.nearest_sites(39.74, -104.99, radius_miles=MAX_RADIUS_MILES)
        self.assertEqual([site["site_number"] for site in near["sites"]], ["06710247", "06719505"])
        self.assertLess(near["sites"][0]["miles"], near["sites"][1]["miles"])
        params = calls[0][1]
        self.assertEqual(params["siteType"], "ST,ST-CA,ST-DCH,ST-TS")
        self.assertIn("bBox", params)
        # The filters that 503 or time out on a bbox must not be sent.
        self.assertNotIn("siteStatus", params)
        self.assertNotIn("hasDataTypeCd", params)

    def test_nearest_sites_keeps_only_what_is_inside_the_radius(self):
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file}))
        near = data.nearest_sites(39.74, -104.99, radius_miles=1.0)
        self.assertEqual(near["sites"], [])
        self.assertGreater(near["sites_in_box"], 0)  # the box held sites; none were close enough

    def test_gauges_near_probes_the_nearest_candidates_in_one_request(self):
        calls: list = []
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file, IV_PATH: iv_payload}, calls))
        near = data.gauges_near(39.74, -104.99, radius_miles=MAX_RADIUS_MILES, limit=2)
        self.assertEqual([path for path, _ in calls], [SITE_PATH, IV_PATH])
        self.assertEqual(calls[1][1]["sites"], "06710247,06719505")
        self.assertEqual(near["checked"], 2)
        # 06710247 was asked and answered with the no-data code, so it counts as silent
        # rather than being shown at zero.
        self.assertEqual([site["site_number"] for site in near["silent"]], ["06710247"])
        self.assertEqual([gauge["site_number"] for gauge in near["readings"]], ["06719505"])
        self.assertEqual(near["readings"][0]["miles"], 13.0)  # Denver to Clear Creek at Golden
        self.assertEqual(near["readings"][0]["series"]["discharge"]["latest"]["value"], 61.6)

    def test_gauges_near_keeps_probing_until_it_has_enough_reporting_gauges(self):
        """A silent gauge must not hide a reporting one behind it."""
        empty = lambda: json.dumps({"value": {"timeSeries": []}})  # noqa: E731
        calls: list = []
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file, IV_PATH: empty}, calls))
        near = data.gauges_near(39.74, -104.99, radius_miles=MAX_RADIUS_MILES, limit=1)
        self.assertEqual(near["readings"], [])
        self.assertEqual(len(near["silent"]), 2)  # every candidate was asked and none reported
        self.assertEqual(near["checked"], 2)
        self.assertEqual(len(calls), 2)  # one site file read, one values request, nothing more

    def test_gauges_near_stops_when_there_is_nothing_in_the_radius(self):
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file}))
        near = data.gauges_near(39.74, -104.99, radius_miles=1.0)
        self.assertEqual(near["readings"], [])
        self.assertEqual(near["checked"], 0)

    def test_trend_reports_direction_change_and_range(self):
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file, IV_PATH: iv_payload}))
        read = data.trend("06719505", hours=24)
        trend = read["trend"]
        self.assertEqual(trend["direction"], "rising")
        self.assertEqual(trend["change"], 3.6)
        self.assertEqual(trend["percent"], 6.2)
        self.assertEqual(trend["minimum"], 58.0)
        self.assertEqual(trend["maximum"], 61.6)
        self.assertEqual(trend["readings"], 2)
        self.assertEqual(trend["unit_code"], "ft3/s")

    def test_trend_says_falling_and_flat(self):
        falling = json.loads(iv_payload())
        values = falling["value"]["timeSeries"][0]["values"][0]["value"]
        values[0]["value"], values[1]["value"] = "61.6", "50.0"
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file,
                                               IV_PATH: lambda: json.dumps(falling)}))
        self.assertEqual(data.trend("06719505")["trend"]["direction"], "falling")
        flat = json.loads(iv_payload())
        values = flat["value"]["timeSeries"][0]["values"][0]["value"]
        values[0]["value"], values[1]["value"] = "61.6", "61.62"
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file,
                                               IV_PATH: lambda: json.dumps(flat)}))
        self.assertEqual(data.trend("06719505")["trend"]["direction"], "flat")

    def test_trend_with_one_reading_is_no_trend_rather_than_a_guess(self):
        single = json.loads(iv_payload())
        series = single["value"]["timeSeries"][0]
        series["values"][0]["value"] = series["values"][0]["value"][1:]
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file,
                                               IV_PATH: lambda: json.dumps(single)}))
        read = data.trend("06719505")
        self.assertIsNone(read["trend"])
        self.assertEqual(read["points"], 1)

    def test_trend_refuses_a_gauge_with_neither_discharge_nor_stage(self):
        temperature_only = json.loads(iv_payload())
        series = temperature_only["value"]["timeSeries"]
        temperature_only["value"]["timeSeries"] = [s for s in series
                                                   if s["variable"]["variableCode"][0]["value"] == "00010"]
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file,
                                               IV_PATH: lambda: json.dumps(temperature_only)}))
        read = data.trend("06719505")
        self.assertFalse(read["found"])
        self.assertIn("no discharge or gage height", read["reason"])

    def test_a_busy_service_is_retried_then_succeeds(self):
        attempts = {"n": 0}
        slept: list = []

        def flaky(path, params):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RiversBusy("USGS water services answered HTTP 503 for /nwis/iv/")
            return iv_payload()

        data = RiversData(fetch=flaky, sleep=slept.append)
        read = data.read_site("06719505")
        self.assertTrue(read["found"])
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(slept), 2)  # it waited between tries, and not before the first

    def test_a_busy_service_that_stays_busy_gives_up_with_the_reason(self):
        attempts = {"n": 0}

        def always_busy(path, params):
            attempts["n"] += 1
            raise RiversBusy("USGS water services answered HTTP 503 for /nwis/iv/")

        data = RiversData(fetch=always_busy, sleep=lambda _: None)
        with self.assertRaises(RiversBusy) as caught:
            data.read_site("06719505")
        self.assertEqual(attempts["n"], RETRY_ATTEMPTS)
        self.assertIn("HTTP 503", str(caught.exception))

    def test_a_rejected_request_is_not_retried(self):
        attempts = {"n": 0}

        def rejected(path, params):
            attempts["n"] += 1
            raise RiversError("USGS water services answered HTTP 400 for /nwis/iv/")

        data = RiversData(fetch=rejected, sleep=lambda _: None)
        with self.assertRaises(RiversError):
            data.read_site("06719505")
        self.assertEqual(attempts["n"], 1)

    def test_an_html_error_report_becomes_a_readable_reason(self):
        body = b'<!DOCTYPE html><html><head><title>Error report</title></head><body>503</body></html>'
        exc = urlerror.HTTPError("https://waterservices.usgs.gov/nwis/iv/", 503, "Busy", None,
                                 io.BytesIO(body))
        message = RiversData._http_error(exc, IV_PATH)
        self.assertIn("HTTP 503", message)
        self.assertIn("busy or unavailable", message)
        self.assertNotIn("html", message)

    def test_a_plain_text_error_keeps_the_service_words(self):
        exc = urlerror.HTTPError("https://waterservices.usgs.gov/nwis/iv/", 400, "Bad Request", None,
                                 io.BytesIO(b"siteType is invalid"))
        message = RiversData._http_error(exc, IV_PATH)
        self.assertIn("HTTP 400", message)
        self.assertIn("siteType is invalid", message)

    def test_a_response_that_is_not_json_is_an_error_not_an_empty_answer(self):
        data = RiversData(fetch=fetch_by_path({IV_PATH: lambda: "<html>nope</html>"}))
        with self.assertRaises(RiversError):
            data.read_site("06719505")

    def test_an_unexpected_payload_type_is_an_error(self):
        data = RiversData(fetch=lambda path, params: {"not": "text"})
        with self.assertRaises(RiversError):
            data.read_site("06719505")

    def test_a_values_response_with_no_time_series_key_is_empty_not_a_crash(self):
        data = RiversData(fetch=fetch_by_path({SITE_PATH: fetch_site_file, IV_PATH: lambda: "{}"}))
        read = data.read_site("06719505")
        self.assertFalse(read["found"])
        self.assertTrue(read["known"])

    def test_the_reader_uses_its_own_base_url_and_user_agent(self):
        data = RiversData(base_url="https://example.test", user_agent="test-agent/1.0")
        self.assertEqual(data.base_url, "https://example.test")
        self.assertEqual(data.user_agent, "test-agent/1.0")


class RouteTests(unittest.TestCase):
    def test_a_site_number_is_a_single_reading(self):
        skill, params = route("what is 06719505 reading?")
        self.assertEqual(skill, "river-now")
        self.assertEqual(params["site"], "06719505")

    def test_a_place_without_near_words_is_the_nearest_reading(self):
        skill, params = route("how high is the river in Golden?")
        self.assertEqual(skill, "river-now")
        self.assertEqual(params["place"], "golden")

    def test_gauges_near_a_place_is_the_list_skill(self):
        skill, params = route("which gauges are near Denver?")
        self.assertEqual(skill, "river-near")
        self.assertEqual(params["place"], "denver")

    def test_near_a_point_is_the_list_skill(self):
        skill, params = route("any gauges within 30 miles of 39.74,-104.99?")
        self.assertEqual(skill, "river-near")
        self.assertEqual(params["point"], "39.74,-104.99")
        self.assertEqual(params["radius"], 30.0)

    def test_near_a_site_number_stays_a_reading(self):
        skill, params = route("what is 06719505 reading near Golden?")
        self.assertEqual(skill, "river-now")
        self.assertEqual(params["site"], "06719505")

    def test_movement_words_make_it_a_trend(self):
        skill, params = route("has 06719505 been rising in the last 48 hours?")
        self.assertEqual(skill, "river-rise")
        self.assertEqual(params["site"], "06719505")
        self.assertEqual(params["hours"], 48)

    def test_a_trend_about_a_place_keeps_the_place(self):
        skill, params = route("is the river falling in Golden?")
        self.assertEqual(skill, "river-rise")
        self.assertEqual(params["place"], "golden")
        self.assertEqual(params["hours"], DEFAULT_HOURS)

    def test_hours_and_count_are_carried_into_the_params(self):
        skill, params = route("which gauges are within 40 miles of Denver and show 3?")
        self.assertEqual(skill, "river-near")
        self.assertEqual(params["radius"], 40.0)
        self.assertEqual(params["count"], 3)

    def test_an_unknown_place_is_carried_through_for_the_refusal(self):
        skill, params = route("which gauges are near Atlantis?")
        self.assertEqual(skill, "river-near")
        self.assertEqual(params["place"], "Atlantis")

    def test_help_and_empty(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = RiversAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def tools(self, result):
        return [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]

    def test_a_site_reading_names_the_gauge_its_numbers_and_its_qualifier(self):
        result, client = self.turn("what is 06719505 reading right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("CLEAR CREEK AT GOLDEN, CO (USGS 06719505)", result["text"])
        self.assertIn("Discharge: 61.6 cubic feet per second", result["text"])
        self.assertIn("Gage height: 3.74 feet", result["text"])
        self.assertIn("2026-09-23T20:45:00.000-06:00", result["text"])
        self.assertIn("Provisional data subject to revision.", result["text"])
        self.assertIn(DATASET_IV, result["text"])
        self.assertEqual(len(client.permission_requests), 1)
        self.assertEqual(self.tools(result)[0]["name"], "river-now")
        self.assertEqual(self.tools(result)[0]["kind"], "fetch")

    def test_a_place_question_names_the_nearest_gauge_and_the_distance(self):
        result, _ = self.turn("how high is the river in Golden?")
        self.assertIn("Nearest active USGS stream gauge to Golden: CHERRY CREEK AT DENVER, CO.", result["text"])
        self.assertIn("0.6 mi away", result["text"])
        self.assertIn("Discharge: 33.1 cubic feet per second", result["text"])
        self.assertIn("A gauge measures one spot on one river", result["text"])

    def test_the_near_list_gives_every_gauge_a_distance_and_a_reading(self):
        result, _ = self.turn("which gauges are near Denver?")
        self.assertIn(f"Active USGS stream gauges within {DEFAULT_RADIUS_MILES:g} miles of Denver", result["text"])
        self.assertIn("CHERRY CREEK AT DENVER, CO. (06713500) — 0.6 mi — Discharge: 33.1", result["text"])
        self.assertIn("LAKEWOOD GULCH AT DENVER, CO (06711780) — 2.2 mi", result["text"])
        self.assertIn("site file lists 498 stream site(s)", result["text"])
        self.assertIn("1 of those published nothing", result["text"])
        self.assertIn("straight-line miles computed here", result["text"])
        self.assertIn(DATASET_SITE, result["text"])

    def test_a_rise_answer_states_the_change_the_window_and_the_range(self):
        result, _ = self.turn("has 06719505 been rising in the last 24 hours?")
        self.assertIn("discharge rose from 52.3 cubic feet per second to 62.6 cubic feet per second "
                      "over the last 24 hours (+10.3 ft3/s, +19.7%)", result["text"])
        self.assertIn("(95 readings in the window)", result["text"])
        self.assertIn("Lowest 52.3 cubic feet per second, highest 63.7 cubic feet per second.", result["text"])
        self.assertIn("not a USGS forecast and not a flood warning", result["text"])

    def test_a_rise_with_no_trend_says_so(self):
        data = FakeData(trend={"found": True, "known": True, "site_number": "06719505",
                               "site_name": "CLEAR CREEK AT GOLDEN, CO", "label": "Discharge",
                               "points": 1, "trend": None, "read_at": "2026-09-24T04:33:13.756Z"})
        result, _ = self.turn("has 06719505 been rising?", data=data)
        self.assertIn("published only 1 reading(s)", result["text"])
        self.assertIn("not enough to say whether it is rising or falling", result["text"])

    def test_a_site_with_no_values_is_reported_not_filled_in(self):
        data = FakeData(site={"found": False, "known": True, "site_number": "99999999",
                              "site_name": "KY STANDALONE CALIBRATIONS", "series": {},
                              "read_at": "2026-09-24T04:33:13.756Z", "hours": LATEST_HOURS})
        result, _ = self.turn("what is 99999999 reading?", data=data)
        self.assertIn("USGS lists KY STANDALONE CALIBRATIONS (99999999)", result["text"])
        self.assertIn("published no real-time discharge, gage height or water temperature", result["text"])

    def test_a_site_usgs_does_not_know_says_exactly_that(self):
        data = FakeData(site={"found": False, "known": False, "site_number": "12345678",
                              "site_name": "", "series": {}, "read_at": None, "hours": LATEST_HOURS})
        result, _ = self.turn("what is 12345678 reading?", data=data)
        self.assertIn("USGS has no stream site 12345678", result["text"])
        self.assertIn(DATASET_SITE, result["text"])

    def test_no_gauge_in_the_radius_is_an_honest_empty_answer(self):
        data = FakeData(near={"latitude": 35.0, "longitude": -140.0, "radius_miles": 25.0,
                              "sites_in_box": 0, "found": 0, "limit": 1, "checked": 0,
                              "readings": [], "silent": [], "candidates": [], "read_at": None})
        result, _ = self.turn("how high is the river in Golden?", data=data)
        self.assertIn("No active USGS stream gauge with real-time data within 25 miles", result["text"])
        self.assertIn("Name a USGS site number instead", result["text"])

    def test_a_gauge_with_no_readings_in_the_window_is_still_a_gauges_question(self):
        """The nearest gauge USGS lists published nothing: say that, do not invent a value."""
        data = FakeData(near={"latitude": 39.74, "longitude": -104.99, "radius_miles": 25.0,
                              "sites_in_box": 604, "found": 498, "limit": 1, "checked": 20,
                              "silent": [{"site_number": "06713500",
                                          "site_name": "CHERRY CREEK AT DENVER, CO.",
                                          "miles": 0.6, "series": {}}],
                              "readings": [], "candidates": [], "read_at": None})
        result, _ = self.turn("how high is the river in Golden?", data=data)
        self.assertIn("No active USGS stream gauge with real-time data within 25 miles", result["text"])

    def test_an_unknown_place_is_refused_without_reading_anything(self):
        result, client = self.turn("which gauges are near Atlantis?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("I do not know the place 'Atlantis'", result["text"])
        self.assertEqual(self.tools(result), [])
        self.assertEqual(client.permission_requests, [])

    def test_no_place_at_all_asks_for_one_without_reading_anything(self):
        result, client = self.turn("how high is the river?")
        self.assertIn("Tell me a USGS site number", result["text"])
        self.assertIn("United States", result["text"])
        self.assertEqual(self.tools(result), [])
        self.assertEqual(client.permission_requests, [])

    def test_help_lists_the_skills_without_permission(self):
        result, client = self.turn("help")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("USGS National Water Information System", result["text"])
        self.assertIn("which gauges are near Denver?", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(self.tools(result), [])

    def test_a_denied_permission_is_a_refusal_and_skips_the_read(self):
        data = FakeData()
        result, _ = self.turn("what is 06719505 reading?", data=data, permission="reject-once")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])
        self.assertEqual(data.calls, [])

    def test_a_reader_failure_is_reported_not_swallowed(self):
        result, _ = self.turn("what is 06719505 reading?", data=FakeData(raise_error=True))
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("could not read the USGS water service: USGS offline", result["text"])
        updates = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call_update"]
        self.assertEqual(updates[-1]["status"], "failed")

    def test_a_bad_site_number_is_rejected_before_the_read(self):
        result, _ = self.turn("what is 06719505 reading?", data=FakeData(raise_value_error=True))
        self.assertIn("could not read the USGS water service: bad site", result["text"])

    def test_the_turn_streams_a_plan_and_closes_the_tool_call_with_a_summary(self):
        result, _ = self.turn("what is 06719505 reading?")
        plans = [update for update in result["updates"] if update.get("sessionUpdate") == "plan"]
        self.assertTrue(plans)
        self.assertIn("river-now", plans[0]["entries"][0]["content"])
        completed = [update for update in result["updates"]
                     if update.get("sessionUpdate") == "tool_call_update" and update.get("status") == "completed"]
        self.assertIn("USGS 06719505 CLEAR CREEK AT GOLDEN, CO", completed[-1]["content"][0]["content"]["text"])

    def test_the_agent_declares_its_name_and_version(self):
        self.assertEqual(RiversAgent.name, "rivers")
        self.assertEqual(RiversAgent.version, "1.0.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
