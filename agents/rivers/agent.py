"""Rivers — USGS real-time stream gauges inside your editor.

The ninth non-coding ACP agent in this repo, and the first river-gauge agent in any editor
protocol: it answers what a USGS gauge is reading right now, which gauges are near a place,
and whether the water has been rising or falling.

Deterministic on purpose: routing is rules, every number comes from the USGS National Water
Information System, and nothing is guessed. It reports a plan, opens one tool call per read,
asks permission before the first one, streams the answer, and closes the tool call with a
one-line summary of what USGS returned.

Honest by construction: a gauge measures one spot on one river rather than a whole valley,
the nearest gauge to a place is named instead of being passed off as "the river there",
distances are straight-line miles computed here, USGS publishes provisional values that are
subject to revision, and USGS covers the United States and its territories only — so a place
outside it is refused rather than given made-up coordinates.
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
    DATASET_IV,
    DATASET_SITE,
    DEFAULT_HOURS,
    DEFAULT_RADIUS_MILES,
    DEFAULT_SITES,
    LATEST_HOURS,
    MAX_RADIUS_MILES,
    UNIT_WORDS,
    RiversData,
    RiversError,
)

HELP = (
    "I read the USGS National Water Information System — real-time stream gauges, keyless.\n"
    "Ask me:\n"
    "  • what is Clear Creek at Golden reading right now? (or a site number, 06719505)\n"
    "  • which gauges are near Denver?\n"
    "  • how high is the river in Golden?\n"
    "  • has 06719505 been rising in the last 48 hours?\n"
    "Every answer names the gauge (USGS site number and station name) and the reading time. A "
    "gauge measures one spot, not a whole river or valley, and USGS provisional values are "
    "subject to revision. I cover USGS gauges: the United States and its territories."
)

PERMISSION_KEY = "rivers-read-usgs"

SKILLS = ("river-now", "river-near", "river-rise", "help")

#: A USGS site number, spelled bare or after the word "site".
_SITE_RE = re.compile(r"\b(\d{8,15})\b")
_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_RADIUS_RE = re.compile(r"\b(\d{1,3})\s*(?:mi|mile|miles)\b", re.IGNORECASE)
_HOURS_RE = re.compile(r"\b(\d{1,3})\s*(?:h|hr|hrs|hour|hours)\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b(?:top|nearest|closest|first|show(?: me)?)\s+(\d{1,2})\b"
                       r"|\b(\d{1,2})\s*gauges?\b", re.IGNORECASE)
#: "is it rising", "over the last 48 hours", "how has it changed" — a question about movement.
_RISE_WORDS = re.compile(
    r"\b(ris(?:e|es|ing|en)|fall(?:s|ing|en)?|fell|drop(?:s|ping|ped)?|climb(?:s|ing|ed)?|"
    r"trend(?:ing)?|chang(?:e|ed|ing)|increas(?:e|ed|ing)|decreas(?:e|ed|ing)|"
    r"higher|lower|going up|going down|up or down|over the (?:last|past)|in the (?:last|past))\b",
    re.IGNORECASE)
#: "near Denver", "which gauges are around here" — a question about several gauges.
_NEAR_WORDS = re.compile(
    r"\b(near|nearby|around|close to|close by|within|gauges?|stations?|closest|nearest)\b",
    re.IGNORECASE)
#: Naming capitalised words with no match in the place list: a place this agent cannot resolve.
_UNKNOWN_PLACE_RE = re.compile(
    r"\b(?:in|at|near|off|around|for|from|over|with|than|and|to|of)\s+(?:the\s+|a\s+|an\s+)?"
    r"([A-Z][\w'\-]{2,20}(?:\s+[A-Z][\w'\-]{2,20})?)")
_IGNORED_PLACES = frozenset({"The", "Us", "USA", "My", "Here", "This", "It", "There", "Today",
                             "Now", "USGS", "What", "Which", "How", "Is", "Are", "Any", "Tell",
                             "Show", "Give", "Site", "River", "Creek", "Gauge", "Gages", "Gauges"})


def place_from_text(text: str) -> str | None:
    """A known place from the built-in list. Longest names win ('fort collins' before 'collins')."""
    lowered = " " + re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]+", " ", text.lower())).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in lowered:
            return name
    return None


def point_matches(text: str):
    """Every 'lat,lon' pair the caller spelled out, in order — never a thousands group."""
    for match in _POINT_RE.finditer(text):
        latitude, longitude = match.group(1), match.group(2)
        digits = longitude.lstrip("-")
        if "." not in digits and len(digits) > 1 and digits.startswith("0"):
            continue  # "1,000" is a thousands group
        yield f"{latitude},{longitude}"


def point_from_text(text: str) -> str | None:
    """The first 'lat,lon' pair the caller spelled out, if any."""
    return next(iter(point_matches(text)), None)


def unknown_places_from_text(text: str) -> list[str]:
    """Capitalised names after a preposition that the built-in list does not know."""
    found: list[str] = []
    for match in _UNKNOWN_PLACE_RE.finditer(text):
        candidate = match.group(1).strip()
        if candidate.split()[0] in _IGNORED_PLACES or place_from_text(candidate):
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def unknown_place_from_text(text: str) -> str | None:
    """The first unknown place name in the text, if the caller named one."""
    found = unknown_places_from_text(text)
    return found[0] if found else None


#: A bare 8-digit calendar date (20260924) is not a site number, even though both are digits.
_DATE_SHAPE_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")


def site_from_text(text: str) -> str | None:
    """The first USGS site number in the text, if any.

    USGS numbers run from 8 to 15 digits (06719505, 13018300, 50093045) and are written
    unpunctuated, so an 8-digit date-shaped token is the one thing that has to be skipped.
    """
    for match in _SITE_RE.finditer(text):
        candidate = match.group(1)
        if _DATE_SHAPE_RE.match(candidate):
            continue
        return candidate
    return None


def radius_from_text(text: str):
    match = _RADIUS_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def hours_from_text(text: str):
    match = _HOURS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def count_from_text(text: str):
    match = _COUNT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1) or match.group(2))
    except (TypeError, ValueError):
        return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    site = site_from_text(text)
    point = point_from_text(text)
    unknown = unknown_place_from_text(text)
    place = place_from_text(text) or (unknown if not (site or point) else None)
    if site:
        params["site"] = site
    if point:
        params["point"] = point
    elif place:
        params["place"] = place
    radius = radius_from_text(text)
    if radius is not None:
        params["radius"] = radius
    count = count_from_text(text)
    if count:
        params["count"] = count

    # Movement first: "has it been rising in the last 48 hours" is a trend, not a reading.
    if _RISE_WORDS.search(text):
        hours = hours_from_text(text)
        params["hours"] = hours or DEFAULT_HOURS
        return "river-rise", params

    # A gauge list needs a place (or a point); a site number alone is a single reading.
    if _NEAR_WORDS.search(text) and not site:
        return "river-near", params

    hours = hours_from_text(text)
    if hours:
        params["hours"] = hours
    return "river-now", params


def resolve_point(params: dict, data: RiversData) -> tuple[float, float, str]:
    """A point parameter wins over a place name; unknown places are never guessed.

    The prompt path checks the request before opening a tool call, so reaching here with no
    location is a programming error and reads as one.
    """
    if params.get("point"):
        lat, lon = data.check_point(params["point"])
        return lat, lon, f"the point {lat},{lon}"
    if params.get("place"):
        found = data.city(params["place"])
        if found:
            lat, lon, label = found
            return lat, lon, label
    raise ValueError("no location to read: name a USGS site number, a point or a place")



def _who(label: str, lat: float, lon: float) -> str:
    """'Denver (39.74,-104.99)', but not 'the point 39.74,-104.99 (39.74,-104.99)'."""
    return label if label.startswith("the point") else f"{label} ({lat},{lon})"


def _unit_words(unit_code: str) -> str:
    return UNIT_WORDS.get(unit_code, unit_code)


def _reading(value: float, unit_code: str) -> str:
    """'61.6 cubic feet per second' — the service's own unit code, in words."""
    number = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{number} {_unit_words(unit_code)}"


