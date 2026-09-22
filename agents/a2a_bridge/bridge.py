"""Bridge — use any A2A agent from inside an ACP editor.

Point it at A2A servers and every skill on their agent cards becomes something you
can ask for in Zed, JetBrains, or any ACP client:

    export ACP_A2A_ENDPOINTS="nyc311=http://127.0.0.1:8787,nycflood=http://127.0.0.1:8788"
    zed / your ACP client -> command: python3 agents/a2a_bridge/bridge.py

What other bridges do not do: it also registers a webhook with the remote A2A server
for watch-capable skills and **relays A2A push notifications back into the editor
session** as they arrive, so "tell me when my street floods" keeps working after the
prompt turn is over.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from a2a_client import A2AClient, A2AError, artifact_data, final_event, text_parts  # noqa: E402

log = logging.getLogger("acp.bridge")

DEFAULT_PUSH_PORT = 8790


def parse_endpoints(raw: str) -> dict[str, str]:
    """'nyc311=http://host:8787,nycflood=http://host:8788' -> {"nyc311": "http://host:8787"}"""
    endpoints: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, url = chunk.split("=", 1)
            endpoints[name.strip()] = url.strip()
        else:
            endpoints[_name_for(chunk)] = chunk
    return endpoints


def _name_for(url: str) -> str:
    """Short label for a bare endpoint: its last path segment, else its host."""
    without_scheme = url.split("//", 1)[-1]
    host, _, path = without_scheme.partition("/")
    return path.rstrip("/").rsplit("/", 1)[-1] or host


class RemoteSkill:
    def __init__(self, agent_name: str, url: str, skill: dict) -> None:
        self.agent_name = agent_name
        self.url = url
        self.skill_id = skill.get("id") or "unknown"
        self.name = skill.get("name") or self.skill_id
        self.description = skill.get("description") or ""
        self.tags = [str(tag).lower() for tag in (skill.get("tags") or [])]
        self.examples = [str(example) for example in (skill.get("examples") or [])]

    @property
    def label(self) -> str:
        return f"{self.agent_name}:{self.skill_id}"

    def keywords(self) -> set[str]:
        words = {self.skill_id.lower(), self.agent_name.lower()}
        words.update(self.tags)
        words.update(word.strip(".,?()").lower() for word in self.description.split() if len(word) > 3)
        return words

    def score(self, text: str) -> int:
        lowered = text.lower()
        score = sum(3 for tag in self.tags if tag in lowered)
        score += sum(1 for word in self.keywords() if word in lowered)
        score += sum(4 for example in self.examples if example.lower()[:45] and example.lower()[:45] in lowered)
        return score


class BridgeAgent(AcpAgent):
    name = "a2a-bridge"
    title = "A2A bridge — remote agents in your editor"
    version = "1.0.0"

    def __init__(self, endpoints: dict[str, str], connection=None, push_host: str = "127.0.0.1",
                 push_port: int = DEFAULT_PUSH_PORT) -> None:
        super().__init__(connection)
        self.endpoints = endpoints
        self.clients = {name: A2AClient(url) for name, url in endpoints.items()}
        self.skills_cache: list[RemoteSkill] | None = None
        self.push_host = push_host
        self.push_port = push_port
        self.push_server: ThreadingHTTPServer | None = None
        self.push_url: str | None = None
        self._subscriptions: dict[str, str] = {}  # taskId -> sessionId
        self._session_tasks: dict[str, dict] = {}  # sessionId -> {label, taskId, contextId, state}
        self._push_lock = threading.RLock()

    # -- catalog -----------------------------------------------------------

    def refresh_skills(self) -> list[RemoteSkill]:
        skills: list[RemoteSkill] = []
        for name, client in self.clients.items():
            card = client.card(refresh=True)
            for skill in card.get("skills") or []:
                skills.append(RemoteSkill(name, client.base_url, skill))
        self.skills_cache = skills
        return skills

    def skills(self) -> list[RemoteSkill]:
        if self.skills_cache is None:
            return self.refresh_skills()
        return self.skills_cache

    def route(self, text: str) -> RemoteSkill | None:
        """Explicit 'agent:skill …' wins; otherwise the best keyword score."""
        lowered = text.lower()
        for skill in self.skills():
            if skill.skill_id.lower() in lowered and f"{skill.agent_name}:".lower() in lowered:
                return skill
        scored = [(skill.score(text), skill) for skill in self.skills()]
        scored = [(score, skill) for score, skill in scored if score > 0]
        if not scored:
            return None
        return max(scored, key=lambda pair: pair[0])[1]

    def catalog_text(self) -> str:
        lines = ["Remote A2A skills I can call:"]
        for skill in self.skills():
            lines.append(f"  • {skill.label} — {skill.name}: {skill.description.split('.')[0]}.")
        lines.append("Ask in plain words, or say 'nycflood:flood-watch …' to pick one exactly.")
        return "\n".join(lines)

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        try:
            count = len(self.skills())
        except A2AError as exc:
            session.remember("agent", f"Could not read the A2A agent cards: {exc}")
            return {}
        session.remember("agent", f"Bridge ready: {count} remote A2A skill(s) available.")
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        if not text.strip() or text.strip().lower() in ("help", "skills", "what can you do"):
            ctx.stream_text(self.catalog_text())
            return STOP_END_TURN

        # Continuing an input-required task beats starting a new one: the remote agent
        # asked a question and this turn is the answer.
        pending = self._session_tasks.get(ctx.session.id) or {}
        continuation: dict = {}
        skill = None
        if pending.get("state") == "input-required" and pending.get("taskId"):
            try:
                skill = next((entry for entry in self.skills() if entry.label == pending.get("label")), None)
            except A2AError as exc:
                ctx.message(f"I cannot reach the A2A agent cards: {exc}")
                return STOP_END_TURN
            if skill is not None:
                continuation = {"task_id": pending["taskId"], "context_id": pending.get("contextId")}
        if skill is None:
            try:
                skill = self.route(text)
            except A2AError as exc:
                ctx.message(f"I cannot reach the A2A agent cards: {exc}")
                return STOP_END_TURN
        if skill is None:
            ctx.message("I could not match that to a remote skill.\n\n" + self.catalog_text())
            return STOP_END_TURN

        ctx.plan([(f"Call {skill.label}", "high"), ("Relay the task events", "medium"),
                  ("Offer a push watch if the skill supports one", "low")])
        tool = f"call_{skill.agent_name}_{skill.skill_id}"
        ctx.tool_call(tool, f"{skill.label}: {text[:60]}", kind="fetch", name=skill.skill_id,
                      raw_input={"endpoint": skill.url, "skill": skill.skill_id, "message": text})
        if not ctx.ask_permission(
            tool, f"Allow this session to send tasks to the A2A agent at {skill.url}?",
            remember_key=f"a2a:{skill.url}",
        ):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission before I send a task to a remote agent.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        client = self.clients[skill.agent_name]
        try:
            events = client.stream(text, **continuation)
        except A2AError as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"The remote agent failed: {exc}")
            return STOP_END_TURN

        ctx.check_cancelled()
        status_events = [event for event in events if event.get("kind") == "status-update" and not event.get("final")]
        for event in status_events:
            note = text_parts((event.get("status") or {}).get("message"))
            if note:
                ctx.message(note + "\n")

        last = final_event(events)
        answer = text_parts((last.get("status") or {}).get("message")) or "The remote agent returned no summary."
        artifacts = [artifact_data(event) for event in events if event.get("kind") == "artifact-update"]
        summary = answer.split("\n")[0]
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(answer)

        state = (last.get("status") or {}).get("state")
        task_id = last.get("taskId")
        self._session_tasks[ctx.session.id] = {
            "label": skill.label,
            "taskId": task_id,
            "contextId": last.get("contextId"),
            "state": state,
        }
        if state == "input-required":
            ctx.message("\nThe remote agent needs more information — answer and I will continue the same task.")
        self._offer_watch(ctx, client, task_id, artifacts)
        return STOP_END_TURN

    # -- push relay --------------------------------------------------------

    def _offer_watch(self, ctx: SessionContext, client: A2AClient, task_id: str | None, artifacts: list[dict]) -> None:
        """If the remote task has a watch, offer to relay its push notifications into this session."""
        if not task_id:
            return
        watch = next((data.get("watch") for data in artifacts if data.get("watch")), None)
        if watch is None:
            try:
                watch = ((client.get_task(task_id).get("metadata") or {}).get("watch"))
            except A2AError as exc:
                log.info("could not read task metadata for %s: %s", task_id, exc)
                return
        if not watch:
            return
        if not ctx.ask_permission(
            f"watch_{task_id}", "Watch this on the remote agent and notify me in this session?",
            remember_key=f"watch:{client.base_url}",
        ):
            return
        try:
            url = self.start_push_listener()
            client.set_push_config(task_id, url, token="acp-bridge")
        except (A2AError, OSError) as exc:
            ctx.message(
                f"\nI could not register a webhook ({exc}). If the A2A server is on this machine, start it "
                "with its ALLOW_PRIVATE_WEBHOOKS=1 switch so it may call back to localhost."
            )
            return
        with self._push_lock:
            self._subscriptions[task_id] = ctx.session.id
        ctx.message(f"\nWatch registered: the remote agent will POST to {url} and I will relay it into this session.")

    def start_push_listener(self) -> str:
        if self.push_server is None:
            agent = self

            class Handler(BaseHTTPRequestHandler):
                server_version = "acp-a2a-bridge/1.0"

                def log_message(self, fmt, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length)
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        event = {}
                    agent.deliver_push(event)
                    body = b"{}"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            self.push_server = ThreadingHTTPServer((self.push_host, self.push_port), Handler)
            port = self.push_server.server_address[1]
            self.push_url = f"http://{self.push_host}:{port}/a2a-push"
            threading.Thread(target=self.push_server.serve_forever, name="bridge-push", daemon=True).start()
            log.info("relaying A2A push notifications on %s", self.push_url)
        return self.push_url

    def deliver_push(self, event: dict) -> bool:
        """Forward one A2A push notification into the editor session that asked for it."""
        task_id = event.get("taskId")
        with self._push_lock:
            session_id = self._subscriptions.get(task_id)
        if session_id is None:
            log.info("push for unwatched task %s ignored", task_id)
            return False
        text = text_parts((event.get("status") or {}).get("message")) or f"A2A task {task_id} changed."
        self.conn.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": f"msg_push_{task_id[:8]}",
                    "content": {"type": "text", "text": f"\n[remote watch] {text}\n"},
                },
            },
        )
        return True

    def stop_push_listener(self) -> None:
        if self.push_server is not None:
            self.push_server.shutdown()
            self.push_server.server_close()
            self.push_server = None


def main(argv=None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="ACP agent that bridges to A2A servers")
    parser.add_argument("--endpoints", default=os.environ.get("ACP_A2A_ENDPOINTS", ""),
                        help="name=url pairs, comma separated (or ACP_A2A_ENDPOINTS)")
    parser.add_argument("--push-port", type=int, default=int(os.environ.get("ACP_BRIDGE_PUSH_PORT", DEFAULT_PUSH_PORT)))
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("ACP_LOG_LEVEL", "INFO").upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    endpoints = parse_endpoints(args.endpoints)
    if not endpoints:
        print("no endpoints: pass --endpoints name=url or set ACP_A2A_ENDPOINTS", file=sys.stderr)
        return 2
    agent = BridgeAgent(endpoints, push_port=args.push_port)
    try:
        agent.run()
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop_push_listener()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
