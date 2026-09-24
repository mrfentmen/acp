"""Read-only reader for KiCad board and schematic files.

KiCad (the open-source EDA suite) saves every document as plain-text s-expressions:
``.kicad_pcb`` for a board, ``.kicad_sch`` for a schematic, ``.kicad_pro`` for the project.
That makes a board auditable with nothing but a parser and an HTTP read — no KiCad install,
no binary format, no proprietary file.

Verified live on 2026-09-24 03:15 UTC against KiCad's own demo projects on GitLab: the
``ecc83`` valve amplifier board parsed as 20 layers / 15 footprints / 59 track segments over
14 nets in format 20241229, its schematic as 26 symbols with 8 embedded library symbols, the
``interf_u`` demo as 174 nets and 84 vias, ``tinytapeout-demo`` as a 4.5 MB board with 309
footprints, and the ``constraints`` demo as an 87-constraint board in the newest format
20260624.

The quirks, all handled here:

1. The format is versioned and moving. KiCad writes a bare format stamp — ``20241229``,
   ``20250907``, ``20260206`` and ``20260624`` were all live in the demo set the same day —
   so nothing here enforces a version: the stamp is reported and the parser reads the fields
   that exist.
2. Strings are quoted with backslash escapes and everything else is a bare token, so
   ``(net 2 "Net-(P3-P1)")`` and ``(property "Reference" "R1")`` read the same way. A token
   becomes a number only when it is one.
3. Documents get big: ``video.kicad_pcb`` is 5.8 MB of text. The read is size-capped and the
   parser is iterative, so there is no recursion depth to hit.
4. A board's size is only visible through its ``Edge.Cuts`` graphics, and an outline can be
   drawn with lines, rectangles, arcs, circles or polygons. What is reported is the bounding
   box of those outline points, and it is called that — not a milling dimension.
5. Net names come in three flavours: names a human wrote, auto-generated ones such as
   ``Net-(P3-P1)`` and ``unconnected-(J1-Pad1)``, and the empty name of net 0. Only the
   written ones are treated as named nets.
6. A footprint's library is one string (``Footprints:CP_Radial...``). A footprint with no
   ``:`` belongs to no library this file can name, which is reported rather than hidden.
7. A schematic keeps each reference twice — as a ``Reference`` property and inside
   ``instances`` — and an unannotated symbol carries a trailing ``?``.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from http.client import IncompleteRead
from urllib import error as urlerror
from urllib import parse, request

BASE_URL = "https://gitlab.com/kicad/code/kicad/-/raw/master/demos"
DATASET = "gitlab.com/kicad/code/kicad · demos"
DEFAULT_USER_AGENT = "acp-kicad/1.0 (+https://github.com/mrfentmen/acp)"

#: A 5.8 MB board is the largest document in the demo set; far past that is not a board
#: anyone asked to audit, so it is refused with its size instead of parsed for minutes.
MAX_BYTES = 8 * 1024 * 1024

#: GitLab answers 429/5xx when it is busy; those reads are retried, a 404 never is.
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.6

#: Demo projects change rarely, so a document is cached for an hour.
DEFAULT_TTL = 3600.0
DEFAULT_TIMEOUT = 60.0

#: Extensions that say what a document is.
PCB_EXTENSION = ".kicad_pcb"
SCH_EXTENSION = ".kicad_sch"

#: Document kinds, as the answers name them.
BOARD = "board"
SCHEMATIC = "schematic"

#: Auto-generated net names: wires KiCad named after the pads they join, not nets a designer
#: named. A "which nets exist" answer has to keep the two apart.
AUTO_NET_RE = re.compile(r"^(?:Net-\(|unconnected-\(|\$)", re.IGNORECASE)

#: How many examples a check line prints before it just counts the rest.
EXAMPLE_LIMIT = 5

#: Real KiCad demo projects, keyed by the name an answer uses. Each entry names the board and
#: the schematic file in the demo folder, with the spellings people actually type.
SAMPLES: dict[str, dict] = {
    "ecc83": {
        "title": "ECC83 push-pull valve amplifier",
        "pcb": "ecc83/ecc83-pp.kicad_pcb",
        "sch": "ecc83/ecc83-pp.kicad_sch",
        "aliases": ("ecc83", "ecc-83", "valve amplifier", "valve amp", "tube amp"),
    },
    "stickhub": {
        "title": "StickHub robot board",
        "pcb": "stickhub/StickHub.kicad_pcb",
        "sch": "stickhub/StickHub.kicad_sch",
        "aliases": ("stickhub", "stick hub"),
    },
    "pic-programmer": {
        "title": "PIC programmer",
        "pcb": "pic_programmer/pic_programmer.kicad_pcb",
        "sch": "pic_programmer/pic_programmer.kicad_sch",
        "aliases": ("pic programmer", "pic_programmer", "pic", "programmer"),
    },
    "interf-u": {
        "title": "Interface board",
        "pcb": "interf_u/interf_u.kicad_pcb",
        "sch": "interf_u/interf_u.kicad_sch",
        "aliases": ("interf_u", "interf-u", "interf u", "interface board", "interface"),
    },
    "tinytapeout": {
        "title": "TinyTapeout demo board",
        "pcb": "tiny_tapeout/tinytapeout-demo.kicad_pcb",
        "sch": "tiny_tapeout/tinytapeout-demo.kicad_sch",
        "aliases": ("tinytapeout", "tiny tapeout", "tt demo", "tapeout"),
    },
    "video": {
        "title": "Video card",
        "pcb": "video/video.kicad_pcb",
        "sch": "video/video.kicad_sch",
        "aliases": ("video card", "video"),
    },
    "sonde-xilinx": {
        "title": "Sonde Xilinx probe",
        "pcb": "sonde xilinx/sonde xilinx.kicad_pcb",
        "sch": "sonde xilinx/sonde xilinx.kicad_sch",
        "aliases": ("sonde xilinx", "sonde", "xilinx sonde", "probe"),
    },
    "cm5-minima": {
        "title": "CM5 Minima carrier",
        "pcb": "cm5_minima/CM5_MINIMA_3.kicad_pcb",
        "sch": "cm5_minima/CM5_MINIMA_3.kicad_sch",
        "aliases": ("cm5 minima", "cm5_minima", "cm5", "minima"),
    },
    "constraints": {
        "title": "Design-rule constraints demo",
        "pcb": "constraints/constraints.kicad_pcb",
        "sch": "constraints/constraints.kicad_sch",
        "aliases": ("constraints", "design rules", "rule constraints"),
    },
}

#: s-expression tokens: a quoted string (with escapes), a bracket, or a bare token.
_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([()])|([^\s()"]+)')

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class KicadError(RuntimeError):
    """A KiCad document could not be read or parsed."""


class KicadBusy(KicadError):
    """GitLab is busy or unreachable: the same read is worth trying again."""


def unescape(value: str) -> str:
    """KiCad escapes a quote and a backslash inside strings; nothing else matters here."""
    if "\\" not in value:
        return value
    return re.sub(r"\\(.)", lambda match: {"n": "\n", "t": "\t"}.get(match.group(1), match.group(1)), value)


def tokenize(text: str) -> list[str]:
    """The document as a flat token list, brackets kept and strings unquoted.

    An empty string is a real token in these files — ``(net 0 "")`` is how a board writes
    "no net" — so the three alternatives are told apart by which group matched rather than
    by whether the match is truthy.
    """
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        quoted, bracket, bare = match.groups()
        if quoted is not None:
            tokens.append(unescape(quoted))
        elif bracket is not None:
            tokens.append(bracket)
        else:
            tokens.append(bare)
    return tokens


def parse_sexp(text: str) -> list:
    """A KiCad document as nested lists: ``["net", "2", "Net-(P3-P1)"]``.

    Iterative on purpose — a 5.8 MB board tokenizes long enough that recursion depth is a
    real risk and not worth taking.
    """
    root: list = []
    stack: list[list] = []
    current = root
    for token in tokenize(text):
        if token == "(":
            node: list = []
            current.append(node)
            stack.append(current)
            current = node
        elif token == ")":
            if not stack:
                raise KicadError("this file has unbalanced brackets, so it is not a KiCad document")
            current = stack.pop()
        else:
            current.append(token)
    if stack:
        raise KicadError("this file has unbalanced brackets, so it is not a KiCad document")
    documents = [child for child in root if isinstance(child, list) and child]
    if not documents:
        raise KicadError("this file holds no s-expression, so it is not a KiCad document")
    return documents[0]


def key_of(node) -> str:
    """The element name of a node (``segment``), or ``""`` for anything that is not one."""
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else ""


def children(node, name: str) -> list:
    """Every direct child with this element name."""
    if not isinstance(node, list):
        return []
    return [child for child in node if isinstance(child, list) and child and child[0] == name]


def child(node, name: str):
    """The first direct child with this element name, if any."""
    found = children(node, name)
    return found[0] if found else None


def args_of(node) -> list:
    """The bare tokens of a node, element name first: ``["net", "2", "Net-(P3-P1)"]``."""
    return [item for item in node if isinstance(item, str)] if isinstance(node, list) else []


def number(value, default=None):
    """A token as a float, or the default when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def numbers(count: int, *values) -> tuple:
    """``count`` tokens as floats, always ``count`` long, missing ones read as None.

    Fixed arity on purpose: KiCad drops trailing optional tokens (an ``(at x y)`` with no
    rotation, a ``(pts)`` with one point), and a caller unpacking three values must not blow
    up because the file left the third one out.
    """
    found = [number(value) for value in values[:count]]
    return tuple(found + [None] * (count - len(found)))


