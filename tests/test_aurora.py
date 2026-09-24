"""Tests for the aurora ACP agent: the SWPC reader, routing, skills, permissions.

    python3 tests/test_aurora.py

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
AURORA_DIR = REPO_ROOT / "agents" / "aurora"
for path in (str(REPO_ROOT), str(AURORA_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    AuroraAgent,
    DEFAULT_FORECAST_DAYS,
    days_from_text,
    place_from_text,
    point_from_text,
    route,
    unknown_place_from_text,
    unknown_places_from_text,
)
from data import (  # noqa: E402
    DATASET_FORECAST,
    DATASET_KP,
    DATASET_KP_1M,
    DATASET_OVATION,
    KP_1M,
    KP_3H,
    KP_FORECAST,
    MAX_DAYS,
    OVATION,
    STORM_SCALE,
    SpaceWeatherData,
    SpaceWeatherError,
    is_storm,
    kp_band,
)

#: Real shapes, copied from the live SWPC products on 2026-09-24 02:30 UTC.
KP_1M_ROWS = [
    {"time_tag": "2026-09-24T02:29:00", "kp_index": 3, "estimated_kp": 2.67, "kp": "3M"},
    {"time_tag": "2026-09-24T02:30:00", "kp_index": 3, "estimated_kp": 2.67, "kp": "3M"},
]

#: The forecast file mixes observed, estimated and predicted rows, and starts in the past.
KP_FORECAST_ROWS = [
    {"time_tag": "2026-09-20T00:00:00", "kp": 4.0, "observed": "observed", "noaa_scale": None},
    {"time_tag": "2026-09-23T21:00:00", "kp": 2.0, "observed": "estimated", "noaa_scale": None},
    {"time_tag": "2026-09-24T00:00:00", "kp": 2.33, "observed": "predicted", "noaa_scale": None},
    {"time_tag": "2026-09-24T03:00:00", "kp": 3.67, "observed": "predicted", "noaa_scale": None},
    {"time_tag": "2026-09-24T06:00:00", "kp": 3.0, "observed": "predicted", "noaa_scale": None},
    {"time_tag": "2026-09-25T00:00:00", "kp": 5.33, "observed": "predicted", "noaa_scale": "G1"},
    {"time_tag": "2026-09-26T00:00:00", "kp": 4.67, "observed": "predicted", "noaa_scale": None},
    {"time_tag": "2026-09-27T00:00:00", "kp": 3.33, "observed": "predicted", "noaa_scale": None},
]

#: A small OVATION grid. Longitudes run 0-359, so -148 is the cell at 212.
OVATION_GRID = {
    "Observation Time": "2026-09-24T02:26:00Z",
    "Forecast Time": "2026-09-24T03:49:00Z",
    "Data Format": "[Longitude, Latitude, Aurora]",
    "coordinates": [
        [212, 65, 42], [211, 65, 30], [212, 66, 55], [212, 64, 20],
        [0, 51, 10], [359, 52, 5], [100, 10, 0], [0, -90, 3],
    ],
}


def kp_rows(values, start="2026-09-23T00:00:00") -> list[dict]:
    """3-hourly rows with the given Kp values, oldest first."""
    first = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return [
        {"time_tag": (first + timedelta(hours=3 * index)).strftime("%Y-%m-%dT%H:%M:%S"),
         "Kp": value, "a_running": 6, "station_count": 8}
        for index, value in enumerate(values)
    ]


def fetch_from(payloads, calls=None):
    """A SpaceWeatherData `fetch` that dispatches on path, so data.py runs for real."""

    def fetch(path, params):
        if calls is not None:
            calls.append((path, dict(params)))
        payload = payloads[path]
        return payload() if callable(payload) else payload

    return fetch


class FakeData(SpaceWeatherData):
    """Same interface as SpaceWeatherData, no network."""

    def __init__(self, recent=None, forecast=None, probability=None, raise_error: bool = False):
        self._recent = recent if recent is not None else {
            "now": {"dataset": DATASET_KP_1M, "time_tag": "2026-09-24T02:30:00", "estimated_kp": 2.67,
                    "kp_index": 3, "band": "quiet", "storm": False,
                    "three_hourly": {"dataset": DATASET_KP, "time_tag": "2026-09-23T21:00:00",
                                     "kp": 1.67, "a_running": 6, "station_count": 8}},
            "window_hours": 24, "peak_kp": 2.67, "peak_band": "quiet",
            "peak_time_tag": "2026-09-23T21:00:00",
            "rows": [{"time_tag": "2026-09-23T21:00:00", "kp": 1.67, "a_running": 6}],
            "dataset": DATASET_KP,
        }
        self._forecast = forecast if forecast is not None else {
            "dataset": DATASET_FORECAST,
            "days": [
                {"date": "2026-09-24", "max_kp": 3.67, "band": "unsettled", "storm": False,
                 "rows": [{"time_tag": "2026-09-24T00:00:00", "kp": 2.33},
                          {"time_tag": "2026-09-24T03:00:00", "kp": 3.67},
                          {"time_tag": "2026-09-24T06:00:00", "kp": 3.0}]},
                {"date": "2026-09-25", "max_kp": 5.33, "band": "G1 (minor)", "storm": True,
                 "rows": [{"time_tag": "2026-09-25T00:00:00", "kp": 5.33}]},
            ],
            "predicted_rows": 6, "other_rows": 2, "peak_kp": 5.33, "peak_band": "G1 (minor)",
            "storm_days": ["2026-09-25"],
        }
        self._probability = probability if probability is not None else {
            "dataset": DATASET_OVATION, "observation_time": "2026-09-24T02:26:00Z",
            "forecast_time": "2026-09-24T03:49:00Z", "probability": 42,
            "nearest_cell": {"latitude": 65, "longitude": 212}, "radius_degrees": 2.0,
            "max_probability_nearby": 55, "cells_read": 65160,
        }
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def recent(self, hours=24):
        if self.raise_error:
            raise SpaceWeatherError("SWPC offline")
        self.calls.append({"kind": "recent", "hours": hours})
        return dict(self._recent)

    def forecast(self, days=DEFAULT_FORECAST_DAYS):
        if self.raise_error:
            raise SpaceWeatherError("SWPC offline")
        self.calls.append({"kind": "forecast", "days": days})
        return dict(self._forecast)

    def aurora_probability(self, lat, lon, radius=2.0):
        if self.raise_error:
            raise SpaceWeatherError("SWPC offline")
        self.calls.append({"kind": "probability", "lat": lat, "lon": lon, "radius": radius})
        return dict(self._probability)


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
    def test_kp_bands_use_noaa_storm_grading(self):
        self.assertEqual(kp_band(0), "quiet")
        self.assertEqual(kp_band(2.67), "quiet")
        self.assertEqual(kp_band(3), "unsettled")
        self.assertEqual(kp_band(4), "active")
        self.assertEqual(kp_band(4.67), "active")
        self.assertEqual(kp_band(5), STORM_SCALE[5])
        self.assertEqual(kp_band(5.33), "G1 (minor)")
        self.assertEqual(kp_band(6), "G2 (moderate)")
        self.assertEqual(kp_band(7), "G3 (strong)")
        self.assertEqual(kp_band(8), "G4 (severe)")
        self.assertEqual(kp_band(9), "G5 (extreme)")
        self.assertEqual(kp_band(9.5), "G5 (extreme)")
        self.assertEqual(kp_band(None), "unknown")

    def test_is_storm_starts_at_kp_five(self):
        self.assertFalse(is_storm(4.99))
        self.assertFalse(is_storm(None))
        self.assertTrue(is_storm(5))
        self.assertTrue(is_storm(5.33))

    def test_place_reads_the_longest_name(self):
        self.assertEqual(place_from_text("aurora from Fairbanks tonight?"), "fairbanks")
        self.assertEqual(place_from_text("visible from New York"), "new york")
        self.assertIsNone(place_from_text("will the aurora be out?"))

    def test_point_reads_a_pair_and_keeps_zero_longitudes(self):
        self.assertEqual(point_from_text("at 64.84,-147.72"), "64.84,-147.72")
        self.assertEqual(point_from_text("at 51.51,-0.13"), "51.51,-0.13")
        self.assertIsNone(point_from_text("a town of 1,000 people"))
        self.assertIsNone(point_from_text("nothing here"))

    def test_unknown_places_exclude_places_this_agent_knows(self):
        self.assertEqual(unknown_places_from_text("aurora from Atlantis?"), ["Atlantis"])
        self.assertEqual(unknown_place_from_text("aurora from Atlantis?"), "Atlantis")
        self.assertIsNone(unknown_place_from_text("aurora from Fairbanks?"))
        self.assertEqual(unknown_places_from_text("nothing here"), [])

    def test_days_are_read_from_both_phrasings(self):
        self.assertEqual(days_from_text("for the next 3 days"), 3)
        self.assertEqual(days_from_text("2 nights"), 2)
        self.assertIsNone(days_from_text("tonight"))
        self.assertIsNone(days_from_text("nothing here"))


class ReaderTests(unittest.TestCase):
    def test_check_helpers(self):
        self.assertEqual(SpaceWeatherData.check_lat("64.84"), 64.84)
        with self.assertRaises(ValueError):
            SpaceWeatherData.check_lat("91")
        self.assertEqual(SpaceWeatherData.check_lon("-147.72"), -147.72)
        with self.assertRaises(ValueError):
            SpaceWeatherData.check_lon("-181")
        self.assertEqual(SpaceWeatherData.check_point("64.84, -147.72"), (64.84, -147.72))
        with self.assertRaises(ValueError):
            SpaceWeatherData.check_point("64.84")
        self.assertEqual(SpaceWeatherData.check_days(str(MAX_DAYS)), MAX_DAYS)
        for bad in ("0", str(MAX_DAYS + 1), "many"):
            with self.assertRaises(ValueError):
                SpaceWeatherData.check_days(bad)

    def test_city_lookup_is_case_insensitive_and_never_guesses(self):
        self.assertEqual(SpaceWeatherData.city("Fairbanks")[2], "Fairbanks")
        self.assertEqual(SpaceWeatherData.city("tromso")[1], -18.96)
        self.assertEqual(SpaceWeatherData.city("New-York")[2], "New York")
        self.assertIsNone(SpaceWeatherData.city("Atlantis"))
        self.assertIsNone(SpaceWeatherData.city(""))

    def test_kp_now_takes_the_last_minute_row_and_the_latest_three_hourly_one(self):
        calls = []
        data = SpaceWeatherData(fetch=fetch_from({KP_1M: KP_1M_ROWS, KP_3H: kp_rows([1.0, 1.67])}, calls))
        read = data.kp_now()
        self.assertEqual(read["time_tag"], "2026-09-24T02:30:00")
        self.assertEqual(read["estimated_kp"], 2.67)
        self.assertEqual(read["kp_index"], 3)
        self.assertEqual(read["band"], "quiet")
        self.assertFalse(read["storm"])
        self.assertEqual(read["three_hourly"]["kp"], 1.67)
        self.assertEqual(read["three_hourly"]["station_count"], 8)
        self.assertEqual(read["dataset"], DATASET_KP_1M)
        self.assertEqual([path for path, _ in calls], [KP_1M, KP_3H])

    def test_kp_rows_returns_the_newest_rows_in_order(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        data = SpaceWeatherData(fetch=fetch_from({KP_3H: kp_rows(values)}))
        rows = data.kp_rows(limit=3)
        self.assertEqual([row["Kp"] for row in rows], [3.0, 4.0, 5.0])
        self.assertEqual(rows[-1]["time_tag"], "2026-09-23T12:00:00")

    def test_recent_reports_the_peak_of_the_window(self):
        data = SpaceWeatherData(fetch=fetch_from({
            KP_1M: KP_1M_ROWS, KP_3H: kp_rows([1.0, 1.67, 5.33, 2.0, 1.0, 0.67, 1.0, 1.33]),
        }))
        read = data.recent(hours=24)
        self.assertEqual(read["window_hours"], 24)
        self.assertEqual(read["peak_kp"], 5.33)
        self.assertEqual(read["peak_band"], "G1 (minor)")
        self.assertEqual(read["peak_time_tag"], "2026-09-23T06:00:00")
        self.assertEqual(len(read["rows"]), 8)
        self.assertEqual(read["rows"][0], {"time_tag": "2026-09-23T00:00:00", "kp": 1.0, "a_running": 6})
        self.assertTrue(read["now"]["storm"] is False)

    def test_forecast_keeps_only_predicted_rows_on_days_still_to_come(self):
        calls = []
        data = SpaceWeatherData(fetch=fetch_from({KP_FORECAST: KP_FORECAST_ROWS}, calls))
        with mock.patch("data.today_utc", return_value="2026-09-24"):
            read = data.forecast(3)
        self.assertEqual([entry["date"] for entry in read["days"]],
                         ["2026-09-24", "2026-09-25", "2026-09-26"])
        first = read["days"][0]
        self.assertEqual(first["max_kp"], 3.67)  # the day's peak, not the first row
        self.assertEqual(len(first["rows"]), 3)
        self.assertEqual(first["band"], "unsettled")
        self.assertFalse(first["storm"])
        storm_day = read["days"][1]
        self.assertEqual(storm_day["max_kp"], 5.33)
        self.assertTrue(storm_day["storm"])
        self.assertEqual(storm_day["band"], "G1 (minor)")
        self.assertEqual(read["storm_days"], ["2026-09-25"])
        self.assertEqual(read["peak_kp"], 5.33)
        self.assertEqual(read["predicted_rows"], 6)
        self.assertEqual(read["other_rows"], 2)  # the observed and estimated rows are not a forecast
        self.assertEqual(read["dataset"], DATASET_FORECAST)

    def test_forecast_honours_the_day_cap(self):
        data = SpaceWeatherData(fetch=fetch_from({KP_FORECAST: KP_FORECAST_ROWS}))
        with mock.patch("data.today_utc", return_value="2026-09-24"):
            read = data.forecast(1)
        self.assertEqual(len(read["days"]), 1)
        self.assertEqual(read["storm_days"], [])

    def test_forecast_with_no_future_rows_is_empty_not_an_error(self):
        data = SpaceWeatherData(fetch=fetch_from({KP_FORECAST: KP_FORECAST_ROWS[:2]}))
        with mock.patch("data.today_utc", return_value="2026-09-24"):
            read = data.forecast(3)
        self.assertEqual(read["days"], [])
        self.assertIsNone(read["peak_kp"])

    def test_aurora_probability_finds_the_nearest_cell_and_the_best_one_nearby(self):
        calls = []
        data = SpaceWeatherData(fetch=fetch_from({OVATION: OVATION_GRID}, calls))
        read = data.aurora_probability(64.84, -147.72)
        self.assertEqual(calls[0][0], OVATION)
        self.assertEqual(read["probability"], 42)  # the closest cell, at 212,65
        self.assertEqual(read["nearest_cell"], {"latitude": 65, "longitude": 212})
        self.assertEqual(read["max_probability_nearby"], 55)  # 212,66 is 55%
        self.assertEqual(read["observation_time"], "2026-09-24T02:26:00Z")
        self.assertEqual(read["forecast_time"], "2026-09-24T03:49:00Z")
        self.assertEqual(read["cells_read"], 8)
        self.assertEqual(read["dataset"], DATASET_OVATION)

    def test_aurora_probability_wraps_longitudes_around_zero(self):
        data = SpaceWeatherData(fetch=fetch_from({OVATION: OVATION_GRID}))
        # London is at -0.13, which the grid stores as 0 (and 359 the other way round).
        read = data.aurora_probability(51.51, -0.13, radius=2.0)
        self.assertEqual(read["nearest_cell"], {"latitude": 51, "longitude": 0})
        self.assertEqual(read["probability"], 10)
        self.assertEqual(read["max_probability_nearby"], 10)

    def test_aurora_probability_is_honest_when_the_grid_has_nothing_nearby(self):
        data = SpaceWeatherData(fetch=fetch_from({OVATION: OVATION_GRID}))
        with self.assertRaises(SpaceWeatherError):
            data.aurora_probability(-33.87, 151.21, radius=1.0)

    def test_an_empty_ovation_grid_is_an_error(self):
        data = SpaceWeatherData(fetch=fetch_from({OVATION: {"Observation Time": None}}))
        with self.assertRaises(SpaceWeatherError):
            data.aurora_probability(64.84, -147.72)

    def test_an_http_error_is_reported_without_the_url(self):
        import urllib.error

        error = urllib.error.HTTPError("https://services.swpc.noaa.gov/x", 503, "Service Unavailable", {},
                                       mock.Mock(read=lambda: b"busy"))
        with mock.patch("data.request.urlopen", side_effect=error):
            data = SpaceWeatherData()
            with self.assertRaises(SpaceWeatherError) as caught:
                data.kp_now()
        message = str(caught.exception)
        self.assertIn("503", message)
        self.assertNotIn("services.swpc.noaa.gov", message)

    def test_a_dead_network_is_reported_as_a_reader_error(self):
        with mock.patch("data.request.urlopen", side_effect=OSError("no route to host")):
            data = SpaceWeatherData()
            with self.assertRaises(SpaceWeatherError) as caught:
                data.kp_rows()
        self.assertIn("space-weather request failed", str(caught.exception))

    def test_an_unexpected_payload_is_an_error(self):
        data = SpaceWeatherData(fetch=lambda path, params: "not json")
        with self.assertRaises(SpaceWeatherError):
            data.kp_rows()
        not_a_list = SpaceWeatherData(fetch=lambda path, params: {"rows": []})
        with self.assertRaises(SpaceWeatherError):
            not_a_list.kp_rows()

    def test_a_product_is_cached_per_path(self):
        calls = []
        data = SpaceWeatherData(fetch=fetch_from({KP_1M: KP_1M_ROWS, KP_3H: kp_rows([1.0])}, calls))
        data.kp_now()
        data.kp_now()
        self.assertEqual([path for path, _ in calls], [KP_1M, KP_3H])


class RouteTests(unittest.TestCase):
    def test_a_conditions_question_is_now(self):
        skill, params = route("how disturbed is the geomagnetic field right now?")
        self.assertEqual(skill, "aurora-now")
        self.assertEqual(params, {})

    def test_a_storm_question_is_now_too(self):
        skill, params = route("is there a geomagnetic storm?")
        self.assertEqual(skill, "aurora-now")
        self.assertEqual(params, {})

    def test_a_forecast_question_carries_its_days(self):
        skill, params = route("what does the Kp forecast look like for the next 3 days?")
        self.assertEqual(skill, "aurora-forecast")
        self.assertEqual(params["days"], 3)

    def test_a_forecast_without_days_uses_the_default(self):
        skill, params = route("what is the space weather outlook for this weekend?")
        self.assertEqual(skill, "aurora-forecast")
        self.assertEqual(params["days"], DEFAULT_FORECAST_DAYS)

    def test_seeing_it_from_a_place_is_visibility(self):
        skill, params = route("will the aurora be visible from Fairbanks tonight?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params["place"], "fairbanks")

    def test_visibility_wins_over_a_forecast_word(self):
        # "tonight" asks about the sky overhead, so the OVATION model answers, not the Kp forecast.
        skill, params = route("is the aurora forecast good tonight in Tromsø?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params["place"], "tromsø")  # the ø spelling resolves too

    def test_a_place_named_with_the_aurora_is_visibility(self):
        skill, params = route("aurora at 64.84,-147.72?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params["point"], "64.84,-147.72")
        skill, params = route("aurora in Fairbanks?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params["place"], "fairbanks")

    def test_a_storm_question_about_a_place_is_still_now(self):
        skill, params = route("is there a geomagnetic storm over Fairbanks?")
        self.assertEqual(skill, "aurora-now")
        self.assertEqual(params["place"], "fairbanks")

    def test_visibility_by_point(self):
        skill, params = route("what is the aurora probability at 64.84,-147.72?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params["point"], "64.84,-147.72")

    def test_visibility_with_no_place_keeps_only_the_skill(self):
        skill, params = route("will the northern lights be out tonight?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params, {})

    def test_an_unknown_place_is_carried_through_for_the_refusal(self):
        skill, params = route("will the aurora be visible from Atlantis?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params["place"], "Atlantis")

    def test_help_and_empty(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = AuroraAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_a_now_answer_gives_both_indices_and_the_window_peak(self):
        result, client = self.turn("how disturbed is the geomagnetic field right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("estimated Kp 2.67 (quiet)", result["text"])
        self.assertIn("3-hourly planetary index reads Kp 1.67", result["text"])
        self.assertIn("Peak of the last 24 hours: Kp 2.67", result["text"])
        self.assertIn("Below Kp 5", result["text"])
        self.assertIn("not a local sky forecast", result["text"])
        self.assertIn(DATASET_KP_1M, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "aurora-now")
        self.assertEqual(tools[0]["kind"], "fetch")
        self.assertEqual(len(client.permission_requests), 1)

    def test_a_now_answer_names_a_storm_when_there_is_one(self):
        stormy = {
            "now": {"dataset": DATASET_KP_1M, "time_tag": "2026-09-24T02:30:00", "estimated_kp": 5.67,
                    "kp_index": 6, "band": "G2 (moderate)", "storm": True,
                    "three_hourly": {"dataset": DATASET_KP, "time_tag": "2026-09-23T21:00:00",
                                     "kp": 5.33, "a_running": 66, "station_count": 8}},
            "window_hours": 24, "peak_kp": 5.67, "peak_band": "G2 (moderate)",
            "peak_time_tag": "2026-09-24T00:00:00", "rows": [], "dataset": DATASET_KP,
        }
        result, _ = self.turn("is there a geomagnetic storm?", data=FakeData(recent=stormy))
        self.assertIn("G2 (moderate) storm", result["text"])
        self.assertIn("G1 to G5", result["text"])

    def test_a_forecast_answer_lists_the_days_and_flags_the_storm_day(self):
        result, _ = self.turn("what does the Kp forecast look like for the next 3 days?")
        self.assertIn("Predicted planetary K index for the next 2 days", result["text"])
        self.assertIn("2026-09-24  peak Kp 3.67 (unsettled, 3 intervals)", result["text"])
        self.assertIn("2026-09-25  peak Kp 5.33 (G1 (minor), 1 interval)", result["text"])
        self.assertNotIn("1 intervals", result["text"])
        self.assertNotIn("day(s)", result["text"])
        self.assertIn("Storm-level days: 2026-09-25", result["text"])
        self.assertIn("confidence falls off sharply", result["text"])
        self.assertIn(DATASET_FORECAST, result["text"])

    def test_a_forecast_summary_count_is_pluralised_correctly(self):
        result, _ = self.turn("Kp forecast for the next 3 days?")
        summary = [update for update in result["updates"]
                   if update.get("sessionUpdate") == "tool_call_update" and update.get("status") == "completed"]
        self.assertIn("2 days, peak Kp", summary[-1]["content"][0]["content"]["text"])

    def test_an_empty_forecast_is_stated_honestly(self):
        empty = {"dataset": DATASET_FORECAST, "days": [], "predicted_rows": 0, "other_rows": 2,
                 "peak_kp": None, "peak_band": "unknown", "storm_days": []}
        result, _ = self.turn("Kp forecast for the next 2 days?", data=FakeData(forecast=empty))
        self.assertIn("no predicted Kp rows", result["text"])
        self.assertIn(DATASET_FORECAST, result["text"])

    def test_a_visibility_answer_gives_the_probability_the_cell_and_the_clouds_caveat(self):
        data = FakeData()
        result, _ = self.turn("will the aurora be visible from Fairbanks tonight?", data=data)
        self.assertIn("Aurora probability at Fairbanks (64.84,-147.72): 42% overhead", result["text"])
        self.assertIn("Model obs time 2026-09-24T02:26:00Z", result["text"])
        self.assertIn("nearest grid cell is 65,212", result["text"])
        self.assertIn("says nothing about clouds", result["text"])
        self.assertIn("65,160-cell global grid", result["text"])
        self.assertEqual(data.calls[-1]["kind"], "probability")

    def test_a_visibility_answer_boosts_high_probability_language(self):
        strong = {"dataset": DATASET_OVATION, "observation_time": "2026-09-24T02:26:00Z",
                  "forecast_time": "2026-09-24T03:49:00Z", "probability": 78,
                  "nearest_cell": {"latitude": 65, "longitude": 212}, "radius_degrees": 2.0,
                  "max_probability_nearby": 80, "cells_read": 65160}
        result, _ = self.turn("aurora at 64.84,-147.72?", data=FakeData(probability=strong))
        self.assertIn("strong model signal", result["text"])
        self.assertIn("Aurora probability at the point 64.84,-147.72", result["text"])
        self.assertNotIn("(64.84,-147.72)", result["text"])

    def test_visibility_with_no_place_asks_where(self):
        data = FakeData()
        result, _ = self.turn("will the northern lights be out tonight?", data=data)
        self.assertIn("Tell me where you are", result["text"])
        self.assertIn("Aurora probability is per location", result["text"])
        self.assertEqual(data.calls, [])

    def test_an_unknown_place_is_refused_without_reading_a_product(self):
        data = FakeData()
        result, _ = self.turn("will the aurora be visible from Atlantis?", data=data)
        self.assertIn("I do not know the place 'Atlantis'", result["text"])
        self.assertIn("will not guess coordinates", result["text"])
        self.assertEqual(data.calls, [])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Space Weather Prediction Center", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("how disturbed is the geomagnetic field?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_product_failure_is_reported_in_the_tool_call(self):
        result, _ = self.turn("how disturbed is the geomagnetic field?", data=FakeData(raise_error=True))
        self.assertIn("could not read the space-weather product", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("how disturbed is the geomagnetic field right now?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)


if __name__ == "__main__":
    unittest.main()
