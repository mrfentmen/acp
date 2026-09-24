"""DXF — read an AutoCAD drawing file inside your editor.

The eleventh ACP agent in this repo, and the first computer-aided-design agent in any editor
protocol: it opens a ``.dxf`` drawing — the format every CAD program can read and write — and
answers what is in it, what is on each layer, what the blocks are, and what is wrong with it.

Deterministic on purpose: routing is rules, every number is counted out of the file itself,
and nothing is guessed. It reports a plan, opens one tool call per read, asks permission before
the first one, streams the answer and closes the tool call with a one-line summary.

Honest by construction: a drawing's size is the bounding box of the geometry that can be
measured here, block references contribute their insertion point rather than an expansion of
the block's contents, fills and areas are counted but never given a length, the checks are
file-level checks and not AutoCAD's AUDIT or a DRC, and a file this agent cannot read is
refused with the reason instead of guessed at.
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
    DATASET,
    DXF_EXTENSION,
    EXAMPLE_LIMIT,
    SAMPLES,
    DxfData,
    DxfError,
    block_report,
    compare_drawings,
    drawing_checks,
    layer_report,
    round_value,
)

HELP = (
    "I read AutoCAD DXF drawings — the one drawing format every CAD program can open — from a "
    "URL or from my built-in set of real DXF files.\n"
    "Ask me:\n"
    "  • what is in the usa drawing?\n"
    "  • how many layers does the houses-of-parliament drawing have?\n"
    "  • what blocks are in the text drawing?\n"
    "  • check the uncommon drawing\n"
    "  • compare the usa and leica drawings\n"
    "  • inspect https://…/my-drawing.dxf\n"
    "I count what the file says: format, units, layers and their colours, block definitions and "
    "inserts, every drawing object by type, how much geometry is drawn, the extents the header "
    "claims, and the text the drawing carries. My checks are file-level checks, not AutoCAD's "
    f"AUDIT and not a DRC. Built-in drawings: {', '.join(sorted(SAMPLES))}."
)

PERMISSION_KEY = "dxf-read-drawing"

SKILLS = ("drawing-summary", "layer-report", "block-report", "drawing-check", "compare", "help")

#: "check it", "anything wrong", "audit this" — a question about problems.
_CHECK_WORDS = re.compile(
    r"\b(check|checks|checking|validate|validated|validation|audit|review|problems?|issues?|"
    r"errors?|wrong|flags?|missing|sanity|clean|healthy|broken|faults?|defects?|"
    r"off|frozen|stale|empty)\b",
    re.IGNORECASE)

#: "compare", "are they the same", "diff these" — a question about two drawings.
_COMPARE_WORDS = re.compile(
    r"\b(compare|compared|comparison|versus|vs\.?|against|diff|differences?|same as|"
    r"identical|match(?:es|ing)?)\b",
    re.IGNORECASE)

#: Words that mean the layer table.
_LAYER_WORDS = re.compile(
    r"\b(layers?|layer table|linetypes?|colou?rs?|defpoints|lineweights?)\b", re.IGNORECASE)

#: Words that mean the block table and its inserts.
_BLOCK_WORDS = re.compile(
    r"\b(blocks?|inserts?|inserted|xrefs?|references?|symbols?)\b", re.IGNORECASE)

#: A drawing URL in the text (trailing punctuation is not part of it).
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


def urls_from_text(text: str) -> list[str]:
    """Every http(s) URL in the text, in order, with trailing punctuation trimmed."""
    found: list[str] = []
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    return found


def samples_from_text(text: str) -> list[str]:
    """Every built-in demo named in the text, longest alias first within each match."""
    flat = " " + re.sub(r"\s+", " ",
                        re.sub(r"[^A-Za-z0-9._ -]+", " ", text.lower())).strip() + " "
    flat = flat.replace("-", " ").replace("_", " ")
    found: list[tuple[int, str]] = []
    for key, sample in SAMPLES.items():
        for alias in (key, *sample["aliases"]):
            phrase = " " + alias.replace("-", " ").replace("_", " ") + " "
            if phrase in flat:
                found.append((flat.index(phrase), key))
                break
    ordered: list[str] = []
    for _position, key in sorted(found):
        if key not in ordered:
            ordered.append(key)
    return ordered


def sample_from_text(text: str) -> str | None:
    """The first built-in demo named in the text, if any."""
    found = samples_from_text(text)
    return found[0] if found else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    urls = urls_from_text(text)
    samples = samples_from_text(text)
    checking = bool(_CHECK_WORDS.search(text))
    comparing = bool(_COMPARE_WORDS.search(text))

    params: dict = {}
    if samples:
        params["samples"] = samples
    if urls:
        params["urls"] = urls

    # A comparison is decided first: it needs two drawings whatever else the question says.
    if comparing:
        return "compare", params
    if checking:
        return "drawing-check", params
    if _BLOCK_WORDS.search(text):
        return "block-report", params
    if _LAYER_WORDS.search(text):
        return "layer-report", params
    return "drawing-summary", params


def _plural(count: int, word: str, plural: str | None = None) -> str:
    """'1 layer' / '3 layers' — a count that reads right at one."""
    if count == 1:
        return f"{count} {word}"
    return f"{count} {plural or word + 's'}"


def _units(value, unit: str = "units") -> str:
    """A measurement without a trailing zero drizzle."""
    if value is None:
        return "unknown"
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def _tally(counts: dict, limit: int = EXAMPLE_LIMIT) -> str:
    """'LINE 10, TEXT 4' — a count map as one readable line."""
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    shown = ", ".join(f"{name or '(none)'} {count}" for name, count in ordered[:limit])
    if len(ordered) > limit:
        shown += f", +{len(ordered) - limit} more"
    return shown or "none"


def _verdict(findings: list[dict]) -> str:
    """A plain verdict line, keeping faults apart from the entries that are only notes."""
    faults = [finding for finding in findings
              if finding["count"] and finding["severity"] == "fault"]
    notes = [finding for finding in findings
             if finding["count"] and finding["severity"] == "note"]
    if not faults and not notes:
        return "Nothing came up in these checks."
    verdict = []
    if faults:
        total = sum(finding["count"] for finding in faults)
        verdict.append(f"{_plural(total, 'thing')} to look at across "
                       f"{_plural(len(faults), 'check')}")
    if notes:
        total = sum(finding["count"] for finding in notes)
        if verdict:
            verdict.append(f"plus {_plural(total, 'note')} (empty layers and blocks, unused "
                           f"definitions, an unset drawing unit or extents) that are not faults")
        else:
            verdict.append(f"no faults; {_plural(total, 'note')} listed for information")
    return "; ".join(verdict) + ". Each line names what the file says and nothing more."


def _finding_lines(findings: list[dict]) -> list[str]:
    """One line per check, with a marker that says whether it is a fault or a note."""
    lines: list[str] = []
    for finding in findings:
        if not finding["count"]:
            lines.append(f"  ✓ {finding['check']}: none")
            continue
        marker = "✗" if finding["severity"] == "fault" else "•"
        lines.append(f"  {marker} {finding['check']}: {finding['count']}")
        if finding["items"]:
            lines.append("      " + "; ".join(finding["items"]))
        lines.append(f"      {finding['detail']}")
    return lines


class DxfAgent(AcpAgent):
    name = "dxf"
    title = "DXF — AutoCAD drawings"
    version = "1.0.0"

    def __init__(self, connection=None, data: DxfData | None = None) -> None:
        super().__init__(connection)
        self.data = data or DxfData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "DXF session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        plan = [(f"Route the request ({skill})", "high")]
        if skill == "compare":
            plan.append(("Read both drawings", "medium"))
            plan.append(("Line the layers, blocks and objects up", "medium"))
        elif skill == "drawing-check":
            plan.append(("Read the DXF drawing", "medium"))
            plan.append(("Run the file-level checks", "medium"))
        elif skill in ("drawing-summary", "layer-report", "block-report"):
            plan.append(("Read the DXF drawing", "medium"))
            plan.append(("Count what the file says", "medium"))
        ctx.plan(plan)

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Drawings come from a URL you give me or from the DXF files I ship links "
                        "for; my checks are file-level, not AutoCAD's AUDIT.")
            return STOP_END_TURN

        problem = self._missing_input(skill, params)
        if problem:
            # Nothing to read, so no permission is asked and no tool call is opened.
            ctx.message(problem)
            return STOP_END_TURN

        references = self._plan_reads(skill, params)
        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read {len(references)} DXF drawing(s) for {skill}", kind="read",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow DXF to read these drawing files?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read that drawing before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, references)
        except (DxfError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that drawing: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is one or two HTTP reads of a few hundred kilobytes; nothing to interrupt.
        return None

    # -- routing help ------------------------------------------------------

    def _plan_reads(self, skill: str, params: dict) -> list[str]:
        """What to read, resolved without touching the network."""
        samples = params.get("samples") or []
        urls = params.get("urls") or []
        if skill == "compare":
            if len(urls) >= 2:
                for url in urls[:2]:
                    try:
                        self.data.kind_of(url)
                    except ValueError as exc:
                        raise ValueError(
                            f"both drawings in a comparison have to be {DXF_EXTENSION} files: "
                            f"{exc}") from exc
                return urls[:2]
            if len(samples) >= 2:
                return samples[:2]
            if len(urls) == 1 and len(samples) == 1:
                return [urls[0], samples[0]]
            if len(urls) == 1:
                raise ValueError(
                    "a comparison needs both drawings: give me the second URL, or name a second "
                    "one of the built-in drawings")
            if len(samples) == 1:
                raise ValueError(
                    "a comparison needs both drawings: name a second built-in drawing, or give "
                    "me the other URL")
            raise ValueError(
                "Tell me which two drawings to compare: two drawing URLs, or two of the built-in "
                f"drawings ({', '.join(sorted(SAMPLES))}).")
        if urls:
            # A URL that is not a .dxf is refused here, before any permission is asked for.
            self.data.kind_of(urls[0])
            return [urls[0]]
        return [samples[0]]

    def _missing_input(self, skill: str, params: dict) -> str | None:
        """What to say when there is nothing readable to ask permission for."""
        if params.get("urls") or params.get("samples"):
            try:
                self._plan_reads(skill, params)
            except ValueError as exc:
                return str(exc)
            return None
        return ("Tell me which drawing to read: a full URL to a .dxf file, or one of the built-in "
                f"drawings ({', '.join(sorted(SAMPLES))}). I do not read DWG, DXB or binary DXF "
                "files.")

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, references: list) -> tuple[str, dict]:
        if skill == "drawing-summary":
            return self._summary(references[0])
        if skill == "drawing-check":
            return self._summary(references[0], checks=True)
        if skill == "layer-report":
            return self._layers(references[0])
        if skill == "block-report":
            return self._blocks(references[0])
        if skill == "compare":
            return self._compare(references)
        return HELP, {"summary": "help", "dataset": None}

    def _summary(self, reference, checks: bool = False) -> tuple[str, dict]:
        drawing = self.data.read(reference)
        header = drawing["header"]
        entities = drawing["entities"]
        geometry = entities["geometry"]
        layers = layer_report(drawing)
        blocks = block_report(drawing)
        findings = drawing_checks(drawing) if checks else []
        units = header["insunits"]

        lines = [
            f"{drawing['label']} — DXF drawing, format {drawing['format']['name']}"
            f" ({drawing['format']['code'] or 'no version stamp'})",
            "",
        ]
        box = geometry["bbox"]
        if box:
            lines.append(f"Size: {_units(box[2] - box[0])} × {_units(box[3] - box[1])} "
                         f"(bounding box of {_plural(geometry['positioned'], 'placed object')})")
            lines.append(f"  • From {_units(box[0])}, {_units(box[1])} to "
                         f"{_units(box[2])}, {_units(box[3])}")
        else:
            lines.append("Size: no measurable geometry in this drawing, so it has no size")
        lines.append(f"Units: {units['name']}"
                     + ("" if units["declared"] else " ($INSUNITS is not in this file)")
                     + (f"; $MEASUREMENT {header['measurement']}"
                        if header["measurement"] is not None else ""))
        lines.append(f"Sections: {', '.join(drawing['sections']) or 'none'}"
                     + (", code page " + header["code_page"] if header["code_page"] else ""))
        lines.append(f"Layers: {_plural(layers['defined'], 'layer')} defined, "
                     f"{layers['used_count']} holding geometry, "
                     f"{len(layers['empty'])} empty")
        lines.append(f"Objects: {_plural(entities['count'], 'object')} — "
                     f"{_tally(entities['by_type']) or 'nothing'}")
        lines.append(f"  • In model space: {entities['by_space']['model']}, "
                     f"in paper space: {entities['by_space']['paper']}")
        if entities["vertices"]:
            lines.append(f"  • Polyline vertices: {entities['vertices']} "
                         f"({entities['closed_polylines']} closed polylines)")
        if geometry["measured"]:
            lines.append(f"Drawn length: {_units(geometry['length'])} across "
                         f"{_plural(geometry['measured'], 'object')} that carry a length "
                         "(lines, polylines, arcs and circles)")
        drawn_layers = sorted(((name, length) for name, length in
                               entities["by_layer_length"].items() if length), key=lambda pair: -pair[1])
        if drawn_layers:
            lines.append("  • Per layer: " + ", ".join(
                f"{name or '(no layer name)'} {_units(length)}" for name, length in drawn_layers[:EXAMPLE_LIMIT]))
        if geometry["unmeasured"]:
            lines.append(f"  • Counted but given no length (fills, areas and curve types that carry "
                         f"none): {_tally(geometry['unmeasured'])}")
        lines.append(f"Blocks: {_plural(blocks['defined'], 'block definition')}, "
                     f"{_plural(blocks['placed'], 'insert')} placing them")
        if entities["inserts"]:
            placed = {}
            for insert in entities["inserts"]:
                placed[insert["block"] or "(no block name)"] = placed.get(insert["block"] or "(no block name)", 0) + 1
            lines.append(f"  • Inserted: {_tally(placed)}")
        lines.append(f"Text: {_plural(entities['text_total'], 'string')}")
        for item in entities["texts"][:EXAMPLE_LIMIT]:
            value = item["value"] or "(empty string)"
            lines.append(f"  • {item['kind']} on {item['layer'] or '(no layer name)'}: "
                         f"{value[:60]}{'…' if len(value) > 60 else ''}")
        if entities["text_total"] > len(entities["texts"]):
            lines.append(f"  • … and {entities['text_total'] - len(entities['texts'])} more "
                         "strings, counted but not listed")
        if entities["layouts"]:
            lines.append("Layouts: " + _tally(entities["layouts"]))
        extents = drawing["extents"]
        if extents:
            lines.append(f"Extents: the header declares {_units(extents[0])}, {_units(extents[1])} "
                         f"to {_units(extents[2])}, {_units(extents[3])}")
        else:
            lines.append("Extents: the header declares none (never zoomed to, or not set)")
        if header["clayer"]:
            lines.append(f"Current layer in the header: {header['clayer']}"
                         + (f" (colour {header['cecolor']})" if header["cecolor"] else ""))

        if checks:
            lines.extend(["", "Checks (file-level, not AutoCAD AUDIT):"])
            lines.extend(_finding_lines(findings))
            lines.extend(["", _verdict(findings)])

        lines.extend([
            "",
            f"Read live from {drawing['url']} ({drawing['bytes']:,} bytes), {self._source_note(drawing)}. "
            "Every number here is counted out of the file itself: nothing is simulated, no drawing "
            "was rendered, and AUDIT was not run.",
        ])
        return "\n".join(lines), self._artifact(drawing, findings)

    def _layers(self, reference) -> tuple[str, dict]:
        drawing = self.data.read(reference)
        report = layer_report(drawing)
        findings = drawing_checks(drawing)

        lines = [
            f"{drawing['label']} — layers",
            "",
            f"{_plural(report['defined'], 'layer')} defined, {report['used_count']} holding "
            f"geometry, {report['entity_total']} objects in the file.",
            "",
        ]
        shown = report["rows"][:25]
        for row in shown:
            flags = []
            if row["off"]:
                flags.append("switched off")
            if row["frozen"]:
                flags.append("frozen")
            if row["locked"]:
                flags.append("locked")
            detail = f", {', '.join(flags)}" if flags else ""
            drawn = f", {_units(row['length'])} drawn" if row["length"] else ""
            note = (f", {_plural(row['unmeasured'], 'object')} with no length in the file"
                    if row["unmeasured"] else "")
            lines.append(f"  • {row['name'] or '(no layer name)'}: "
                         f"{_plural(row['entities'], 'object')}{drawn}, colour {row['color']}"
                         f", linetype {row['linetype'] or '(not set)'}{detail}{note}")
        if len(report["rows"]) > len(shown):
            lines.append(f"  • … and {len(report['rows']) - len(shown)} more layers")

        if report["orphan_layers"]:
            lines.extend(["", "Layers entities sit on that the table does not define:"])
            for name in report["orphan_layers"]:
                lines.append(f"  • {name or '(no layer name)'}")
        if report["empty"]:
            lines.extend(["", f"Empty layers ({len(report['empty'])}): "
                              + ", ".join(report["empty"][:EXAMPLE_LIMIT])])
        flagged = [finding for finding in findings
                   if finding["severity"] == "fault" and finding["count"]]
        lines.extend([
            "",
            f"Read live from {drawing['url']} ({drawing['bytes']:,} bytes). One layer per line, "
            "with the colour number and linetype exactly as the LAYER table writes them; "
            f"{len(flagged)} of the file-level checks fire on this drawing "
            "(ask me to check it for the details).",
        ])
        artifact = {
            "summary": (f"{report['defined']} layers defined, {report['used_count']} holding "
                        f"geometry, {len(report['empty'])} empty, {report['entity_total']} objects"),
            "dataset": DATASET,
            "source": "DXF drawings" if drawing.get("source") == "sample" else "URL",
            "url": drawing["url"],
            "label": drawing["label"],
            "lines": [{"name": row["name"], "objects": row["entities"], "color": row["color"],
                       "off": row["off"], "frozen": row["frozen"], "locked": row["locked"],
                       "length": round_value(row["length"], 3)} for row in report["rows"]],
            "orphan_layers": report["orphan_layers"],
            "empty_layers": report["empty"],
        }
        return "\n".join(lines), artifact

    def _blocks(self, reference) -> tuple[str, dict]:
        drawing = self.data.read(reference)
        report = block_report(drawing)

        lines = [
            f"{drawing['label']} — blocks",
            "",
            f"{_plural(report['defined'], 'block definition')}, {_plural(report['placed'], 'insert')} "
            "placing them from the ENTITIES section.",
            "",
        ]
        for row in report["rows"][:25]:
            kind = "anonymous" if row["anonymous"] else ("xref" if row["xref"] else "named")
            base = ", ".join(_units(value) for value in row["base"])
            contents = _tally(row["by_type"]) if row["by_type"] else "nothing"
            lines.append(f"  • {row['key']}: {_plural(row['entities'], 'object')} ({contents}), "
                         f"placed {_plural(row['inserts'], 'time')}, {kind}, base {base}"
                         + (f", on layer {row['layer']}" if row["layer"] else ""))
        if len(report["rows"]) > 25:
            lines.append(f"  • … and {len(report['rows']) - 25} more blocks")
        if report["undefined"]:
            lines.extend(["", "Inserts naming a block the file does not define:"])
            for name in report["undefined"]:
                lines.append(f"  • {name or '(no block name)'}")
        if report["unused"]:
            lines.extend(["", f"Defined but never inserted ({len(report['unused'])}): "
                              + ", ".join(report["unused"][:EXAMPLE_LIMIT])])
        if report["anonymous"]:
            lines.extend(["", f"Generated blocks ({len(report['anonymous'])}): "
                              + ", ".join(report["anonymous"][:EXAMPLE_LIMIT])])

        lines.extend([
            "",
            f"Read live from {drawing['url']} ({drawing['bytes']:,} bytes). A block's contents are "
            "counted once, as the definition writes them: inserts place that definition and are "
            "not expanded here, so a rotated or scaled insert contributes its insertion point and "
            "nothing else to this answer.",
        ])
        artifact = {
            "summary": (f"{report['defined']} block definitions, {report['placed']} inserts, "
                        f"{len(report['unused'])} never inserted, {len(report['undefined'])} undefined"),
            "dataset": DATASET,
            "source": "DXF drawings" if drawing.get("source") == "sample" else "URL",
            "url": drawing["url"],
            "label": drawing["label"],
            "blocks": [{"name": row["key"], "objects": row["entities"], "inserts": row["inserts"],
                        "anonymous": row["anonymous"], "xref": row["xref"]}
                       for row in report["rows"]],
            "unused": report["unused"],
            "undefined": report["undefined"],
        }
        return "\n".join(lines), artifact

    def _compare(self, references: list) -> tuple[str, dict]:
        left = self.data.read(references[0])
        right = self.data.read(references[1])
        comparison = compare_drawings(left, right)
        left_side = comparison["left"]
        right_side = comparison["right"]

        def size(side):
            if not side["size"]:
                return "no measurable geometry"
            return f"{_units(side['size'][0])} × {_units(side['size'][1])}"

        lines = [
            f"{left['label']} vs {right['label']}",
            "",
            f"Left: DXF {left_side['format']}, {_plural(left_side['layers'], 'layer')}, "
            f"{_plural(left_side['entities'], 'object')}, {_plural(left_side['blocks'], 'block')}, "
            f"{_units(left_side['length'])} drawn, {size(left_side)}",
            f"Right: DXF {right_side['format']}, {_plural(right_side['layers'], 'layer')}, "
            f"{_plural(right_side['entities'], 'object')}, {_plural(right_side['blocks'], 'block')}, "
            f"{_units(right_side['length'])} drawn, {size(right_side)}",
            "",
        ]
        for title, names in (("Layers only on the left", comparison["layers_only_left"]),
                            ("Layers only on the right", comparison["layers_only_right"]),
                            ("Object types only on the left", comparison["types_only_left"]),
                            ("Object types only on the right", comparison["types_only_right"]),
                            ("Blocks only on the left", comparison["blocks_only_left"]),
                            ("Blocks only on the right", comparison["blocks_only_right"])):
            if not names:
                continue
            shown = names[:EXAMPLE_LIMIT]
            lines.append(f"  • {title}: {len(names)} — " + ", ".join(shown)
                         + (f" … +{len(names) - len(shown)} more"
                            if len(names) > len(shown) else ""))
        lines.append(f"  • Shared: {len(comparison['layers_shared'])} layer names, "
                     f"{len(comparison['types_shared'])} object types, "
                     f"{len(comparison['blocks_shared'])} block names")
        if left_side["entities"] != right_side["entities"]:
            difference = right_side["entities"] - left_side["entities"]
            lines.append(f"  • Objects: {left_side['entities']} against "
                         f"{right_side['entities']} ({difference:+d})")
        if left_side["format"] != right_side["format"]:
            lines.append(f"  • The two files are written in different DXF versions "
                         f"({left_side['format']} and {right_side['format']})")
        matched = (left_side["format"] == right_side["format"]
                   and left_side["layers"] == right_side["layers"]
                   and left_side["entities"] == right_side["entities"]
                   and left_side["blocks"] == right_side["blocks"]
                   and not comparison["layers_only_left"] and not comparison["layers_only_right"]
                   and not comparison["types_only_left"] and not comparison["types_only_right"])
        lines.extend([
            "",
            ("These two drawings agree on the version, the layer names and the object totals. "
             "That is agreement about the files, not about the geometry: nothing here dissolves "
             "or overlays the two to find out whether they draw the same thing."
             if matched else
             "A name or a type on one side only is a real difference between the files: one "
             "drawing was built on a layer, a block or an object type the other never uses. The "
             "totals above say how much of that there is, and nothing here compares the geometry "
             "itself."),
            "",
            f"Read live from {left['url']} and {right['url']} "
            f"({left['bytes'] + right['bytes']:,} bytes together). This is a comparison of two "
            "files, not an overlay and not AutoCAD's drawing comparison.",
        ])
        artifact = {
            "summary": (f"left {left_side['layers']} layers / {left_side['entities']} objects, "
                        f"right {right_side['layers']} layers / {right_side['entities']} objects, "
                        f"{len(comparison['layers_shared'])} shared layer names"),
            "dataset": DATASET,
            "source": "DXF drawings",
            "left_url": left["url"],
            "right_url": right["url"],
            "left_format": left_side["format"],
            "right_format": right_side["format"],
            "match": bool(matched),
            **comparison,
        }
        return "\n".join(lines), artifact

    @staticmethod
    def _source_note(record: dict) -> str:
        """Where the drawing came from, said plainly."""
        if record.get("source") == "url":
            return "the URL you gave me"
        return "the demo repository's own file"

    @staticmethod
    def _artifact(record: dict, findings: list[dict]) -> dict:
        """A compact record of what the file said, for the client's tool call and tests."""
        entities = record["entities"]
        geometry = entities["geometry"]
        box = geometry["bbox"]
        summary_bits = [
            f"DXF {record['format']['name']}",
            f"{len(record['layers'])} layers",
            f"{entities['count']} objects",
            f"{len(record['blocks'])} blocks",
            f"{round_value(geometry['length'])} units drawn",
        ]
        if box:
            summary_bits.append(f"{round_value(box[2] - box[0])}×{round_value(box[3] - box[1])} "
                                "bounding box")
        summary = ", ".join(summary_bits)
        if findings:
            flagged = sum(finding["count"] for finding in findings
                          if finding["severity"] == "fault")
            summary += f"; {flagged} fault(s) in file-level checks"
        return {
            "summary": summary,
            "dataset": DATASET,
            "source": "DXF drawings" if record.get("source") == "sample" else "URL",
            "url": record["url"],
            "label": record["label"],
            "bytes": record["bytes"],
            "format": record["format"],
            "units": record["header"]["insunits"],
            "layers": len(record["layers"]),
            "blocks": len(record["blocks"]),
            "objects": entities["count"],
            "by_type": entities["by_type"],
            "by_space": entities["by_space"],
            "length": round_value(geometry["length"], 3),
            "bbox": [round_value(value, 3) for value in box] if box else None,
            "extents": record["extents"],
            "text_total": entities["text_total"],
            "checks": [{"check": finding["check"], "count": finding["count"],
                        "severity": finding["severity"]} for finding in findings
                       if finding["count"]],
        }


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        DxfAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
