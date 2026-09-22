"""Vehicles — NHTSA safety recalls inside your editor.

The fifth non-coding ACP agent in this repo, and the first vehicle-safety agent of any
kind: it answers whether a car has open recalls, what other owners complained about,
which models a make sold in a given year, and what a VIN decodes to — straight from
NHTSA's own public services.

Deterministic on purpose: routing is rules, every fact comes from NHTSA, and nothing is
guessed. It reports a plan, opens one tool call per lookup, asks permission before the
first read, streams the answer, and closes the tool call with a one-line summary of the
dataset it read.

The one thing it will not do is pretend to know a vehicle it did not read: if only a make
is given it reads NHTSA's model list back and asks which model instead of guessing.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (  # noqa: E402
    DATASET_COMPLAINTS,
    DATASET_MODELS,
    DATASET_RECALLS,
    DATASET_VIN,
    VehicleData,
    VehicleError,
)

HELP = (
    "I read NHTSA's public safety data — the agency that tracks recalls and owner complaints.\n"
    "Ask me:\n"
    "  • does a 2020 Honda Civic have recalls?\n"
    "  • what do owners complain about on a 2018 Ford F-150?\n"
    "  • what models did Toyota sell in 2024?\n"
    "  • decode VIN 5UXWX7C5*BA\n"
    "Every answer names the NHTSA dataset it came from. I report what the agency published; "
    "I am not a mechanic and this is not a safety inspection."
)

PERMISSION_KEY = "vehicles-read-public-nhtsa"

SKILLS = ("vehicle-recalls", "vehicle-complaints", "vehicle-models", "decode-vin", "help")

_YEAR_RE = re.compile(r"\b(19[4-9]\d|20\d{2})\b")
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9*]{11,17}\b", re.IGNORECASE)
_RECALL_RE = re.compile(r"\b(recalls?|recalled|safety campaign|park (?:it|outside)|open recall)\b", re.IGNORECASE)
_COMPLAINT_RE = re.compile(r"\b(complaints?|complain|problems?|issues?|gripes?|owner reports?)\b", re.IGNORECASE)
_MODEL_RE = re.compile(r"\b(models?|builds|trim levels?|sold)\b", re.IGNORECASE)
_VIN_WORD_RE = re.compile(r"\bvin\b", re.IGNORECASE)

#: Words that are never a make or model, used to trim a phrase down to the vehicle.
_STOPWORDS = frozenset({
    "a", "an", "the", "my", "our", "this", "that", "these", "those", "for", "of", "on", "in", "about",
    "any", "all", "does", "do", "is", "are", "was", "were", "has", "have", "had", "get", "got", "with",
    "any", "many", "some", "what", "which", "how", "did", "does", "sell", "sells", "any", "and", "to", "from", "by", "at", "it", "its",
    "there", "here", "about", "complain", "complaining", "reported", "reports",
    "recall", "recalls", "recalled", "complaint", "complaints", "problem", "problems", "issue", "issues",
    "safety", "campaign", "campaigns", "model", "models", "sold", "build", "builds", "vin", "number",
    "car", "cars", "vehicle", "vehicles", "truck", "trucks", "suv", "suvs", "sedan", "sedans", "please",
})


def _clean(words: list[str]) -> list[str]:
    """Trim stopwords off both ends of a phrase, leaving the vehicle words."""
    start, end = 0, len(words)
    while start < end and words[start].lower().strip("?.,!") in _STOPWORDS:
        start += 1
    while end > start and words[end - 1].lower().strip("?.,!") in _STOPWORDS:
        end -= 1
    return [word.strip("?.,!;") for word in words[start:end]]


def _vehicle_words(text: str) -> tuple[str | None, str | None]:
    """Find (make, model) in a sentence, by the words next to a model year.

    'does a 2020 honda civic have recalls' -> ('honda', 'civic')
    'honda civic 2020 recalls'             -> ('honda', 'civic')
    'honda recalls 2020'                   -> ('honda', None)
    """
    words = text.split()
    year_at = next((index for index, word in enumerate(words) if _YEAR_RE.fullmatch(word.strip("?.,!"))), None)
    if year_at is None:
        # No year: the vehicle words are the last few that are not stopwords.
        candidates = [_clean(_clean(words))[-3:]]
    else:
        # The make and model sit immediately next to the year, before or after it.
        candidates = [_clean(words[max(0, year_at - 3):year_at]),
                      _clean(words[year_at + 1:year_at + 4])]

    for phrase in candidates:
        phrase = [word for word in phrase if word]
        if not phrase:
            continue
        if len(phrase) == 1:
            return phrase[0], None
        return phrase[0], " ".join(phrase[1:])
    return None, None


def _year(text: str) -> int | None:
    match = _YEAR_RE.search(text)
    return int(match.group(1)) if match else None


def _first_vehicle_word(text: str) -> str | None:
    """The first word that could be a make — 'what models did Toyota sell in 2024' -> Toyota."""
    for word in text.split():
        word = word.strip("?.,!;")
        if word and word.lower() not in _STOPWORDS and not _YEAR_RE.fullmatch(word):
            return word
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    make, model = _vehicle_words(text)
    year = _year(text)
    params: dict = {}
    if make:
        params["make"] = make
    if model:
        params["model"] = model
    if year:
        params["year"] = year

    if _VIN_WORD_RE.search(text):
        tokens = [token.strip("?.,!;") for token in text.split()]
        vin = next((token for token in tokens if _VIN_RE.fullmatch(token)), None)
        if not vin:
            at = next((index for index, token in enumerate(tokens) if token.lower() == "vin"), None)
            if at is not None and at + 1 < len(tokens):
                vin = tokens[at + 1]  # whatever follows 'vin', so a bad VIN gets a real error
        return "decode-vin", ({"vin": vin} if vin else {})

    bare_vin = next((token.strip("?.,!") for token in text.split() if _VIN_RE.fullmatch(token.strip("?.,!"))), None)
    if bare_vin and len(text.split()) == 1:
        params["vin"] = bare_vin
        return "decode-vin", params

    if _MODEL_RE.search(text) and not _RECALL_RE.search(text) and not _COMPLAINT_RE.search(text):
        make = params.get("make") if params.get("make") else _first_vehicle_word(text)
        return "vehicle-models", {"make": make, "year": params.get("year")}

    if _COMPLAINT_RE.search(text) and not _RECALL_RE.search(text):
        return "vehicle-complaints", params

    return "vehicle-recalls", params


def _recall_flags(recall: dict) -> str:
    flags = []
    if recall.get("park_it"):
        flags.append("DO NOT DRIVE (park it)")
    if recall.get("park_outside"):
        flags.append("park outside")
    if recall.get("over_the_air_update"):
        flags.append("fixable over the air")
    return f" — {', '.join(flags)}" if flags else ""


class VehiclesAgent(AcpAgent):
    name = "vehicles"
    title = "Vehicles — NHTSA safety recalls"
    version = "1.0.0"

    def __init__(self, connection=None, data: VehicleData | None = None) -> None:
        super().__init__(connection)
        self.data = data or VehicleData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Vehicles session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read NHTSA's own dataset", "medium"),
            ("Answer with the dataset id, campaign or complaint numbers", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("NHTSA publishes recalls by campaign and complaints by ODI number.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read NHTSA public data for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Vehicles to read NHTSA's public safety APIs?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read NHTSA's public data before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (VehicleError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the NHTSA dataset: {exc}")
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
        if skill == "vehicle-recalls":
            return self._recalls(params)
        if skill == "vehicle-complaints":
            return self._complaints(params)
        if skill == "vehicle-models":
            return self._models(params)
        if skill == "decode-vin":
            return self._decode(params)
        return HELP, {"summary": "help", "dataset": None}

    def _missing_vehicle(self, make: str | None) -> tuple[str, dict]:
        """No model given: read NHTSA's own model list and ask, instead of guessing."""
        if not make:
            return (
                "I need a make and a model year to look this up — for example, "
                "“does a 2020 Honda Civic have recalls?”.",
                {"summary": "no vehicle given", "dataset": None},
            )
        year = datetime.now().year
        result = self.data.models(make, year)
        names = ", ".join(result["models"][:12])
        return (
            f"I need a model too. For the {year} model year NHTSA lists these {result['make']} models: "
            f"{names}{', and more' if result['truncated'] else ''}.\n"
            f"Ask again with one of them and a model year, for example “{result['make']} {year} "
            f"{(result['models'] or ['Accord'])[0]} recalls”.",
            {"summary": f"{DATASET_MODELS}: {result['count']} model(s) for {result['make']} {year}",
             "dataset": DATASET_MODELS, "make": result["make"], "year": year, "models": result["models"][:25]},
        )

    def _recalls(self, params: dict) -> tuple[str, dict]:
        make, model = params.get("make"), params.get("model")
        if not make or not model:
            return self._missing_vehicle(make)
        year = int(params.get("year") or datetime.now().year)
        assumed = "" if params.get("year") else f" (no model year given, so I used {year})"
        result = self.data.recalls(make, model, year)
        recalls = result["recalls"]
        label = f"{result['year']} {result['make']} {result['model']}"
        if not recalls:
            return (
                f"NHTSA has no recall on record for the {label}{assumed}. That means the agency's "
                f"recall dataset returned no campaign for that make, model and year — it does not mean "
                f"the car is free of every defect. Check the model spelling with “what {result['make']} "
                f"models are there for {result['year']}?”.",
                {"summary": f"{DATASET_RECALLS}: 0 recalls for {label}", "dataset": DATASET_RECALLS,
                 "count": 0, "make": result["make"], "model": result["model"], "year": result["year"]},
            )
        listing = "\n".join(
            f"  • {item['campaign']} — {item['component'] or 'component not stated'}"
            f" (reported {item['reported'] or 'date not stated'}){_recall_flags(item)}\n"
            f"    {item['consequence'] or 'consequence not stated'}\n"
            f"    Fix: {item['remedy'] or 'remedy not stated'}"
            for item in recalls[:8]
        )
        urgent = [item for item in recalls if item["park_it"] or item["park_outside"]]
        headline = ""
        if urgent:
            headline = (f"⚠ NHTSA flags {len(urgent)} of these for parking: "
                        f"{', '.join(sorted({item['campaign'] or '' for item in urgent}))}. "
                        "Follow the agency's instruction before driving.\n\n")
        answer = (
            f"{headline}NHTSA has {result['count']} recall campaign(s) on record for the {label}{assumed}:\n"
            f"{listing}\n"
            + (f"  … and {len(recalls) - 8} more.\n" if len(recalls) > 8 else "")
            + f"\nSource: {DATASET_RECALLS} (read live from api.nhtsa.gov). Recalls are free to fix at a "
            "franchised dealer."
        )
        return answer, {
            "summary": f"{DATASET_RECALLS}: {result['count']} recall(s) for {label}",
            "dataset": DATASET_RECALLS,
            "make": result["make"], "model": result["model"], "year": result["year"],
            "count": result["count"], "recalls": recalls,
        }

    def _complaints(self, params: dict) -> tuple[str, dict]:
        make, model = params.get("make"), params.get("model")
        if not make or not model:
            return self._missing_vehicle(make)
        year = int(params.get("year") or datetime.now().year)
        assumed = "" if params.get("year") else f" (no model year given, so I used {year})"
        result = self.data.complaints(make, model, year)
        label = f"{result['year']} {result['make']} {result['model']}"
        if not result["count"]:
            return (
                f"NHTSA has no owner complaint on record for the {label}{assumed}.",
                {"summary": f"{DATASET_COMPLAINTS}: 0 complaints for {label}",
                 "dataset": DATASET_COMPLAINTS, "count": 0},
            )
        components = "\n".join(
            f"  • {item['component']}: {item['complaints']} complaint(s)" for item in result["top_components"]
        )
        recent_lines = []
        for item in result["recent"]:
            hurts = "".join((", injury reported" if item["injuries"] else "",
                             ", death reported" if item["deaths"] else "",
                             ", fire" if item["fire"] else ""))
            line = (f"  • ODI {item['odi_number']} ({item['filed'] or 'date not stated'}) — "
                    f"{item['component'] or 'component not stated'}{hurts}")
            if item.get("summary"):
                line += f"\n    “{item['summary'][:220]}"
            recent_lines.append(line)
        recent = "\n".join(recent_lines)
        answer = (
            f"NHTSA has {result['count']} owner complaint(s) on the {label}{assumed}. "
            f"In that set owners reported {result['crashes']} crash(es), {result['fires']} fire(s), "
            f"{result['injuries']} injur(ies) and {result['deaths']} death(s).\n"
            f"Most complained-about components:\n{components}\n"
            f"Most recent filings (dates exactly as NHTSA published them):\n{recent}\n\n"
            f"Source: {DATASET_COMPLAINTS} (read live from api.nhtsa.gov). Complaints are owner reports, "
            "not agency findings — a recall is the finding."
        )
        return answer, {
            "summary": f"{DATASET_COMPLAINTS}: {result['count']} complaint(s) for {label}",
            "dataset": DATASET_COMPLAINTS,
            "make": result["make"], "model": result["model"], "year": result["year"],
            "count": result["count"], "injuries": result["injuries"], "deaths": result["deaths"],
            "crashes": result["crashes"], "fires": result["fires"],
            "top_components": result["top_components"], "recent": result["recent"],
        }

    def _models(self, params: dict) -> tuple[str, dict]:
        make = params.get("make")
        if not make:
            return (
                "Tell me a make and I will read NHTSA's model list — for example, "
                "“what models did Toyota sell in 2024?”.",
                {"summary": "no make given", "dataset": None},
            )
        year = int(params.get("year") or datetime.now().year)
        result = self.data.models(make, year)
        if not result["count"]:
            return (
                f"NHTSA lists no {result['make']} models for {year}. Check the make spelling — the agency "
                "files makes in its own list, and not every brand sells in the United States.",
                {"summary": f"{DATASET_MODELS}: 0 models for {result['make']} {year}",
                 "dataset": DATASET_MODELS, "make": result["make"], "year": year},
            )
        listing = ", ".join(result["models"])
        answer = (
            f"NHTSA lists {result['count']} {result['make']} model(s) for the {year} model year:\n"
            f"  {listing}{', …' if result['truncated'] else ''}\n\n"
            f"Source: {DATASET_MODELS} (read live from api.nhtsa.gov). These are the names the agency's "
            "recall data is filed under, so they are the exact spellings to ask about."
        )
        return answer, {
            "summary": f"{DATASET_MODELS}: {result['count']} model(s) for {result['make']} {year}",
            "dataset": DATASET_MODELS, "make": result["make"], "year": year,
            "models": result["models"],
        }

    def _decode(self, params: dict) -> tuple[str, dict]:
        vin = params.get("vin")
        if not vin:
            return (
                "Give me the VIN and I will decode it — for example, “decode VIN 5UXWX7C5*BA”.",
                {"summary": "no VIN given", "dataset": None},
            )
        result = self.data.decode_vin(vin)
        vehicle = result["vehicle"]
        if not vehicle:
            return (
                f"NHTSA's decoder returned nothing for VIN {result['vin']}"
                + (f" ({result['error_text']})" if result["error_text"] else "") + ".",
                {"summary": f"{DATASET_VIN}: no fields for {result['vin']}", "dataset": DATASET_VIN},
            )
        label = " ".join(filter(None, [vehicle.get("ModelYear"), vehicle.get("Make"), vehicle.get("Model")]))
        listing = "\n".join(f"  • {field}: {value}" for field, value in vehicle.items())
        honesty = (
            ""
            if result["fully_decoded"]
            else f"\nNHTSA's decoder reported: {result['error_text'] or result['error_code']}. "
                 "Fields it could not resolve are left out rather than guessed."
        )
        answer = (
            f"VIN {result['vin']} decodes to {label or 'an unidentified vehicle'}:\n{listing}\n"
            f"  • fields resolved: {result['fields_decoded']}\n\n"
            f"Source: {DATASET_VIN} (read live from vpic.nhtsa.dot.gov, the decoder the US government "
            f"publishes for exactly this).{honesty}"
        )
        return answer, {
            "summary": f"{DATASET_VIN}: {label or 'no label'} ({result['fields_decoded']} fields)",
            "dataset": DATASET_VIN, "vin": result["vin"], "vehicle": vehicle,
            "fully_decoded": result["fully_decoded"], "error_text": result["error_text"],
        }


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        VehiclesAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
