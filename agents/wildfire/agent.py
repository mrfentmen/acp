"""Wildfire — interagency fire incidents inside your editor.

The sixth non-coding ACP agent in this repo, and the first wildfire agent in any editor
protocol: it answers what is burning, where, how big and how contained — straight from
the incident layer the US land-management agencies publish (NIFC WFIGS).

Deterministic on purpose: routing is rules, every number comes from NIFC, and nothing is
guessed. It reports a plan, opens one tool call per lookup, asks permission before the
first read, streams the answer, and closes the tool call with a one-line summary of the
layer it read.

Distance is real: the near skill computes great-circle miles from your point to each
reported incident location and says so, because "35 miles away" is the number a person
actually needs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (  # noqa: E402
    CITY_COORDS,
    DATASET,
    WildfireData,
    WildfireError,
)

HELP = (
    "I read the interagency wildfire incident layer that US land-management agencies publish.\n"
    "Ask me:\n"
    "  • what wildfires are burning in California right now?\n"
    "  • any fires within 150 miles of Denver?\n"
    "  • how much fire is burning in the country right now?\n"
    "  • tell me about the Timber fire\n"
    "Every answer names the layer it came from and the acreage agencies reported. I am not an "
    "evacuation notice — follow local authorities for that."
)

PERMISSION_KEY = "wildfire-read-public-nifc"

SKILLS = ("wildfire-active", "wildfire-near", "wildfire-summary", "wildfire-lookup", "help")

DEFAULT_RADIUS_MILES = 100.0

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
#: "5,000 acres" is a size, not a coordinate, so a point is never read straight off a size phrase.
_ACRES_AFTER_RE = re.compile(r"\s*acres?\b", re.IGNORECASE)
_ACRES_RE = re.compile(r"\b(?:over|above|at least|more than|greater than|bigger than)?\s*([\d][\d,]*)\s*acres?\b",
                       re.IGNORECASE)
_RADIUS_RE = re.compile(r"\bwithin\s+([\d][\d,]*)\s*(?:mi|mile|miles)\b", re.IGNORECASE)
_SUMMARY_WORDS = re.compile(
    r"\b(how many|how much|how big|total|totals|summary|nationwide|country|overall|statewide)\b", re.IGNORECASE)
_UNCONTAINED_WORDS = re.compile(r"\b(uncontained|not contained|out of control|zero percent|0 percent)\b",
                                re.IGNORECASE)
_NEAR_WORDS = re.compile(r"\b(near me|nearby|close by|closest|around here|within \d+)\b", re.IGNORECASE)
#: "near Gotham" names a place the built-in list may not know, and that must be refused
#: rather than silently answered with the national list. "near me" stays place-less.
_NEAR_PLACE_RE = re.compile(r"\b(?:near|around|close to)\s+(?!me\b)([A-Za-z][\w'\-]{1,30})", re.IGNORECASE)
_LOOKUP_WORDS = re.compile(r"\b(about|details?|tell me about|look ?up|search|find|named|called)\b", re.IGNORECASE)
_NAME_RE = re.compile(
    r"\b(?:about|on|called|named|search for|look ?up|find)\s+(?:the\s+)?([A-Za-z][\w'\- ]{1,40}?)\s+(?:fire|incident)s?\b",
    re.IGNORECASE)
_CAPS_NAME_RE = re.compile(r"\b([A-Z][\w'\-]{2,})\s+(?:fire|incident)\b")
_LOWER_CODE_RE = re.compile(r"\b(?:in|for|across|state of|near)\s+([a-z]{2})\b")
#: Two-letter codes that are also ordinary English words. "in or out" is not Oregon.
_AMBIGUOUS_LOWER_CODES = frozenset(
    {"al", "as", "de", "hi", "id", "in", "la", "ma", "me", "mi", "mo", "ms", "ne", "oh", "ok", "or", "pa"}
)
#: States the layer files, so a typo is caught here instead of by the service.
STATE_CODES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
)
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def state_from_text(text: str) -> str | None:
    """A state code in UPPERCASE ('CA'), a lowercase code after a preposition ('in or'),
    or a full state name ('Oregon'). Bare lowercase words are never states."""
    match = re.search(r"\b([A-Z]{2})\b", text)
    if match and match.group(1) in STATE_CODES:
        return match.group(1)
    match = _LOWER_CODE_RE.search(text)
    if match and match.group(1) not in _AMBIGUOUS_LOWER_CODES and match.group(1).upper() in STATE_CODES:
        return match.group(1).upper()
    lowered = text.lower()
    for name, code in STATE_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return code
    return None


def place_from_text(text: str) -> str | None:
    """A US city from the built-in list. Longest names win ('new york' over 'york')."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in re.sub(r"\s+", " ", lowered):
            return name
    return None


#: Words that can sit in front of "fire" without naming one ("find the fire", "any big fire").
_NOT_A_NAME = frozenset({"the", "a", "an", "this", "that", "these", "those", "large", "big", "new",
                         "any", "some", "every", "another", "other", "next", "latest", "wild"})