def _qualifier_note(series: dict) -> str | None:
    """USGS's own words for the codes attached to a reading, joined when there are several."""
    words = [q["description"] for q in series.get("qualifiers") or [] if q.get("description")]
    return "; ".join(words) if words else None


def _series_lines(series: dict, indent: str = "  • ") -> list[str]:
    """One line per parameter USGS published for a gauge, newest reading first."""
    lines: list[str] = []
    for key in ("discharge", "gage_height", "water_temperature"):
        reading = series.get(key)
        if not reading or not reading.get("latest"):
            continue
        latest = reading["latest"]
        lines.append(f"{indent}{reading['label']}: {_reading(latest['value'], reading['unit_code'])} "
                     f"as of {latest['time']}")
    return lines


class RiversAgent(AcpAgent):
    name = "rivers"
    title = "Rivers — USGS stream gauges"
    version = "1.0.0"

    def __init__(self, connection=None, data: RiversData | None = None) -> None:
        super().__init__(connection)
        self.data = data or RiversData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Rivers session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the USGS water service", "medium"),
            ("Answer with the gauge, its reading and the reading time", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Gauge readings come from the USGS National Water Information System; "
                        "a gauge measures one spot, and provisional values can be revised.")
            return STOP_END_TURN

        missing = self._missing_input(skill, params)
        if missing:
            # Nothing was read, so no permission is asked and no tool call is opened.
            ctx.message(missing)
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read the USGS water service for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Rivers to read the USGS water services?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public USGS water service before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (RiversError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the USGS water service: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is one or two HTTP reads; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _missing_input(self, skill: str, params: dict) -> str | None:
        """What to say when there is nothing readable to ask permission for.

        Returns None when the request carries a site number or a point; a place is checked
        against the agent's own list, so a name it does not know is refused, never guessed.
        """
        if params.get("site") or params.get("point"):
            return None
        if params.get("place"):
            if self.data.city(params["place"]):
                return None
            return (f"I do not know the place {params['place']!r}, so I will not guess coordinates "
                    "for it. Give me a USGS site number like 06719505, a latitude/longitude point, or "
                    "a US city from my list.")
        return ("Tell me a USGS site number (like 06719505), a US city from my list (Denver, Golden, "
                "Boise…) or a latitude/longitude point. USGS stream gauges are in the United States "
                "and its territories only.")

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "river-near":
            return self._near(params)
        if skill == "river-rise":
            return self._rise(params)
        if skill == "river-now":
            return self._now(params)
        return HELP, {"summary": "help", "dataset": None}

    def _now(self, params: dict) -> tuple[str, dict]:
        """One gauge: the named site, or the nearest active gauge to the named place."""
        if params.get("site"):
            read = self.data.read_site(params["site"], hours=params.get("hours") or LATEST_HOURS)
            read = {**read, "via": "site"}
            if not read["found"]:
                return self._no_readings(read)
            return self._reading_answer(read)

        lat, lon, label = resolve_point(params, self.data)
        # The probe window only has to be long enough to hold a recent reading, and the latest
        # reading is the same whatever the window, so the whole candidate set stays cheap.
        near = self.data.gauges_near(lat, lon, DEFAULT_RADIUS_MILES, limit=1, hours=LATEST_HOURS)
        if not near["readings"]:
            return self._no_gauges(near, label, lat, lon)
        gauge = near["readings"][0]
        return self._reading_answer({**gauge, "found": True, "known": True, "via": "nearest",
                                     "place": label, "radius_miles": near["radius_miles"],
                                     "read_at": near["read_at"], "hours": LATEST_HOURS})

    def _reading_answer(self, read: dict) -> tuple[str, dict]:
        series = read["series"]
        worse = [reading for reading in series.values() if reading.get("latest")]
        latest_time = max((reading["latest"]["time"] for reading in worse), default=None)
        artifact = {
            "summary": (f"USGS {read['site_number']} {read['site_name']}: "
                        + ", ".join(f"{reading['label']} {reading['latest']['value']:g} "
                                    f"{reading['unit_code']}" for reading in worse
                                    if reading.get("latest"))),
            "dataset": DATASET_IV,
            "source": "USGS National Water Information System",
            "site_number": read["site_number"],
            "site_name": read["site_name"],
            "latitude": read.get("latitude"),
            "longitude": read.get("longitude"),
            "via": read.get("via"),
            "place": read.get("place"),
            "radius_miles": read.get("radius_miles"),
            "reading_time": latest_time,
            "read_at": read.get("read_at"),
            "hours": read.get("hours"),
            "series": {key: {"label": reading["label"], "unit_code": reading["unit_code"],
                             "value": reading["latest"]["value"], "time": reading["latest"]["time"],
                             "qualifiers": reading["qualifiers"]}
                       for key, reading in series.items() if reading.get("latest")},
        }
        where = f"{read['site_name']} (USGS {read['site_number']})" if read["site_name"] else f"USGS {read['site_number']}"
        lines = []
        if read.get("via") == "nearest" and read.get("place"):
            lines.append(f"Nearest active USGS stream gauge to {read['place']}: {where}, "
                         f"{read.get('miles')} mi away.")
        else:
            lines.append(f"{where}:")
        lines.extend(_series_lines(series))
        notes = [note for note in (_qualifier_note(series[key]) for key in
                                  ("discharge", "gage_height", "water_temperature") if key in series) if note]
        if notes:
            lines.append(f"  • USGS qualifier: {notes[0]}")
        lines.append(
            f"\nRead live from {DATASET_IV} (USGS NWIS) at "
            f"{read.get('read_at') or 'the service time'}. A gauge measures one spot on one river, "
            f"not a whole valley, and provisional values are subject to revision."
        )
        return "\n".join(lines), artifact

    def _no_readings(self, read: dict) -> tuple[str, dict]:
        """USGS either has no such site, or the site published nothing in the window."""
        number = read["site_number"]
        artifact = {"summary": f"USGS {number}: no real-time reading in the last "
                               f"{read.get('hours') or LATEST_HOURS} hours",
                    "dataset": DATASET_IV, "site_number": number, "site_name": read.get("site_name"),
                    "known": read.get("known"), "found": False, "hours": read.get("hours")}
        if not read.get("known"):
            return (
                f"USGS has no stream site {number} in its site file, so there is nothing to read. "
                f"Site numbers are 8 to 15 digits and look like 06719505; the site file is at "
                f"{DATASET_SITE}.",
                artifact,
            )
        nearest = read.get("nearest")
        lead = (f"USGS lists {read['site_name']} ({number})"
                + (f", the nearest active gauge to {read['place']}, {nearest.get('miles')} mi away"
                   if nearest else "")
                + f", but it published no real-time discharge, gage height or water temperature in "
                  f"the last {read.get('hours') or LATEST_HOURS} hours. Gauges go quiet: sensors fail, "
                  f"records are revised, and a site can be removed from the active network.")
        return lead, artifact

    def _no_gauges(self, near: dict, label: str, lat: float, lon: float) -> tuple[str, dict]:
        artifact = {"summary": f"no active USGS gauge within {near['radius_miles']:g} mi of {label}",
                    "dataset": DATASET_SITE, "place": label, "latitude": lat, "longitude": lon,
                    "radius_miles": near["radius_miles"], "sites_in_box": near["sites_in_box"],
                    "found": 0}
        return (
            f"No active USGS stream gauge with real-time data within {near['radius_miles']:g} miles of "
            f"{_who(label, lat, lon)} — the site file returned {near['sites_in_box']} stream site(s) in "
            f"the wider box, none of them reporting in that radius. Name a USGS site number instead, "
            f"or widen the radius (up to {MAX_RADIUS_MILES:g} miles).\n\n"
            f"Read live from {DATASET_SITE} (USGS NWIS). USGS covers the United States and its "
            f"territories, so a place outside them will find nothing here.",
            artifact,
        )

    def _near(self, params: dict) -> tuple[str, dict]:
        lat, lon, label = resolve_point(params, self.data)
        radius = self.data.check_radius(params.get("radius") or DEFAULT_RADIUS_MILES)
        count = self.data.check_limit(params.get("count") or DEFAULT_SITES)
        near = self.data.gauges_near(lat, lon, radius, limit=count)
        if not near["readings"]:
            return self._no_gauges(near, label, lat, lon)
        artifact = {
            "summary": (f"{DATASET_SITE} + {DATASET_IV}: {len(near['readings'])} reporting gauge(s) "
                        f"within {radius:g} mi of {label}, nearest {near['readings'][0]['site_name']}"),
            "dataset": DATASET_IV,
            "source": "USGS National Water Information System",
            "place": label,
            "latitude": lat,
            "longitude": lon,
            "radius_miles": radius,
            "found": near["found"],
            "checked": near["checked"],
            "silent": len(near["silent"]),
            "sites_in_box": near["sites_in_box"],
            "read_at": near["read_at"],
            "gauges": [{"site_number": gauge["site_number"], "site_name": gauge["site_name"],
                        "miles": gauge["miles"],
                        "readings": {key: {"label": reading["label"], "unit_code": reading["unit_code"],
                                           "value": reading["latest"]["value"],
                                           "time": reading["latest"]["time"]}
                                     for key, reading in (gauge.get("series") or {}).items()
                                     if reading.get("latest")}}
                       for gauge in near["readings"]],
        }
        lines = [
            f"Active USGS stream gauges within {radius:g} miles of {_who(label, lat, lon)}: "
            f"{len(near['readings'])} reporting, nearest {len(near['readings'])} shown."
        ]
        for gauge in near["readings"]:
            readings = _series_lines(gauge.get("series") or {}, indent="")
            detail = "; ".join(line.strip() for line in readings) or "no real-time reading in the window"
            lines.append(f"  • {gauge['site_name']} ({gauge['site_number']}) — {gauge['miles']} mi — {detail}")
        if near["checked"]:
            lines.append(f"  • USGS's site file lists {near['found']} stream site(s) within the radius; "
                         f"the nearest "
                         f"{near['checked']} were asked for values, {len(near['silent'])} of those "
                         f"published nothing in the last {LATEST_HOURS} hours.")
        lines.append(
            f"\nDistances are straight-line miles computed here from {_who(label, lat, lon)} to the "
            f"coordinates USGS reports for each gauge. Read live from {DATASET_SITE} and {DATASET_IV} "
            f"(USGS NWIS) in two requests — one site-file box, one multi-site values request; nothing "
            f"is estimated per gauge. A gauge measures one spot, so a quiet reading at the nearest "
            f"gauge is not a statement about the whole river."
        )
        return "\n".join(lines), artifact

    def _rise(self, params: dict) -> tuple[str, dict]:
        if params.get("site"):
            number = self.data.check_site_number(params["site"])
        else:
            lat, lon, label = resolve_point(params, self.data)
            near = self.data.gauges_near(lat, lon, DEFAULT_RADIUS_MILES, limit=1, hours=LATEST_HOURS)
            if not near["readings"]:
                return self._no_gauges(near, label, lat, lon)
            number = near["readings"][0]["site_number"]
        hours = self.data.check_hours(params.get("hours") or DEFAULT_HOURS)
        read = self.data.trend(number, hours=hours)
        if not read.get("found"):
            if read.get("reason"):
                artifact = {"summary": f"USGS {read['site_number']}: {read['reason']}",
                            "dataset": DATASET_IV, "site_number": read["site_number"], "found": False}
                return (f"{read['site_name']} ({read['site_number']}) published no discharge or gage "
                        f"height in the last {hours} hours, so there is no trend to report.", artifact)
            return self._no_readings({**read, "hours": hours})
        trend = read.get("trend")
        where = f"{read['site_name']} ({read['site_number']})" if read["site_name"] else f"USGS {read['site_number']}"
        artifact = {
            "summary": (f"USGS {read['site_number']}: {read['label']} "
                        + (f"{trend['direction']} {trend['change']:+g} {trend['unit_code']} "
                           f"({trend['percent']:+g}%) over {hours}h" if trend else "no trend in the window")),
            "dataset": DATASET_IV,
            "source": "USGS National Water Information System",
            "site_number": read["site_number"],
            "site_name": read["site_name"],
            "hours": hours,
            "trend": trend,
            "read_at": read.get("read_at"),
        }
        if not trend:
            return (
                f"{where} published only {read.get('points')} reading(s) of "
                f"{read.get('label', 'discharge').lower()} in the last {hours} hours, which is not "
                f"enough to say whether it is rising or falling.\n\nRead live from {DATASET_IV} (USGS NWIS).",
                artifact,
            )
        direction = {"rising": "rose", "falling": "fell", "flat": "held steady"}[trend["direction"]]
        percent = f", {trend['percent']:+g}%" if trend["percent"] is not None else ""
        lines = [
            f"{where}: {trend['label'].lower()} {direction} from "
            f"{_reading(trend['first']['value'], trend['unit_code'])} to "
            f"{_reading(trend['last']['value'], trend['unit_code'])} over the last {hours} hours "
            f"({trend['change']:+g} {trend['unit_code']}{percent}).",
            f"  • First reading {trend['first']['time']}, last reading {trend['last']['time']} "
            f"({trend['readings']} readings in the window).",
            f"  • Lowest {_reading(trend['minimum'], trend['unit_code'])}, "
            f"highest {_reading(trend['maximum'], trend['unit_code'])}.",
        ]
        note = _qualifier_note({"qualifiers": trend.get("qualifiers") or []})
        if note:
            lines.append(f"  • USGS qualifier: {note}")
        lines.append(
            f"\nThe trend is computed here from the points USGS returned for {where} — it is not a "
            f"USGS forecast and not a flood warning. Read live from {DATASET_IV} at "
            f"{read.get('read_at') or 'the service time'}; provisional values are subject to revision, "
            f"so a rise can be revised after the fact."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        RiversAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