def flag(node, name: str, default: bool = True) -> bool:
    """A ``(yes|no)`` child (``in_bom yes``) as a bool."""
    found = child(node, name)
    if found is None:
        return default
    words = args_of(found)
    if len(words) < 2:
        return default
    return words[1].lower() not in ("no", "false", "0")


def point_child(node, name: str):
    """The (x, y) of a ``(start x y)``-shaped child."""
    found = child(node, name)
    if found is None:
        return None
    x, y = numbers(2, *args_of(found)[1:3])
    return None if x is None or y is None else (x, y)


def start_end(node) -> tuple:
    """The start, mid and end points of a graphic or track element."""
    return point_child(node, "start"), point_child(node, "mid"), point_child(node, "end")


def at_point(node) -> tuple:
    """An ``(at x y [rot])`` child as (x, y, rotation)."""
    found = child(node, "at")
    if found is None:
        return (None, None, None)
    return numbers(3, *args_of(found)[1:4])


def layer_of(node, default: str = "") -> str:
    """The layer name of an element (``(layer "Edge.Cuts")``)."""
    found = child(node, "layer")
    if found is None:
        return default
    words = args_of(found)
    return words[1] if len(words) > 1 else default


def layers_of(node) -> list[str]:
    """Every layer name on an element (``(layers "F.Cu" "B.Cu")``)."""
    found = child(node, "layers")
    return args_of(found)[1:] if found is not None else []


def value_of(node, name: str):
    """The value token of a child (``(width 0.8)`` → ``"0.8"``)."""
    found = child(node, name)
    if found is None:
        return None
    words = args_of(found)
    return words[1] if len(words) > 1 else None


def measure_of(node, name: str):
    """A single-number child as a float (``(width 0.8)``, ``(size 0.8)``, ``(drill 0.4)``)."""
    return number(value_of(node, name))


