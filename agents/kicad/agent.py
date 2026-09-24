"""KiCad — read a board or schematic file inside your editor.

The tenth ACP agent in this repo, and the first EDA agent in any editor protocol: it opens a
KiCad ``.kicad_pcb`` board or ``.kicad_sch`` schematic and answers what is in it, what is
wrong with it, and whether the two documents still agree.

Deterministic on purpose: routing is rules, every number is counted out of the file itself,
and nothing is guessed. It reports a plan, opens one tool call per read, asks permission
before the first one, streams the answer and closes the tool call with a one-line summary.

Honest by construction: a board's size is reported as the bounding box of its Edge.Cuts
graphics rather than a milling dimension, the checks are file-level checks and not KiCad's
DRC/ERC engines, a net with one pad is reported without pretending to know why, and a
document this agent cannot read is refused instead of guessed at.
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
    BOARD,
    DATASET,
    EXAMPLE_LIMIT,
    SAMPLES,
    SCHEMATIC,
    KicadData,
    KicadError,
    board_checks,
    parity_checks,
    schematic_checks,
)

HELP = (
    "I read KiCad project files — boards and schematics — from a URL or from my built-in set "
    "of real KiCad demo projects.\n"
    "Ask me:\n"
    "  • what is in the ecc83 board?\n"
    "  • check the stickhub board\n"
    "  • how many nets does pic_programmer have?\n"
    "  • check the tinytapeout schematic\n"
    "  • does the interf_u schematic still match its board?\n"
    "  • inspect https://…/my-board.kicad_pcb\n"
    "I count what the file says: layers, footprints, pads, vias, track length, nets, zones, "
    "symbols, wires and labels. My checks are file-level checks, not KiCad's DRC or ERC: I have "
    f"the board and the schematic, not the design rules you run them with. Built-in demos: "
    f"{', '.join(sorted(SAMPLES))}."
)

PERMISSION_KEY = "kicad-read-document"

SKILLS = ("board-summary", "board-check", "schematic-summary", "schematic-check", "parity", "help")

#: "check it", "anything wrong", "audit this" — a question about problems.
_CHECK_WORDS = re.compile(
    r"\b(check|checks|checking|validate|validated|validation|audit|review|"
    r"problems?|issues?|errors?|wrong|flags?|drc|erc|missing|sanity|clean|healthy|broken)\b",
    re.IGNORECASE)

#: "compare", "does it match", "against the board" — a question about two documents.
_COMPARE_WORDS = re.compile(
    r"\b(compare|compared|comparison|parity|match(?:es|ing)?|mismatch|diff|differences?|"
    r"versus|vs\.?|against|agree|agreeing|consistent|in sync|out of sync|eco|still)\b",
    re.IGNORECASE)

#: Words that mean the board file (the file extension counts as a word here).
_BOARD_WORDS = re.compile(r"\b(pcb|board|boards|layout|footprint|footprints|copper|drc)\b|\.kicad_pcb",
                          re.IGNORECASE)
#: Words that mean the schematic file.
_SCHEMATIC_WORDS = re.compile(
    r"\b(schematic|schematics|sch|symbols?|erc|netlist|hierarchy)\b|\.kicad_sch", re.IGNORECASE)

#: A document URL in the text (trailing punctuation is not part of it).
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


def urls_from_text(text: str) -> list[str]:
    """Every http(s) URL in the text, in order, with trailing punctuation trimmed."""
    found: list[str] = []
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    return found


def sample_from_text(text: str) -> str | None:
    """A built-in demo named in the text, matched longest name first."""
    lowered = " " + re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9._ -]+", " ", text.lower())).strip() + " "
    best = None
    for key, sample in SAMPLES.items():
        for alias in (key, *sample["aliases"]):
            flat = alias.replace("-", " ").replace("_", " ")
            if f" {flat} " in lowered.replace("-", " ").replace("_", " ") and (
                    best is None or len(flat) > len(best[1])):
                best = (key, flat)
    return best[0] if best else None


def kind_from_text(text: str) -> str | None:
    """Which document the text is asking about, when it says so."""
    wants_board = bool(_BOARD_WORDS.search(text))
    wants_schematic = bool(_SCHEMATIC_WORDS.search(text))
    if wants_board and wants_schematic:
        return "both"
    if wants_schematic:
        return SCHEMATIC
    if wants_board:
        return BOARD
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    urls = urls_from_text(text)
    sample = sample_from_text(text)
    kind = kind_from_text(text)
    if kind is None and urls:
        # A URL says what it is even when the question does not.
        try:
            kind = KicadData.kind_of(urls[0])
        except ValueError:
            kind = None  # refused later, with the reason
    checking = bool(_CHECK_WORDS.search(text))
    comparing = bool(_COMPARE_WORDS.search(text))

    params: dict = {}
    if sample:
        params["sample"] = sample
    if urls:
        params["urls"] = urls

    # A comparison needs both documents, so it is decided before plain reading.
    if comparing and (len(urls) >= 2 or sample or kind in (SCHEMATIC, "both")):
        return "parity", params

    if checking:
        if kind == SCHEMATIC:
            return "schematic-check", params
        return "board-check", params

    if kind == SCHEMATIC:
        return "schematic-summary", params
    return "board-summary", params


def _plural(count: int, word: str, plural: str | None = None) -> str:
    """'1 footprint' / '3 footprints' — a count that reads right at one."""
    if count == 1:
        return f"{count} {word}"
    return f"{count} {plural or word + 's'}"


def _mm(value) -> str:
    """A millimetre measurement without a trailing zero drizzle."""
    if value is None:
        return "unknown"
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return f"{text} mm"


def _count_map(counts: dict, names: tuple) -> str:
    """'87 vias, 1113 segments' — only the entries that are actually there."""
    parts = [f"{counts.get(name, 0)} {name if counts.get(name, 0) != 1 else name.rstrip('s')}"
             for name in names if counts.get(name)]
    return ", ".join(parts) or "nothing"


def _title_block_lines(title_block: dict) -> list[str]:
    if not title_block:
        return []
    order = ("title", "date", "rev", "company")
    parts = [f"{name} {title_block[name]!r}" for name in order if title_block.get(name)]
    parts.extend(f"{name} {value!r}" for name, value in title_block.items() if name not in order)
    return ["Title block: " + ", ".join(parts)]


def _verdict(findings: list[dict]) -> str:
    """A plain verdict line, keeping faults apart from the entries that are only notes."""
    faults = [finding for finding in findings
              if finding["count"] and finding.get("severity", "check") == "check"]
    notes = [finding for finding in findings
             if finding["count"] and finding.get("severity") == "note"]
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
            verdict.append(f"plus {_plural(total, 'note')} (do-not-populate and BOM-excluded "
                           f"parts, multi-unit symbols) that are not faults")
        else:
            verdict.append(f"no faults; {_plural(total, 'note')} listed for information")
    return "; ".join(verdict) + ". Each line names what the file says and nothing more."


def _finding_lines(findings: list[dict]) -> list[str]:
    """One line per check, with a marker that says whether it is a fault or a note."""
    lines: list[str] = []
    for finding in findings:
        if not finding["count"]:
            lines.append(f"  ✓ {finding['title']}: none")
            continue
        marker = "✗" if finding.get("severity", "check") == "check" else "•"
        lines.append(f"  {marker} {finding['title']}: {finding['count']}")
        if finding["examples"]:
            lines.append("      " + "; ".join(finding["examples"][:EXAMPLE_LIMIT]))
        lines.append(f"      {finding['detail']}")
    return lines


class KicadAgent(AcpAgent):
    name = "kicad"
    title = "KiCad — boards and schematics"
    version = "1.0.0"

    def __init__(self, connection=None, data: KicadData | None = None) -> None:
        super().__init__(connection)
        self.data = data or KicadData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "KiCad session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        plan = [(f"Route the request ({skill})", "high")]
        if skill == "parity":
            plan.append(("Read the schematic and the board", "medium"))
            plan.append(("Line the references and net names up", "medium"))
        elif skill in ("board-summary", "schematic-summary"):
            plan.append(("Read the KiCad document", "medium"))
            plan.append(("Count what the file says", "medium"))
        elif skill in ("board-check", "schematic-check"):
            plan.append(("Read the KiCad document", "medium"))
            plan.append(("Run the file-level checks", "medium"))
        ctx.plan(plan)

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Boards and schematics come from a URL you give me or from the KiCad demo "
                        "projects I ship links for; my checks are file-level, not DRC or ERC.")
            return STOP_END_TURN

        problem = self._missing_input(skill, params)
        if problem:
            # Nothing to read, so no permission is asked and no tool call is opened.
            ctx.message(problem)
            return STOP_END_TURN

        reads = self._plan_reads(skill, params)
        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read {len(reads)} KiCad document(s) for {skill}", kind="read",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow KiCad to read these project files?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read those KiCad files before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, reads)
        except (KicadError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that KiCad document: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is one or two HTTP reads of a few hundred kilobytes; nothing to interrupt.
        return None

    # -- routing help ------------------------------------------------------

    def _plan_reads(self, skill: str, params: dict) -> list[tuple[str, str | None]]:
        """What to read, as (kind, reference) pairs, resolved without touching the network."""
        sample = params.get("sample")
        urls = params.get("urls") or []
        if skill == "parity":
            if len(urls) >= 2:
                reads = [(self.data.kind_of(url), url) for url in urls[:2]]
                kinds = {found for found, _ in reads}
                if kinds != {BOARD, SCHEMATIC}:
                    raise ValueError(
                        "a comparison needs one board and one schematic, and both URLs you gave "
                        "me are the same kind of document")
                return reads
            if len(urls) == 1:
                raise ValueError(
                    "a comparison needs both documents: give me the schematic URL and the board "
                    "URL, or name one of the built-in demos (which I have both files for)")
            if sample:
                return [(SCHEMATIC, sample), (BOARD, sample)]
            raise ValueError(
                "Tell me which two documents to compare: name one of the built-in demos (I have "
                "both its files), or give me the schematic URL and the board URL.")
        if urls:
            # The extension decides the kind, and a non-KiCad URL is refused here, before any
            # permission is asked for.
            return [(self.data.kind_of(urls[0]), urls[0])]
        if skill.startswith("schematic"):
            return [(SCHEMATIC, sample)]
        return [(BOARD, sample)]

    def _missing_input(self, skill: str, params: dict) -> str | None:
        """What to say when there is nothing readable to ask permission for."""
        if params.get("urls") or params.get("sample"):
            # A URL or a demo name is enough; anything else is refused before permission.
            try:
                self._plan_reads(skill, params)
            except ValueError as exc:
                return str(exc)
            return None
        return ("Tell me which KiCad document to read: a full URL to a .kicad_pcb or .kicad_sch "
                "file, or one of the built-in demos "
                f"({', '.join(sorted(SAMPLES))}). I do not read .kicad_pro, .kicad_sym or Gerber "
                "files.")

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, reads: list) -> tuple[str, dict]:
        if skill == "board-summary":
            return self._board(reads[0])
        if skill == "schematic-summary":
            return self._schematic(reads[0])
        if skill == "board-check":
            return self._board(reads[0], checks=True)
        if skill == "schematic-check":
            return self._schematic(reads[0], checks=True)
        if skill == "parity":
            return self._parity(reads)
        return HELP, {"summary": "help", "dataset": None}

    def _board(self, read, checks: bool = False) -> tuple[str, dict]:
        board = self.data.read(read[1], read[0])
        outline = board["outline"]
        pads = board["pads"]
        tracks = board["tracks"]
        findings = board_checks(board) if checks else []
        named = board["named_nets"]
        auto = [net for net in board["nets"] if not net["named"]]

        lines = [
            f"{board['label']} — KiCad board, format {board['format_version']}"
            f" ({board['generator']} {board['generator_version']})",
            "",
        ]
        if outline:
            lines.append(f"Size: {_mm(outline['width_mm'])} × {_mm(outline['height_mm'])} outline "
                         f"box (bounding box of {outline['points']} Edge.Cuts points)")
        else:
            lines.append("Size: no Edge.Cuts outline in this file, so the board has no size yet")
        lines.append(f"Board: {_mm(board['thickness_mm'])} thick, {board['paper'] or 'no'} sheet, "
                     f"{_plural(len(board['layers']), 'layer')} declared "
                     f"({_plural(len(board['copper_layers']), 'copper layer')}: "
                     f"{', '.join(board['copper_layers']) or 'none'})")
        parts = [f"{_plural(len(board['footprints']), 'footprint')}",
                 f"{_plural(pads['total'], 'pad')}",
                 f"{_plural(len(board['vias']), 'via')}"]
        lines.append("Parts: " + ", ".join(parts))
        if pads["by_type"]:
            lines.append("  • Pads: " + ", ".join(f"{count} {name.replace('_', ' ')}"
                                                  for name, count in sorted(pads["by_type"].items()))
                         + (f"; drills {_mm(pads['min_drill_mm'])}–{_mm(pads['max_drill_mm'])}"
                            if pads["drilled"] else ""))
        widths = (f", widths {_mm(tracks['min_width_mm'])}–{_mm(tracks['max_width_mm'])}"
                  if tracks["min_width_mm"] is not None else "")
        lines.append(f"Routing: {_plural(tracks['segments'], 'segment')}"
                     + (f" + {_plural(tracks['arcs'], 'arc')}" if tracks["arcs"] else "")
                     + f", {_mm(tracks['length_mm'])} of track on "
                       f"{_plural(len(tracks['by_layer']), 'layer')}{widths}")
        if tracks["by_layer"]:
            lines.append("  • Per layer: " + ", ".join(
                f"{layer} {_mm(record['length_mm'])} ({record['segments']})"
                for layer, record in sorted(tracks["by_layer"].items())))
        if board["zones"]:
            zoned = ", ".join(sorted({zone["net"] or "(no net)" for zone in board["zones"]}))
            lines.append(f"Copper: {_plural(len(board['zones']), 'zone')} on {zoned}")
        lines.append(f"Nets: {_plural(len(board['nets']), 'net')} "
                     f"({len(named)} named, {len(auto)} auto-generated)")
        others = _count_map(board["counts"],
                            ("gr_text", "dimension", "image", "group", "constraint", "table"))
        if others != "nothing":
            lines.append(f"Other items: {others}")
        lines.extend(_title_block_lines(board["title_block"]))

        if checks:
            lines.extend(["", "Checks (file-level, not KiCad DRC):"])
            lines.extend(_finding_lines(findings))
            lines.extend(["", _verdict(findings)])

        lines.extend([
            "",
            f"Read live from {board['url']} ({board['bytes']:,} bytes), {self._source_note(board)}. "
            "Every number here is counted out of the file itself: nothing is simulated, and no "
            "DRC or ERC rule set was run.",
        ])
        return "\n".join(lines), self._artifact(board, findings)

    def _schematic(self, read, checks: bool = False) -> tuple[str, dict]:
        schematic = self.data.read(read[1], read[0])
        symbols = schematic["symbols"]
        parts = [symbol for symbol in symbols if not symbol["power"]]
        power = [symbol for symbol in symbols if symbol["power"]]
        references = sorted({symbol["reference"] for symbol in parts if symbol["reference"]})
        unannotated = [symbol for symbol in parts if symbol["reference"].endswith("?")]
        findings = schematic_checks(schematic) if checks else []

        lines = [
            f"{schematic['label']} — KiCad schematic, format {schematic['format_version']}"
            f" ({schematic['generator']} {schematic['generator_version']})",
            "",
            f"Symbols: {_plural(len(symbols), 'symbol')} placed "
            f"({len(parts)} parts, {len(power)} power/flag symbols), "
            f"{_plural(schematic['lib_symbols'], 'library symbol')} embedded",
            f"References: {_plural(len(references), 'unique reference')}, "
            f"{len(unannotated)} unannotated",
            f"Wiring: {_plural(schematic['wires']['count'], 'wire')} "
            f"({_mm(schematic['wires']['length_mm'])} drawn), "
            f"{_plural(schematic['junctions'], 'junction')}, "
            f"{_plural(schematic['no_connects'], 'no-connect flag')}",
        ]
        if schematic["buses"]["count"]:
            lines.append(f"Buses: {_plural(schematic['buses']['count'], 'bus')} "
                         f"({_mm(schematic['buses']['length_mm'])} drawn)")
        labels = (f"{len(schematic['labels'])} local, {len(schematic['global_labels'])} global, "
                  f"{len(schematic['hierarchical_labels'])} hierarchical")
        lines.append(f"Labels: {labels}")
        if schematic["sheets"]:
            lines.append(f"Hierarchy: {_plural(len(schematic['sheets']), 'sheet')}")
            for sheet in schematic["sheets"]:
                lines.append(f"  • {sheet['name'] or '(unnamed)'} → {sheet['file'] or '(no file)'} "
                             f"({_plural(len(sheet['pins']), 'sheet pin')})")
        lines.extend(_title_block_lines(schematic["title_block"]))

        if checks:
            lines.extend(["", "Checks (file-level, not KiCad ERC):"])
            lines.extend(_finding_lines(findings))
            lines.extend(["", _verdict(findings)])

        lines.extend([
            "",
            f"Read live from {schematic['url']} ({schematic['bytes']:,} bytes). Every number is "
            "counted out of the file itself; a symbol count is what is placed on this sheet, not "
            "what the whole hierarchy contains.",
        ])
        return "\n".join(lines), self._artifact(schematic, findings)

    def _parity(self, reads: list) -> tuple[str, dict]:
        documents = []
        for kind, reference in reads:
            documents.append(self.data.read(reference, kind))
        schematic = next(record for record in documents if record["kind"] == SCHEMATIC)
        board = next(record for record in documents if record["kind"] == BOARD)
        parity = parity_checks(schematic, board)

        lines = [
            f"Schematic vs board — {schematic['label']} against {board['label']}",
            "",
            f"References: {parity['schematic']['references']} in the schematic, "
            f"{parity['board']['references']} on the board, {parity['matching_references']} match.",
        ]
        for title, names in (("In the schematic but not on the board",
                              parity["references_only_in_schematic"]),
                             ("On the board but not in the schematic",
                              parity["references_only_in_board"])):
            lines.append(f"  • {title}: {len(names)}")
            if names:
                shown = names[:EXAMPLE_LIMIT]
                lines.append("      " + ", ".join(shown)
                             + (f" … +{len(names) - len(shown)} more" if len(names) > len(shown) else ""))
        lines.append(f"Net names: {parity['schematic']['net_names']} in the schematic "
                     f"({parity['schematic']['labels']} labels, "
                     f"{parity['schematic']['power_nets']} power nets), "
                     f"{parity['board']['nets']} named nets on the board, "
                     f"{parity['matching_net_names']} match.")
        for title, names in (("Schematic net names the board does not carry",
                              parity["net_names_not_a_board_net"]),
                             ("Board nets the schematic does not name",
                              parity["board_nets_not_in_schematic"])):
            if not names:
                continue
            shown = names[:EXAMPLE_LIMIT]
            lines.append(f"  • {title}: {len(names)} — " + ", ".join(shown)
                         + (f" … +{len(names) - len(shown)} more" if len(names) > len(shown) else ""))
        lines.extend([
            "",
            "A reference is the hard link between the two documents, so a reference on one side "
            "only is a real difference: the board has a part the schematic does not place, or the "
            "schematic has a part that was never put on the board. Net names are looser — the "
            "schematic's own net names are its labels plus its power symbols, the board names its "
            "nets after them, and single-pin nets are named after their pads on purpose — so a "
            "name only one side carries is a lead to check rather than a proven fault. A "
            "hierarchy is read one sheet at a time: with sub-sheets, only this sheet's symbols "
            "are compared.",
            "",
            f"Read live from {schematic['url']} and {board['url']} "
            f"({schematic['bytes'] + board['bytes']:,} bytes together). This is a comparison of "
            "two files, not KiCad's own update-from-schematic; nothing was written back.",
        ])
        artifact = {
            "summary": (f"parity: {parity['matching_references']} references match, "
                        f"{len(parity['references_only_in_schematic'])} only in the schematic, "
                        f"{len(parity['references_only_in_board'])} only on the board, "
                        f"{parity['matching_net_names']} net names match"),
            "dataset": DATASET,
            "source": "KiCad demo projects",
            "schematic_url": schematic["url"],
            "board_url": board["url"],
            "schematic_format": schematic["format_version"],
            "board_format": board["format_version"],
            **parity,
        }
        return "\n".join(lines), artifact

    @staticmethod
    def _source_note(record: dict) -> str:
        """Where the document came from, said plainly."""
        if record.get("source") == "url":
            return "the URL you gave me"
        return "the demo project's own file"

    @staticmethod
    def _artifact(record: dict, findings: list[dict]) -> dict:
        """A compact record of what the file said, for the client's tool call and tests."""
        summary_bits = []
        if record["kind"] == BOARD:
            outline = record["outline"]
            summary_bits = [f"board {record['format_version']}",
                            f"{len(record['copper_layers'])} copper layers",
                            f"{len(record['footprints'])} footprints",
                            f"{len(record['named_nets'])} named nets",
                            f"{record['tracks']['segments']} segments",
                            f"{len(record['vias'])} vias"]
            if outline:
                summary_bits.append(f"{outline['width_mm']}×{outline['height_mm']} mm outline box")
        else:
            summary_bits = [f"schematic {record['format_version']}",
                            f"{len(record['symbols'])} symbols",
                            f"{len(record['sheets'])} sheets",
                            f"{record['wires']['count']} wires"]
        summary = ", ".join(summary_bits)
        if findings:
            flagged = sum(finding["count"] for finding in findings)
            summary += f"; {flagged} finding(s) in file-level checks"
        return {
            "summary": summary,
            "dataset": DATASET,
            "source": "KiCad demo projects" if record.get("source") == "sample" else "URL",
            "kind": record["kind"],
            "url": record["url"],
            "label": record["label"],
            "bytes": record["bytes"],
            "format_version": record["format_version"],
            "title_block": record["title_block"],
            "checks": [{"id": finding["id"], "count": finding["count"],
                        "severity": finding.get("severity", "check"),
                        "title": finding["title"]}
                       for finding in findings if finding["count"]],
        }


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        KicadAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
