"""Civic — a non-coding ACP agent for New York City public data.

Every ACP agent on the vendors' list is a coding agent. This one answers civic
questions inside any ACP editor (Zed, JetBrains, Neovim plugins) from live NYC Open
Data: 311 complaints, FloodNet street flooding, drinking water samples.

It is deliberately model-free: intent routing is deterministic, every answer cites
its dataset id and freshness, and nothing is invented. It reports a plan, opens a
tool call per lookup, asks the client for permission before its first read of a
public dataset, streams the answer, and closes the tool call - the whole ACP
surface, used for something other than code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from datasets import CivicData, CivicDataError  # noqa: E402

HELP = (
    "I read New York City public data. Ask me things like:\n"
    "  • did complaint 12345678 get fixed?\n"
    "  • what 311 complaints came in around 11235 this month?\n"
    "  • which streets flooded in the last 30 days?\n"
    "  • how is the drinking water testing at site 55450?\n"
    "I only read public datasets; I never file a complaint or pay a ticket."
)

PERMISSION_KEY = "civic-read-public-data"

SKILLS = ("complaint-status", "complaints-near", "flood-recent", "water-quality", "help")

_ID_RE = re.compile(r"\b(\d{6,9})\b")
_ZIP_RE = re.compile(r"\b(1[01]\d{3})\b")
_WINDOW_RE = re.compile(r"\blast\s+(\d{1,3})\s*(hour|hours|day|days|week|weeks)\b", re.IGNORECASE)
_WINDOW_HOURS = {"hour": 1, "hours": 1, "day": 24, "days": 24, "week": 168, "weeks": 168}
_PERIOD_RE = re.compile(r"\b(today|tonight|this week|last week|this month|last month)\b", re.IGNORECASE)
_PERIOD_HOURS = {"today": 24, "tonight": 24, "this week": 168, "last week": 168, "this month": 720, "last month": 720}
_SITE_RE = re.compile(r"\bsite\s+([A-Za-z0-9]{3,10})\b", re.IGNORECASE)
_FLOOD_WORDS = re.compile(r"\b(flood|flooded|flooding|floodnet)\b", re.IGNORECASE)
_WATER_WORDS = re.compile(r"\b(water|chlorine|turbidity|coliform|e\.? ?coli|drinking)\b", re.IGNORECASE)
_NEAR_WORDS = re.compile(r"\b(near|around|neighborhood|block|zip|nearby|area|complaints in)\b", re.IGNORECASE)
_311_WORDS = re.compile(r"\b(311|complaint|ticket about|pothole|noise|rat|rodent|heat|trash)\b", re.IGNORECASE)


def _window_hours(text: str) -> int | None:
    """Time window in hours from 'last 30 days', 'this week', 'today' … or None."""
    window = _WINDOW_RE.search(text)
    if window:
        return int(window.group(1)) * _WINDOW_HOURS[window.group(2).lower()]
    period = _PERIOD_RE.search(text)
    if period:
        return _PERIOD_HOURS[period.group(1).lower()]
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}
    hours = _window_hours(text)
    site = _SITE_RE.search(text)
    if site and _WATER_WORDS.search(text):
        return "water-quality", {"site": site.group(1), "days": max(1, min(hours // 24 if hours else 180, 730))}
    if _FLOOD_WORDS.search(text):
        zip_match = _ZIP_RE.search(text)
        return "flood-recent", {"hours": hours or 72, "zip_code": zip_match.group(1) if zip_match else None}
    if _WATER_WORDS.search(text):
        return "water-quality", {"site": None, "days": max(1, min((hours or 180 * 24) // 24, 730))}
    key = _ID_RE.search(text)
    if key:
        return "complaint-status", {"unique_key": key.group(1)}
    zip_match = _ZIP_RE.search(text)
    if zip_match:
        return "complaints-near", {"zip_code": zip_match.group(1), "days": 30}
    if _311_WORDS.search(text) or _NEAR_WORDS.search(text):
        return "complaints-near", {"zip_code": None, "days": 30}
    return "help", {}


class CivicAgent(AcpAgent):
    name = "civic"
    title = "Civic — NYC public data agent"
    version = "1.0.0"

    def __init__(self, connection=None, data: CivicData | None = None) -> None:
        super().__init__(connection)
        self.data = data or CivicData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Civic session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([(f"Route the request ({skill})", "high"), ("Read the public dataset", "medium"),
                  ("Answer with the dataset id and freshness", "medium")])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message(self._active_context(text))
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read NYC Open Data for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Civic to read public NYC Open Data?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public dataset before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params, ctx)
        except (CivicDataError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the dataset: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Nothing long-running to interrupt: the skill calls are single HTTP reads.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict, ctx: SessionContext) -> tuple[str, dict]:
        if skill == "complaint-status":
            if not params.get("unique_key"):
                return (
                    "Which complaint? Give me the 311 unique key (6–9 digits, shown on your 311 report).",
                    {"summary": "no complaint key provided", "dataset": None},
                )
            key = params["unique_key"]
            row = self.data.complaint(key)
            freshness = self.data.freshness("311")
            if row is None:
                return (
                    f"No 311 complaint with key {key} exists in dataset erm2-nwe9. Double-check the "
                    "number on the 311 site.",
                    {"summary": f"erm2-nwe9: no row for {key}", "dataset": "erm2-nwe9", "freshness": freshness},
                )
            line = (
                f"Complaint {key}: {row.get('complaint_type')}"
                + (f" ({row['descriptor']})" if row.get("descriptor") else "")
                + f" — status {row.get('status')}."
            )
            if row.get("created_date"):
                line += f" Filed {row['created_date'][:10]}."
            if row.get("closed_date"):
                line += f" Closed {row['closed_date'][:10]}."
            if row.get("resolution_description"):
                line += f"\nResolution: {row['resolution_description']}"
            if row.get("street_name"):
                line += f"\nLocation: {row.get('street_name')}, {row.get('incident_zip') or row.get('borough') or ''}".rstrip(", ")
            line += f"\n\nSource: NYC Open Data erm2-nwe9 (updated {freshness or 'unknown'})."
            return line, {"summary": line.split("\n")[0], "dataset": "erm2-nwe9", "freshness": freshness, "complaint": row}

        if skill == "complaints-near":
            if not params.get("zip_code"):
                return (
                    "Which ZIP code? I can list the most recent 311 complaints reported there.",
                    {"summary": "no ZIP provided", "dataset": None},
                )
            zip_code = params["zip_code"]
            days = int(params.get("days", 30))
            rows = self.data.complaints_near(zip_code, days=days, limit=10)
            freshness = self.data.freshness("311")
            if not rows:
                return (
                    f"No 311 complaints were reported in {zip_code} in the last {days} days.",
                    {"summary": f"erm2-nwe9: 0 rows for {zip_code} in {days} days", "dataset": "erm2-nwe9",
                     "freshness": freshness},
                )
            counts: dict[str, int] = {}
            for row in rows:
                counts[row.get("complaint_type", "Unknown")] = counts.get(row.get("complaint_type", "Unknown"), 0) + 1
            listing = "\n".join(
                f"  • {row.get('complaint_type')} — {row.get('status')} ({row.get('created_date', '')[:10]})"
                for row in rows[:10]
            )
            answer = (
                f"Most recent 311 complaints in {zip_code} (last {days} days, {len(rows)} shown):\n{listing}\n\n"
                f"Types: " + ", ".join(f"{name} ×{count}" for name, count in sorted(counts.items(), key=lambda kv: -kv[1]))
                + f"\n\nSource: NYC Open Data erm2-nwe9 (updated {freshness or 'unknown'})."
            )
            return answer, {"summary": f"erm2-nwe9: {len(rows)} rows for {zip_code}", "dataset": "erm2-nwe9",
                            "freshness": freshness, "complaints": rows}

        if skill == "flood-recent":
            hours = int(params.get("hours", 72))
            zip_code = params.get("zip_code")
            rows = self.data.flood_recent(hours=hours, zip_code=zip_code, limit=10)
            freshness = self.data.freshness("flood")
            where = f" in {zip_code}" if zip_code else " citywide"
            if not rows:
                return (
                    f"No street-flooding events were recorded{where} in the last {hours} hours. FloodNet "
                    "publishes completed events only, so a dry window means no event was recorded, not "
                    "that no street got wet.",
                    {"summary": f"aq7i-eu5q: 0 events{where} in {hours}h", "dataset": "aq7i-eu5q", "freshness": freshness},
                )
            listing = "\n".join(
                f"  • {row.get('sensor_name')} — {row.get('max_depth_inches')} in for {row.get('duration_mins')} min "
                f"starting {str(row.get('flood_start_time'))[:16].replace('T', ' ')}"
                for row in rows[:10]
            )
            answer = (
                f"{len(rows)} street-flooding event(s){where} in the last {hours} hours:\n{listing}\n\n"
                f"Source: NYC Open Data aq7i-eu5q (updated {freshness or 'unknown'})."
            )
            return answer, {"summary": f"aq7i-eu5q: {len(rows)} events{where} in {hours}h", "dataset": "aq7i-eu5q",
                            "freshness": freshness, "events": rows}

        if skill == "water-quality":
            site = params.get("site")
            days = int(params.get("days", 180))
            if not site:
                return (
                    "Which drinking-water monitoring site? Site codes look like 55450 or 1S03A. The dataset "
                    "publishes site codes without addresses, so I cannot look up a house.",
                    {"summary": "no water site provided", "dataset": None},
                )
            samples, summary = self.data.water_quality(site, days=days)
            freshness = self.data.freshness("water")
            if not samples:
                return (
                    f"No drinking water samples are published for site {site} in the last {days} days.",
                    {"summary": f"bkwf-xfky: 0 samples for {site}", "dataset": "bkwf-xfky", "freshness": freshness},
                )
            chlorine = summary["chlorine_mg_l"]
            answer = (
                f"Site {site}: {summary['samples']} sample(s) from {summary['first_sample_date']} to "
                f"{summary['last_sample_date']}.\n"
                f"  • free chlorine {chlorine['min']}–{chlorine['max']} mg/L\n"
                f"  • highest turbidity {summary['turbidity_ntu_max']} NTU\n"
                f"  • coliform detections {summary['coliform_detections']} of {summary['coliform_samples']} samples\n"
                f"  • E. coli detections {summary['e_coli_detections']} of {summary['e_coli_samples']} samples\n\n"
                "These are raw DEP monitoring records, not a health ruling. For a water problem in a "
                f"building, call 311.\n\nSource: NYC Open Data bkwf-xfky (updated {freshness or 'unknown'})."
            )
            return answer, {"summary": f"bkwf-xfky: {summary['samples']} samples for {site}",
                            "dataset": "bkwf-xfky", "freshness": freshness, "water": summary}

        return HELP, {"summary": "help", "dataset": None}

    def _active_context(self, text: str) -> str:
        """A one-line note about what the datasets can and cannot answer."""
        if _WATER_WORDS.search(text):
            return "Note: the water dataset identifies monitoring sites by code and carries no coordinates, so I cannot map a site to an address."
        if _FLOOD_WORDS.search(text):
            return "Note: FloodNet reports completed flood events, so answers always state the time window they looked at."
        return "Note: 311 is read-only here — I can report on complaints, not file them."


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        CivicAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