def drill_of(pad_or_via):
    """``(drill 0.8)`` or ``(drill oval 1 1.5)`` as (diameter, width, height), or None."""
    found = child(pad_or_via, "drill")
    if found is None:
        return None
    words = args_of(found)[1:]
    if not words:
        return None
    if words[0] in ("oval", "oblong"):
        width, height = numbers(2, *words[1:3])
        return (None, width, height)
    return (number(words[0]), None, None)


def arc_length(start, mid, end):
    """The true length of an arc through three points, by the circle they define."""
    if not start or not mid or not end:
        return None
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    determinant = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-12:
        # Three points on one line: KiCad still calls it an arc, so measure the two chords.
        return math.dist(start, mid) + math.dist(mid, end)
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / determinant
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / determinant
    radius = math.hypot(ax - ux, ay - uy)
    first = math.atan2(ay - uy, ax - ux)
    through = math.atan2(by - uy, bx - ux)
    last = math.atan2(cy - uy, cx - ux)
    sweep = (last - first) % (2 * math.pi)
    if (through - first) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    return abs(sweep) * radius


def polyline_points(node) -> list[tuple]:
    """Every ``(xy x y)`` of a ``(pts ...)`` child, in order."""
    points: list[tuple] = []
    for entry in children(child(node, "pts"), "xy"):
        x, y = numbers(2, *args_of(entry)[1:3])
        if x is not None and y is not None:
            points.append((x, y))
    return points


def polyline_length(points: list[tuple]) -> float:
    """The drawn length of a point chain."""
    return sum(math.dist(points[index], points[index + 1]) for index in range(len(points) - 1))


def bounds(points: list[tuple]):
    """The bounding box of a point list, as spans in millimetres."""
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {"min_x": round(min(xs), 3), "max_x": round(max(xs), 3),
            "min_y": round(min(ys), 3), "max_y": round(max(ys), 3),
            "width_mm": round(max(xs) - min(xs), 2), "height_mm": round(max(ys) - min(ys), 2),
            "points": len(points)}


def round_mm(value, digits: int = 2):
    return None if value is None else round(value, digits)


def bump(counter: dict, name: str, amount: float = 1) -> None:
    """Count into a plain dict, so an artifact stays JSON-shaped."""
    counter[name] = counter.get(name, 0) + amount


def net_args(node) -> tuple:
    """A ``(net ...)`` element itself as (number, name), whichever of the two it wrote.

    Two shapes are live at once: older boards write ``(net 2 "GND")`` and newer ones write
    only the name, ``(net "GND")``. Reading a single shape would quietly report a board as
    having no nets, so both are read here. A bare number is a reference to the file's own net
    table, and a bare word is the net's name.
    """
    words = args_of(node)[1:] if key_of(node) == "net" else []
    if not words:
        return (None, "")
    if len(words) >= 2:
        return (int(number(words[0], 0)), words[1])
    single = words[0]
    if single.lstrip("-").isdigit():
        return (int(single), "")
    return (None, single)


def net_id(node) -> tuple:
    """The ``(net ...)`` child of a pad, track, via or zone as (number, name)."""
    return net_args(child(node, "net")) if child(node, "net") is not None else (None, "")


def net_name(name: str) -> str:
    """A net name as a person wrote it on the schematic.

    The board does two things to the name a label carries: it escapes the characters that
    cannot appear bare (a slash becomes ``{slash}``, a space ``{space}``) and it scopes a
    local label to its sheet path with a leading slash, so ``8MH-OUT`` is stored as
    ``/8MH-OUT``. A comparison has to undo both, or every local label looks unmatched.
    """
    return str(name or "").replace("{slash}", "/").replace("{space}", " ")


def net_key(number, name) -> str:
    """A stable key for a net that may carry a number, a name, or only one of the two."""
    if name:
        return name
    return f"#{0 if number is None else number}"


#: Layer names that make up the copper stack.
COPPER_RE = re.compile(r"\.Cu$")

#: Layers that say a footprint has a courtyard.
COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")


def title_block_of(root) -> dict:
    """A ``title_block`` as a plain dict (comments keep their number: ``comment 1``)."""
    block = child(root, "title_block")
    if block is None:
        return {}
    fields: dict = {}
    for entry in block:
        if not isinstance(entry, list):
            continue
        words = args_of(entry)
        if not words:
            continue
        if words[0] == "comment" and len(words) > 2:
            fields[f"comment {words[1]}"] = words[2]
        elif len(words) > 1:
            fields[words[0]] = words[-1]
    return fields


