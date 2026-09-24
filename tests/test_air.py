"""Tests for the air ACP agent: the Open-Meteo reader, routing, skills, permissions.

    python3 tests/test_air.py

No network: the reader runs against injected payloads and the agent against a FakeData.
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
AIR_DIR = REPO_ROOT / "agents" / "air"
for path in (str(REPO_ROOT), str(AIR_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    AirAgent,
    DEFAULT_FORECAST_HOURS,
    hours_from_text,
    place_from_text,
    point_from_text,
    route,
    targets_from_text,
    unknown_place_from_text,
    unknown_places_from_text,
)
from data import (  # noqa: E402
    CURRENT_FIELDS,
    CURRENT_PATH,
    DATASET,
    DEFAULT_RANKING,
    MAX_HOURS,
    MAX_PLACES,
    AirQualityData,
    AirQualityError,
    aqi_band,
    aqi_category,
    round_or_none,
)

#: Real shapes, copied from the live API on 2026-09-24 02:00 UTC.
DENVER_CURRENT = {
    "latitude": 39.7, "longitude": -105.0, "timezone": "GMT", "elevation": 1609.0,
    "current_units": {
        "time": "iso8601", "interval": "seconds", "us_aqi": "USAQI",
        "pm2_5": "µg/m³", "pm10": "µg/m³", "ozone": "µg/m³", "nitrogen_dioxide": "µg/m³",
        "sulphur_dioxide": "µg/m³", "carbon_monoxide": "µg/m³", "uv_index": "",
        "alder_pollen": "grains/m³", "birch_pollen": "grains/m³", "grass_pollen": "grains/m³",
        "mugwort_pollen": "grains/m³", "olive_pollen": "grains/m³", "ragweed_pollen": "grains/m³",
    },
    "current": {
        "time": "2026-09-24T02:00", "interval": 3600, "us_aqi": 52, "pm2_5": 18.0, "pm10": 19.4,
        "ozone": 23.0, "nitrogen_dioxide": 34.2, "sulphur_dioxide": 1.2, "carbon_monoxide": 165.0,
        "uv_index": 0.0, "alder_pollen": None, "birch_pollen": None, "grass_pollen": None,
        "mugwort_pollen": None, "olive_pollen": None, "ragweed_pollen": None,
    },
}

#: One request with three coordinates comes back as an array, in the order asked.
RANKING_PAYLOAD = [
    {"latitude": 39.7, "longitude": -105.0, "current_units": {"us_aqi": "USAQI", "pm2_5": "µg/m³"},
     "current": {"time": "2026-09-24T02:00", "us_aqi": 52, "pm2_5": 18.0, "pm10": 19.4, "ozone": 23.0}},
    {"latitude": 28.6, "longitude": 77.21, "current_units": {"us_aqi": "USAQI", "pm2_5": "µg/m³"},
     "current": {"time": "2026-09-24T02:00", "us_aqi": 161, "pm2_5": 58.3, "pm10": 90.0, "ozone": 40.0}},
    {"latitude": 39.9, "longitude": 116.41, "current_units": {"us_aqi": "USAQI", "pm2_5": "µg/m³"},
     "current": {"time": "2026-09-24T02:00", "us_aqi": 186, "pm2_5": 88.0, "pm10": 120.0, "ozone": 45.0}},
]


def hourly_payload(count: int = 72, start: str = "2026-09-24T00:00") -> dict:
    """An hourly block whose AQI equals its index, so peak/cleanest are easy to check."""
    first = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    times = [(first + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M") for index in range(count)]
    return {
        "latitude": 39.7, "longitude": -105.0,
        "hourly_units": {"time": "iso8601", "us_aqi": "USAQI", "pm2_5": "µg/m³"},
        "hourly": {"time": times, "us_aqi": list(range(count)), "pm2_5": [float(index) for index in range(count)]},
    }


def fetch_from(payloads, calls=None):
    """An AirQualityData `fetch` that dispatches on path, so data.py runs for real."""

    def fetch(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        payload = payloads[path]
        return payload() if callable(payload) else payload

    return fetch


def _row_of(payload: dict) -> dict:
    """The reader's observation shape, built by the real reader so tests cannot drift."""
    return AirQualityData(fetch=fetch_from({CURRENT_PATH: payload}))._observation(39.7, -105.0)


