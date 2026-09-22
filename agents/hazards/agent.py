"""Hazards — weather alerts and earthquakes inside your editor.

The second non-coding ACP agent in this repo, and the first hazard-feed agent of any
kind: it answers "is anything dangerous happening near X" from the two live feeds
governments actually publish (NWS active alerts, USGS earthquake catalog), citing the
feed it read.

Deterministic on purpose: routing is rules, the numbers come from the agencies, and
nothing is guessed. It reports a plan, opens a tool call per lookup, asks permission
before its first read, streams the answer, and closes the tool call with a one-line
summary of what it read.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import NWS_DATASET, SEVERITY_ORDER, STATE_CODES, USGS_DATASET, HazardData, HazardDataError  # noqa: E402

HELP = (
    "I read two live hazard feeds: National Weather Service active alerts and the USGS\n"
    "earthquake catalog. Ask me things like:\n"
    "  • any weather alerts in NY right now?\n"
    "  • how many alerts are active nationwide?\n"
    "  • any earthquakes above magnitude 4.5 in the last 24 hours?\n"
    "  • has anything shaken near Tokyo this month?\n"
    "I only read what the agencies publish; I do not forecast and I do not warn you."
)

PERMISSION_KEY = "hazards-read-public-feeds"

SKILLS = ("alerts-active", "alerts-counts", "quakes-recent", "quakes-near", "help")

#: Approximate metro centres, so "near Tokyo" works without coordinates.
CITY_POINTS = {
    "anchorage": (61.22, -149.90),
    "athens": (37.98, 23.73),
    "christchurch": (-43.53, 172.64),
    "honolulu": (21.31, -157.86),
    "istanbul": (41.01, 28.98),
    "jakarta": (-6.21, 106.85),
    "kathmandu": (27.72, 85.32),
    "lima": (-12.05, -77.04),
    "los angeles": (34.05, -118.24),
    "manila": (14.60, 120.98),
    "mexico city": (19.43, -99.13),
    "new york": (40.71, -74.01),
    "osaka": (34.69, 135.50),
    "port-au-prince": (18.54, -72.34),
    "quito": (-0.18, -78.47),
    "reykjavik": (64.15, -21.94),
    "san francisco": (37.77, -122.42),
    "san salvador": (13.69, -89.19),
    "santiago": (-33.45, -70.67),
    "seattle": (47.61, -122.33),
    "taipei": (25.03, 121.57),
    "tehran": (35.69, 51.39),
    "tokyo": (35.68, 139.69),
    "wellington": (-41.29, 174.78),
}

#: Two-letter codes that are ordinary English words; never read as states in lowercase.
_AMBIGUOUS_CODES = frozenset(
    {"al", "as", "de", "hi", "id", "in", "la", "ma", "me", "mi", "mo", "ms", "ne", "oh", "ok", "or", "pa"}
)

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?)\b")
_MAG_RE = re.compile(r"\b(?:m|mag|magnitude)\s?(\d{1,2}(?:\.\d+)?)\b")
_MAG_PLUS_RE = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s*(?:\+|or\s+(?:higher|greater|above|more|bigger)\b)")
_WINDOW_RE = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3})\s*(hours?|days?|weeks?|months?|years?)\b")
_RADIUS_RE = re.compile(r"\b(\d{1,5})\s*(?:km|kms|kilometers?|kilometres?)\b")
_LOWER_STATE_RE = re.compile(r"\b(?:in|for|across|state of)\s+([a-z]{2})\b")
STATE_NAMES = {name.lower(): code for code, name in STATE_CODES.items()}

_ALERT_WORDS = re.compile(r"\b(alert|alerts|warning|warnings|advisory|watch|severe weather)\b", re.IGNORECASE)
_QUAKE_WORDS = re.compile(r"\b(earthquake|earthquakes|quake|quakes|seismic|tremor|shaking|shook|magnitude)\b", re.IGNORECASE)
_COUNT_WORDS = re.compile(r"\b(how many|count|counts|total|nationwide|summary)\b", re.IGNORECASE)
_NEAR_WORDS = re.compile(r"\b(near|around|within|close to|nearby)\b", re.IGNORECASE)
_EVENT_WORDS = {
    "tornado": "Tornado",
    "flood": "Flood",
    "flash flood": "Flash Flood",
    "thunderstorm": "Thunderstorm",
    "heat": "Heat",
    "wind": "Wind",
    "winter": "Winter",
    "snow": "Snow",
    "ice": "Ice",
    "hurricane": "Hurricane",
    "tropical": "Tropical",
    "fire": "Fire",
    "freeze": "Freeze",
    "fog": "Fog",
    "rip current": "Rip Current",
    "small craft": "Small Craft",
    "air quality": "Air Quality",
}
_SEVERITY_WORDS = {severity.lower(): severity for severity in SEVERITY_ORDER}
_WINDOW_HOURS = {"hour": 1, "hours": 1, "day": 24, "days": 24, "week": 168, "weeks": 168,
                 "month": 720, "months": 720, "year": 8760, "years": 8760}
_NAMED_WINDOWS = {"today": 24, "tonight": 24, "this week": 168, "last week": 168,
                  "this month": 720, "last month": 720, "this year": 8760, "last year": 8760}


def state_from_text(text: str) -> str | None:
    """An uppercase code ('NY'), a lowercase code after a preposition ('in ny'), or a state name."""
    for match in re.finditer(r"\b([A-Z]{2})\b", text):
        if match.group(1) in STATE_CODES:
            return match.group(1)
    lowered = text.lower()
    match = _LOWER_STATE_RE.search(lowered)
    if match and match.group(1) not in _AMBIGUOUS_CODES:
        return match.group(1).upper()
    for name, code in STATE_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return code
    return None


def detect_city(text_lower: str) -> str | None:
    for name in sorted(CITY_POINTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", text_lower):
            return name
    return None


def magnitude_from_text(text: str) -> float | None:
    match = _MAG_RE.search(text.lower()) or _MAG_PLUS_RE.search(text.lower())
    if not match:
        return None
    try:
        return HazardData.check_magnitude(match.group(1))
    except ValueError:
        return None


def human_hours(hours: float) -> str:
    """24 -> '24 hours', 168 -> '7 days'. Whole days read better, but never '1 day'."""
    if hours >= 48 and hours % 24 == 0:
        days = hours / 24
        return f"{days:g} day" + ("s" if days != 1 else "")
    return f"{hours:g} hour" + ("s" if hours != 1 else "")


def window_hours_from_text(text: str) -> int | None:
    lowered = text.lower()
    match = _WINDOW_RE.search(lowered)
    if match:
        return int(match.group(1)) * _WINDOW_HOURS[match.group(2)]
    for phrase, hours in _NAMED_WINDOWS.items():
        if phrase in lowered:
            return hours
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    lowered = text.lower()
    hours = window_hours_from_text(text)
    point_match = _POINT_RE.search(text)
    point = re.sub(r"\s+", "", point_match.group(1)) if point_match else None
    city = detect_city(lowered)
    if not point and city:
        latitude, longitude = CITY_POINTS[city]
        point = f"{latitude},{longitude}"
    magnitude = magnitude_from_text(text)

    if _QUAKE_WORDS.search(text) and not _ALERT_WORDS.search(text):
        if point:
            radius = _RADIUS_RE.search(lowered)
            return "quakes-near", {
                "point": point,
                "place": city,
                "radius_km": radius.group(1) if radius else 300,
                "hours": hours or 720,
                "min_magnitude": magnitude if magnitude is not None else 2.0,
            }
        return "quakes-recent", {"hours": hours or 24, "min_magnitude": magnitude if magnitude is not None else 2.5}

    state = state_from_text(text)
    if _ALERT_WORDS.search(text) or state:
        if _COUNT_WORDS.search(text):
            return "alerts-counts", {"state": state}
        severity = next((value for word, value in _SEVERITY_WORDS.items() if re.search(rf"\b{re.escape(word)}\b", lowered)), None)
        event = next((value for word, value in _EVENT_WORDS.items() if re.search(rf"\b{re.escape(word)}\b", lowered)), None)
        return "alerts-active", {"state": state, "severity": severity, "event_contains": event}

    if point and _NEAR_WORDS.search(text):
        return "quakes-near", {"point": point, "place": city, "radius_km": 300, "hours": hours or 720, "min_magnitude": 2.0}
    return "help", {}


class HazardsAgent(AcpAgent):
    name = "hazards"
    title = "Hazards — NWS alerts and USGS earthquakes"
    version = "1.0.0"

    def __init__(self, connection=None, data: HazardData | None = None) -> None:
        super().__init__(connection)
        self.data = data or HazardData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Hazards session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the live agency feed", "medium"),
            ("Answer with the feed id, counts and freshness", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message(self._active_context(text))
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read live hazard feeds for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Hazards to read the public NWS and USGS feeds?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public feeds before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (HazardDataError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the feed: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a single HTTP read; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "alerts-active":
            return self._alerts_active(params)
        if skill == "alerts-counts":
            return self._alerts_counts(params)
        if skill == "quakes-recent":
            return self._quakes_recent(params)
        if skill == "quakes-near":
            return self._quakes_near(params)
        return HELP, {"summary": "help", "dataset": None}

    def _alerts_active(self, params: dict) -> tuple[str, dict]:
        state = params.get("state")
        alerts = self.data.weather_alerts(
            state=state, severity=params.get("severity"), event_contains=params.get("event_contains"), limit=10
        )
        freshness = self.data.freshness("alerts")
        place = HazardData.place_label(state)
        filters = [name for name in (params.get("severity"), params.get("event_contains")) if name]
        filter_text = f" matching {' + '.join(filters)}" if filters else ""
        if not alerts:
            return (
                f"No active NWS alerts for {place}{filter_text} right now. (Read live from "
                f"{NWS_DATASET}, {freshness}.)",
                {"summary": f"{NWS_DATASET}: 0 active alerts for {place}", "dataset": NWS_DATASET,
                 "freshness": freshness, "place": place},
            )
        listing = "\n".join(
            f"  • [{alert.get('severity')}] {alert.get('event')} — {alert.get('areaDesc')}"
            + (f"\n      until {alert['ends']}" if alert.get("ends") else "")
            + (f"\n      {alert['instruction'][:160]}" if alert.get("instruction") else "")
            for alert in alerts[:6]
        )
        answer = (
            f"{len(alerts)} active NWS alert(s) for {place}{filter_text}, most severe first:\n{listing}\n\n"
            f"Source: {NWS_DATASET} (read live {freshness}). NWS is the authority; this is a read of "
            "their feed, not a forecast."
        )
        return answer, {"summary": f"{NWS_DATASET}: {len(alerts)} active alerts for {place}",
                        "dataset": NWS_DATASET, "freshness": freshness, "place": place}

    def _alerts_counts(self, params: dict) -> tuple[str, dict]:
        counts = self.data.alert_counts()
        freshness = self.data.freshness("alerts")
        state = params.get("state")
        in_state = int(counts["areas"].get(state, 0)) if state else None
        ranked = sorted(counts["areas"].items(), key=lambda item: -item[1])[:5]
        top = ", ".join(f"{code} {value}" for code, value in ranked)
        state_line = (
            f" {HazardData.place_label(state)} has {in_state}." if state else ""
        )
        answer = (
            f"NWS has {counts['total']} active alerts nationwide right now "
            f"({counts['land']} land, {counts['marine']} marine).{state_line}\n"
            f"Busiest areas: {top}.\n\n"
            f"Source: {NWS_DATASET}/count (read live {freshness})."
        )
        artifact = {"summary": f"{NWS_DATASET}: {counts['total']} alerts nationwide", "dataset": NWS_DATASET,
                    "freshness": freshness, "counts": counts}
        if in_state is not None:
            artifact["state_count"] = in_state
        return answer, artifact

    def _quakes_recent(self, params: dict) -> tuple[str, dict]:
        magnitude = float(params.get("min_magnitude", 2.5))
        hours = float(params.get("hours", 24))
        quakes = self.data.recent_quakes(min_magnitude=magnitude, hours=hours, limit=10)
        freshness = self.data.freshness("quakes")
        source = f"{USGS_DATASET} (read live {freshness})"
        window = human_hours(hours)
        if not quakes:
            return (
                f"No M{magnitude}+ earthquakes recorded worldwide in the last {window}. "
                f"Source: {source}.",
                {"summary": f"{USGS_DATASET}: 0 quakes M{magnitude}+ in {hours:g}h", "dataset": USGS_DATASET,
                 "freshness": freshness},
            )
        listing = "\n".join(self._quake_line(quake) for quake in quakes[:6])
        answer = (
            f"{len(quakes)} M{magnitude}+ earthquake(s) worldwide in the last {window}, newest first:\n"
            f"{listing}\n\nSource: {source}. USGS catalogs what already happened; it is not a forecast."
        )
        return answer, {"summary": f"{USGS_DATASET}: {len(quakes)} quakes M{magnitude}+ in {hours:g}h",
                        "dataset": USGS_DATASET, "freshness": freshness, "quakes": quakes}

    def _quakes_near(self, params: dict) -> tuple[str, dict]:
        point = params["point"]
        radius = float(params.get("radius_km", 300))
        magnitude = float(params.get("min_magnitude", 2.0))
        hours = float(params.get("hours", 720))
        quakes = self.data.quakes_near(point=point, radius_km=radius, min_magnitude=magnitude, hours=hours, limit=10)
        freshness = self.data.freshness("quakes")
        # Places arrive lowercased from the router ("tokyo"); present them properly.
        place = params["place"].title() if params.get("place") else f"the point {point}"
        window = human_hours(hours)
        if not quakes:
            return (
                f"No M{magnitude}+ earthquakes within {radius:g} km of {place} in the last {window}.\n\n"
                f"Source: {USGS_DATASET} (read live {freshness}). A quiet window is normal in most places; it "
                "is not a prediction either way.",
                {"summary": f"{USGS_DATASET}: 0 quakes near {place}", "dataset": USGS_DATASET,
                 "freshness": freshness, "place": place},
            )
        biggest = max(quakes, key=lambda quake: quake.get("magnitude") if quake.get("magnitude") is not None else -99)
        listing = "\n".join(self._quake_line(quake) for quake in quakes[:6])
        answer = (
            f"{len(quakes)} M{magnitude}+ earthquake(s) within {radius:g} km of {place} in the last {window}.\n"
            f"Largest: M{biggest.get('magnitude')} — {biggest.get('place')}.\n{listing}\n\n"
            f"Source: {USGS_DATASET} (read live {freshness})."
        )
        return answer, {"summary": f"{USGS_DATASET}: {len(quakes)} quakes near {place}", "dataset": USGS_DATASET,
                        "freshness": freshness, "place": place, "quakes": quakes}

    @staticmethod
    def _quake_line(quake: dict) -> str:
        magnitude = quake.get("magnitude")
        depth = quake.get("depth_km")
        magnitude_text = f"M{magnitude}" if magnitude is not None else "M?"
        depth_text = f" at {depth} km depth" if depth is not None else ""
        return f"  • {magnitude_text} — {quake.get('place') or 'unknown place'}{depth_text} ({quake.get('time')})"

    @staticmethod
    def _active_context(text: str) -> str:
        if _QUAKE_WORDS.search(text):
            return "Note: the USGS catalog lists earthquakes that already happened; it cannot say what comes next."
        if _ALERT_WORDS.search(text):
            return "Note: I read the NWS alert feed; the National Weather Service is the authority for warnings."
        return "Note: I only read the two public feeds — weather alerts and earthquakes."


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        HazardsAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