def parse_board(text: str) -> dict:
    """A ``.kicad_pcb`` document as a structured board record."""
    root = parse_sexp(text)
    if key_of(root) != "kicad_pcb":
        if key_of(root) == "kicad_sch":
            raise KicadError("that file is a schematic, not a board")
        raise KicadError("that file is not a KiCad board: it does not start with (kicad_pcb")

    layers = []
    for entry in [item for item in (child(root, "layers") or []) if isinstance(item, list)]:
        # A layer entry leads with its number rather than a keyword:
        # (0 "F.Cu" signal "top_copper").
        fields = args_of(entry)
        if len(fields) >= 2:
            layers.append({"number": int(number(fields[0], -1)), "name": fields[1],
                           "type": fields[2] if len(fields) > 2 else None,
                           "user_name": fields[3] if len(fields) > 3 else None})

    #: Every net this board mentions, keyed by name (or #number when the file names none).
    #: The board's own net table is read first, then every pad, track, via and zone adds to
    #: it, because the newest format keeps no table at all.
    net_table: dict[str, dict] = {}
    #: Net number -> key, so a pad that spells a net out (``(net 2 "GND")``) and a track that
    #: only numbers it (``(net 2)``) land on the same net instead of two.
    by_number: dict[int, str] = {}

    def register(net_number, found_name) -> str:
        key = net_key(net_number, found_name)
        if net_number is not None and net_number in by_number:
            key = by_number[net_number]
        entry = net_table.get(key)
        if entry is None:
            entry = net_table[key] = {"key": key, "number": net_number, "name": found_name or "",
                                      "named": bool(found_name) and not AUTO_NET_RE.match(found_name),
                                      "pads": 0, "routed": False, "zoned": False}
        elif found_name and not entry["name"]:
            # A later mention of the same net fills in the name the first one left out.
            entry["name"] = found_name
            entry["named"] = not AUTO_NET_RE.match(found_name)
        if net_number is not None:
            if entry["number"] is None:
                entry["number"] = net_number
            by_number.setdefault(net_number, key)
        return key

    #: The file's own net table is read first, in file order, so the named nets come out in
    #: the order the board lists them.
    for table_entry in children(root, "net"):
        register(*net_args(table_entry))

    footprints: list[dict] = []
    pad_total = 0
    pads_by_type: dict = {}
    drills: list[float] = []
    for entry in children(root, "footprint"):
        properties = {args_of(prop)[1]: args_of(prop)[2] for prop in children(entry, "property")
                      if len(args_of(prop)) > 2}
        pad_nodes = children(entry, "pad")
        pad_total += len(pad_nodes)
        pad_types = []
        pad_nets = []
        pad_numbers = []
        for pad in pad_nodes:
            fields = args_of(pad)
            kind = fields[2] if len(fields) > 2 else "unknown"
            pad_types.append(kind)
            pad_numbers.append(fields[1] if len(fields) > 1 else "")
            bump(pads_by_type, kind)
            if child(pad, "net") is not None:
                key = register(*net_id(pad))
                pad_nets.append(key)
                net_table[key]["pads"] += 1
            bore = drill_of(pad)
            if bore and bore[0]:
                drills.append(bore[0])
            elif bore and bore[1] and bore[2]:
                drills.append(min(bore[1], bore[2]))
        courtyard = any(layer_of(item) in COURTYARD_LAYERS
                        for item in entry if key_of(item).startswith("fp_"))
        x, y, rotation = at_point(entry)
        library = args_of(entry)[1] if len(args_of(entry)) > 1 else ""
        attributes: list[str] = []
        for attribute in children(entry, "attr"):
            attributes.extend(args_of(attribute)[1:])
        footprints.append({
            "library": library,
            "library_nickname": library.split(":", 1)[0] if ":" in library else None,
            "reference": properties.get("Reference", ""),
            "value": properties.get("Value", ""),
            "properties": properties,
            "x": x, "y": y, "rotation": rotation,
            "layer": layer_of(entry, "F.Cu"),
            "pads": len(pad_nodes),
            "pad_types": sorted(set(pad_types)),
            "pad_nets": pad_nets,
            "pad_numbers": pad_numbers,
            "courtyard": courtyard,
            "attributes": attributes,
            "dnp": "dnp" in attributes,
            "exclude_from_bom": "exclude_from_bom" in attributes,
            "board_only": "board_only" in attributes,
        })


    track_segments = 0
    track_arcs = 0
    track_length = 0.0
    track_by_layer: dict = {}
    routed_nets: set[str] = set()
    widths: list[float] = []
    zero_width: list[dict] = []
    for name in ("segment", "arc"):
        for entry in children(root, name):
            layer = layer_of(entry, "(no layer)")
            record = track_by_layer.setdefault(layer, {"segments": 0, "length_mm": 0.0})
            record["segments"] += 1
            if child(entry, "net") is not None:
                key = register(*net_id(entry))
                routed_nets.add(key)
                net_table[key]["routed"] = True
            start, mid, end = start_end(entry)
            if name == "segment":
                track_segments += 1
                length = math.dist(start, end) if start and end else 0.0
            else:
                track_arcs += 1
                length = arc_length(start, mid, end) or 0.0
            track_length += length
            record["length_mm"] += length
            width = measure_of(entry, "width")
            if width is not None:
                widths.append(width)
                if width <= 0:
                    zero_width.append({"layer": layer, "width": width, "start": start, "end": end})
    for record in track_by_layer.values():
        record["length_mm"] = round(record["length_mm"], 2)

    vias = []
    for entry in children(root, "via"):
        x, y, _ = at_point(entry)
        bore = drill_of(entry)
        net_number, named = net_id(entry)
        if child(entry, "net") is not None:
            key = register(net_number, named)
            net_table[key]["routed"] = True
            routed_nets.add(key)
        else:
            key = net_key(None, "")
        vias.append({
            "x": x, "y": y,
            "size": measure_of(entry, "size"),
            "drill": bore[0] if bore else None,
            "layers": layers_of(entry) or ["F.Cu", "B.Cu"],
            "net": key, "net_number": net_number, "net_name": named,
            "kind": value_of(entry, "type") or "through",
        })

    zones = []
    for entry in children(root, "zone"):
        key = register(*net_id(entry))
        if not net_table[key]["name"]:
            # The older shape names a zone's net in its own child rather than in (net …).
            name = value_of(entry, "net_name") or ""
            if name:
                net_table[key]["name"] = name
                net_table[key]["named"] = not AUTO_NET_RE.match(name)
        net_table[key]["zoned"] = True
        zones.append({
            "net": key, "net_number": net_table[key]["number"],
            "net_name": net_table[key]["name"],
            "layers": layers_of(entry) or ([layer_of(entry)] if layer_of(entry) else []),
            "filled": child(entry, "filled_polygon") is not None,
        })

    graphics: dict = {}
    graphics_by_layer: dict = {}
    edge_points: list[tuple] = []
    for entry in root:
        name = key_of(entry)
        if not name.startswith("gr_"):
            continue
        bump(graphics, name)
        layer = layer_of(entry, "(no layer)")
        bump(graphics_by_layer, layer)
        if layer == "Edge.Cuts":
            start, mid, end = start_end(entry)
            edge_points.extend(point for point in (start, mid, end) if point)
            edge_points.extend(polyline_points(entry))
            centre = point_child(entry, "center")
            if centre:
                edge_points.append(centre)

    constraints = []
    for entry in children(root, "constraint"):
        kind = value_of(entry, "type")
        if kind is None:
            fields = args_of(entry)
            kind = fields[1] if len(fields) > 1 else "unknown"
        constraints.append(kind)

    counts: dict = {}
    for entry in root:
        if isinstance(entry, list) and entry:
            bump(counts, key_of(entry))

    general = child(root, "general")
    return {
        "kind": BOARD,
        "format_version": value_of(root, "version"),
        "generator": value_of(root, "generator"),
        "generator_version": value_of(root, "generator_version"),
        "paper": value_of(root, "paper"),
        "title_block": title_block_of(root),
        "thickness_mm": measure_of(general, "thickness") if general is not None else None,
        "layers": layers,
        "copper_layers": [entry["name"] for entry in layers if COPPER_RE.search(entry["name"])],
        "nets": list(net_table.values()),
        "named_nets": [net for net in net_table.values() if net["named"]],
        "footprints": footprints,
        "pads": {"total": pad_total, "by_type": pads_by_type, "drilled": len(drills),
                 "min_drill_mm": round_mm(min(drills)) if drills else None,
                 "max_drill_mm": round_mm(max(drills)) if drills else None},
        "tracks": {"segments": track_segments, "arcs": track_arcs,
                   "length_mm": round(track_length, 2), "by_layer": track_by_layer,
                   "routed_nets": sorted(routed_nets),
                   "min_width_mm": round_mm(min(widths)) if widths else None,
                   "max_width_mm": round_mm(max(widths)) if widths else None,
                   "zero_width": zero_width},
        "vias": vias,
        "zones": zones,
        "graphics": graphics,
        "graphics_by_layer": graphics_by_layer,
        "outline": bounds(edge_points),
        "constraints": constraints,
        "counts": counts,
    }