class FakeData(AirQualityData):
    """Same interface as AirQualityData, no network."""

    def __init__(self, now=None, forecast=None, ranking=None, raise_error: bool = False):
        self._now = now if now is not None else {
            "dataset": DATASET, "time": "2026-09-24T02:00", "latitude": 39.7, "longitude": -105.0,
            "elevation_m": 1609.0, "aqi": 52.0, "category": "Moderate",
            "guidance": "acceptable for most people; unusually sensitive people should consider "
                        "limiting long or heavy outdoor exertion",
            "pollutants": {"pm2_5": 18.0, "pm10": 19.4, "ozone": 23.0, "nitrogen_dioxide": 34.2,
                           "sulphur_dioxide": 1.2, "carbon_monoxide": 165.0},
            "pollen": None, "uv_index": 0.0,
            "units": {"us_aqi": "USAQI", "pm2_5": "µg/m³", "pm10": "µg/m³", "ozone": "µg/m³",
                      "nitrogen_dioxide": "µg/m³", "sulphur_dioxide": "µg/m³",
                      "carbon_monoxide": "µg/m³", "uv_index": ""},
        }
        self._forecast = forecast if forecast is not None else {
            "dataset": DATASET, "latitude": 39.7, "longitude": -105.0, "hours": 3,
            "first_hour": "2026-09-24T02:00", "last_hour": "2026-09-24T04:00",
            "peak": {"aqi": 69.0, "time": "2026-09-24T04:00", "category": "Moderate"},
            "cleanest": {"aqi": 52.0, "time": "2026-09-24T02:00"},
            "series": [{"time": "2026-09-24T02:00", "aqi": 52.0, "pm2_5": 18.0},
                       {"time": "2026-09-24T03:00", "aqi": 60.0, "pm2_5": 22.0},
                       {"time": "2026-09-24T04:00", "aqi": 69.0, "pm2_5": 26.0}],
            "units": {"us_aqi": "USAQI", "pm2_5": "µg/m³"},
        }
        self._ranking = ranking if ranking is not None else {
            "dataset": DATASET, "count": 3, "units": {"us_aqi": "USAQI", "pm2_5": "µg/m³"},
            "worst": {"place": "Beijing", "aqi": 186.0, "category": "Unhealthy", "pm2_5": 88.0,
                      "pm10": 120.0, "ozone": 45.0, "time": "2026-09-24T02:00",
                      "latitude": 39.9, "longitude": 116.41},
            "best": {"place": "Denver", "aqi": 52.0, "category": "Moderate", "pm2_5": 18.0,
                     "pm10": 19.4, "ozone": 23.0, "time": "2026-09-24T02:00",
                     "latitude": 39.7, "longitude": -105.0},
            "places": [
                {"place": "Beijing", "aqi": 186.0, "category": "Unhealthy", "pm2_5": 88.0,
                 "pm10": 120.0, "ozone": 45.0, "time": "2026-09-24T02:00"},
                {"place": "Delhi", "aqi": 161.0, "category": "Unhealthy", "pm2_5": 58.3,
                 "pm10": 90.0, "ozone": 40.0, "time": "2026-09-24T02:00"},
                {"place": "Denver", "aqi": 52.0, "category": "Moderate", "pm2_5": 18.0,
                 "pm10": 19.4, "ozone": 23.0, "time": "2026-09-24T02:00"},
            ],
        }
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def now(self, latitude, longitude):
        if self.raise_error:
            raise AirQualityError("Open-Meteo offline")
        self.calls.append({"kind": "now", "lat": latitude, "lon": longitude})
        return dict(self._now)

    def forecast(self, latitude, longitude, hours=DEFAULT_FORECAST_HOURS):
        if self.raise_error:
            raise AirQualityError("Open-Meteo offline")
        self.calls.append({"kind": "forecast", "lat": latitude, "lon": longitude, "hours": hours})
        return dict(self._forecast)

    def ranking(self, places):
        if self.raise_error:
            raise AirQualityError("Open-Meteo offline")
        self.calls.append({"kind": "ranking", "places": list(places)})
        return dict(self._ranking)


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
    def test_aqi_bands_follow_the_epa_ceilings(self):
        self.assertEqual(aqi_band(0)[0], "Good")
        self.assertEqual(aqi_band(50)[0], "Good")
        self.assertEqual(aqi_band(51)[0], "Moderate")
        self.assertEqual(aqi_band(100)[0], "Moderate")
        self.assertEqual(aqi_band(101)[0], "Unhealthy for Sensitive Groups")
        self.assertEqual(aqi_band(150)[0], "Unhealthy for Sensitive Groups")
        self.assertEqual(aqi_band(151)[0], "Unhealthy")
        self.assertEqual(aqi_band(186)[0], "Unhealthy")
        self.assertEqual(aqi_band(220)[0], "Very Unhealthy")
        self.assertEqual(aqi_band(420)[0], "Hazardous")
        self.assertEqual(aqi_band(900)[0], "Hazardous")  # above the table still reads as hazardous
        self.assertEqual(aqi_band(None)[0], "unavailable")
        self.assertIn("sensitive groups", aqi_band(140)[1])
        self.assertEqual(aqi_category(52), "Moderate")

    def test_round_or_none_keeps_nulls_null(self):
        self.assertEqual(round_or_none(18.04), 18.0)
        self.assertEqual(round_or_none(18.06), 18.1)
        self.assertIsNone(round_or_none(None))
        self.assertIsNone(round_or_none(""))
        self.assertEqual(round_or_none(1, 0), 1.0)

    def test_place_reads_the_longest_name(self):
        self.assertEqual(place_from_text("how bad is the air in Denver?"), "denver")
        self.assertEqual(place_from_text("air quality in New York today"), "new york")
        self.assertIsNone(place_from_text("how bad is the air?"))
        self.assertEqual(place_from_text("is the air in San Jose bad?"), "san jose")

    def test_targets_reads_every_named_place_in_order(self):
        self.assertEqual(targets_from_text("Denver, Phoenix and Los Angeles"),
                         ["denver", "phoenix", "los angeles"])
        self.assertEqual(targets_from_text("compare Beijing with Delhi"), ["beijing", "delhi"])
        self.assertEqual(targets_from_text("nothing here"), [])

    def test_targets_reads_literal_coordinates_too(self):
        self.assertEqual(targets_from_text("compare 39.74,-104.99 with 28.61,77.21"),
                         ["39.74,-104.99", "28.61,77.21"])
        self.assertEqual(targets_from_text("Denver and 39.74,-104.99"),
                         ["denver", "39.74,-104.99"])
        self.assertEqual(targets_from_text("a city of 1,000 people"), [])

    def test_an_unknown_capitalised_place_is_read_after_a_preposition(self):
        self.assertEqual(unknown_place_from_text("how bad is the air in Atlantis?"), "Atlantis")
        self.assertEqual(unknown_place_from_text("what about the air at New Atlantis today"),
                         "New Atlantis")
        # A place this agent does know is not "unknown", and nor is a pronoun.
        self.assertIsNone(unknown_place_from_text("how bad is the air in Denver?"))
        self.assertIsNone(unknown_place_from_text("the air in the Us"))
        self.assertEqual(unknown_places_from_text("compare Denver with Gotham"), ["Gotham"])
        self.assertEqual(unknown_places_from_text("nothing here"), [])

    def test_hours_are_read_from_both_phrasings(self):
        self.assertEqual(hours_from_text("for the next 48 hours"), 48)
        self.assertEqual(hours_from_text("next 6 hrs"), 6)
        self.assertEqual(hours_from_text("12 hours"), 12)
        self.assertIsNone(hours_from_text("tomorrow"))

    def test_point_reads_a_lat_lon_pair_and_never_a_thousands_group(self):
        self.assertEqual(point_from_text("what is the AQI at 39.74,-104.99?"), "39.74,-104.99")
        self.assertEqual(point_from_text("-33.87,151.21"), "-33.87,151.21")
        # London's longitude starts with a zero and is still a longitude.
        self.assertEqual(point_from_text("the AQI at 51.51,-0.13"), "51.51,-0.13")
        self.assertEqual(point_from_text("the AQI at 5.60,0.19"), "5.60,0.19")
        self.assertIsNone(point_from_text("a city of 1,000 people"))
        self.assertIsNone(point_from_text("nothing here"))


