"""Newline-delimited JSON-RPC 2.0 over stdio — the ACP transport.

ACP frames each JSON-RPC request, notification or response as a single line of
UTF-8 JSON terminated by "\\n" (agentclientprotocol.com/protocol/transports). This
module is that wire: one Connection is one side of the conversation.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
from typing import Any, Callable

log = logging.getLogger("acp.rpc")

#: Return this from a request handler when the response will be sent later
#: (for example, from the worker thread that runs a prompt turn).
DEFERRED = object()


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


class Connection:
    """One side of an ACP connection. Safe to use from several threads."""

    def __init__(self, reader, writer, name: str = "acp") -> None:
        self.reader = reader
        self.writer = writer
        self.name = name
        self._ids = itertools.count(1)
        self._pending: dict[Any, dict] = {}
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._request_handler: Callable[[str, dict, Any], Any] | None = None
        self._notification_handler: Callable[[str, dict], None] | None = None
        self._closed = threading.Event()

    # -- wiring ------------------------------------------------------------

    def set_request_handler(self, handler: Callable[[str, dict, Any], Any]) -> None:
        """handler(method, params, request_id) -> result, or DEFERRED to answer later."""
        self._request_handler = handler

    def set_notification_handler(self, handler: Callable[[str, dict], None]) -> None:
        self._notification_handler = handler

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # -- outbound ----------------------------------------------------------

    def _write(self, payload: dict) -> None:
        line = json.dumps(payload, separators=(",", ":"))
        with self._write_lock:
            if self._closed.is_set():
                raise JsonRpcError(-32603, "connection is closed")
            self.writer.write(line + "\n")
            self.writer.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def respond(self, request_id, result) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id, code: int, message: str, data: Any = None) -> None:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write({"jsonrpc": "2.0", "id": request_id, "error": error})

    def request(self, method: str, params: dict | None = None, timeout: float | None = None):
        """Send a request and block until its result arrives (or timeout)."""
        request_id = next(self._ids)
        event = threading.Event()
        slot: dict = {"event": event, "result": None, "error": None}
        with self._lock:
            self._pending[request_id] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        except JsonRpcError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"no response to {method} within {timeout}s")
        if slot["error"]:
            raise JsonRpcError(slot["error"]["code"], slot["error"].get("message", ""), slot["error"].get("data"))
        return slot["result"]

    # -- inbound -----------------------------------------------------------

    def serve(self) -> None:
        """Read messages until EOF. Blocks, so call it from a dedicated thread."""
        try:
            for line in self.reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("%s: dropping non-JSON line (%d bytes)", self.name, len(line))
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        finally:
            self._close_pending()

    def _close_pending(self) -> None:
        self._closed.set()
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["error"] = {"code": -32603, "message": "connection closed before a response arrived"}
            slot["event"].set()

    def _dispatch(self, message: dict) -> None:
        is_response = "id" in message and ("result" in message or "error" in message)
        if is_response:
            with self._lock:
                slot = self._pending.pop(message["id"], None)
            if slot is None:
                log.debug("%s: response for unknown request id %r", self.name, message["id"])
                return
            slot["result"] = message.get("result")
            slot["error"] = message.get("error")
            slot["event"].set()
            return

        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(method, str):
            return

        if "id" not in message:  # notification: never answered
            if self._notification_handler is not None:
                try:
                    self._notification_handler(method, params)
                except Exception:
                    log.exception("%s: notification handler failed for %s", self.name, method)
            return

        if self._request_handler is None:
            self.respond_error(message["id"], -32601, f"method not found: {method}")
            return
        try:
            result = self._request_handler(method, params, message["id"])
        except JsonRpcError as exc:
            self.respond_error(message["id"], exc.code, exc.message, exc.data)
            return
        except Exception as exc:  # a broken handler must not kill the connection
            log.exception("%s: request handler failed for %s", self.name, method)
            self.respond_error(message["id"], -32603, f"internal error: {exc}")
            return
        if result is DEFERRED:
            return
        self.respond(message["id"], result)

    def close(self) -> None:
        self._close_pending()
        with self._write_lock:
            try:
                self.writer.close()
            except Exception:
                pass