def parse_schematic(text: str) -> dict:
    """A ``.kicad_sch`` document as a structured schematic record."""
    root = parse_sexp(text)
    if key_of(root) != "kicad_sch":
        if key_of(root) == "kicad_pcb":
            raise KicadError("that file is a board, not a schematic")
        raise KicadError("that file is not a KiCad schematic: it does not start with (kicad_sch")

    symbols: list[dict] = []
    for entry in children(root, "symbol"):
        properties = {args_of(prop)[1]: args_of(prop)[2] for prop in children(entry, "property")
                      if len(args_of(prop)) > 2}
        reference = properties.get("Reference", "")
        x, y, rotation = at_point(entry)
        symbols.append({
            "reference": reference,
            "value": properties.get("Value", ""),
            "lib_id": value_of(entry, "lib_id") or "",
            "unit": int(number(value_of(entry, "unit"), 1)),
            "footprint": properties.get("Footprint", ""),
            "properties": properties,
            "x": x, "y": y, "rotation": rotation,
            "mirror": value_of(entry, "mirror"),
            "pins": len(children(entry, "pin")),
            "in_bom": flag(entry, "in_bom"),
            "on_board": flag(entry, "on_board"),
            "dnp": flag(entry, "dnp", False),
            "power": reference.startswith("#"),
        })

    def run_length(nodes) -> tuple[int, float]:
        total = 0.0
        for node in nodes:
            points = polyline_points(node)
            if not points:
                start, _, end = start_end(node)
                points = [point for point in (start, end) if point]
            total += polyline_length(points)
        return len(nodes), total

    wires, wire_length = run_length(children(root, "wire"))
    buses, bus_length = run_length(children(root, "bus"))

    sheets = []
    for entry in children(root, "sheet"):
        properties = {args_of(prop)[1]: args_of(prop)[2] for prop in children(entry, "property")
                      if len(args_of(prop)) > 2}
        x, y, _ = at_point(entry)
        size = child(entry, "size")
        width, height = numbers(2, *args_of(size)[1:3]) if size is not None else (None, None)
        sheets.append({
            "name": properties.get("Sheetname", ""),
            "file": properties.get("Sheetfile", ""),
            "x": x, "y": y, "width": width, "height": height,
            "properties": properties,
            "pins": [args_of(pin)[1] for pin in children(entry, "pin") if len(args_of(pin)) > 1],
        })

    def label_names(name: str) -> list[str]:
        return [args_of(entry)[1] for entry in children(root, name) if len(args_of(entry)) > 1]

    counts: dict = {}
    for entry in root:
        if isinstance(entry, list) and entry:
            bump(counts, key_of(entry))

    return {
        "kind": SCHEMATIC,
        "format_version": value_of(root, "version"),
        "generator": value_of(root, "generator"),
        "generator_version": value_of(root, "generator_version"),
        "uuid": value_of(root, "uuid"),
        "paper": value_of(root, "paper"),
        "title_block": title_block_of(root),
        "symbols": symbols,
        "wires": {"count": wires, "length_mm": round(wire_length, 2)},
        "buses": {"count": buses, "length_mm": round(bus_length, 2)},
        "junctions": len(children(root, "junction")),
        "no_connects": len(children(root, "no_connect")),
        "labels": label_names("label"),
        "global_labels": label_names("global_label"),
        "hierarchical_labels": label_names("hierarchical_label"),
        "texts": [args_of(entry)[1] for entry in children(root, "text") if len(args_of(entry)) > 1],
        "sheets": sheets,
        "lib_symbols": len(children(child(root, "lib_symbols"), "symbol")) if child(root, "lib_symbols") else 0,
        "images": len(children(root, "image")),
        "counts": counts,
    }


