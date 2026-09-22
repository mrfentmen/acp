"""Minimal ACP client — the editor half, for demos, probes and tests.

    client = AcpClient(command=["python3", "agents/civic/civic_acp.py"])
    client.start()
    client.initialize()
    session_id = client.new_session(cwd="/tmp")
    turn = client.prompt("Did complaint 12345678 get fixed?")
    for update in turn["updates"]: ...

It answers session/request_permission according to `permission`, records every
session/update notification it receives and every permission request it was asked.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading

from .rpc import Connection, JsonRpcError

log = logging.getLogger("acp.client")

PERMISSION_OPTIONS = {
    "allow-once": "allow-once",
    "allow-always": "allow-always",
    "reject": "reject-once",
    "reject-once": "reject-once",
}


class AcpClient:
    def __init__(self, command: list[str] | None = None, connection: Connection | None = None,
                 permission: str = "allow-once", timeout: float = 60.0, stderr=None) -> None:
        self.command = command
        self.permission = PERMISSION_OPTIONS.get(permission, "allow-once")
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self.conn = connection
        self.updates: list[dict] = []
        self.permission_requests: list[dict] = []
        self.notifications: list[str] = []
        self._thread: threading.Thread | None = None
        self._stderr = stderr
        if connection is not None:
            self._wire()
        self.initialized_payload: dict = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "AcpClient":
        if self.conn is not None:
            self._thread = threading.Thread(target=self.conn.serve, name="acp-client-reader", daemon=True)
            self._thread.start()
            return self
        if not self.command:
            raise ValueError("either command or connection is required")
        executable = shutil.which(self.command[0])
        if executable is None:
            raise FileNotFoundError(f"command not found: {self.command[0]}")
        self.process = subprocess.Popen(
            [executable, *self.command[1:]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            bufsize=1,
        )
        self.conn = Connection(self.process.stdout, self.process.stdin, name="acp-client")
        self._wire()
        self._thread = threading.Thread(target=self.conn.serve, name="acp-client-reader", daemon=True)
        self._thread.start()
        return self

    def _wire(self) -> None:
        self.conn.set_notification_handler(self._on_notification)
        self.conn.set_request_handler(self._on_request)

    def stop(self) -> int | None:
        if self.process is not None:
            try:
                self.process.stdin.close()
            except Exception:
                pass
            try:
                return self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                return self.process.wait(timeout=10)
        if self.conn is not None:
            self.conn.close()
        return None

    def __enter__(self) -> "AcpClient":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- inbound -----------------------------------------------------------

    def _on_notification(self, method: str, params: dict) -> None:
        self.notifications.append(method)
        if method == "session/update":
            self.updates.append(params.get("update") or {})

    def _on_request(self, method: str, params: dict, request_id):
        if method != "session/request_permission":
            raise JsonRpcError(-32601, f"client does not implement {method}")
        self.permission_requests.append(params)
        return {"outcome": {"outcome": "selected", "optionId": self.permission}}

    # -- protocol calls ----------------------------------------------------

    def initialize(self, name: str = "acp-client", version: str = "1.0.0",
                   capabilities: dict | None = None, protocol_version: int = 1) -> dict:
        self.initialized_payload = self.conn.request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "clientCapabilities": capabilities if capabilities is not None else {"fs": {"readTextFile": True, "writeTextFile": True}},
                "clientInfo": {"name": name, "title": name, "version": version},
            },
            timeout=self.timeout,
        )
        return self.initialized_payload

    def new_session(self, cwd: str, mcp_servers: list[dict] | None = None) -> str:
        result = self.conn.request("session/new", {"cwd": cwd, "mcpServers": mcp_servers or []}, timeout=self.timeout)
        return result["sessionId"]

    def load_session(self, session_id: str, cwd: str) -> dict:
        return self.conn.request("session/load", {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
                                 timeout=self.timeout)

    def close_session(self, session_id: str) -> dict:
        return self.conn.request("session/close", {"sessionId": session_id}, timeout=self.timeout)

    def cancel(self, session_id: str) -> None:
        self.conn.notify("session/cancel", {"sessionId": session_id})

    def prompt(self, text: str, session_id: str | None = None, blocks: list[dict] | None = None) -> dict:
        """Send a prompt turn and return {"stopReason", "updates", "text"} for just this turn."""
        if session_id is None:
            raise ValueError("prompt() needs a session_id (call new_session first)")
        prompt_blocks = blocks if blocks is not None else [{"type": "text", "text": text}]
        self.updates = []
        result = self.conn.request(
            "session/prompt", {"sessionId": session_id, "prompt": prompt_blocks}, timeout=self.timeout
        )
        text = "".join(
            update.get("content", {}).get("text", "")
            for update in self.updates
            if update.get("sessionUpdate") == "agent_message_chunk"
        )
        return {"stopReason": result.get("stopReason"), "updates": list(self.updates), "text": text}

    def tool_calls(self) -> list[dict]:
        return [update for update in self.updates if update.get("sessionUpdate") == "tool_call"]

    def text(self) -> str:
        return "".join(
            update.get("content", {}).get("text", "")
            for update in self.updates
            if update.get("sessionUpdate") == "agent_message_chunk"
        )
