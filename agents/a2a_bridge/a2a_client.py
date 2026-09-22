"""A2A client (0.3.0 JSON-RPC) for the bridge agent.

Reads a remote agent card, sends tasks, streams task events over SSE, and registers
webhook push configs. Stdlib only; every method returns plain dicts.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid

PROTOCOL_VERSION = "0.3.0"

CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")


class A2AError(RuntimeError):
    """The remote agent could not be reached or answered with an error."""


class A2AClient:
    def __init__(self, base_url: str, timeout: float = 45.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._card: dict | None = None
        self._lock = threading.Lock()

    # -- card --------------------------------------------------------------

    def card(self, refresh: bool = False) -> dict:
        with self._lock:
            if self._card is not None and not refresh:
                return self._card
        last_error: Exception | None = None
        for path in CARD_PATHS:
            try:
                with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as resp:
                    card = json.loads(resp.read().decode("utf-8"))
                if isinstance(card, dict) and card.get("name"):
                    with self._lock:
                        self._card = card
                    return card
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
                last_error = exc
        raise A2AError(f"no agent card at {self.base_url}: {last_error}")

    def skills(self) -> list[dict]:
        return list(self.card().get("skills") or [])

    # -- tasks -------------------------------------------------------------

    def _rpc(self, method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}).encode()
        req = urllib.request.Request(
            self.base_url + "/", data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise A2AError(f"{method} failed: {exc}") from exc
        if "error" in payload:
            raise A2AError(f"{method} error {payload['error'].get('code')}: {payload['error'].get('message')}")
        return payload.get("result") or {}

    @staticmethod
    def user_message(text: str, task_id: str | None = None, context_id: str | None = None) -> dict:
        message = {
            "kind": "message",
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        return message

    def send(self, text: str, task_id: str | None = None, context_id: str | None = None) -> dict:
        """message/send — returns the Task object."""
        return self._rpc("message/send", {"message": self.user_message(text, task_id, context_id)})

    def stream(self, text: str, task_id: str | None = None, context_id: str | None = None) -> list[dict]:
        """message/stream — returns the task events (task, status-update, artifact-update)."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "message/stream",
                "params": {"message": self.user_message(text, task_id, context_id)},
            }
        ).encode()
        req = urllib.request.Request(
            self.base_url + "/",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        events: list[dict] = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[6:])
                    if "result" in payload and isinstance(payload["result"], dict):
                        events.append(payload["result"])
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise A2AError(f"message/stream failed: {exc}") from exc
        if not events:
            raise A2AError("message/stream returned no events")
        return events

    def get_task(self, task_id: str) -> dict:
        """tasks/get — the task document, where A2A servers keep metadata like a watch spec."""
        return self._rpc("tasks/get", {"id": task_id})

    def set_push_config(self, task_id: str, url: str, token: str | None = None) -> dict:
        config: dict = {"url": url, "id": f"bridge-{uuid.uuid4().hex[:8]}"}
        if token:
            config["token"] = token
        return self._rpc("tasks/pushNotificationConfig/set", {"taskId": task_id, "pushNotificationConfig": config})


def artifact_data(event: dict) -> dict:
    return ((event.get("artifact") or {}).get("parts") or [{}])[0].get("data") or {}


def text_parts(message: dict | None) -> str:
    return " ".join(part.get("text", "") for part in ((message or {}).get("parts") or []) if part.get("kind") == "text")


def final_event(events: list[dict]) -> dict:
    for event in reversed(events):
        if event.get("final"):
            return event
    return events[-1]