def _finding(identifier: str, title: str, found: list, detail: str, severity: str = "check") -> dict:
    """One check result: what it was, what turned up, and whether it is a fault or a note."""
    return {"id": identifier, "title": title, "count": len(found),
            "examples": found[:EXAMPLE_LIMIT], "detail": detail, "severity": severity}


def board_checks(board: dict) -> list[dict]:
    """Problems the board file alone can prove, each with a count and examples."""
    findings: list[dict] = []

    findings.append(_finding(
        "zero-width-track", "Zero-width track segments",
        [f"{entry['layer']} {entry['start']}→{entry['end']}" for entry in board["tracks"]["zero_width"]],
        "A track of width 0 draws on screen but carries no copper, so nothing is manufactured there."))

    findings.append(_finding(
        "no-courtyard", "Footprints with no courtyard layer",
        [f"{footprint['reference']} ({footprint['library']})" for footprint in board["footprints"]
         if not footprint["courtyard"]],
        "The courtyard is the keep-out outline DRC uses for component-collision checks; without "
        "one, two parts can be placed through each other."))

    findings.append(_finding(
        "no-library", "Footprints not tied to a library",
        [f"{footprint['reference']} ({footprint['library']})" for footprint in board["footprints"]
         if not footprint["library_nickname"]],
        "The footprint name carries no library nickname, so it came from no library this file "
        "can name and a re-import will not find it again."))

    #: A multi-unit part is placed as one footprint per unit, all sharing a reference, so two
    #: footprints with one reference are only a fault when they claim the same pad. Footprints
    #: with the same reference and different pads are how KiCad stores a multi-unit part.
    by_reference: dict = {}
    for footprint in board["footprints"]:
        if footprint["reference"]:
            by_reference.setdefault(footprint["reference"], []).append(footprint)
    clashes = []
    for reference, found in by_reference.items():
        if len(found) < 2:
            continue
        claimed: dict = {}
        for footprint in found:
            for pad in footprint["pad_numbers"]:
                claimed[pad] = claimed.get(pad, 0) + 1
        shared = sorted(pad for pad, count in claimed.items() if count > 1)
        if shared:
            clashes.append(f"{reference} × {len(found)} footprints sharing pad {shared[0]}")
    findings.append(_finding(
        "duplicate-reference", "Reference designators whose footprints claim the same pad",
        clashes,
        "Two footprints with one reference are normal for a multi-unit part, as long as each unit "
        "owns its own pads. Sharing a pad number means two parts are being placed as one."))

    findings.append(_finding(
        "via-drill", "Vias whose drill is missing or as large as the pad",
        [f"({via['x']},{via['y']}) drill {via['drill']} size {via['size']}" for via in board["vias"]
         if not via["drill"] or not via["size"] or via["drill"] >= via["size"]],
        "A via needs a ring of copper between its drill and its pad; with a drill as large as "
        "the pad there is nothing left to plate."))

    findings.append(_finding(
        "single-pad-net", "Named nets a single pad sits on",
        [net["name"] for net in board["named_nets"] if net["pads"] == 1],
        "A named net with one pad goes nowhere: either the other end was never placed, or the "
        "net label is left over from a deleted part. A pad joined only to a zone fill still "
        "counts as one pad here."))

    outline = board["outline"]
    outside = []
    if outline:
        for footprint in board["footprints"]:
            x, y = footprint["x"], footprint["y"]
            if x is None or y is None:
                continue
            if not (outline["min_x"] <= x <= outline["max_x"] and outline["min_y"] <= y <= outline["max_y"]):
                outside.append(f"{footprint['reference']} at ({x},{y})")
    findings.append(_finding(
        "off-board", "Footprints outside the board outline", outside,
        "The footprint origin falls outside the bounding box of the Edge.Cuts graphics, so the "
        "part hangs off the board or is parked next to the drawing sheet."))

    findings.append(_finding(
        "unfilled-zone", "Copper zones that are not filled",
        [f"{zone['net'] or '(no net)'} on {'/'.join(zone['layers']) or 'no layer'}"
         for zone in board["zones"] if not zone["filled"]],
        "A zone with no filled polygon is not copper in the file, so the board will not match "
        "the design until it is refilled."))

    findings.append(_finding(
        "unrouted-net", "Named nets with no track on them",
        [net["name"] for net in board["named_nets"]
         if net["pads"] >= 2 and not net["routed"] and not net["zoned"]],
        "These nets join two or more pads with no track segment or arc anywhere in the file — "
        "a board saved mid-route, or one whose routing was deleted. Nets poured into a copper "
        "zone are left out, because a plane is copper even though it is not a track."))

    findings.append(_finding(
        "no-outline", "No Edge.Cuts outline",
        [] if outline else ["(no Edge.Cuts graphics in this file)"],
        "A board with no outline has no size: no fab can cut it, and every off-board check "
        "above is blind without one."))

    return findings


