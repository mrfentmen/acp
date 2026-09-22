#!/usr/bin/env python3
"""Probe an ACP agent the way an editor would: spawn it, initialize, prompt, print updates.

    python3 tools/probe.py --agent "python3 agents/civic/agent.py" \
        --prompt "did complaint 12345678 get fixed?"

Prints the initialize result, every tool call, every permission request, and the
agent's answer. Exits non-zero if the agent fails to answer.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import AcpClient  # noqa: E402  (path setup must come first)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="probe an ACP agent")
    parser.add_argument("--agent", required=True, help="command that starts the agent (ACP over stdio)")
    parser.add_argument("--prompt", action="append", default=[], help="repeatable")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--permission", default="allow-once", choices=["allow-once", "allow-always", "reject"])
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--show-server-log", action="store_true", help="let the agent's stderr through")
    args = parser.parse_args(argv)

    prompts = args.prompt or ["help"]
    client = AcpClient(
        command=shlex.split(args.agent),
        permission=args.permission,
        timeout=args.timeout,
        stderr=None if args.show_server_log else sys.stderr,
    )
    failures = 0
    try:
        client.start()
        info = client.initialize(name="acp-probe")
        agent_info = info.get("agentInfo", {})
        print(f"== initialized: {agent_info.get('title') or agent_info.get('name')} v{agent_info.get('version')}")
        print(f"   ACP protocolVersion={info.get('protocolVersion')} capabilities={json.dumps(info.get('agentCapabilities'))}")
        session_id = client.new_session(cwd=args.cwd)
        print(f"== session {session_id} (cwd {args.cwd})\n")

        for prompt in prompts:
            print(f"--- prompt: {prompt}")
            turn = client.prompt(prompt, session_id)
            for update in turn["updates"]:
                kind = update.get("sessionUpdate")
                if kind == "plan":
                    for entry in update.get("entries", []):
                        print(f"   PLAN [{entry.get('priority')}] {entry.get('content')}")
                elif kind == "tool_call":
                    print(f"   TOOL {update.get('title')} (kind={update.get('kind')}, status={update.get('status')})")
                elif kind == "tool_call_update" and update.get("status"):
                    detail = ""
                    for block in update.get("content") or []:
                        if block.get("type") == "content":
                            detail = " — " + str(block.get("content", {}).get("text", ""))[:80]
                    print(f"   TOOL {update.get('toolCallId')} -> {update['status']}{detail}")
            for request in client.permission_requests:
                print(f"   PERMISSION asked: {request.get('toolCall', {}).get('title')}")
            client.permission_requests.clear()
            print(f"   STOP {turn['stopReason']}")
            print("   ANSWER:")
            for line in (turn["text"] or "(no text)").splitlines():
                print(f"     {line}")
            print()
            if not turn["text"]:
                failures += 1
    except Exception as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.stop()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
