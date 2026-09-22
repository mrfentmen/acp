# acp — Agent Client Protocol agents for things that are not code

A stdlib-only ACP v1 toolkit plus three agents that plug into any ACP editor (Zed,
JetBrains, Neovim plugins, Obsidian — anything that can launch an ACP agent).

Every agent on the vendors' list is a coding agent. These three are not:

| Agent | What it is | Why it is new |
|---|---|---|
| [`agents/civic`](agents/civic) | Answers NYC civic questions from live public data (311 complaints, FloodNet street flooding, drinking water samples) inside your editor | The first non-coding ACP agent: no repository, no files, no model required — it reads public datasets and cites them |
| [`agents/hazards`](agents/hazards) | Weather alerts (NWS) and earthquakes (USGS) inside your editor, live | The first hazard-feed agent in any editor protocol: asks "is anything dangerous near X", answers from the two feeds governments actually publish, and never forecasts |
| [`agents/a2a_bridge`](agents/a2a_bridge) | Turns any A2A agent into something you can use from an ACP editor, and **relays A2A push notifications into the editor session** | Other bridges stop at request/response; this one keeps a watch alive after the turn ends, so "tell me when my street floods" arrives as an editor message |

```
editor (ACP client) --ACP--> agent --A2A--> public-data server
                                     <--POST-- push notification --+
```

## Quick start

```bash
# 1. Civic — ask it about New York City (no API key, no model, live public data)
python3 tools/probe.py --agent "python3 agents/civic/agent.py" \
    --prompt "did complaint 70483808 get fixed?" \
    --prompt "which streets flooded in the last 30 days?" \
    --prompt "how is the drinking water testing at site 55450?"

# 2. Hazards — ask an agency feed a direct question (no API key, no model)
python3 tools/probe.py --agent "python3 agents/hazards/agent.py" \
    --prompt "any weather alerts in NY right now?" \
    --prompt "any earthquakes above magnitude 4.5 in the last 24 hours?" \
    --prompt "has anything shaken near Tokyo this month?"

# 3. Bridge — needs A2A servers; point it at the ones in the sibling `a2a` repo
export ACP_A2A_ENDPOINTS="nyc311=http://127.0.0.1:8787,nycflood=http://127.0.0.1:8788,nycwater=http://127.0.0.1:8789"
python3 tools/probe.py --agent "python3 agents/a2a_bridge/bridge.py" \
    --prompt "skills" \
    --prompt "which streets flooded in the last 30 days?" \
    --prompt "tell me when 11211 floods again"
```

Real output from the second command (bridge → A2A → live FloodNet data → webhook):

```
--- prompt: which streets flooded in the last 30 days?
   PLAN [high] Call nycflood:flood-recent
   TOOL nycflood:flood-recent: which streets flooded in the last 30 days? (kind=fetch, status=pending)
   TOOL call_nycflood_flood-recent -> completed — 20 street-flooding event(s) recorded …
   STOP end_turn
   ANSWER:
     20 street-flooding event(s) recorded for New York City in the last 720 hours.
     Deepest: BX - Ditmars St/Hunter Ave 2 at 5.39 in, 18 days ago.

--- prompt: tell me when 11211 floods again
   TOOL call_nycflood_flood-watch -> completed — Watching 1 FloodNet sensor(s) for 11211 …
   PERMISSION asked: Watch this on the remote agent and notify me in this session?
   ANSWER:
     Watching 1 FloodNet sensor(s) for 11211. Latest recorded event: BK - Richardson St/N 11th St at 8.7 in, 65 days ago.
     Watch registered: the remote agent will POST to http://127.0.0.1:8790/a2a-push and I will relay it into this session.
```

### Using it in an editor

Zed (settings → external agents / `agent_servers`), JetBrains ACP support, or any ACP
client: give it the command and it works.

```jsonc
{
  "agent_servers": {
    "Civic": { "command": "python3", "args": ["/path/to/acp/agents/civic/agent.py"] },
    "A2A bridge": {
      "command": "python3",
      "args": ["/path/to/acp/agents/a2a_bridge/bridge.py"],
      "env": { "ACP_A2A_ENDPOINTS": "nyc311=http://127.0.0.1:8787" }
    }
  }
}
```

## Protocol coverage