def schematic_checks(schematic: dict) -> list[dict]:
    """Problems the schematic file alone can prove, each with a count and examples."""
    findings: list[dict] = []
    parts = [symbol for symbol in schematic["symbols"] if not symbol["power"]]

    findings.append(_finding(
        "unannotated", "Unannotated symbols (reference ends in ?)",
        [f"{symbol['reference']} ({symbol['value'] or symbol['lib_id']})"
         for symbol in parts if symbol["reference"].endswith("?")],
        "An unannotated symbol has no unique reference yet, so the footprint on the board "
        "cannot be matched to it and the BOM cannot list it properly."))

    #: A multi-unit symbol is drawn as one symbol per unit and they all share a reference, so a
    #: repeated reference is only a fault when the same *unit* is placed twice.
    seen: dict = {}
    for symbol in parts:
        if symbol["reference"]:
            pair = (symbol["reference"], symbol["unit"])
            seen[pair] = seen.get(pair, 0) + 1
    findings.append(_finding(
        "duplicate-reference", "The same reference and unit placed twice",
        [f"{reference} unit {unit} × {count}"
         for (reference, unit), count in seen.items() if count > 1],
        "Two symbols sharing a reference *and* a unit are one part drawn twice, so the netlist "
        "joins them and KiCad renumbers one on the next annotation. A part with several units "
        "carries the same reference on each unit by design and is not counted here."))

    findings.append(_finding(
        "no-footprint", "Parts in the BOM and on the board with no footprint",
        [f"{symbol['reference']} ({symbol['value'] or symbol['lib_id']})" for symbol in parts
         if symbol["in_bom"] and symbol["on_board"] and not symbol["footprint"]],
        "The symbol is marked for both the BOM and the board but assigns no footprint, so the "
        "update-PCB step cannot place it."))

    findings.append(_finding(
        "dnp", "Symbols marked do-not-populate",
        [f"{symbol['reference']} ({symbol['value'] or symbol['lib_id']})"
         for symbol in parts if symbol["dnp"]],
        "DNP is a deliberate choice rather than a fault; it is listed so the parts missing from "
        "the assembled board are visible.", severity="note"))

    findings.append(_finding(
        "excluded-from-bom", "Symbols excluded from the BOM",
        [f"{symbol['reference']} ({symbol['value'] or symbol['lib_id']})"
         for symbol in parts if not symbol["in_bom"]],
        "Excluding a part from the BOM is normal for a mechanical or test-only part, and it is "
        "also where a real part goes missing by accident.", severity="note"))

    units: dict = {}
    for symbol in parts:
        if symbol["reference"]:
            units.setdefault(symbol["reference"], set()).add(symbol["unit"])
    findings.append(_finding(
        "multi-unit", "Parts with more than one unit placed",
        [f"{reference} × {len(found)} units" for reference, found in sorted(units.items())
         if len(found) > 1],
        "A multi-unit part (an op-amp package, a break-out connector) carries one reference "
        "across several units by design, which is why the duplicate check above counts units.",
        severity="note"))

    findings.append(_finding(
        "sheet-without-file", "Hierarchical sheets with no file",
        [sheet["name"] or "(unnamed sheet)" for sheet in schematic["sheets"] if not sheet["file"]],
        "A sheet that names no .kicad_sch file cannot be loaded or checked, and its contents "
        "are invisible to every answer here."))

    return findings


def parity_checks(schematic: dict, board: dict) -> dict:
    """Compare one schematic against one board: references and named nets on each side.

    A reference is the hard link between the two documents, so a mismatch there is a real
    difference. Net names are looser: the board names nets from the schematic's own labels, so
    a label the board never used is reported as a review lead, and auto-generated board net
    names (``Net-(P3-P1)``) are left out of the comparison altogether.
    """
    schematic_references = {symbol["reference"] for symbol in schematic["symbols"]
                            if symbol["reference"] and not symbol["power"]
                            and not symbol["reference"].endswith("?")}
    board_references = {footprint["reference"] for footprint in board["footprints"]
                        if footprint["reference"]}

    schematic_labels = {net_name(name) for name in
                        set(schematic["labels"]) | set(schematic["global_labels"])
                        | set(schematic["hierarchical_labels"])}
    #: A schematic names nets two ways: with a label, and with a power symbol (GND, +3V3). Both
    #: become net names on the board, so both belong in the comparison.
    power_nets = {net_name(symbol["value"]) for symbol in schematic["symbols"]
                  if symbol["power"] and symbol["value"] and symbol["value"] != "PWR_FLAG"}
    schematic_nets = schematic_labels | power_nets
    #: The board prefixes a local label with its sheet path, so the leading slash comes off
    #: before the two sides are lined up.
    board_nets = {net_name(net["name"]).lstrip("/") for net in board["named_nets"]}

    return {
        "schematic": {"references": len(schematic_references), "labels": len(schematic_labels),
                      "power_nets": len(power_nets), "net_names": len(schematic_nets)},
        "board": {"references": len(board_references), "nets": len(board_nets)},
        "references_only_in_schematic": sorted(schematic_references - board_references),
        "references_only_in_board": sorted(board_references - schematic_references),
        "net_names_not_a_board_net": sorted(schematic_nets - board_nets),
        "board_nets_not_in_schematic": sorted(board_nets - schematic_nets),
        "matching_references": len(schematic_references & board_references),
        "matching_net_names": len(schematic_nets & board_nets),
    }