class ReaderTests(unittest.TestCase):
    def test_check_helpers(self):
        self.assertEqual(AirQualityData.check_lat("39.74"), 39.74)
        with self.assertRaises(ValueError):
            AirQualityData.check_lat("91")
        self.assertEqual(AirQualityData.check_lon("-105.5"), -105.5)
        with self.assertRaises(ValueError):
            AirQualityData.check_lon("-181")
        self.assertEqual(AirQualityData.check_point("39.74, -104.99"), (39.74, -104.99))
        with self.assertRaises(ValueError):
            AirQualityData.check_point("39.74")
        self.assertEqual(AirQualityData.check_hours("48"), 48)
        with self.assertRaises(ValueError):
            AirQualityData.check_hours("0")
        with self.assertRaises(ValueError):
            AirQualityData.check_hours(str(MAX_HOURS + 1))

    def test_city_lookup_is_case_insensitive_and_never_guesses(self):
        self.assertEqual(AirQualityData.city("Denver")[2], "Denver")
        self.assertEqual(AirQualityData.city("new york")[0], 40.71)
        self.assertEqual(AirQualityData.city("san jose")[2], "San Jose")
        self.assertIsNone(AirQualityData.city("Atlantis"))
        self.assertIsNone(AirQualityData.city(""))

    def test_now_reads_the_current_block_with_units_and_a_band(self):
        calls = []
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: DENVER_CURRENT}, calls))
        read = data.now(39.74, -104.99)
        path, params = calls[0]
        self.assertEqual(path, CURRENT_PATH)
        self.assertEqual(params["latitude"], "39.7400")
        self.assertEqual(params["longitude"], "-104.9900")
        self.assertEqual(params["current"].split(","), list(CURRENT_FIELDS))
        self.assertEqual(params["timezone"], "UTC")
        self.assertEqual(read["aqi"], 52.0)
        self.assertEqual(read["category"], "Moderate")
        self.assertIn("sensitive", read["guidance"])
        self.assertEqual(read["time"], "2026-09-24T02:00")
        self.assertEqual(read["pollutants"]["pm2_5"], 18.0)
        self.assertEqual(read["pollutants"]["carbon_monoxide"], 165.0)
        self.assertEqual(read["units"]["pm2_5"], "µg/m³")
        self.assertEqual(read["dataset"], DATASET)

    def test_pollen_that_is_published_is_reported_and_absent_pollen_is_none(self):
        # Denver publishes no pollen at all, which is not the same as zero.
        self.assertIsNone(_row_of(DENVER_CURRENT)["pollen"])
        with_grass = {**DENVER_CURRENT,
                      "current": {**DENVER_CURRENT["current"], "grass_pollen": 3.0, "birch_pollen": None}}
        pollen = _row_of(with_grass)["pollen"]
        self.assertEqual(pollen["grass_pollen"], 3.0)
        self.assertIsNone(pollen["birch_pollen"])

    def test_forecast_starts_at_the_current_hour_not_at_midnight(self):
        calls = []
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: hourly_payload()}, calls))
        # The model's series starts at 00:00; the read was taken at 02:00.
        with mock.patch("data.current_hour_utc", return_value="2026-09-24T02:00"):
            read = data.forecast(39.74, -104.99, hours=24)
        self.assertEqual(read["first_hour"], "2026-09-24T02:00")
        self.assertEqual(read["hours"], 24)
        self.assertEqual(read["series"][0]["aqi"], 2.0)  # values equal their index
        self.assertEqual(read["peak"], {"aqi": 25.0, "time": "2026-09-25T01:00", "category": "Good"})
        self.assertEqual(read["cleanest"]["aqi"], 2.0)
        # Two days are asked for because the window starts mid-day: never fewer than needed.
        self.assertEqual(calls[0][1]["forecast_days"], "2")
        self.assertEqual(calls[0][1]["hourly"].split(","), ["us_aqi", "pm2_5", "pm10", "ozone"])

    def test_forecast_asks_for_enough_days_and_caps_at_the_api_maximum(self):
        calls = []
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: hourly_payload(count=200)}, calls))
        with mock.patch("data.current_hour_utc", return_value="2026-09-24T00:00"):
            read = data.forecast(39.74, -104.99, hours=72)
        self.assertEqual(calls[0][1]["forecast_days"], "4")
        self.assertEqual(read["hours"], 72)
        self.assertEqual(read["last_hour"], "2026-09-26T23:00")

    def test_forecast_with_no_hours_left_is_an_error(self):
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: hourly_payload(count=2)}))
        with mock.patch("data.current_hour_utc", return_value="2026-09-30T00:00"):
            with self.assertRaises(AirQualityError):
                data.forecast(39.74, -104.99, hours=3)

    def test_ranking_sends_one_request_and_ranks_worst_first(self):
        calls = []
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: RANKING_PAYLOAD}, calls))
        read = data.ranking([("Denver", 39.74, -104.99), ("Delhi", 28.61, 77.21), ("Beijing", 39.90, 116.41)])
        path, params = calls[0]
        self.assertEqual(params["latitude"], "39.7400,28.6100,39.9000")
        self.assertEqual(params["longitude"], "-104.9900,77.2100,116.4100")
        self.assertEqual(params["current"], "us_aqi,pm2_5,pm10,ozone")
        self.assertEqual(read["count"], 3)
        self.assertEqual([row["place"] for row in read["places"]], ["Beijing", "Delhi", "Denver"])
        self.assertEqual(read["worst"]["place"], "Beijing")
        self.assertEqual(read["best"]["place"], "Denver")
        self.assertEqual(read["places"][0]["category"], "Unhealthy")
        self.assertEqual(read["units"]["us_aqi"], "USAQI")

    def test_ranking_validates_its_input(self):
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: RANKING_PAYLOAD}))
        with self.assertRaises(ValueError):
            data.ranking([])
        with self.assertRaises(ValueError):
            data.ranking([(f"p{index}", 1.0, 2.0) for index in range(MAX_PLACES + 1)])
        with self.assertRaises(ValueError):
            data.ranking([("nowhere", 91.0, 2.0)])

    def test_ranking_rejects_a_mismatched_number_of_locations(self):
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: RANKING_PAYLOAD[:2]}))
        with self.assertRaises(AirQualityError):
            data.ranking([("Denver", 39.74, -104.99), ("Delhi", 28.61, 77.21), ("Beijing", 39.9, 116.41)])

    def test_a_single_location_may_come_back_as_a_bare_object(self):
        payload = {"latitude": 39.7, "longitude": -105.0, "current_units": {"us_aqi": "USAQI"},
                   "current": {"time": "2026-09-24T02:00", "us_aqi": 52, "pm2_5": 18.0}}
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: payload}))
        read = data.ranking([("Denver", 39.74, -104.99)])
        self.assertEqual(read["count"], 1)
        self.assertEqual(read["worst"]["aqi"], 52.0)

    def test_the_400_body_reason_is_reported(self):
        import urllib.error

        body = b'{"error":true,"reason":"Forecast days is invalid. Allowed range 0 to 7. Given 7."}'
        error = urllib.error.HTTPError("https://air-quality-api.open-meteo.com/x", 400, "Bad Request", {},
                                       mock.Mock(read=lambda: body))
        with mock.patch("data.request.urlopen", side_effect=error):
            data = AirQualityData()
            with self.assertRaises(AirQualityError) as caught:
                data.now(39.74, -104.99)
        message = str(caught.exception)
        self.assertIn("Forecast days is invalid", message)
        self.assertNotIn("air-quality-api.open-meteo.com", message)

    def test_an_error_payload_inside_a_200_is_still_an_error(self):
        # The model's `error` flag is a boolean, so it must be tested, not just read.
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: {"error": True, "reason": "bad latitude"}}))
        with self.assertRaises(AirQualityError) as caught:
            data.now(39.74, -104.99)
        self.assertIn("bad latitude", str(caught.exception))

    def test_unexpected_payloads_are_errors_not_empty_reads(self):
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: ["not", "an", "object"]}))
        with self.assertRaises(AirQualityError):
            data.now(39.74, -104.99)
        missing_current = AirQualityData(fetch=fetch_from({CURRENT_PATH: {"latitude": 39.7}}))
        with self.assertRaises(AirQualityError):
            missing_current.now(39.74, -104.99)

    def test_a_dead_network_is_reported_as_a_reader_error(self):
        with mock.patch("data.request.urlopen", side_effect=OSError("no route to host")):
            data = AirQualityData()
            with self.assertRaises(AirQualityError) as caught:
                data.now(39.74, -104.99)
        self.assertIn("air-quality request failed", str(caught.exception))

    def test_a_read_is_cached_by_its_parameters(self):
        calls = []
        data = AirQualityData(fetch=fetch_from({CURRENT_PATH: DENVER_CURRENT}, calls))
        data.now(39.74, -104.99)
        data.now(39.74, -104.99)
        data.now(28.61, 77.21)
        self.assertEqual(len(calls), 2)  # the repeat is served from the cache


