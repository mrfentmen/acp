"""Tests for the A2A bridge agent, including the push-notification relay.

    python3 tests/test_bridge.py

A fake A2A server (real HTTP, real SSE) stands in for the remote agent, so the
bridge is exercised over its actual wire protocol.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import unittest
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = REPO_ROOT / "agents" / "a2a_bridge"
for path in (str(REPO_ROOT), str(BRIDGE_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN  # noqa: E402

from bridge import BridgeAgent, RemoteSkill, parse_endpoints  # noqa: E402

CARD = {
    "protocolVersion": "0.3.0",
    "name": "Fake Flood Agent",
    "description": "flood test double",
    "capabilities": {"streaming": True, "pushNotifications": True},
    "skills": [
        {
            "id": "flood-recent",
            "name": "Recent street flooding",
            "description": "Street flooding events in the last N hours",
            "tags": ["flood", "nyc"],
            "examples": ["Which streets flooded in the last 24 hours?"],
        },
        {
            "id": "flood-watch",
            "name": "Watch a street for flooding",
            "description": "watch sensors and get notified when a street floods",
            "tags": ["flood", "watch"],
            "examples": ["Tell me when any sensor in 11211 floods again."],
        },
    ],
}

WATCH_METADATA = {"watch": {"kind": "flood-watch", "sensor_ids": ["BK-x-1"], "observed": {"last_start": ""}}}


class FakeA2A:
    """Records every call and answers like a small A2A server."""

    def __init__(self, mode: str = "complete") -> None:
        self.mode = mode
        self.stream_calls: list[dict] = []
        self.push_configs: list[dict] = []
        self.task_id = "task-1"
        self.server: ThreadingHTTPServer | None = None
        self.base = ""

    def watch_metadata(self) -> dict:
        """Only watch-capable requests ('watch …') get a watch spec, like a real server."""
        if self.mode == "no_watch" or not self.stream_calls:
            return {}
        parts = (self.stream_calls[-1].get("message") or {}).get("parts") or []
        text = " ".join(part.get("text", "") for part in parts).lower()
        return WATCH_METADATA if "watch" in text else {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeA2A":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/.well-known/agent"):
                    self._json(200, CARD)
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode())
                method = payload.get("method")
                params = payload.get("params") or {}
                if method == "message/stream":
                    fake.stream_calls.append(params)
                    self._sse(payload["id"])
                elif method == "tasks/get":
                    self._json(200, {"jsonrpc": "2.0", "id": payload["id"], "result": {
                        "kind": "task", "id": fake.task_id, "contextId": "ctx-1",
                        "status": {"state": "completed", "timestamp": "2026-09-22T00:00:00Z"},
                        "metadata": fake.watch_metadata()}})
                elif method == "tasks/pushNotificationConfig/set":
                    fake.push_configs.append(params)
                    self._json(200, {"jsonrpc": "2.0", "id": payload["id"], "result": params})
                else:
                    self._json(200, {"jsonrpc": "2.0", "id": payload["id"],
                                     "error": {"code": -32601, "message": "nope"}})

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _sse(self, request_id):
                events = [
                    {"kind": "task", "id": fake.task_id, "contextId": "ctx-1", "status": {"state": "submitted"},
                     "parts": None, "taskId": fake.task_id},
                    {"kind": "status-update", "taskId": fake.task_id, "contextId": "ctx-1",
                     "status": {"state": "working"}, "final": False},
                    {"kind": "artifact-update", "taskId": fake.task_id, "contextId": "ctx-1",
                     "artifact": {"name": "flood-recent", "parts": [{"kind": "data",
                                "data": {"dataset": "aq7i-eu5q", "count": 1}}]}},
                    {"kind": "status-update", "taskId": fake.task_id, "contextId": "ctx-1",
                     "status": {"state": "input-required" if fake.mode == "input_required" else "completed",
                                "message": {"kind": "message", "role": "agent", "messageId": "m",
                                            "parts": [{"kind": "text",
                                                       "text": "Flooding: BK - Richardson St/N 11th St at 2.36 in."}]}},
                     "final": True},
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for event in events:
                    line = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": event})
                    self.wfile.write(f"data: {line}\n\n".encode())
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


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


class BridgeTestCase(unittest.TestCase):
    mode = "complete"

    def setUp(self):
        self.fake = FakeA2A(self.mode).start()
        self.addCleanup(self.fake.stop)
        self.clients: list[AcpClient] = []
        self.agents: list[BridgeAgent] = []

    def connect(self, permission: str = "allow-once") -> tuple[BridgeAgent, AcpClient, str]:
        agent_conn, client_conn = connected_pair()
        agent = BridgeAgent({"nycflood": self.fake.base}, agent_conn, push_port=0)
        self.agents.append(agent)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        self.clients.append(client)
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        return agent, client, session_id

    def tearDown(self):
        for client in self.clients:
            client.stop()
        for agent in self.agents:
            agent.stop_push_listener()


class EndpointTests(unittest.TestCase):
    def test_parse_endpoints(self):
        self.assertEqual(
            parse_endpoints("nyc311=http://127.0.0.1:8787, nycflood=http://127.0.0.1:8788"),
            {"nyc311": "http://127.0.0.1:8787", "nycflood": "http://127.0.0.1:8788"},
        )
        self.assertEqual(parse_endpoints("http://host:1234"), {"host:1234": "http://host:1234"})
        self.assertEqual(parse_endpoints("https://example.com/agents/flood"), {"flood": "https://example.com/agents/flood"})
        self.assertEqual(parse_endpoints(""), {})

    def test_remote_skill_scoring(self):
        skill = RemoteSkill("nycflood", "http://x", CARD["skills"][0])
        self.assertEqual(skill.label, "nycflood:flood-recent")
        self.assertGreater(skill.score("which streets flooded in 11211?"), 0)
        self.assertEqual(skill.score("totally unrelated words"), 0)


class BridgeTurnTests(BridgeTestCase):
    def test_plain_text_routes_and_relays(self):
        _, client, session_id = self.connect()
        turn = client.prompt("which streets flooded in the last 30 days?", session_id)
        self.assertEqual(turn["stopReason"], STOP_END_TURN)
        self.assertIn("2.36 in", turn["text"])
        self.assertEqual(len(self.fake.stream_calls), 1)
        tools = [u for u in turn["updates"] if u.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "flood-recent")
        self.assertEqual(tools[0]["rawInput"]["endpoint"], self.fake.base)
        self.assertEqual(len(client.permission_requests), 1)

    def test_catalog_on_no_match(self):
        _, client, session_id = self.connect()
        turn = client.prompt("zzzz qqqq", session_id)
        self.assertIn("nycflood:flood-recent", turn["text"])
        self.assertIn("nycflood:flood-watch", turn["text"])

    def test_explicit_skill_name(self):
        _, client, session_id = self.connect()
        client.prompt("nycflood:flood-recent — what happened?", session_id)
        self.assertEqual(len(self.fake.stream_calls), 1)

    def test_skills_listing_without_a1_call(self):
        _, client, session_id = self.connect()
        turn = client.prompt("skills", session_id)
        self.assertIn("Remote A2A skills", turn["text"])
        self.assertEqual(self.fake.stream_calls, [])

    def test_push_registration_and_relay(self):
        _, client, session_id = self.connect()
        client.prompt("watch 11211", session_id)
        self.assertEqual(len(self.fake.push_configs), 1)
        webhook = self.fake.push_configs[0]["pushNotificationConfig"]["url"]
        self.assertIn("/a2a-push", webhook)

        client.updates.clear()
        notification = {
            "kind": "status-update",
            "taskId": self.fake.task_id,
            "status": {"state": "working",
                       "message": {"kind": "message", "role": "agent", "messageId": "n1",
                                   "parts": [{"kind": "text", "text": "Sensor BK-x-1 just flooded: 3.5 in."}]}},
            "final": False,
        }
        req = urllib.request.Request(webhook, data=json.dumps(notification).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-A2A-Notification-Token": "acp-bridge"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)

        for _ in range(50):
            if any("[remote watch]" in (update.get("content") or {}).get("text", "") for update in client.updates):
                break
            threading.Event().wait(0.05)
        relayed = [u for u in client.updates if u.get("sessionUpdate") == "agent_message_chunk"
                   and "[remote watch]" in (u.get("content") or {}).get("text", "")]
        self.assertEqual(len(relayed), 1)
        self.assertIn("3.5 in", relayed[0]["content"]["text"])

    def test_permission_denied_blocks_the_call(self):
        _, client, session_id = self.connect(permission="reject")
        turn = client.prompt("which streets flooded?", session_id)
        self.assertEqual(self.fake.stream_calls, [])
        self.assertIn("need permission", turn["text"])


class BridgeNoWatchTests(BridgeTestCase):
    mode = "no_watch"

    def test_no_push_registration_without_a_watch(self):
        _, client, session_id = self.connect()
        client.prompt("watch 11211", session_id)
        self.assertEqual(self.fake.push_configs, [])
        self.assertEqual(len(client.permission_requests), 1)  # only the call permission


class BridgeContinuationTests(BridgeTestCase):
    mode = "input_required"

    def test_follow_up_continues_the_same_task(self):
        _, client, session_id = self.connect()
        first = client.prompt("watch for flooding", session_id)
        self.assertIn("needs more information", first["text"])
        self.assertEqual(len(self.fake.stream_calls), 1)
        client.prompt("11211", session_id)
        self.assertEqual(len(self.fake.stream_calls), 2)
        second = self.fake.stream_calls[1]
        self.assertEqual(second["message"]["taskId"], self.fake.task_id)


if __name__ == "__main__":
    unittest.main()