class KicadData:
    """Reads KiCad documents: a built-in demo catalog, or any .kicad_pcb/.kicad_sch URL."""

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, user_agent: str | None = None, sleep=None) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("KICAD_BASE_URL") or BASE_URL).rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get("KICAD_CACHE_TTL", "3600"))
        self.timeout = float(timeout if timeout is not None else env.get("KICAD_HTTP_TIMEOUT", "60"))
        self.user_agent = user_agent or env.get("KICAD_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._sleep = sleep or time.sleep
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.RLock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str) -> bytes:
        """One HTTP attempt: the raw body, or a classified failure."""
        req = request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise KicadError(
                        f"that document is larger than {MAX_BYTES // (1024 * 1024)} MB, which is "
                        "past the size a board read here is meant for")
                return body
        except urlerror.HTTPError as exc:
            message = self._http_error(exc, url)
            if exc.code in RETRY_STATUSES:
                raise KicadBusy(message) from exc
            raise KicadError(message) from exc
        except KicadError:
            raise
        except IncompleteRead as exc:
            # A raw-file host can close a response early; what arrived is still the document.
            if exc.partial:
                return bytes(exc.partial)
            raise KicadBusy(f"the host closed the response early for {url}") from exc
        except Exception as exc:  # urllib raises many types; callers see one
            raise KicadBusy(f"could not read {url}: {exc}") from exc

    @staticmethod
    def _http_error(exc, url: str) -> str:
        """A readable reason for a failed read."""
        if exc.code == 404:
            return f"no document at {url} (HTTP 404)"
        if exc.code in RETRY_STATUSES:
            return f"the host answered HTTP {exc.code} for {url}; it is busy, so try again"
        return f"the host answered HTTP {exc.code} for {url}"

    def _read(self, url: str) -> str:
        """One cached read, retrying the failures that are the host's own."""
        now = time.time()
        with self._lock:
            hit = self._cache.get(url)
            if hit and hit[0] > now:
                return hit[1]
        failure: KicadBusy | None = None
        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                self._sleep(RETRY_BACKOFF * attempt)
            try:
                body = self._fetch(url)
            except KicadBusy as exc:
                failure = exc
                continue
            if isinstance(body, str):
                body = body.encode("utf-8", "replace")
            if not isinstance(body, (bytes, bytearray)):
                raise KicadError("the host returned an unexpected payload")
            text = bytes(body).decode("utf-8", "replace")
            with self._lock:
                self._cache[url] = (now + self.cache_ttl, text)
            return text
        raise failure or KicadBusy(f"could not read {url}")

    # -- references --------------------------------------------------------

    @staticmethod
    def is_url(reference) -> bool:
        return bool(_URL_RE.match(str(reference or "").strip()))

    @staticmethod
    def check_url(value) -> str:
        """A document URL: http(s) only, and it has to name a KiCad document."""
        url = str(value or "").strip().strip("<>")
        if not _URL_RE.match(url):
            raise ValueError("a document URL has to start with http:// or https://")
        return url

    @staticmethod
    def kind_of(reference) -> str:
        """Board or schematic, from the file half of a reference."""
        path = parse.urlparse(str(reference or "")).path.lower()
        if path.endswith(PCB_EXTENSION):
            return BOARD
        if path.endswith(SCH_EXTENSION):
            return SCHEMATIC
        raise ValueError(
            f"I only read KiCad {PCB_EXTENSION} and {SCH_EXTENSION} documents, and "
            f"{path.rsplit('/', 1)[-1] or reference!r} is neither")

    def sample(self, name):
        """A demo project from the built-in catalog, by the name or alias given."""
        key = " ".join(str(name or "").lower().strip().split())
        key = key.replace("-", " ").replace("_", " ").strip()
        if not key or len(key) < 3:
            return None
        for sample_key, sample in SAMPLES.items():
            if key == sample_key.replace("-", " "):
                return sample_key, sample
        best = None
        for sample_key, sample in SAMPLES.items():
            for alias in (sample_key, *sample["aliases"]):
                flat = alias.replace("-", " ").replace("_", " ")
                if key and (key == flat or key in flat or flat in key):
                    if best is None or len(flat) > len(best[2]):
                        best = (sample_key, sample, flat)
        return (best[0], best[1]) if best else None

    @staticmethod
    def sample_names() -> list[str]:
        return sorted(SAMPLES)

    def sample_url(self, key: str, kind: str) -> str:
        """The demo URL for a catalog entry, in the kind of document asked for."""
        sample = SAMPLES[key]
        path = sample["pcb"] if kind == BOARD else sample["sch"]
        return f"{self.base_url}/" + parse.quote(path, safe="/")

    def resolve(self, reference, kind: str | None = None):
        """A document reference -> (kind, url, label, where it came from).

        A URL is used as given (its extension decides the kind). A name is looked up in the
        built-in catalog; when a name matches a demo and no kind was asked for, the board is
        the default because that is the document people mean by "the board".
        """
        text = str(reference or "").strip()
        if self.is_url(text):
            url = self.check_url(text)
            found_kind = self.kind_of(url)
            label = parse.urlparse(url).path.rsplit("/", 1)[-1] or url
            return found_kind, url, label, "url"
        found = self.sample(text)
        if not found:
            raise KicadError(
                f"I do not know a KiCad demo called {text!r}. Give me a full document URL, or "
                f"one of the built-in demos: {', '.join(self.sample_names())}.")
        key, sample = found
        want = kind or BOARD
        url = self.sample_url(key, want)
        label = f"{key} {want} ({sample['title']})"
        return want, url, label, "sample"

    def document(self, reference, kind: str | None = None) -> dict:
        """Fetch a document and return (kind, label, source, url, text)."""
        found_kind, url, label, source = self.resolve(reference, kind)
        text = self._read(url)
        return {"kind": found_kind, "url": url, "label": label, "source": source, "text": text,
                "bytes": len(text.encode("utf-8"))}

    def read(self, reference, kind: str | None = None) -> dict:
        """Fetch and parse one document into its structured record."""
        document = self.document(reference, kind)
        record = (parse_board if document["kind"] == BOARD else parse_schematic)(document["text"])
        return {**record, "url": document["url"], "label": document["label"],
                "source": document["source"], "bytes": document["bytes"]}


__all__ = [
    "BASE_URL",
    "BOARD",
    "DATASET",
    "DEFAULT_TTL",
    "KicadBusy",
    "KicadData",
    "KicadError",
    "MAX_BYTES",
    "PCB_EXTENSION",
    "SAMPLES",
    "SCHEMATIC",
    "SCH_EXTENSION",
    "arc_length",
    "board_checks",
    "bounds",
    "parse_board",
    "parse_schematic",
    "parse_sexp",
    "parity_checks",
    "schematic_checks",
]