| ACP v1 feature | Where |
|---|---|
| `initialize` with version + capability negotiation | `acp_kit/agent.py` |
| `session/new`, `session/load` (history replay), `session/close` | `acp_kit/agent.py` |
| `session/prompt` answered from a worker thread (deferred response) | `acp_kit/agent.py`, `acp_kit/rpc.py` (`DEFERRED`) |
| Streaming `session/update`: `plan`, `agent_message_chunk`, `tool_call`, `tool_call_update`, `usage_update` | `acp_kit/agent.py` `SessionContext` |
| `session/cancel` → the turn stops and answers `stopReason: "cancelled"` | `acp_kit/agent.py` |
| `session/request_permission` with allow-once / allow-always / reject | `acp_kit/agent.py` |
| Text, resource and resource_link prompt blocks | `acp_kit/agent.py` (`prompt_text`) |
| Newline-delimited JSON-RPC 2.0 over stdio | `acp_kit/rpc.py` |
| A working client (for tests, probes, demos) | `acp_kit/client.py` |

## Reading the code

```
acp_kit/            the toolkit: transport, agent base, client
  rpc.py            ndjson JSON-RPC 2.0, deferred responses, pending-request bookkeeping
  agent.py          ACP lifecycle + SessionContext (everything an agent may send)
  client.py         minimal client: spawn an agent, drive a turn, answer permissions
agents/civic/       datasets.py (NYC Open Data reader) · agent.py (routing + skills)
agents/hazards/     data.py (NWS + USGS readers) · agent.py (routing + skills)
agents/a2a_bridge/  a2a_client.py (A2A 0.3 client) · bridge.py (ACP agent + push relay)
tools/probe.py      drive an agent like an editor does, print every update
tests/              kit, civic and bridge tests (real in-memory ACP conversations)
```

## Tests

```bash
python3 tests/test_kit.py      # transport, lifecycle, permissions, cancel
python3 tests/test_civic.py    # routing, skills, honesty paths
python3 tests/test_bridge.py   # card routing, A2A calls, push relay (fake A2A server over real HTTP + SSE)
python3 tests/test_hazards.py  # NWS/USGS parsing, routing, honesty on quiet windows
```

69 tests. The bridge tests run against a fake A2A server that speaks the real wire
protocol (agent card, `message/stream` SSE, `tasks/get`, `tasks/pushNotificationConfig/set`).

## Configuration

| Variable | Used by | Purpose |
|---|---|---|
| `ACP_A2A_ENDPOINTS` | bridge | `name=url` pairs, comma separated |
| `HAZARDS_USER_AGENT` | hazards | Contactable User-Agent; NWS answers 403 without one |
| `HAZARDS_CACHE_TTL`, `HAZARDS_HTTP_TIMEOUT` | hazards | Feed caching and request timeout |
| `ACP_BRIDGE_PUSH_PORT` | bridge | Port for the webhook listener it registers with remote A2A servers (default 8790; `0` picks a free port) |
| `ACP_LOG_LEVEL` | both | Log level (logs go to stderr, never stdout — stdout is the ACP channel) |

The A2A servers must allow a loopback webhook for the watch relay to register
(`<PREFIX>_ALLOW_PRIVATE_WEBHOOKS=1`), which is exactly what local demos need.

## Honest limits

- **Civic is deterministic, not a language model.** It routes intents with rules and
  answers from public data. That is why it never invents a complaint status; it is
  also why it will not handle phrasing far outside the patterns in
  `agents/civic/agent.py`.
- **Civic reads, it never writes.** No filing complaints, no paying tickets.
- **Water data is per monitoring site.** DEP publishes site codes without
  coordinates, so the agent refuses to map a site to an address.
- **The bridge trusts the A2A cards it is pointed at.** It shows you the endpoint in
  the permission prompt before sending anything.
- **The push relay listens on localhost** and matches notifications by task id. It is
  a local developer tool: do not expose the port.
- **Hazards reads, it never forecasts.** NWS returns only alerts *currently in
  effect*, and USGS is a catalog of earthquakes that already happened, so a quiet
  answer means "nothing published right now" — not "nothing is coming". The agent
  says so in the text and does not play at being a warning system.

## License

MIT — see [LICENSE](LICENSE).
