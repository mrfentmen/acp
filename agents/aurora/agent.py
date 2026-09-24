"""Aurora — NOAA space weather inside your editor.

The eighth non-coding ACP agent in this repo, and the first space-weather agent in any
editor protocol: it answers how disturbed the geomagnetic field is right now, what the
next few days look like, and whether the aurora is likely to be overhead where you are —
straight from NOAA's Space Weather Prediction Center (planetary K index and the OVATION
aurora model).

Deterministic on purpose: routing is rules, every number comes from SWPC, and nothing is
guessed. It reports a plan, opens one tool call per read, asks permission before the first
one, streams the answer, and closes the tool call with a one-line summary of what SWPC
returned.

Honest by construction: Kp is a planetary index rather than a local sky forecast, the
OVATION probability says nothing about clouds, SWPC's own forecast beyond about a day is
low confidence, and a place the built-in list does not know is refused rather than given
made-up coordinates.
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
    DATASET_KP,
    DATASET_KP_1M,
    DATASET_OVATION,
    MAX_DAYS,
    SpaceWeatherData,
    SpaceWeatherError,
    STORM_SCALE,
)

HELP = (
    "I read NOAA's Space Weather Prediction Center — the planetary K index and the OVATION "
    "aurora model, keyless.\n"
    "Ask me:\n"
    "  • how disturbed is the geomagnetic field right now?\n"
    "  • is a geomagnetic storm coming in the next 3 days?\n"
    "  • will the aurora be visible from Fairbanks tonight?\n"
    "  • what is the aurora probability at 64.84,-147.72?\n"
    "Every answer names the SWPC product and its timestamp. Kp is a planetary index, not a "
    "local sky forecast, and a probability is for aurora overhead — it says nothing about clouds."
)

PERMISSION_KEY = "aurora-read-swpc"

SKILLS = ("aurora-now", "aurora-forecast", "aurora-visibility", "help")

DEFAULT_FORECAST_DAYS = MAX_DAYS

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_DAYS_RE = re.compile(r"\b(?:next|coming|following|for the next|in the next)\s+(\d{1,2})\s*"
                      r"(?:days?|nights?)\b", re.IGNORECASE)
_BARE_DAYS_RE = re.compile(r"\b(\d{1,2})\s*(?:days?|nights?)\b", re.IGNORECASE)
#: Seeing it is a question about your own sky, which the OVATION model answers per grid cell.
_VISIBILITY_WORDS = re.compile(
    r"\b(visib\w*|see the aurora|see it|chance of aurora|will i see|overhead|probability|"
    r"northern lights|aurora tonight|tonight|lucky)\b", re.IGNORECASE)
_FORECAST_WORDS = re.compile(
    r"\b(forecast|outlook|predicted|prediction|next \d+ days?|this week|weekend|coming days?)\b",
    re.IGNORECASE)
_STORM_WORDS = re.compile(r"\b(storm|g[1-5]\b|substorm|geomagnetic)\b", re.IGNORECASE)
#: Naming a place and the aurora together is a question about the sky overhead.
_AURORA_SUBJECT_RE = re.compile(r"\b(aurora|auroras|northern lights)\b", re.IGNORECASE)
#: "...in Atlantis" with no match in the place list: a name the caller gave that this agent
#: cannot resolve. Reported honestly instead of being treated as "no place given".
_UNKNOWN_PLACE_RE = re.compile(
    r"\b(?:in|at|near|off|around|for|from|over|with|than|and)\s+(?:the\s+|a\s+|an\s+)?"
    r"([A-Z][\w'\-]{2,20}(?:\s+[A-Z][\w'\-]{2,20})?)")
_IGNORED_PLACES = frozenset({"The", "Us", "USA", "My", "Here", "This", "It", "There", "Today",
                             "Tomorrow", "Tonight", "Now"})


def place_from_text(text: str) -> str | None:
    """A known place from the built-in list. Longest names win ('san jose' before 'jose').

    Letters outside ASCII count: Tromsø is spelled with an ø and must still resolve.
    """
    lowered = " " + re.sub(r"\s+", " ", re.sub(r"[^\w ]+", " ", text.lower())).strip() + " "
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


def days_from_text(text: str) -> int | None:
    match = _DAYS_RE.search(text) or _BARE_DAYS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    point = point_from_text(text)
    place = place_from_text(text) or (unknown_place_from_text(text) if not point else None)
    if point:
        params["point"] = point
    elif place:
        params["place"] = place

    # "Will I see it tonight?" is a question about the sky overhead, so it goes to the
    # OVATION model even when the caller also says "forecast" — as does naming a place
    # alongside the aurora itself.
    if _VISIBILITY_WORDS.search(text) or (params and _AURORA_SUBJECT_RE.search(text)):
        return "aurora-visibility", params

    if _FORECAST_WORDS.search(text) or days_from_text(text) is not None:
        params["days"] = days_from_text(text) or DEFAULT_FORECAST_DAYS
        return "aurora-forecast", params

    return "aurora-now", params


def resolve_point(params: dict, data: SpaceWeatherData) -> tuple[float, float, str] | None:
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


def _who(label: str, lat: float, lon: float) -> str:
    """'Fairbanks (64.84,-147.72)', but not 'the point 64.84,-147.72 (64.84,-147.72)'."""
    return label if label.startswith("the point") else f"{label} ({lat},{lon})"


def _kp(value) -> str:
    return "not published" if value is None else f"Kp {float(value):g}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _storm_words(kp) -> str:
    band = STORM_SCALE.get(int(min(round(float(kp or 0)), 9)))
    return f"a {band} storm" if band else "no storm"


class AuroraAgent(AcpAgent):
    name = "aurora"
    title = "Aurora — NOAA space weather"
    version = "1.0.0"

    def __init__(self, connection=None, data: SpaceWeatherData | None = None) -> None:
        super().__init__(connection)
        self.data = data or SpaceWeatherData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Aurora session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read NOAA SWPC's product", "medium"),
            ("Answer with the index, its band and the product timestamp", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Space weather comes from NOAA's Space Weather Prediction Center; Kp is "
                        "a planetary index, and OVATION's probability is for aurora overhead.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read the SWPC product for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Aurora to read NOAA's public space-weather products?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public space-weather products before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (SpaceWeatherError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the space-weather product: {exc}")
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
        if skill == "aurora-forecast":
            return self._forecast(params)
        if skill == "aurora-visibility":
            return self._visibility(params)
        if skill == "aurora-now":
            return self._now(params)
        return HELP, {"summary": "help", "dataset": None}

    def _now(self, params: dict) -> tuple[str, dict]:
        read = self.data.recent(hours=24)
        now = read["now"]
        peak_kp = read["peak_kp"]
        artifact = {
            "summary": (f"{DATASET_KP_1M}: estimated Kp {now.get('estimated_kp')} "
                        f"({now.get('band')}) at {now.get('time_tag')}"),
            "dataset": DATASET_KP_1M,
            "source": "NOAA Space Weather Prediction Center",
            "time_tag": now.get("time_tag"),
            "estimated_kp": now.get("estimated_kp"),
            "kp_index": now.get("kp_index"),
            "band": now.get("band"),
            "storm": now.get("storm"),
            "three_hourly": now.get("three_hourly"),
            "window_hours": read["window_hours"],
            "peak_kp": peak_kp,
            "peak_band": read["peak_band"],
            "peak_time_tag": read["peak_time_tag"],
            "rows": read["rows"],
        }
        three = now.get("three_hourly") or {}
        lines = [
            f"Geomagnetic conditions right now: estimated Kp {now.get('estimated_kp')} "
            f"({now.get('band')}) as of {now.get('time_tag')} UTC.",
            f"  • The 3-hourly planetary index reads {_kp(three.get('kp'))} for the interval "
            f"beginning {three.get('time_tag')} UTC, from {three.get('station_count')} stations "
            f"(a_running {three.get('a_running')}).",
            f"  • Peak of the last {read['window_hours']} hours: {_kp(peak_kp)} "
            f"({read['peak_band']}) at {read['peak_time_tag']} UTC.",
        ]
        if now.get("storm"):
            lines.append(f"  • That is {_storm_words(now.get('estimated_kp'))}: NOAA's geomagnetic "
                         f"storm scale runs G1 to G5 from Kp 5 up.")
        else:
            lines.append("  • Below Kp 5, which is NOAA's storm threshold — quiet-to-active "
                         "conditions, not a storm.")
        lines.append(
            f"\nRead live from {DATASET_KP_1M} and {DATASET_KP} (NOAA SWPC). Kp is a planetary "
            f"index averaged over stations, not a local sky forecast, and it says nothing about "
            f"cloud cover where you are."
        )
        return "\n".join(lines), artifact

    def _forecast(self, params: dict) -> tuple[str, dict]:
        days = self.data.check_days(params.get("days") or DEFAULT_FORECAST_DAYS)
        read = self.data.forecast(days)
        artifact = {
            "summary": (f"swpc.noaa.gov/planetary-k-index-forecast: {_plural(len(read['days']), 'day')}, "
                        f"peak {_kp(read['peak_kp'])} ({read['peak_band']})"),
            "dataset": read["dataset"],
            "source": "NOAA Space Weather Prediction Center",
            "days": read["days"],
            "predicted_rows": read["predicted_rows"],
            "other_rows": read["other_rows"],
            "peak_kp": read["peak_kp"],
            "peak_band": read["peak_band"],
            "storm_days": read["storm_days"],
        }
        if not read["days"]:
            return (
                "SWPC's forecast file carries no predicted Kp rows for the days still to come, so "
                "there is nothing to report right now. Read live from "
                f"{read['dataset']}.",
                artifact,
            )
        lines = [
            f"Predicted planetary K index for the next {_plural(len(read['days']), 'day')} "
            f"(NOAA SWPC, {read['dataset']}):"
        ]
        for entry in read["days"]:
            flag = f" — {STORM_SCALE.get(int(min(round(entry['max_kp']), 9)))} storm" if entry["storm"] else ""
            lines.append(f"  • {entry['date']}  peak Kp {entry['max_kp']:g} "
                         f"({entry['band']}, {_plural(len(entry['rows']), 'interval')}){flag}")
        lines.append(f"  • Peak across the window: {_kp(read['peak_kp'])} ({read['peak_band']}).")
        if read["storm_days"]:
            lines.append(f"  • Storm-level days: {', '.join(read['storm_days'])}.")
        else:
            lines.append("  • No day in this window reaches Kp 5, NOAA's storm threshold.")
        lines.append(
            f"\nRead live from {read['dataset']} ({read['predicted_rows']} predicted rows; the file "
            f"also carries {read['other_rows']} observed or estimated rows for the past week). This "
            f"is a model forecast: SWPC's own confidence falls off sharply beyond about a day, and "
            f"a Kp number is not a promise that your sky will be clear."
        )
        return "\n".join(lines), artifact

    def _visibility(self, params: dict) -> tuple[str, dict]:
        resolved = resolve_point(params, self.data)
        if not resolved:
            asked_for = params.get("place")
            if asked_for:
                message = (f"I do not know the place {asked_for!r}, so I will not guess coordinates "
                           "for it. Give me a latitude/longitude point or a city from my list.")
            else:
                message = ("Tell me where you are: a city from my list (Fairbanks, Tromsø, "
                           "Reykjavík, Duluth…) or a latitude/longitude point like 64.84,-147.72. "
                           "Aurora probability is per location.")
            return message, {"summary": "unknown place", "dataset": DATASET_OVATION,
                             "place": asked_for, "known": False}
        lat, lon, label = resolved
        read = self.data.aurora_probability(lat, lon)
        probability = read["probability"]
        artifact = {
            "summary": (f"{DATASET_OVATION}: {probability}% aurora probability at {label} for the "
                        f"model run {read['forecast_time']}"),
            "dataset": DATASET_OVATION,
            "source": "NOAA Space Weather Prediction Center (OVATION model)",
            "place": label,
            "latitude": lat,
            "longitude": lon,
            "probability": probability,
            "max_probability_nearby": read["max_probability_nearby"],
            "nearest_cell": read["nearest_cell"],
            "radius_degrees": read["radius_degrees"],
            "observation_time": read["observation_time"],
            "forecast_time": read["forecast_time"],
            "cells_read": read["cells_read"],
        }
        lines = [
            f"Aurora probability at {_who(label, lat, lon)}: {probability}% overhead, from NOAA's "
            f"OVATION model.",
            f"  • Model obs time {read['observation_time']}, forecast time {read['forecast_time']} — "
            f"the nearest grid cell is {read['nearest_cell']['latitude']},"
            f"{read['nearest_cell']['longitude']}, and the best cell within "
            f"{read['radius_degrees']:g}° reads {read['max_probability_nearby']}%.",
        ]
        if probability >= 50:
            lines.append("  • That is a strong model signal: if the sky is dark and clear, look up.")
        elif probability >= 10:
            lines.append("  • That is a modest signal: the aurora is modelled overhead at times, "
                         "low on the horizon is the usual catch.")
        else:
            lines.append("  • That is a low signal for this location right now; higher latitudes "
                         "and stronger Kp raise it.")
        lines.append(
            f"\nRead live from {DATASET_OVATION} (NOAA SWPC), a {read['cells_read']:,}-cell global "
            f"grid. This is the probability of aurora *overhead* — it says nothing about clouds, "
            f"moonlight or local light pollution, so check a weather forecast too. The value is a "
            f"model output for a grid cell, not an observation from your window."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        AuroraAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