def name_from_text(text: str) -> str | None:
    """'tell me about the Timber fire' -> Timber."""
    match = _NAME_RE.search(text)
    if match:
        candidate = re.sub(r"^(?:the|a|an)\s+", "", match.group(1).strip(), flags=re.IGNORECASE)
        if 2 <= len(candidate) <= 40 and candidate.lower() not in _NOT_A_NAME:
            return candidate
    match = _CAPS_NAME_RE.search(text)
    return match.group(1) if match else None


def point_from_text(text: str) -> str | None:
    """A 'lat,lon' pair the caller spelled out — never the pieces of a thousands-separated size."""
    for match in _POINT_RE.finditer(text):
        latitude, longitude = match.group(1), match.group(2)
        if _ACRES_AFTER_RE.match(text[match.end():]):
            continue  # "over 5,000 acres"
        digits = longitude.lstrip("-")
        # "12,345" is a thousands group; "-0.13" is London.
        if "." not in digits and len(digits) > 1 and digits.startswith("0"):
            continue
        return f"{latitude},{longitude}"
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    point = point_from_text(text)
    place = place_from_text(text)
    near_place = None if place else (match.group(1).lower() if (match := _NEAR_PLACE_RE.search(text)) else None)
    state = state_from_text(text)
    acres_match = _ACRES_RE.search(text)
    min_acres = float(acres_match.group(1).replace(",", "")) if acres_match else None
    radius_match = _RADIUS_RE.search(text)
    radius = float(radius_match.group(1).replace(",", "")) if radius_match else None
    name = name_from_text(text)

    if min_acres is not None:
        params["min_acres"] = min_acres
    if state:
        params["state"] = state

    if name and _LOOKUP_WORDS.search(text):
        params["name"] = name
        return "wildfire-lookup", params

    if point or place or near_place or _NEAR_WORDS.search(text):
        if point:
            params["point"] = point
        if place or near_place:
            # An unknown place is passed through so the skill can refuse it by name instead of
            # answering a different question with the national list.
            params["place"] = place or near_place
        params["radius_miles"] = radius if radius is not None else DEFAULT_RADIUS_MILES
        return "wildfire-near", params

    if _SUMMARY_WORDS.search(text):
        return "wildfire-summary", params

    # A lookup ask with nothing to look up: the skill asks for the name rather than dumping
    # the national list and ignoring what was asked.
    if _LOOKUP_WORDS.search(text) and not state and not params.get("min_acres"):
        return "wildfire-lookup", params

    if _UNCONTAINED_WORDS.search(text):
        params["uncontained"] = True
    return "wildfire-active", params


def _fire_line(row: dict, distance: bool = False) -> str:
    contained = row.get("percent_contained")
    containment = "containment not reported" if contained is None else f"{contained:g}% contained"
    place = ", ".join(part for part in (row.get("county"), row.get("state")) if part)
    line = (f"  • {row.get('name') or 'unnamed incident'} — {row.get('acres') or 0:,.0f} acres, {containment}"
            f"{f' ({place})' if place else ''}, discovered {row.get('discovered') or 'date not reported'}")
    if distance and row.get("distance_miles") is not None:
        line += f", {row['distance_miles']:,.1f} miles away"
    return line


