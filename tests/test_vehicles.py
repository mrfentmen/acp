"""Tests for the vehicles ACP agent: NHTSA readers, routing, skills, permissions.

    python3 tests/test_vehicles.py
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
VEHICLES_DIR = REPO_ROOT / "agents" / "vehicles"
for path in (str(REPO_ROOT), str(VEHICLES_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    VehiclesAgent,
    _clean,
    _first_vehicle_word,
    _vehicle_words,
    route,
)
from data import (  # noqa: E402
    COMPLAINTS_PATH,
    DATASET_COMPLAINTS,
    DATASET_MODELS,
    DATASET_RECALLS,
    DATASET_VIN,
    MODELS_PATH,
    RECALLS_PATH,
    VehicleData,
    VehicleError,
    _flag,
)

#: Real shapes, copied from the live APIs on 2026-09-22.
RECALL_ROWS = [
    {"NHTSACampaignNumber": "21V215000", "Component": "FUEL SYSTEM, GASOLINE:DELIVERY:FUEL PUMP",
     "ReportReceivedDate": "25/03/2021", "Consequence": "Fuel pump failure can cause an engine stall.",
     "Remedy": "Dealers will replace the fuel pump.", "Summary": "Honda is recalling certain vehicles.",
     "parkIt": "", "parkOutSide": "", "overTheAirUpdate": "",
     "Manufacturer": "Honda (American Honda Motor Co.)"},
    {"NHTSACampaignNumber": "20V123000", "Component": "AIR BAGS:FRONTAL",
     "ReportReceivedDate": "02/03/2020", "Consequence": "An inflator may rupture.",
     "Remedy": "Dealers will replace the air bag module.", "Summary": "Air bag recall.",
     "parkIt": "Y", "parkOutSide": "Y", "overTheAirUpdate": "N", "Manufacturer": "Honda"},
]

COMPLAINT_ROWS = [
    {"odiNumber": 11763441, "dateComplaintFiled": "09/10/2026", "dateOfIncident": "01/01/2024",
     "components": "EQUIPMENT", "crash": False, "fire": False, "numberOfInjuries": 1,
     "numberOfDeaths": 0, "summary": "The screen freezes while driving.", "vin": ""},
    {"odiNumber": 11760000, "dateComplaintFiled": "01/02/2026", "dateOfIncident": "15/01/2026",
     "components": "FUEL/PROPULSION SYSTEM", "crash": True, "fire": True, "numberOfInjuries": 0,
     "numberOfDeaths": 1, "summary": "Stalled on the highway.", "vin": ""},
    {"odiNumber": 11750000, "dateComplaintFiled": "05/01/2026", "dateOfIncident": "",
     "components": "FUEL/PROPULSION SYSTEM", "crash": False, "fire": False, "numberOfInjuries": 0,
     "numberOfDeaths": 0, "summary": "Fuel smell.", "vin": ""},
]

VIN_ROW = {
    "Make": "BMW", "Model": "X3", "ModelYear": "2011", "Trim": "xDrive35i",
    "BodyClass": "Sport Utility Vehicle [SUV]/Multipurpose Vehicle [MPV]", "DriveType": "AWD/All-Wheel Drive",
    "EngineCylinders": "6", "DisplacementL": "3.0", "EngineHP": "300", "FuelTypePrimary": "Gasoline",
    "PlantCountry": "GERMANY", "PlantCity": "MUNICH", "Manufacturer": "BMW MANUFACTURER CORPORATION",
    "ErrorCode": "6", "ErrorText": "6 - Incomplete VIN", "TransmissionStyle": "", "Series": "",
}


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


def fetch_from(payloads, calls=None):
    """A VehicleData `fetch` that dispatches on path, so data.py runs for real."""

    def fetch(base, path, params):
        if calls is not None:
            calls.append((base, path, dict(params)))
        payload = payloads[path]
        return payload() if callable(payload) else payload

    return fetch


class FakeData(VehicleData):
    """Same interface as VehicleData, no network."""

    def __init__(self, recalls=None, complaints=None, models=None, vin=None, raise_error: bool = False):
        self._recalls = recalls if recalls is not None else {
            "dataset": DATASET_RECALLS, "make": "Honda", "model": "CIVIC", "year": 2020, "count": 2,
            "recalls": [
                {"campaign": "21V215000", "component": "FUEL SYSTEM, GASOLINE:DELIVERY:FUEL PUMP",
                 "reported": "25/03/2021", "consequence": "Fuel pump failure can cause an engine stall.",
                 "remedy": "Dealers will replace the fuel pump.", "park_it": None, "park_outside": None,
                 "over_the_air_update": None, "manufacturer": "Honda", "dataset": DATASET_RECALLS},
                {"campaign": "20V123000", "component": "AIR BAGS:FRONTAL", "reported": "02/03/2020",
                 "consequence": "An inflator may rupture.", "remedy": "Dealers will replace the air bag module.",
                 "park_it": True, "park_outside": True, "over_the_air_update": False,
                 "manufacturer": "Honda", "dataset": DATASET_RECALLS},
            ],
            "park_it_flags": ["AIR BAGS:FRONTAL"],
        }
        self._complaints = complaints if complaints is not None else {
            "dataset": DATASET_COMPLAINTS, "make": "Honda", "model": "CIVIC", "year": 2020, "count": 224,
            "injuries": 1, "deaths": 1, "crashes": 1, "fires": 1,
            "top_components": [{"component": "FUEL/PROPULSION SYSTEM", "complaints": 2},
                               {"component": "EQUIPMENT", "complaints": 1}],
            "recent": [{"odi_number": 11763441, "filed": "09/10/2026", "incident": "01/01/2024",
                        "component": "EQUIPMENT", "crash": False, "fire": False, "injuries": 1, "deaths": 0,
                        "summary": "The screen freezes while driving.", "dataset": DATASET_COMPLAINTS}],
        }
        self._models = models if models is not None else {
            "dataset": DATASET_MODELS, "make": "Honda", "year": 2020, "count": 3,
            "models": ["CIVIC", "ACCORD", "CR-V"], "truncated": False,
        }
        self._vin = vin if vin is not None else {
            "dataset": DATASET_VIN, "vin": "5UXWX7C5*BA",
            "vehicle": {"Make": "BMW", "Model": "X3", "ModelYear": "2011", "FuelTypePrimary": "Gasoline"},
            "error_code": "6", "error_text": "6 - Incomplete VIN", "fully_decoded": False,
            "fields_decoded": 4,
        }
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def recalls(self, make, model, year):
        if self.raise_error:
            raise VehicleError("NHTSA dataset offline")
        self.calls.append({"kind": "recalls", "make": make, "model": model, "year": year})
        return dict(self._recalls)

    def complaints(self, make, model, year, **kwargs):
        if self.raise_error:
            raise VehicleError("NHTSA dataset offline")
        self.calls.append({"kind": "complaints", "make": make, "model": model, "year": year})
        return dict(self._complaints)

    def models(self, make, year, **kwargs):
        if self.raise_error:
            raise VehicleError("NHTSA dataset offline")
        self.calls.append({"kind": "models", "make": make, "year": year})
        return dict(self._models)

    def decode_vin(self, vin):
        if self.raise_error:
            raise VehicleError("NHTSA dataset offline")
        self.calls.append({"kind": "vin", "vin": vin})
        return dict(self._vin)


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
    def test_clean_trims_stopwords_off_both_ends(self):
        self.assertEqual(_clean(["a", "honda", "civic"]), ["honda", "civic"])
        self.assertEqual(_clean(["recalls", "for", "honda", "civic"]), ["honda", "civic"])
        self.assertEqual(_clean(["honda", "civic", "have", "recalls"]), ["honda", "civic"])
        self.assertEqual(_clean(["what", "any"]), [])

    def test_vehicle_words_reads_both_sides_of_the_year(self):
        self.assertEqual(_vehicle_words("does a 2020 honda civic have recalls"), ("honda", "civic"))
        self.assertEqual(_vehicle_words("honda civic 2020 recalls"), ("honda", "civic"))
        self.assertEqual(_vehicle_words("recalls for a 2018 Ford F-150"), ("Ford", "F-150"))
        self.assertEqual(_vehicle_words("honda recalls 2020"), ("honda", None))
        self.assertEqual(_vehicle_words("honda civic recalls"), ("honda", "civic"))

    def test_first_vehicle_word_skips_question_words(self):
        self.assertEqual(_first_vehicle_word("what models did Toyota sell in 2024"), "Toyota")
        self.assertEqual(_first_vehicle_word("what honda models are there for 2020"), "honda")
        self.assertIsNone(_first_vehicle_word("what models are there"))

    def test_flag_reads_the_nhtsa_letters(self):
        self.assertIsNone(_flag(""))
        self.assertIsNone(_flag(None))
        self.assertTrue(_flag("Y"))
        self.assertFalse(_flag("N"))
        self.assertTrue(_flag("2"))  # the agency uses 2 as well as Y


class ReaderTests(unittest.TestCase):
    def test_check_helpers(self):
        self.assertEqual(VehicleData.check_positive("5"), 5)
        with self.assertRaises(ValueError):
            VehicleData.check_positive("0")
        with self.assertRaises(ValueError):
            VehicleData.check_positive("200", maximum=100)
        self.assertEqual(VehicleData.check_year("2020"), 2020)
        with self.assertRaises(ValueError):
            VehicleData.check_year("1200")
        with self.assertRaises(ValueError):
            VehicleData.check_year("2999")
        self.assertEqual(VehicleData.check_vin("5uxwx7c5*ba"), "5UXWX7C5*BA")
        with self.assertRaises(ValueError):
            VehicleData.check_vin("TOOSHORT")
        with self.assertRaises(ValueError):
            VehicleData.check_vin("1HGCM82633A004352O")  # O is never in a VIN
        self.assertEqual(VehicleData.check_model("CR-V"), "CR-V")

    def test_recalls_are_read_and_sorted_newest_first(self):
        calls = []
        data = VehicleData(fetch=fetch_from({RECALLS_PATH: {"Count": 2, "results": RECALL_ROWS}}, calls))
        result = data.recalls("honda", "civic", 2020)
        base, path, params = calls[0]
        self.assertEqual(path, RECALLS_PATH)
        self.assertEqual(params["modelYear"], "2020")
        self.assertEqual(result["recalls"][0]["campaign"], "21V215000")  # 2021 before 2020
        self.assertEqual(result["recalls"][1]["park_it"], True)
        self.assertEqual(result["park_it_flags"], ["AIR BAGS:FRONTAL"])
        self.assertEqual(result["dataset"], DATASET_RECALLS)
        self.assertEqual(result["model"], "CIVIC")

    def test_an_empty_recall_payload_is_zero_not_an_error(self):
        data = VehicleData(fetch=fetch_from({RECALLS_PATH: {"Count": 0, "results": []}}))
        result = data.recalls("honda", "notamodel", 2020)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["recalls"], [])

    def test_complaints_tally_harm_and_component_counts(self):
        calls = []
        data = VehicleData(fetch=fetch_from({COMPLAINTS_PATH: {"count": 3, "results": COMPLAINT_ROWS}}, calls))
        result = data.complaints("honda", "civic", 2020)
        base, path, params = calls[0]
        self.assertEqual(path, COMPLAINTS_PATH)
        self.assertEqual(params["make"], "honda")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["injuries"], 1)
        self.assertEqual(result["deaths"], 1)
        self.assertEqual(result["crashes"], 1)
        self.assertEqual(result["fires"], 1)
        self.assertEqual(result["top_components"][0], {"component": "FUEL/PROPULSION SYSTEM", "complaints": 2})
        self.assertEqual(result["recent"][0]["odi_number"], 11763441)  # 09/10/2026 is the newest

    def test_models_are_deduplicated_and_truncated(self):
        rows = [{"model": "CIVIC"}, {"model": "civic"}, {"model": "ACCORD"}, {"model": ""}]
        data = VehicleData(fetch=fetch_from({MODELS_PATH: {"results": rows}}))
        result = data.models("honda", 2020, limit=2)
        # The same model under two spellings is one model, and the first spelling wins.
        self.assertEqual(result["models"], ["CIVIC", "ACCORD"])
        self.assertEqual(result["count"], 2)
        self.assertFalse(result["truncated"])
        self.assertTrue(data.models("honda", 2020, limit=1)["truncated"])

    def test_vin_decode_keeps_only_resolved_fields(self):
        calls = []
        data = VehicleData(fetch=fetch_from({"/api/vehicles/DecodeVinValues/5UXWX7C5*BA":
                                             {"Count": 1, "results": [VIN_ROW]}}, calls))
        result = data.decode_vin("5uxwx7c5*ba")
        base, path, params = calls[0]
        self.assertEqual(base, data.vpic_url)
        self.assertEqual(params["format"], "json")
        self.assertEqual(result["vehicle"]["Make"], "BMW")
        self.assertNotIn("TransmissionStyle", result["vehicle"])  # empty fields are dropped
        self.assertFalse(result["fully_decoded"])
        self.assertEqual(result["error_text"], "6 - Incomplete VIN")
        self.assertEqual(result["dataset"], DATASET_VIN)

    def test_vin_decode_flags_a_clean_decode(self):
        row = dict(VIN_ROW, ErrorCode="0", ErrorText="0 - VIN decoded clean.")
        data = VehicleData(fetch=fetch_from({"/api/vehicles/DecodeVinValues/1HGCM82633A004352":
                                             {"results": [row]}}))
        result = data.decode_vin("1HGCM82633A004352")
        self.assertTrue(result["fully_decoded"])

    def test_http_400_with_an_empty_result_set_is_no_data(self):
        import urllib.error

        body = b'{"count":0,"message":"Results returned successfully","results":[]}'
        error = urllib.error.HTTPError("https://api.nhtsa.gov/x", 400, "Bad Request", {},
                                      mock.Mock(read=lambda: body))
        with mock.patch("data.request.urlopen", side_effect=error):
            data = VehicleData()
            result = data.recalls("honda", "zzz", 2020)
        self.assertEqual(result["count"], 0)

    def test_http_500_is_reported_without_the_url(self):
        import urllib.error

        error = urllib.error.HTTPError("https://api.nhtsa.gov/x", 500, "Server Error", {},
                                      mock.Mock(read=lambda: b"boom"))
        with mock.patch("data.request.urlopen", side_effect=error):
            data = VehicleData()
            with self.assertRaises(VehicleError) as caught:
                data.recalls("honda", "civic", 2020)
        message = str(caught.exception)
        self.assertIn("NHTSA answered 500", message)
        self.assertNotIn("api.nhtsa.gov", message)


class RouteTests(unittest.TestCase):
    def test_recalls_is_the_default_for_a_vehicle(self):
        skill, params = route("does a 2020 honda civic have recalls?")
        self.assertEqual(skill, "vehicle-recalls")
        self.assertEqual(params["make"], "honda")
        self.assertEqual(params["model"], "civic")
        self.assertEqual(params["year"], 2020)

    def test_complaints(self):
        skill, params = route("what do owners complain about on a 2018 Ford F-150?")
        self.assertEqual(skill, "vehicle-complaints")
        self.assertEqual(params["make"], "Ford")
        self.assertEqual(params["model"], "F-150")
        self.assertEqual(params["year"], 2018)

    def test_models(self):
        skill, params = route("what models did Toyota sell in 2024?")
        self.assertEqual(skill, "vehicle-models")
        self.assertEqual(params["make"], "Toyota")
        self.assertEqual(params["year"], 2024)

    def test_decode_vin(self):
        skill, params = route("decode VIN 5UXWX7C5*BA")
        self.assertEqual(skill, "decode-vin")
        self.assertEqual(params["vin"], "5UXWX7C5*BA")
        self.assertEqual(route("1HGCM82633A004352")[0], "decode-vin")

    def test_help_and_unknown(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_a_make_on_its_own_still_routes_to_a_lookup(self):
        skill, params = route("honda recalls 2020")
        self.assertEqual(skill, "vehicle-recalls")
        self.assertEqual(params["make"], "honda")
        self.assertNotIn("model", params)


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = VehiclesAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_recalls_answer_cites_campaigns_dataset_and_parking_flag(self):
        result, client = self.turn("does a 2020 honda civic have recalls?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("21V215000", result["text"])
        self.assertIn(DATASET_RECALLS, result["text"])
        self.assertIn("DO NOT DRIVE", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "vehicle-recalls")
        self.assertEqual(tools[0]["kind"], "fetch")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("does a 2020 honda civic have recalls?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_a_missing_model_asks_and_reads_the_model_list(self):
        data = FakeData()
        result, _ = self.turn("honda recalls 2020", data=data)
        self.assertIn("I need a model too", result["text"])
        self.assertIn("CIVIC", result["text"])
        self.assertEqual(data.calls[-1]["kind"], "models")
        self.assertEqual(data.calls[-1]["make"], "honda")

    def test_a_missing_vehicle_asks_for_one(self):
        result, _ = self.turn("any recalls?")
        self.assertIn("I need a make and a model year", result["text"])

    def test_no_recalls_is_stated_honestly(self):
        empty = {"dataset": DATASET_RECALLS, "make": "Honda", "model": "ZZZ", "year": 2020, "count": 0,
                 "recalls": [], "park_it_flags": []}
        result, _ = self.turn("honda zzz 2020", data=FakeData(recalls=empty))
        self.assertIn("no recall on record", result["text"])
        self.assertIn("does not mean", result["text"])

    def test_complaints_answer_tallies_harm(self):
        result, _ = self.turn("what do owners complain about on a 2020 honda civic?")
        self.assertIn("224 owner complaint(s)", result["text"])
        self.assertIn("FUEL/PROPULSION SYSTEM: 2 complaint(s)", result["text"])
        self.assertIn("11763441", result["text"])
        self.assertIn(DATASET_COMPLAINTS, result["text"])

    def test_models_answer(self):
        result, _ = self.turn("what models did honda sell in 2020?")
        self.assertIn("ACCORD", result["text"])
        self.assertIn(DATASET_MODELS, result["text"])

    def test_decode_answer_and_incomplete_notice(self):
        result, _ = self.turn("decode VIN 5UXWX7C5*BA")
        self.assertIn("BMW X3", result["text"])
        self.assertIn(DATASET_VIN, result["text"])
        self.assertIn("Incomplete VIN", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("NHTSA", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("does a 2020 honda civic have recalls?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_dataset_failure_is_reported(self):
        result, _ = self.turn("does a 2020 honda civic have recalls?", data=FakeData(raise_error=True))
        self.assertIn("could not read the NHTSA dataset", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_a_bad_vin_is_a_validation_message(self):
        # Real reader with no network: check_vin rejects the VIN before any request is made.
        data = VehicleData(fetch=lambda base, path, params: self.fail("no request should be made"))
        result, _ = self.turn("decode VIN TOOSHORT", data=data)
        self.assertIn("a VIN must be 11-17", result["text"])

    def test_routing_passes_the_vin_through(self):
        data = FakeData()
        self.turn("decode VIN 5UXWX7C5*BA", data=data)
        self.assertEqual(data.calls[-1], {"kind": "vin", "vin": "5UXWX7C5*BA"})


if __name__ == "__main__":
    unittest.main()