class RouteTests(unittest.TestCase):
    def test_a_plain_question_is_a_reading(self):
        skill, params = route("how bad is the air in Denver right now?")
        self.assertEqual(skill, "air-now")
        self.assertEqual(params["place"], "denver")

    def test_a_point_is_a_reading(self):
        skill, params = route("what is the AQI at 39.74,-104.99?")
        self.assertEqual(skill, "air-now")
        self.assertEqual(params["point"], "39.74,-104.99")
        self.assertNotIn("place", params)

    def test_a_point_with_a_zero_longitude_is_still_a_reading(self):
        skill, params = route("how bad is the air at 51.51,-0.13?")
        self.assertEqual(skill, "air-now")
        self.assertEqual(params["point"], "51.51,-0.13")

    def test_a_forecast_question_carries_its_hours(self):
        skill, params = route("what will the air quality be like in Los Angeles for the next 48 hours?")
        self.assertEqual(skill, "air-forecast")
        self.assertEqual(params["place"], "los angeles")
        self.assertEqual(params["hours"], 48)

    def test_a_forecast_without_hours_uses_the_default(self):
        skill, params = route("what is the air quality outlook for Denver tomorrow?")
        self.assertEqual(skill, "air-forecast")
        self.assertEqual(params["hours"], DEFAULT_FORECAST_HOURS)

    def test_several_named_places_are_a_comparison(self):
        skill, params = route("how bad is the air in Denver and Los Angeles?")
        self.assertEqual(skill, "air-ranking")
        self.assertEqual(params["places"], ["denver", "los angeles"])

    def test_a_comparison_word_without_places_uses_the_built_in_set(self):
        skill, params = route("which city has the worst air right now?")
        self.assertEqual(skill, "air-ranking")
        self.assertEqual(params["places"], list(DEFAULT_RANKING))

    def test_a_comparison_by_point_keeps_the_places(self):
        skill, params = route("compare 39.74,-104.99 with 28.61,77.21")
        self.assertEqual(skill, "air-ranking")
        self.assertEqual(params["places"], ["39.74,-104.99", "28.61,77.21"])

    def test_a_comparison_carries_names_it_cannot_resolve(self):
        skill, params = route("compare Denver with Gotham")
        self.assertEqual(skill, "air-ranking")
        self.assertEqual(params["places"], ["denver", "Gotham"])

    def test_a_worse_than_question_is_a_comparison(self):
        skill, params = route("is the air in Denver worse than in Boise?")
        self.assertEqual(skill, "air-ranking")
        self.assertEqual(params["places"], ["denver", "boise"])

    def test_a_forecast_on_a_point_keeps_the_point(self):
        skill, params = route("what is the air outlook at 39.74,-104.99 for the next 6 hours?")
        self.assertEqual(skill, "air-forecast")
        self.assertEqual(params["point"], "39.74,-104.99")
        self.assertEqual(params["hours"], 6)

    def test_an_unknown_place_is_carried_through_for_the_refusal(self):
        skill, params = route("how bad is the air in Atlantis?")
        self.assertEqual(skill, "air-now")
        self.assertEqual(params["place"], "Atlantis")

    def test_help_and_empty(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_no_place_at_all_still_routes_to_a_reading(self):
        skill, params = route("how bad is the air?")
        self.assertEqual(skill, "air-now")
        self.assertEqual(params, {})


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = AirAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_a_reading_answer_names_the_band_the_units_and_the_model(self):
        result, client = self.turn("how bad is the air in Denver right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("US AQI 52 — Moderate", result["text"])
        self.assertIn("PM2.5 18 µg/m³", result["text"])
        self.assertIn("UV index 0", result["text"])
        self.assertIn("Europe-only", result["text"])
        self.assertIn("What the band means", result["text"])
        self.assertIn("grid cell", result["text"])
        self.assertIn(DATASET, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "air-now")
        self.assertEqual(tools[0]["kind"], "fetch")
        self.assertEqual(len(client.permission_requests), 1)

    def test_a_point_read_does_not_repeat_the_coordinates(self):
        result, _ = self.turn("what is the AQI at 39.74,-104.99?")
        self.assertIn("Air quality at the point 39.74,-104.99 for the hour", result["text"])
        self.assertNotIn("(39.74,-104.99)", result["text"])

    def test_a_forecast_answer_lists_hours_and_the_peak(self):
        data = FakeData()
        result, _ = self.turn("what will the air be like in Denver for the next 3 hours?", data=data)
        self.assertIn("3 hours from 2026-09-24T02:00", result["text"])
        self.assertIn("Peak US AQI 69", result["text"])
        self.assertIn("cleanest hour 52", result["text"])
        self.assertIn("2026-09-24T04:00  AQI 69", result["text"])
        self.assertIn("not a health advisory", result["text"])
        self.assertEqual(data.calls[-1]["hours"], 3)

    def test_a_comparison_answer_is_worst_first_and_says_gaps_matter(self):
        result, _ = self.turn("compare Denver, Delhi and Beijing")
        self.assertIn("3 place(s) compared in one request, worst air first", result["text"])
        self.assertIn("Beijing — US AQI 186 (Unhealthy)", result["text"])
        self.assertIn("Denver — US AQI 52 (Moderate)", result["text"])
        self.assertIn("smaller than about 10 points is not a real difference", result["text"])

    def test_a_comparison_skips_places_it_does_not_know(self):
        data = FakeData()
        result, _ = self.turn("compare Denver with Gotham", data=data)
        self.assertIn("Skipped (not in my place list): Gotham", result["text"])
        self.assertEqual(data.calls[-1]["kind"], "ranking")

    def test_a_comparison_can_be_two_coordinates(self):
        data = FakeData()
        self.turn("compare 39.74,-104.99 with 28.61,77.21", data=data)
        self.assertEqual([label for label, _, _ in data.calls[-1]["places"]],
                         ["39.74,-104.99", "28.61,77.21"])

    def test_a_comparison_with_nothing_known_asks_for_places(self):
        data = FakeData()
        result, _ = self.turn("which city has the worst air in Atlantis?", data=data)
        self.assertIn("None of those places are in my list", result["text"])
        self.assertEqual(data.calls, [])

    def test_an_unknown_place_is_refused_without_reading_the_model(self):
        data = FakeData()
        result, _ = self.turn("how bad is the air in Atlantis?", data=data)
        self.assertIn("I do not know the place 'Atlantis'", result["text"])
        self.assertIn("will not guess coordinates", result["text"])
        self.assertEqual(data.calls, [])

    def test_no_place_at_all_asks_where(self):
        data = FakeData()
        result, _ = self.turn("how bad is the air?", data=data)
        self.assertIn("Tell me where to look", result["text"])
        self.assertEqual(data.calls, [])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Open-Meteo", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("how bad is the air in Denver?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_model_failure_is_reported_in_the_tool_call(self):
        result, _ = self.turn("how bad is the air in Denver?", data=FakeData(raise_error=True))
        self.assertIn("could not read the air-quality model", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("how bad is the air in Denver?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)


if __name__ == "__main__":
    unittest.main()