class WildfireAgent(AcpAgent):
    name = "wildfire"
    title = "Wildfire — interagency fire incidents"
    version = "1.0.0"

    def __init__(self, connection=None, data: WildfireData | None = None) -> None:
        super().__init__(connection)
        self.data = data or WildfireData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Wildfire session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read NIFC's incident layer", "medium"),
            ("Answer with acreage, containment and the layer id", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("The incident layer holds currently active incidents only, with the "
                        "acreage the managing agencies reported.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read the NIFC wildfire layer for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Wildfire to read the public NIFC incident layer?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public wildfire layer before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (WildfireError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the wildfire layer: {exc}")
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
        if skill == "wildfire-near":
            return self._near(params)
        if skill == "wildfire-summary":
            return self._summary(params)
        if skill == "wildfire-lookup":
            return self._lookup(params)
        if skill == "wildfire-active":
            return self._active(params)
        return HELP, {"summary": "help", "dataset": None}

    def _active(self, params: dict) -> tuple[str, dict]:
        result = self.data.incidents(state=params.get("state"), min_acres=params.get("min_acres"),
                                     limit=int(params.get("limit") or 10),
                                     uncontained=bool(params.get("uncontained")))
        scope = result["where"] if result["where"] != "1=1" else "every active incident"
        artifact = {
            "summary": f"{DATASET}: {result['count']} incident(s) matching {scope}",
            "dataset": DATASET,
            "scope": scope,
            "count": result["count"],
            "acres": result["acres"],
            "incidents": result["incidents"],
        }
        if not result["count"]:
            return (f"No active wildfire matches {scope} in the interagency list right now.", artifact)
        listing = "\n".join(_fire_line(row) for row in result["incidents"][:10])
        answer = (
            f"{result['count']} active wildfire(s) matching {scope}, largest first "
            f"({result['acres']:,.0f} acres in this set):\n{listing}\n\n"
            f"Source: {DATASET} (read live from NIFC). Coordinates for every fire are in the "
            "artifact. This is what the agencies have reported, not every fire burning."
        )
        return answer, artifact

    def _near(self, params: dict) -> tuple[str, dict]:
        found = None
        if params.get("point"):
            lat, lon = self.data.check_point(params["point"])
            found = (lat, lon, f"the point {lat},{lon}")
        elif params.get("place"):
            found = WildfireData.city(params["place"])
        if not found:
            asked_for = params.get("place")
            if asked_for:
                message = (f"I do not know the place {asked_for!r}, so I will not guess coordinates "
                           "for it. Give me a latitude/longitude point or a city from my list.")
            else:
                message = ("Tell me where to look: a city from my list (Denver, Boise, Los Angeles…) or "
                           "a latitude/longitude point like 39.74,-104.99.")
            return (
                message,
                {"summary": "unknown place", "dataset": DATASET, "place": asked_for, "known": False},
            )
        lat, lon, label = found
        radius = float(params.get("radius_miles") or DEFAULT_RADIUS_MILES)
        result = self.data.near(lat, lon, radius_miles=radius, limit=int(params.get("limit") or 10))
        artifact = {
            "summary": f"{DATASET}: {result['count']} incident(s) within {radius:g} mi of {label}",
            "dataset": DATASET,
            "place": label,
            "latitude": lat,
            "longitude": lon,
            "radius_miles": radius,
            "count": result["count"],
            "acres": result["acres"],
            "incidents": result["incidents"],
        }
        if not result["count"]:
            return (f"No active wildfire is listed within {radius:g} miles of {label} ({lat},{lon}) right "
                    f"now.\n\nSource: {DATASET} (read live from NIFC's interagency incident layer).",
                    artifact)
        listing = "\n".join(_fire_line(row, distance=True) for row in result["incidents"][:10])
        answer = (
            f"{result['count']} active wildfire(s) within {radius:g} miles of {label} ({lat},{lon}), "
            f"nearest first:\n{listing}\n\n"
            f"Source: {DATASET} (read live from NIFC). Distances are great-circle miles computed here "
            "from the reported fire locations — distance is not risk, and this is not an evacuation "
            "notice. Follow local authorities."
        )
        return answer, artifact

    def _summary(self, params: dict) -> tuple[str, dict]:
        result = self.data.summary(state=params.get("state"))
        artifact = {
            "summary": f"{DATASET}: {result['count']} incident(s), {result['acres']:,.0f} acres in {result['scope']}",
            "dataset": DATASET,
            "scope": result["scope"],
            "count": result["count"],
            "acres": result["acres"],
            "uncontained": result["uncontained"],
            "biggest": result["biggest"],
            "by_state": result["by_state"],
        }
        lines = [
            f"{result['count']} active wildfire(s) on the interagency list for {result['scope']}, "
            f"covering {result['acres']:,.0f} acres.",
            f"  • Still under half contained: {result['uncontained']}",
        ]
        if result["biggest"]:
            lines.append("  • Largest fires:")
            for row in result["biggest"][:3]:
                lines.append(f"      {_fire_line(row).strip().removeprefix('• ')}")
        if result["by_state"]:
            ranked = ", ".join(f"{entry['state']} {entry['acres']:,.0f} acres ({entry['count']})"
                               for entry in result["by_state"][:5])
            lines.append(f"  • Most acres by state: {ranked}")
        lines.append(f"\nSource: {DATASET} (read live from NIFC). Acreage and containment come from the "
                     "managing agencies and are updated as they report, not continuously.")
        return "\n".join(lines), artifact

    def _lookup(self, params: dict) -> tuple[str, dict]:
        name = params.get("name")
        if not name:
            return (
                "Which incident? Give me the name, or part of it, as the agencies spell it.",
                {"summary": "no name given", "dataset": DATASET},
            )
        result = self.data.lookup(name, limit=int(params.get("limit") or 5))
        artifact = {
            "summary": f"{DATASET}: {result['count']} incident(s) matching {result['query']!r}",
            "dataset": DATASET,
            "query": result["query"],
            "count": result["count"],
            "incidents": result["incidents"],
        }
        if not result["count"]:
            return (f"No active incident whose name contains {result['query']!r} is on the interagency "
                    f"list right now — that list holds currently active incidents only.", artifact)
        lines = [f"{result['count']} active incident(s) matching {result['query']!r}:"]
        for row in result["incidents"]:
            lines.append(_fire_line(row))
            details = []
            if row.get("cause"):
                details.append(f"cause: {row['cause']}")
            if row.get("type_name"):
                details.append(f"type: {row['type_name']}")
            if row.get("management"):
                details.append(f"managing: {row['management']}")
            if row.get("gacc"):
                details.append(f"coordination center: {row['gacc']}")
            if details:
                lines.append(f"    {', '.join(details)}")
            if row.get("id"):
                lines.append(f"    incident id: {row['id']}")
            if row.get("latitude") is not None:
                lines.append(f"    location: {row['latitude']},{row['longitude']} (last updated "
                             f"{row.get('last_updated') or 'unknown'})")
        lines.append(f"\nSource: {DATASET} (read live from NIFC).")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        WildfireAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
