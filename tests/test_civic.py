"""Tests for the civic ACP agent: routing, skills, permissions, cancellation.

    python3 tests/test_civic.py
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CIVIC_DIR = REPO_ROOT / "agents" / "civic"
for path in (str(REPO_ROOT), str(CIVIC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import CivicAgent, route  # noqa: E402
from datasets import CivicData, CivicDataError, summarize_samples  # noqa: E402

COMPLAINT = {
    "unique_key": "12345678",
    "complaint_type": "Street Condition",
    "descriptor": "Pothole",
    "status": "Closed",
    "created_date": "2026-09-01T00:00:00.000",
    "closed_date": "2026-09-10T00:00:00.000",
    "resolution_description": "Repaired",
    "street_name": "OCEAN AVENUE",
    "incident_zip": "11235",
    "borough": "BROOKLYN",
    "agency_name": "DOT",
    "dataset": "erm2-nwe9",
}

SAMPLE = {
    "sample_number": "202620761",
    "sample_date": "2026-08-31T00:00:00.000",
    "sample_time": "9:32",
    "sample_site": "55450",
    "residual_free_chlorine_mg_l": "0.24",
    "turbidity_ntu": "0.52",
    "coliform_quanti_tray_mpn_100ml": "<1",
    "e_coli_quanti_tray_mpn_100ml": "<1",
    "dataset": "bkwf-xfky",
}


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


class FakeData(CivicData):
    """Same interface as CivicData, no network."""

    def __init__(self, complaint=COMPLAINT, raise_error: bool = False):
        self.complaint_row = complaint
        self.raise_error = raise_error

    def complaint(self, unique_key):
        if self.raise_error:
            raise CivicDataError("dataset offline")
        if str(unique_key) != "12345678":
            return None
        return dict(self.complaint_row)

    def complaints_near(self, zip_code, days=30, limit=10):
        if self.raise_error:
            raise CivicDataError("dataset offline")
        return [dict(COMPLAINT)]

    def flood_recent(self, hours=72, zip_code=None, limit=10):
        if self.raise_error:
            raise CivicDataError("dataset offline")
        return [{"sensor_name": "BK - Richardson St/N 11th St", "flood_start_time": "2026-09-08T00:14:50.000",
                 "max_depth_inches": "2.36", "duration_mins": "116", "dataset": "aq7i-eu5q"}]

    def water_quality(self, site, days=180):
        if self.raise_error:
            raise CivicDataError("dataset offline")
        samples = [dict(SAMPLE)] if str(site) == "55450" else []
        return samples, summarize_samples(samples)

    def freshness(self, dataset_key):
        return "2026-09-22T00:00:00Z"


class RouteTests(unittest.TestCase):
    def test_complaint_id(self):
        skill, params = route("did complaint 12345678 get fixed?")
        self.assertEqual(skill, "complaint-status")
        self.assertEqual(params["unique_key"], "12345678")

    def test_zip_goes_to_near(self):
        skill, params = route("what 311 complaints came in around 11235 this month?")
        self.assertEqual(skill, "complaints-near")
        self.assertEqual(params["zip_code"], "11235")

    def test_flood_window(self):
        skill, params = route("which streets flooded in the last 30 days?")
        self.assertEqual(skill, "flood-recent")
        self.assertEqual(params["hours"], 720)

    def test_flood_by_zip(self):
        skill, params = route("did anything flood in 11211 this week?")
        self.assertEqual(skill, "flood-recent")
        self.assertEqual(params["zip_code"], "11211")
        self.assertEqual(params["hours"], 168)

    def test_water_site(self):
        skill, params = route("how is the drinking water testing at site 1S03A?")
        self.assertEqual(skill, "water-quality")
        self.assertEqual(params["site"], "1S03A")

    def test_water_without_site(self):
        skill, params = route("how is the water quality?")
        self.assertEqual(skill, "water-quality")
        self.assertIsNone(params["site"])

    def test_help_and_empty(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("hello there")[0], "help")


class SummarizeTests(unittest.TestCase):
    def test_detections(self):
        positive = {**SAMPLE, "coliform_quanti_tray_mpn_100ml": "12.4", "e_coli_quanti_tray_mpn_100ml": "2"}
        summary = summarize_samples([SAMPLE, positive])
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["coliform_detections"], 1)
        self.assertEqual(summary["e_coli_detections"], 1)
        self.assertEqual(summary["turbidity_ntu_max"], 0.52)

    def test_empty(self):
        summary = summarize_samples([])
        self.assertEqual(summary["samples"], 0)
        self.assertIsNone(summary["chlorine_mg_l"]["min"])


class CivicTurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = CivicAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_complaint_answer_cites_dataset(self):
        turn, client = self.turn("did complaint 12345678 get fixed?")
        self.assertEqual(turn["stopReason"], STOP_END_TURN)
        self.assertIn("erm2-nwe9", turn["text"])
        self.assertIn("Closed", turn["text"])
        self.assertIn("Repaired", turn["text"])
        tools = [u for u in turn["updates"] if u.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "complaint-status")
        self.assertEqual(tools[0]["kind"], "fetch")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked(self):
        turn, _ = self.turn("did complaint 12345678 get fixed?")
        chunks = [u for u in turn["updates"] if u.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_unknown_complaint_is_honest(self):
        turn, _ = self.turn("complaint 99999999")
        self.assertIn("No 311 complaint with key 99999999", turn["text"])

    def test_flood_answer(self):
        turn, _ = self.turn("which streets flooded in the last 24 hours?")
        self.assertIn("aq7i-eu5q", turn["text"])
        self.assertIn("Richardson", turn["text"])

    def test_water_answer_carries_caveat(self):
        turn, _ = self.turn("how is the drinking water at site 55450?")
        self.assertIn("bkwf-xfky", turn["text"])
        self.assertIn("call 311", turn["text"])

    def test_water_without_site_asks(self):
        turn, _ = self.turn("how is the drinking water quality?")
        self.assertIn("Which drinking-water monitoring site", turn["text"])

    def test_help_does_not_ask_permission(self):
        turn, client = self.turn("what can you do?")
        self.assertIn("did complaint", turn["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        turn, _ = self.turn("did complaint 12345678 get fixed?", permission="reject")
        self.assertEqual(turn["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", turn["text"])

    def test_dataset_failure_is_reported(self):
        turn, _ = self.turn("did complaint 12345678 get fixed?", data=FakeData(raise_error=True))
        self.assertIn("could not read the dataset", turn["text"])
        statuses = [u.get("status") for u in turn["updates"] if u.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
