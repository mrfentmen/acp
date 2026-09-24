"""Air — Open-Meteo's air-quality model inside your editor.

The seventh non-coding ACP agent in this repo, and the first air-quality agent in any
editor protocol: it answers how bad the air is right now, what the next hours look like,
and which of several cities is worst — straight from the CAMS-driven model Open-Meteo
serves (US AQI, PM2.5, PM10, ozone, NO₂, SO₂, CO, UV and pollen).

Deterministic on purpose: routing is rules, every number comes from Open-Meteo, and nothing
is guessed. It reports a plan, opens one tool call per read, asks permission before the
first one, streams the answer, and closes the tool call with a one-line summary of what the
model returned.

Honest by construction: a value is a model grid cell, not a rooftop monitor; pollen is
Europe-only, so a null is stated as "not published here" rather than zero; and a place the
built-in list does not know is refused instead of being given made-up coordinates.
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
    DEFAULT_RANKING,
    LABELS,
    MAX_PLACES,
    AirQualityData,
    AirQualityError,
)

HELP = (
    "I read Open-Meteo's air-quality model — the CAMS-driven forecast grid, keyless.\n"
    "Ask me:\n"
    "  • how bad is the air in Denver right now?\n"
    "  • what will the air quality be like in Los Angeles for the next 48 hours?\n"
    "  • which city has the worst air: Denver, Phoenix or Los Angeles?\n"
    "  • what is the AQI at 39.74,-104.99?\n"
    "Every answer names the model it came from and the hour it applies to. A value is a model "
    "grid cell, not a monitor on your street, and it is not a health advisory."
)

PERMISSION_KEY = "air-read-open-meteo"

SKILLS = ("air-now", "air-forecast", "air-ranking", "help")

DEFAULT_FORECAST_HOURS = 24
MAX_LISTED_PLACES = 10

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_HOURS_RE = re.compile(r"\b(?:next|coming|following|in the next|for the next|for)\s+(\d{1,3})\s*"
                       r"(?:hours?|hrs?|h)\b", re.IGNORECASE)
_BARE_HOURS_RE = re.compile(r"\b(\d{1,3})\s*(?:hours?|hrs?)\b", re.IGNORECASE)
_FORECAST_WORDS = re.compile(
    r"\b(forecast|outlook|tomorrow|tonight|later|next \d+ hours?|for the next|will be|going to be|hourly)\b",
    re.IGNORECASE)
_RANKING_WORDS = re.compile(
    r"\b(worst|best|cleanest|dirtiest|worse|better|compare|comparison|ranking|rank|which cit|"
    r"versus|vs\.?)\b",
    re.IGNORECASE)
#: "...in Atlantis" with no match in the place list: a name the caller gave that this agent
#: cannot resolve. Reported honestly instead of being treated as "no place given".
_UNKNOWN_PLACE_RE = re.compile(
    r"\b(?:in|at|near|off|around|for|with|than|and)\s+(?:the\s+|a\s+|an\s+)?"
    r"([A-Z][\w'\-]{2,20}(?:\s+[A-Z][\w'\-]{2,20})?)")
_IGNORED_PLACES = frozenset({"The", "Us", "USA", "My", "Here", "This", "It", "There", "Today", "Tomorrow"})


def place_from_text(text: str) -> str | None:
    """A known place from the built-in list. Longest names win ('san jose' before 'jose')."""
    lowered = " " + re.sub(r"[\s]+", " ", re.sub(r"[^a-z ]+", " ", text.lower())).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in lowered:
            return name
    return None


def point_matches(text: str):
    """Every 'lat,lon' pair the caller spelled out, in order — never a thousands group."""
    for match in _POINT_RE.finditer(text):
        latitude, longitude = match.group(1), match.group(2)
        digits = longitude.lstrip("-")
        # "1,000" is a thousands group; "-0.13" is London.
        if "." not in digits and len(digits) > 1 and digits.startswith("0"):
            continue
        yield f"{latitude},{longitude}"


def targets_from_text(text: str) -> list[str]:
    """Known place names and literal coordinates in the order they appear (for comparisons)."""
    found: list[tuple[int, str]] = []
    lowered = text.lower()
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        match = re.search(rf"\b{re.escape(name)}\b", lowered)
        if match:
            found.append((match.start(), name))
    for match in _POINT_RE.finditer(text):
        point = next(iter(point_matches(match.group(0))), None)
        if point:
            found.append((match.start(), point))
    found.sort()
    ordered: list[str] = []
    for _, target in found:
        if target not in ordered:
            ordered.append(target)
    return ordered


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


def hours_from_text(text: str) -> int | None:
    match = _HOURS_RE.search(text) or _BARE_HOURS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def point_from_text(text: str) -> str | None:
    """The first 'lat,lon' pair the caller spelled out, if any."""
    return next(iter(point_matches(text)), None)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    point = point_from_text(text)
    targets = targets_from_text(text)
    known = [target for target in targets if target in CITY_COORDS]
    place = known[0] if known else unknown_place_from_text(text)
    if point:
        params["point"] = point
    elif place:
        params["place"] = place

    # Two places (or a comparison word) make this a comparison rather than a reading. Names
    # this agent cannot resolve are carried through too, so the answer can name them as skipped.
    if len(targets) >= 2 or _RANKING_WORDS.search(text):
        unknown = [name for name in unknown_places_from_text(text) if name not in targets]
        params.pop("point", None)
        params.pop("place", None)
        params["places"] = ((targets + unknown) or list(DEFAULT_RANKING))[:MAX_PLACES]
        return "air-ranking", params

    if _FORECAST_WORDS.search(text):
        params["hours"] = hours_from_text(text) or DEFAULT_FORECAST_HOURS
        return "air-forecast", params

    return "air-now", params


def resolve_point(params: dict, data: AirQualityData) -> tuple[float, float, str] | None:
    """A point parameter wins over a place name; unknown places are never guessed."""
    if params.get("point"):
        lat, lon = data.check_point(params["point"])
        return lat, lon, f"the point {lat},{lon}"
    if params.get("place"):
        found = data.city(params["place"])
        if found:
            lat, lon, label = found
            return lat, lon, label
    return None


def resolve_places(names: list[str], data: AirQualityData) -> tuple[list[tuple[str, float, float]], list[str]]:
    """Split a list of names into resolved (label, lat, lon) triples and unknown names."""
    resolved: list[tuple[str, float, float]] = []
    unknown: list[str] = []
    for name in names[:MAX_PLACES]:
        text = " ".join(str(name).split())
        if not text:
            continue
        point = _POINT_RE.fullmatch(re.sub(r"\s+", "", text))
        if point:
            lat, lon = data.check_point(text)
            resolved.append((f"{lat},{lon}", lat, lon))
            continue
        found = data.city(text)
        if found:
            resolved.append((found[2], found[0], found[1]))
        else:
            unknown.append(text)
    return resolved, unknown


def _value(value, unit: str = "") -> str:
    if value is None:
        return "not published"
    return f"{value:g}{(' ' + unit) if unit else ''}"


def _unit(units: dict, field: str) -> str:
    return str(units.get(field) or "")


def _where(label: str, lat: float, lon: float) -> str:
    """'Denver (39.74,-104.99)', but not 'the point 39.74,-104.99 (39.74,-104.99)'."""
    return label if label.startswith("the point") else f"{label} ({lat},{lon})"


def _source_note() -> str:
    return (f"Read live from Open-Meteo's air-quality API ({DATASET}), which serves a model grid "
            f"cell — not a monitor on your street. The band names are the standard U.S. EPA AQI "
            f"categories for the US AQI value shown.")


class AirAgent(AcpAgent):
    name = "air"
    title = "Air — Open-Meteo air quality"
    version = "1.0.0"

    def __init__(self, connection=None, data: AirQualityData | None = None) -> None:
        super().__init__(connection)
        self.data = data or AirQualityData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Air session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read Open-Meteo's air-quality model", "medium"),
            ("Answer with the AQI, its EPA band and the model hour", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Air quality comes from Open-Meteo's CAMS-driven model grid; pollen is "
                        "published for Europe only, so a blank pollen line elsewhere is not zero.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read the Open-Meteo air-quality model for {skill}", kind="fetch",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Air to read Open-Meteo's public air-quality model?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public air-quality model before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (AirQualityError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the air-quality model: {exc}")
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
        if skill == "air-forecast":
            return self._forecast(params)
        if skill == "air-ranking":
            return self._ranking(params)
        if skill == "air-now":
            return self._now(params)
        return HELP, {"summary": "help", "dataset": None}

    @staticmethod
    def _no_place(asked_for: str | None) -> tuple[str, dict]:
        if asked_for:
            message = (f"I do not know the place {asked_for!r}, so I will not guess coordinates for "
                       "it. Give me a latitude/longitude point or a city from my list.")
        else:
            message = ("Tell me where to look: a city from my list (Denver, Los Angeles, London…) or "
                       "a latitude/longitude point like 39.74,-104.99.")
        return message, {"summary": "unknown place", "dataset": DATASET, "place": asked_for, "known": False}

    def _now(self, params: dict) -> tuple[str, dict]:
        resolved = resolve_point(params, self.data)
        if not resolved:
            return self._no_place(params.get("place"))
        lat, lon, label = resolved
        read = self.data.now(lat, lon)
        units = read["units"]
        artifact = {
            "summary": (f"{DATASET}: US AQI {_value(read['aqi'])} ({read['category']}) at {label} "
                        f"for {read['time']} UTC"),
            "dataset": DATASET,
            "source": "Open-Meteo air-quality API (CAMS-driven model)",
            "place": label,
            "latitude": lat,
            "longitude": lon,
            "time": read["time"],
            "aqi": read["aqi"],
            "category": read["category"],
            "pollutants": read["pollutants"],
            "pollen": read["pollen"],
            "uv_index": read["uv_index"],
            "units": units,
        }
        lines = [
            f"Air quality at {_where(label, lat, lon)} for the hour beginning {read['time']} UTC: "
            f"US AQI {_value(read['aqi'])} — {read['category']}."
        ]
        pollutant_bits = []
        for field in ("pm2_5", "pm10", "ozone", "nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide"):
            value = read["pollutants"].get(field)
            if value is not None:
                pollutant_bits.append(f"{LABELS[field]} {value:g} {_unit(units, field)}".strip())
        if pollutant_bits:
            lines.append("  • " + ", ".join(pollutant_bits))
        if read["uv_index"] is not None:
            lines.append(f"  • UV index {read['uv_index']:g}")
        if read["pollen"]:
            pollen_bits = [f"{LABELS[field]} {value:g} {_unit(units, field)}".strip()
                           for field, value in read["pollen"].items() if value is not None]
            lines.append("  • Pollen: " + ", ".join(pollen_bits))
        else:
            lines.append("  • Pollen: this model publishes none for this location (pollen coverage "
                         "is Europe-only), so a blank here is not zero.")
        lines.append(f"  • What the band means: {read['guidance']}.")
        lines.append("\n" + _source_note())
        return "\n".join(lines), artifact

    def _forecast(self, params: dict) -> tuple[str, dict]:
        resolved = resolve_point(params, self.data)
        if not resolved:
            return self._no_place(params.get("place"))
        lat, lon, label = resolved
        hours = self.data.check_hours(params.get("hours") or DEFAULT_FORECAST_HOURS)
        read = self.data.forecast(lat, lon, hours)
        artifact = {
            "summary": (f"{DATASET}: {read['hours']} hours from {read['first_hour']} at {label}, "
                        f"peak US AQI {_value(read['peak']['aqi'])}"),
            "dataset": DATASET,
            "source": "Open-Meteo air-quality API (CAMS-driven model)",
            "place": label,
            "latitude": lat,
            "longitude": lon,
            "hours": read["hours"],
            "first_hour": read["first_hour"],
            "last_hour": read["last_hour"],
            "peak": read["peak"],
            "cleanest": read["cleanest"],
            "units": read["units"],
            "series": read["series"],
        }
        lines = [
            f"Hourly air-quality outlook for {_where(label, lat, lon)}, {read['hours']} hours from "
            f"{read['first_hour']} to {read['last_hour']} UTC:",
            f"  • Peak US AQI {_value(read['peak']['aqi'])} at {read['peak']['time']} "
            f"({read['peak']['category']}); cleanest hour {_value(read['cleanest']['aqi'])} AQI at "
            f"{read['cleanest']['time']}.",
        ]
        for row in read["series"][:12]:
            lines.append(f"      {row['time']}  AQI {_value(row['aqi'])}"
                         f"  PM2.5 {_value(row['pm2_5'])} {_unit(read['units'], 'pm2_5')}".rstrip())
        if read["hours"] > 12:
            lines.append(f"      ... {read['hours'] - 12} more hours are in the artifact.")
        lines.append(
            f"\nRead live from Open-Meteo's air-quality API ({DATASET}). This is hourly model output "
            f"for a grid cell, not a measurement and not a health advisory; smoke episodes can move "
            f"much faster than a forecast grid, so check a local agency reading before you rely on it."
        )
        return "\n".join(lines), artifact

    def _ranking(self, params: dict) -> tuple[str, dict]:
        names = params.get("places") or list(DEFAULT_RANKING)
        if isinstance(names, str):
            names = [part.strip() for part in names.split(",") if part.strip()]
        resolved, unknown = resolve_places(list(names), self.data)
        if not resolved:
            return (
                "None of those places are in my list. Give me coordinates (like 39.74,-104.99) or a "
                "city I know, such as Denver, Delhi, Beijing or London.",
                {"summary": "no known places", "dataset": DATASET, "unknown": unknown},
            )
        read = self.data.ranking(resolved)
        worst = read["worst"]
        artifact = {
            "summary": (f"{DATASET}: {read['count']} place(s) compared, worst {worst['place']} "
                        f"US AQI {_value(worst['aqi'])}"),
            "dataset": DATASET,
            "source": "Open-Meteo air-quality API (CAMS-driven model)",
            "requested": len(names),
            "count": read["count"],
            "unknown": unknown,
            "time": read["places"][0]["time"] if read["places"] else None,
            "units": read["units"],
            "worst": worst,
            "best": read["best"],
            "places": read["places"],
        }
        lines = [
            f"{read['count']} place(s) compared in one request, worst air first (US AQI, hour "
            f"beginning {artifact['time']} UTC):"
        ]
        for row in read["places"][:MAX_LISTED_PLACES]:
            lines.append(f"  • {row['place']} — US AQI {_value(row['aqi'])} ({row['category']}), "
                         f"PM2.5 {_value(row['pm2_5'])} {_unit(read['units'], 'pm2_5')}".rstrip())
        if read["count"] > MAX_LISTED_PLACES:
            lines.append(f"  • ... {read['count'] - MAX_LISTED_PLACES} more in the artifact.")
        if unknown:
            lines.append(f"  • Skipped (not in my place list): {', '.join(unknown)}.")
        lines.append(
            f"\nEvery row is the same model hour from Open-Meteo's air-quality API ({DATASET}) — one "
            f"request, so the comparison is fair; an AQI gap smaller than about 10 points is not a "
            f"real difference between cities."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        AirAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
