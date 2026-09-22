"""The agent half of ACP v1.

An agent is a program that an editor launches as a subprocess. This module owns the
protocol: initialize, session/new, session/load, session/prompt, session/cancel,
session/close, streaming session/update notifications, and
session/request_permission. Subclasses answer prompts (see the agents/ directory).

Wire shapes come from agentclientprotocol.com/protocol/v1 (read 2026-09-22).
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid

from .rpc import DEFERRED, Connection, JsonRpcError

log = logging.getLogger("acp.agent")

PROTOCOL_VERSION = 1

#: stopReason values defined by ACP v1
STOP_END_TURN = "end_turn"
STOP_MAX_TOKENS = "max_tokens"
STOP_MAX_TURN_REQUESTS = "max_turn_requests"
STOP_REFUSAL = "refusal"
STOP_CANCELLED = "cancelled"


class Cancelled(Exception):
    """Raised inside a prompt turn once the client sends session/cancel."""


def text_of(content_block: dict) -> str:
    """Text of one content block, or "" for blocks that carry no plain text."""
    kind = content_block.get("type")
    if kind == "text":
        return content_block.get("text") or ""
    if kind == "resource":
        resource = content_block.get("resource") or {}
        return resource.get("text") or ""
    if kind == "resource_link":
        return content_block.get("name") or content_block.get("uri") or ""
    return ""


def prompt_text(prompt: list[dict]) -> str:
    return "\n".join(filter(None, (text_of(block) for block in prompt or []))).strip()


class Session:
    """Server-side conversation state for one session id."""

    def __init__(self, session_id: str, cwd: str) -> None:
        self.id = session_id
        self.cwd = cwd
        self.mcp_servers: list[dict] = []
        self.history: list[dict] = []  # [{"role": "user"|"agent", "text": str}]
        self.tool_calls: dict[str, dict] = {}
        self.allowed: set[str] = set()  # permission remember-keys approved with allow_always
        self.cancel = threading.Event()
        self.lock = threading.RLock()

    def remember(self, role: str, text: str) -> None:
        with self.lock:
            self.history.append({"role": role, "text": text})


class SessionContext:
    """Session-scoped helpers handed to AcpAgent.prompt(): everything it may send."""

    def __init__(self, agent: "AcpAgent", session: Session) -> None:
        self.agent = agent
        self.session = session
        self.conn = agent.conn

    # -- inbound text ------------------------------------------------------

    @property
    def last_prompt(self) -> str:
        with self.session.lock:
            for entry in reversed(self.session.history):
                if entry["role"] == "user":
                    return entry["text"]
        return ""

    @property
    def client_capabilities(self) -> dict:
        return self.agent.client_capabilities

    def check_cancelled(self) -> None:
        if self.session.cancel.is_set():
            raise Cancelled()

    # -- outbound updates --------------------------------------------------

    def _update(self, update: dict) -> None:
        self.conn.notify("session/update", {"sessionId": self.session.id, "update": update})

    def message(self, text: str, message_id: str | None = None) -> str:
        """Send one agent_message_chunk. Returns the messageId used."""
        message_id = message_id or f"msg_agent_{uuid.uuid4().hex[:8]}"
        self._update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": message_id,
                "content": {"type": "text", "text": text},
            }
        )
        return message_id

    def stream_text(self, text: str, chunk_size: int = 80) -> str:
        """Send text in chunks, checking for cancellation between them."""
        message_id = f"msg_agent_{uuid.uuid4().hex[:8]}"
        for start in range(0, max(len(text), 1), chunk_size):
            self.check_cancelled()
            self.message(text[start : start + chunk_size], message_id)
        return message_id

    def plan(self, entries: list[tuple[str, str]]) -> None:
        """entries: [(content, priority)] with priority in high/medium/low."""
        self._update(
            {
                "sessionUpdate": "plan",
                "entries": [{"content": content, "priority": priority, "status": "pending"} for content, priority in entries],
            }
        )

    def tool_call(self, tool_call_id: str, title: str, kind: str = "other", status: str = "pending",
                  name: str | None = None, content: list[dict] | None = None, raw_input=None,
                  locations: list[dict] | None = None) -> None:
        update = {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": title, "kind": kind, "status": status}
        if name:
            update["name"] = name
        if content:
            update["content"] = content
        if raw_input is not None:
            update["rawInput"] = raw_input
        if locations:
            update["locations"] = locations
        self.session.tool_calls[tool_call_id] = update
        self._update(update)

    def tool_call_update(self, tool_call_id: str, status: str | None = None, content: list[dict] | None = None,
                         title: str | None = None) -> None:
        update = {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id}
        if status:
            update["status"] = status
        if title:
            update["title"] = title
        if content is not None:
            update["content"] = content
        self._update(update)

    def usage(self, used: int, size: int, cost: dict | None = None) -> None:
        update = {"sessionUpdate": "usage_update", "used": used, "size": size}
        if cost:
            update["cost"] = cost
        self._update(update)

    @staticmethod
    def text_content(text: str) -> list[dict]:
        return [{"type": "content", "content": {"type": "text", "text": text}}]

    # -- permissions -------------------------------------------------------

    def ask_permission(self, tool_call_id: str, title: str, remember_key: str | None = None) -> bool:
        """Ask the client to approve a tool call. Returns True when allowed."""
        if remember_key and remember_key in self.session.allowed:
            return True
        options = [
            {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
            {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
        ]
        result = self.conn.request(
            "session/request_permission",
            {"sessionId": self.session.id, "toolCall": {"toolCallId": tool_call_id, "title": title}, "options": options},
            timeout=300,
        )
        outcome = (result or {}).get("outcome") or {}
        if outcome.get("outcome") != "selected":
            return False
        chosen = outcome.get("optionId")
        if chosen == "allow-always" and remember_key:
            self.session.allowed.add(remember_key)
        return chosen in ("allow-once", "allow-always")


class AcpAgent:
    """Base class: implement the domain hooks, get a working ACP agent."""

    name = "acp-agent"
    title = "ACP Agent"
    version = "1.0.0"
    supports_load = True
    supports_close = True
    supports_images = False
    supports_audio = False
    supports_embedded_context = True

    def __init__(self, connection: Connection | None = None) -> None:
        self.conn = connection or Connection(sys.stdin, sys.stdout, name=self.name)
        self.conn.set_request_handler(self._handle_request)
        self.conn.set_notification_handler(self._handle_notification)
        self.sessions: dict[str, Session] = {}
        self.initialized = False
        self.client_capabilities: dict = {}
        self.client_info: dict = {}
        self._sessions_lock = threading.RLock()

    # -- capabilities ------------------------------------------------------

    def agent_capabilities(self) -> dict:
        capabilities = {
            "loadSession": self.supports_load,
            "promptCapabilities": {
                "image": self.supports_images,
                "audio": self.supports_audio,
                "embeddedContext": self.supports_embedded_context,
            },
        }
        if self.supports_close:
            capabilities["sessionCapabilities"] = {"close": {}}
        return capabilities

    def auth_methods(self) -> list[dict]:
        return []

    # -- protocol ----------------------------------------------------------

    def _session(self, session_id: str) -> Session:
        with self._sessions_lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise JsonRpcError(-32602, f"unknown sessionId: {session_id}")
        return session

    def _handle_request(self, method: str, params: dict, request_id=None):
        if method == "initialize":
            return self._initialize(params)
        if not self.initialized:
            raise JsonRpcError(-32600, "initialize must be called before any other request")
        if method == "session/new":
            return self._new_session(params)
        if method == "session/load":
            return self._load_session(params)
        if method == "session/close":
            return self._close_session(params)
        if method == "session/prompt":
            session = self._session(params.get("sessionId", ""))
            session.cancel.clear()
            threading.Thread(
                target=self._run_prompt,
                args=(self.conn, session, params, request_id),
                name=f"prompt-{session.id[:8]}",
                daemon=True,
            ).start()
            return DEFERRED
        if method == "authenticate":
            return {}
        if method == "logout":
            return {}
        raise JsonRpcError(-32601, f"method not found: {method}")

    def _handle_notification(self, method: str, params: dict) -> None:
        if method == "session/cancel":
            try:
                session = self._session(params.get("sessionId", ""))
            except JsonRpcError:
                return
            session.cancel.set()
            self.on_cancel(session)

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        if isinstance(requested, int) and requested != PROTOCOL_VERSION:
            log.warning("client asked for ACP version %s; answering with %s", requested, PROTOCOL_VERSION)
        self.client_capabilities = params.get("clientCapabilities") or {}
        self.client_info = params.get("clientInfo") or {}
        self.initialized = True
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": self.agent_capabilities(),
            "agentInfo": {"name": self.name, "title": self.title, "version": self.version},
            "authMethods": self.auth_methods(),
        }

    def _new_session(self, params: dict) -> dict:
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise JsonRpcError(-32602, "cwd is required and must be an absolute path")
        session = Session(f"sess_{uuid.uuid4().hex[:12]}", cwd)
        session.mcp_servers = list(params.get("mcpServers") or [])
        with self._sessions_lock:
            self.sessions[session.id] = session
        extra = self.new_session(session) or {}
        result = {"sessionId": session.id}
        result.update(extra)
        return result

    def _load_session(self, params: dict) -> dict:
        if not self.supports_load:
            raise JsonRpcError(-32601, "loadSession is not supported")
        session = self._session(params.get("sessionId", ""))
        session.cancel.clear()
        self.load_session(session)
        with session.lock:
            history = list(session.history)
        for entry in history:
            self.conn.notify(
                "session/update",
                {
                    "sessionId": session.id,
                    "update": {
                        "sessionUpdate": "user_message_chunk" if entry["role"] == "user" else "agent_message_chunk",
                        "messageId": f"msg_replay_{uuid.uuid4().hex[:8]}",
                        "content": {"type": "text", "text": entry["text"]},
                    },
                },
            )
        return {}

    def _close_session(self, params: dict) -> dict:
        session = self._session(params.get("sessionId", ""))
        session.cancel.set()
        self.close_session(session)
        with self._sessions_lock:
            self.sessions.pop(session.id, None)
        return {}

    def _run_prompt(self, conn: Connection, session: Session, params: dict, request_id) -> None:
        ctx = SessionContext(self, session)
        prompt = params.get("prompt") or []
        text = prompt_text(prompt)
        session.remember("user", text)
        stop_reason = STOP_END_TURN
        try:
            stop_reason = self.prompt(ctx, prompt) or STOP_END_TURN
        except Cancelled:
            stop_reason = STOP_CANCELLED
        except JsonRpcError as exc:
            conn.respond_error(request_id, exc.code, exc.message)
            return
        except Exception as exc:  # never leave the client waiting forever
            log.exception("prompt turn failed")
            conn.respond_error(request_id, -32603, f"prompt failed: {exc}")
            return
        finally:
            session.cancel.clear()
        conn.respond(request_id, {"stopReason": stop_reason})

    # -- domain hooks ------------------------------------------------------

    def new_session(self, session: Session) -> dict:
        """Called after session/new; return extra fields for the result (optional)."""
        return {}

    def load_session(self, session: Session) -> None:
        """Called on session/load before history is replayed."""

    def close_session(self, session: Session) -> None:
        """Called when the client closes a session."""

    def on_cancel(self, session: Session) -> None:
        """Called when the client cancels; the prompt worker also sees Cancelled."""

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        """Handle one prompt turn. Return a stopReason."""
        raise NotImplementedError

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        """Serve stdin until the client closes it. Blocks."""
        self.announce()
        self.conn.serve()

    def announce(self) -> None:
        log.info("%s ready (ACP v%s)", self.name, PROTOCOL_VERSION)
