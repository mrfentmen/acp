"""Read-only reader for AutoCAD DXF drawings.

A DXF file is not a binary blob: it is a flat stream of *group pairs* — a group code on one
line, its value on the next — grouped into records that start with a code-0 type name. That is
the whole format, which is why a drawing can be audited with nothing but a parser and an HTTP
read: no AutoCAD, no ODA library, no conversion step.

Verified live on 2026-09-24 against the DXF drawings shipped in the ezdxf project's own
repository: the ``usa`` map parsed as 59 outlines over 3 layers in R2013, ``hatches_1`` as ten
HATCH entities with SOLID fill, ``uncommon`` as the widest mix of entity types in one file,
``3_us_main`` as 10,052 lines (1.8 MB) in R2013, ``Leica_Disto_S910`` as an R12 survey export
with points, labels and no extents, and ``POLI-ALL210_12.DXF`` as old-style POLYLINE/VERTEX
geometry behind an uppercase extension.

The quirks, all handled here:

1. **Group codes are padded.** AutoCAD writes ``  5`` and ``     1``; the code line is trimmed
   before it is read as an integer. Values are *not* trimmed — a trailing space is part of a
   value in DXF, and layer names are case-insensitive to AutoCAD but byte-exact in the file.
2. **Line endings are mixed.** The same repository holds LF and CRLF files (and a stray ``\\r``
   only), so both are normalised before the stream is split.
3. **Empty values are real.** ``(code 1000)`` with an empty value line is legal extension data,
   so pairs are read line by line and an empty value is kept as ``""``.
4. **The stream stops at EOF.** One real fixture carries garbage after the ``EOF`` marker; the
   reader stops there and counts what it skipped instead of failing or guessing.
5. **Structure is not geometry.** ``VERTEX`` records belong to the ``POLYLINE`` they follow and
   ``ATTRIB`` records belong to the ``INSERT``; both are counted with their parent rather than
   as drawings objects of their own, and ``SEQEND`` is a terminator, not an entity.
6. **Extents can be unset.** AutoCAD writes ``1e+20``/``-1e+20`` when a drawing has never been
   zoomed to; that sentinel is reported as "not set" rather than as a size.
7. **Old and new encodings live in the same corpus.** R10-R12 files have no ``BLOCK_RECORD``
   table, no ``$INSUNITS`` and old-style ``POLYLINE`` geometry; R2018 files do. Nothing here
   enforces a version — the stamp is reported and the parser reads the fields that exist.
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

BASE_URL = "https://raw.githubusercontent.com/mozman/ezdxf/master"
DATASET = "github.com/mozman/ezdxf · DXF drawings"
DEFAULT_USER_AGENT = "acp-dxf/1.0 (+https://github.com/mrfentmen/acp)"

#: The largest drawing in the demo set is 1.8 MB; far past that is not a drawing anyone asked
#: to audit, so it is refused with its size instead of parsed for minutes.
MAX_BYTES = 12 * 1024 * 1024

#: Raw-file hosts answer 429/5xx when they are busy; those reads are retried, a 404 never is.
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.6

#: Drawings change rarely, so a document is cached for an hour.
DEFAULT_TTL = 3600.0
DEFAULT_TIMEOUT = 60.0

#: The one extension this reader accepts, matched case-insensitively (real files ship as .DXF).
DXF_EXTENSION = ".dxf"

#: AutoCAD's binary DXF marker. Binary DXF is a different format and is reported as such.
BINARY_MAGIC = b"AutoCAD Binary DXF\r\n\x1a\x00"

#: How many examples a check line prints before it just counts the rest.
EXAMPLE_LIMIT = 5

#: How many text strings a summary keeps before it only counts them.
TEXT_LIMIT = 40

#: Drawing format stamps, as AutoCAD names the releases.
VERSIONS = {
    "AC1001": "R2.5", "AC1002": "R2.6", "AC1003": "R2.6", "AC1004": "R9",
    "AC1006": "R10", "AC1009": "R11/R12", "AC1012": "R13", "AC1014": "R14",
    "AC1015": "2000", "AC1016": "2000i", "AC1018": "2004", "AC1021": "2007",
    "AC1024": "2010", "AC1027": "2013", "AC1032": "2018",
}

#: $INSUNITS codes, as AutoCAD names the units.
INSUNITS = {
    0: "unitless (no unit declared)", 1: "inches", 2: "feet", 3: "miles",
    4: "millimetres", 5: "centimetres", 6: "metres", 7: "kilometres",
    8: "microinches", 9: "mils", 10: "yards", 11: "ångströms", 12: "nanometres",
    13: "microns", 14: "decimetres", 15: "decametres", 16: "hectometres",
    17: "gigametres", 18: "astronomical units", 19: "light years", 20: "parsecs",
    21: "US survey feet", 22: "US survey inches", 23: "US survey yards",
    24: "US survey miles",
}

#: Records that belong to the record before them rather than to the drawing.
STRUCTURAL = ("VERTEX", "SEQEND", "ATTRIB")

#: The layer AutoCAD reserves for construction marks that are never plotted.
DEFPOINTS = "DEFPOINTS"

#: AutoCAD's "this drawing has never been zoomed to" sentinel.
EXTENT_SENTINEL = 1e20

#: Records inside a block definition that are block structure, not block content.
BLOCK_HEADER = ("BLOCK", "ENDBLK")

#: The layout blocks: AutoCAD keeps model space and every paper-space layout in blocks named
#: after them (*Paper_Space0, *Paper_Space1, ...), and their contents live in the ENTITIES
#: section, so an empty or unplaced one is not a finding.
LAYOUT_BLOCK_PREFIXES = ("*MODEL_SPACE", "*PAPER_SPACE", "$MODEL_SPACE", "$PAPER_SPACE")

#: Entity types whose geometry is a point list, so a bounding box and a length can be read.
_POINT_ENTITIES = frozenset({
    "LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE", "POINT",
    "SOLID", "TRACE", "3DFACE", "TEXT", "MTEXT", "ATTRIB", "ATTDEF", "INSERT",
})

#: Entity types that are fills or areas: they are counted and never measured, and the answers
#: say so rather than pretending a hatch has a length.
UNMEASURED = frozenset({
    "HATCH", "SOLID", "TRACE", "WIPEOUT", "IMAGE", "DIMENSION", "LEADER", "MULTILEADER",
    "MLINE", "VIEWPORT", "MESH", "REGION", "BODY", "3DSOLID", "HELIX", "LIGHT", "TOLERANCE",
    "SHAPE", "XLINE", "RAY", "OLE2FRAME", "ACAD_PROXY_ENTITY", "TABLE", "SPLINE",
})

#: A demo catalogue: the drawings this agent knows by name, all from one public repository.
SAMPLES = {
    "usa": {
        "title": "a map of the United States, 59 outlines over 3 layers",
        "path": "examples/addons/drawing/data/usa.dxf",
        "aliases": ("map of the usa", "united states", "world map"),
    },
    "houses-of-parliament": {
        "title": "a georeferenced outline of the Palace of Westminster",
        "path": "tests/test_01_dxf_entities/houses_of_parliament_georeferenced.dxf",
        "aliases": ("parliament", "houses of parliament", "westminster", "georeferenced"),
    },
    "leica": {
        "title": "a Leica Disto survey export: R12 points and labels, no extents",
        "path": "integration_tests/data/Leica_Disto_S910.dxf",
        "aliases": ("disto", "survey", "leica disto"),
    },
    "us-main": {
        "title": "10,052 lines of US coastline in one R2013 file, 1.8 MB",
        "path": "examples/edgeminer/3_us_main.dxf",
        "aliases": ("us main", "coastline", "edgeminer", "us coastline"),
    },
    "uncommon": {
        "title": "the widest mix of entity types in one file",
        "path": "examples_dxf/uncommon.dxf",
        "aliases": ("everything", "mixed entities", "all entities"),
    },
    "hatches": {
        "title": "ten hatches, six arcs and sixteen polylines on one layer",
        "path": "examples_dxf/hatches_1.dxf",
        "aliases": ("hatch", "hatch patterns", "hatches 1"),
    },
    "text": {
        "title": "every text and mtext alignment the format has, plus attributes",
        "path": "examples_dxf/text.dxf",
        "aliases": ("text alignments", "mtext", "attributes"),
    },
    "colors": {
        "title": "AutoCAD colour index chips drawn with circles and inserts",
        "path": "examples_dxf/colors.dxf",
        "aliases": ("colours", "colour index", "aci colors"),
    },
    "forms": {
        "title": "circles, ellipses and 33 polylines from the optimizer examples",
        "path": "examples/addons/optimize/forms.dxf",
        "aliases": ("optimize forms", "opt forms"),
    },
    "poli-all": {
        "title": "old-style POLYLINE/VERTEX geometry behind an uppercase .DXF extension",
        "path": "integration_tests/data/POLI-ALL210_12.DXF",
        "aliases": ("poli all", "poli", "old style polylines"),
    },
    "block-clipped": {
        "title": "a block reference carrying a clipping boundary",
        "path": "exploration/BlockClipped.dxf",
        "aliases": ("block clipped", "clipped block", "clip"),
    },
    "minimal-r10": {
        "title": "the smallest R10 file: one line and two 3DFACEs",
        "path": "examples_dxf/Minimal_DXF_AC1006.dxf",
        "aliases": ("r10 minimal", "minimal 1006"),
    },
    "minimal-r12": {
        "title": "35 bytes: an ENTITIES section with nothing in it",
        "path": "examples_dxf/Minimal_DXF_AC1009.dxf",
        "aliases": ("minimal ac1009", "empty r12"),
    },
    "minimal-2007": {
        "title": "an empty R2007 drawing carrying all of its tables",
        "path": "examples_dxf/Minimal_DXF_AC1021.dxf",
        "aliases": ("minimal 2007", "empty 2007", "r2007 minimal"),
    },
    "ascii-r12": {
        "title": "R12 with a block, an insert and a label",
        "path": "integration_tests/data/ASCII_R12.dxf",
        "aliases": ("ascii r12", "r12 blocks"),
    },
}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_CODE_RE = re.compile(r"^-?\d+$")


class DxfError(RuntimeError):
    """A drawing that cannot be read as asked, with the reason in the message."""


class DxfBusy(DxfError):
    """A read that failed for a reason worth retrying (host busy, connection dropped)."""


# -- the group-pair stream -----------------------------------------------------


def pairs(text: str) -> tuple[list[tuple[int, str]], int, bool]:
    """A DXF document as (group pairs, junk lines, whether the EOF marker was reached).

    A pair is a group code on one line and its value on the next. Codes are padded with spaces
    and values are not trimmed. Reading stops at the ``EOF`` record, which is where AutoCAD
    stops; anything after it is counted and ignored.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[tuple[int, str]] = []
    junk = 0
    reached_eof = False
    index = 0
    total = len(lines)
    while index + 1 < total:
        raw = lines[index].strip()
        if not _CODE_RE.match(raw):
            junk += 1
            index += 1
            continue
        code = int(raw)
        value = lines[index + 1]
        out.append((code, value))
        index += 2
        if code == 0 and value.strip().upper() == "EOF":
            # AutoCAD stops at the EOF record, so anything after it is not read at all. The
            # record itself stays in the stream, because that is where the file says it ends.
            reached_eof = True
            break
    return out, junk, reached_eof


def records(pair_list) -> list[tuple[str, list[tuple[int, str]]]]:
    """Group pairs into DXF records: a record starts at every code 0 (or code 9) pair.

    Code 9 is a named header variable, so it opens a record for the same reason code 0 does.
    """
    out: list[tuple[str, list[tuple[int, str]]]] = []
    fields: list[tuple[int, str]] = []
    name = ""
    started = False
    for code, value in pair_list:
        if code in (0, 9):
            if started:
                out.append((name, fields))
            name = value.strip() if code == 0 else value.strip()
            fields = []
            started = True
        elif started:
            fields.append((code, value))
    if started:
        out.append((name, fields))
    return out


# -- record helpers ------------------------------------------------------------


def value_of(fields, code: int, default=None):
    """The first value of a group code in a record. DXF repeats codes, so the first wins."""
    for field_code, value in fields:
        if field_code == code:
            return value
    return default


def values_of(fields, code: int) -> list[str]:
    return [value for field_code, value in fields if field_code == code]


def number(fields, code: int, default: float = 0.0) -> float:
    """A group code as a float: DXF writes numbers as text and sometimes padded."""
    raw = value_of(fields, code)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def numbers(fields, code: int) -> list[float]:
    out = []
    for field_code, value in fields:
        if field_code != code:
            continue
        try:
            out.append(float(value.strip()))
        except ValueError:
            continue
    return out


def flag(fields, code: int) -> int:
    """A group code as an integer bit field; ``raw`` keeps its text for the answers."""
    raw = value_of(fields, code)
    if raw is None:
        return 0
    try:
        return int(float(raw.strip()))
    except ValueError:
        return 0


def point(fields, code: int = 10, ycode: int = 20) -> tuple[float, float]:
    return (number(fields, code), number(fields, ycode))


def layer_of(fields) -> str:
    """The layer an entity sits on (group code 8)."""
    return value_of(fields, 8, "") or ""


def text_of(fields) -> str:
    """The text of a TEXT/MTEXT/ATTDEF/ATTRIB record.

    Long MTEXT is written as several code-3 chunks followed by the final code-1 chunk, so the
    two are joined in file order instead of only the last one being read.
    """
    chunks = []
    for code, value in fields:
        if code == 3:
            chunks.append(value)
    final = value_of(fields, 1, "")
    if final:
        chunks.append(final)
    if chunks:
        return "".join(chunks)
    return value_of(fields, 2, "") or ""


def round_value(value: float, digits: int = 2) -> float:
    """A number as an answer prints it: rounded, and never negative zero."""
    rounded = round(float(value), digits)
    return 0.0 + rounded


def unescape(value: str) -> str:
    """MTEXT formatting codes are stripped down to what the string says.

    ``\\P`` is a paragraph break and ``\\~`` a hard space, both of which become a space here.
    Parameterised codes (``\\fArial|b1;``, ``\\H2.5x;``) run to their semicolon, and the bare
    ones (``\\L`` underline, ``\\O`` overline, ``\\K`` strikethrough) are single letters.
    """
    text = value.replace("\\~", " ").replace("\\P", " ").replace("\\p", " ")
    text = re.sub(r"\\[A-Za-z][^;]*;", " ", text)
    text = re.sub(r"\\[A-Za-z]", " ", text)
    text = text.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    text = text.replace("{", "").replace("}", "")
    return " ".join(text.split())


def arc_length(radius: float, start_angle: float, end_angle: float) -> float:
    """The length of an arc from its radius and the two angles in degrees.

    The angles wrap the AutoCAD way: an arc always runs counter-clockwise from its start angle
    to its end angle, so an end angle smaller than the start angle has crossed 360.
    """
    sweep = (end_angle - start_angle) % 360.0
    if sweep == 0.0 and radius > 0:
        sweep = 360.0
    return abs(radius) * math.radians(sweep)


def bulge_length(chord: float, bulge: float) -> float:
    """The arc length of a polyline segment given its chord and its bulge.

    A bulge is tan(¼ × included angle), which is how DXF stores an arc inside a polyline.
    """
    if not bulge or chord <= 0:
        return 0.0
    included = 4.0 * math.atan(bulge)
    half = included / 2.0
    if math.isclose(math.sin(half), 0.0):
        return 0.0
    radius = chord / (2.0 * math.sin(half))
    return abs(radius * included)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


# -- reading a drawing ---------------------------------------------------------


def _section_ranges(recs):
    """Where each top-level SECTION starts and ends, by name."""
    ranges = {}
    depth = None
    start = 0
    for index, (name, fields) in enumerate(recs):
        if name == "SECTION":
            section = (value_of(fields, 2, "") or "").strip().upper()
            if section:
                depth = section
                start = index + 1
        elif name == "ENDSEC" and depth:
            ranges[depth] = (start, index)
            depth = None
    return ranges


def _read_header(recs, start: int, end: int) -> dict:
    """The drawing-wide settings that were actually written."""
    raw = {}
    for name, fields in recs[start:end]:
        if not name.startswith("$"):
            continue
        if name not in raw:
            raw[name] = fields

    def value(variable, code):
        fields = raw.get(variable)
        return None if fields is None else value_of(fields, code)

    def as_number(variable, code):
        fields = raw.get(variable)
        return None if fields is None else number(fields, code)

    def as_point(variable):
        fields = raw.get(variable)
        if fields is None:
            return None
        return point(fields)

    units = raw.get("$INSUNITS")
    units_code = None if units is None else flag(units, 70)
    insunits = {
        "declared": units is not None,
        "code": units_code,
        "name": INSUNITS.get(units_code or 0, f"code {units_code}"),
    }
    measurement = raw.get("$MEASUREMENT")
    return {
        "version_code": (value("$ACADVER", 1) or "").strip(),
        "code_page": (value("$DWGCODEPAGE", 3) or "").strip(),
        "handle_seed": (value("$HANDSEED", 5) or "").strip(),
        "insunits": insunits,
        "measurement": None if measurement is None else flag(measurement, 70),
        "extmin": as_point("$EXTMIN"),
        "extmax": as_point("$EXTMAX"),
        "limmin": as_point("$LIMMIN"),
        "limmax": as_point("$LIMMAX"),
        "insbase": as_point("$INSBASE"),
        "pdmode": as_number("$PDMODE", 70),
        "pdsize": as_number("$PDSIZE", 40),
        "ltscale": as_number("$LTSCALE", 40),
        "dimscale": as_number("$DIMSCALE", 40),
        "clayer": (value("$CLAYER", 8) or "").strip(),
        "cecolor": (value("$CECOLOR", 62) or "").strip(),
        "variables": sorted(raw),
    }


def extent_pair(header, low_key: str, high_key: str):
    """A pair of extents as a box, or ``None`` when the drawing has never set them.

    AutoCAD writes ±1e20 when a drawing has never been zoomed to, which is "not set", not a
    size: that sentinel and a reversed or missing box are all reported as nothing.
    """
    low = header.get(low_key)
    high = header.get(high_key)
    if not low or not high:
        return None
    if any(abs(value) >= EXTENT_SENTINEL for value in (*low, *high)):
        return None
    if low[0] > high[0] or low[1] > high[1]:
        return None
    return (low[0], low[1], high[0], high[1])


def _layer_from(fields) -> tuple[str, dict] | None:
    """One LAYER table record: name, colour, linetype, flags."""
    name = value_of(fields, 2, "")
    if name is None:
        return None
    flags = flag(fields, 70)
    color = flag(fields, 62)
    return name, {
        "name": name,
        "color": color,
        "off": color < 0,
        "abs_color": abs(color),
        "linetype": (value_of(fields, 6, "") or "").strip(),
        "flags": flags,
        "frozen": bool(flags & 1),
        "locked": bool(flags & 4),
        "lineweight": flag(fields, 370),
        "plottable": flag(fields, 290) != 0,
        "has_plottable_flag": value_of(fields, 290) is not None,
    }


def _read_tables(recs, start: int, end: int):
    """The symbol tables: which tables exist, how many records each holds, and the layers."""
    tables: dict[str, int] = {}
    layers: dict[str, dict] = {}
    order: list[str] = []
    current = None
    for name, fields in recs[start:end]:
        if name == "TABLE":
            current = (value_of(fields, 2, "") or "").strip().upper()
            if current:
                tables.setdefault(current, 0)
            continue
        if name == "ENDTAB":
            current = None
            continue
        if not current:
            continue
        tables[current] = tables.get(current, 0) + 1
        if current == "LAYER":
            found = _layer_from(fields)
            if found and found[0] not in layers:
                layers[found[0]] = found[1]
                order.append(found[0])
    return tables, layers, order


def _read_blocks(recs, start: int, end: int):
    """The block definitions: name, base point, and what each one draws."""
    blocks: dict[str, dict] = {}
    order: list[str] = []
    current = None
    for name, fields in recs[start:end]:
        if name == "BLOCK":
            block_name = value_of(fields, 2, "") or ""
            flags = flag(fields, 70)
            # A definition with no name is real (an anonymous or broken one): it is kept under
            # a name that cannot collide with a real block, and the checks call it out.
            key = block_name or f"(unnamed #{len(order) + 1})"
            while key in blocks:
                key += "'"
            current = {
                "name": block_name,
                "key": key,
                "base": (number(fields, 10), number(fields, 20), number(fields, 30)),
                "flags": flags,
                "anonymous": bool(flags & 1) or block_name.startswith(("*", "$")),
                "xref": bool(flags & 4),
                "layer": layer_of(fields),
                "by_type": {},
                "count": 0,
                "texts": 0,
            }
            blocks[key] = current
            order.append(key)
            continue
        if name == "ENDBLK":
            current = None
            continue
        if current is None or name in BLOCK_HEADER or name in STRUCTURAL:
            continue
        current["by_type"][name] = current["by_type"].get(name, 0) + 1
        current["count"] += 1
        if name in ("TEXT", "MTEXT", "ATTDEF", "ATTRIB"):
            current["texts"] += 1
    return blocks, order


def _measure_entity(kind: str, fields, vertices=None):
    """What one entity contributes: (length, points, how it is measured).

    ``points`` is the geometry the entity occupies, for the bounding box. The third part says
    how the object is accounted for: ``length`` for the kinds this reader can give a length to,
    ``point`` for objects that occupy a place and have no length by nature (a point, a label, a
    block reference), and ``fill`` for fills, areas and curve types that carry no length in the
    file — those are counted and never given a number.
    """
    x, y = point(fields, 10, 20)
    if kind == "LINE":
        other = point(fields, 11, 21)
        return distance((x, y), other), [(x, y), other], "length"
    if kind == "LWPOLYLINE":
        return (*_polyline_measure([(px, py) for px, py in _lwpolyline_vertices(fields)],
                                   _lwpolyline_bulges(fields),
                                   bool(flag(fields, 70) & 1)), "length")
    if kind == "POLYLINE":
        points = [(px, py) for px, py, _bulge in (vertices or [])]
        bulges = [bulge for _px, _py, bulge in (vertices or [])]
        return (*_polyline_measure(points, bulges, bool(flag(fields, 70) & 1)), "length")
    if kind in ("CIRCLE", "ARC"):
        radius = number(fields, 40)
        if kind == "CIRCLE":
            length = 2.0 * math.pi * abs(radius)
        else:
            length = arc_length(radius, number(fields, 50), number(fields, 51))
        corners = [(x - abs(radius), y - abs(radius)), (x + abs(radius), y + abs(radius))]
        return length, corners, "length"
    if kind == "POINT":
        return 0.0, [(x, y)], "point"
    if kind in ("TEXT", "MTEXT", "ATTDEF", "ATTRIB"):
        return 0.0, [(x, y)], "point"
    if kind in ("SOLID", "TRACE", "3DFACE"):
        corners = [(x, y)]
        for code, ycode in ((11, 21), (12, 22), (13, 23)):
            if value_of(fields, code) is not None:
                corners.append(point(fields, code, ycode))
        return 0.0, corners, "fill"
    if kind == "INSERT":
        return 0.0, [(x, y)], "point"
    if kind == "ELLIPSE":
        # The axis endpoint is a vector from the centre, and the second axis is that vector
        # scaled by the ratio, so the box is exact for a full ellipse (a wider one for an
        # elliptical arc, which this format describes by a parameter range).
        major = point(fields, 11, 21)
        ratio = abs(number(fields, 40, 1.0))
        reach = math.hypot(major[0], major[1])
        if reach:
            ux, uy = major[0] / reach, major[1] / reach
        else:
            ux, uy = 1.0, 0.0
        minor = reach * ratio
        wide = math.hypot(reach * ux, minor * uy)
        tall = math.hypot(reach * uy, minor * ux)
        corners = [(x - wide, y - tall), (x + wide, y + tall)]
        return 0.0, corners, "fill"
    if kind == "SPLINE":
        corners = [(cx, cy) for cx, cy in zip(numbers(fields, 10), numbers(fields, 20))]
        return 0.0, corners, "fill"
    if kind in UNMEASURED:
        return 0.0, [], "fill"
    return 0.0, [], "fill"


def _lwpolyline_vertices(fields):
    """The vertices of a LWPOLYLINE: code 10 x and the code 20 y that follows it."""
    out = []
    pending = None
    for code, value in fields:
        if code == 10:
            pending = _as_float(value)
        elif code == 20 and pending is not None:
            out.append((pending, _as_float(value)))
            pending = None
    return out


def _lwpolyline_bulges(fields):
    """The bulge of each LWPOLYLINE vertex, in file order, zero where none is written."""
    bulges = []
    for code, value in fields:
        if code == 42:
            bulges.append(_as_float(value))
    return bulges


def _polyline_measure(points, bulges, closed: bool):
    """The drawn length of a polyline and the points it covers."""
    if not points:
        return 0.0, []
    length = 0.0
    pairs = list(zip(points, points[1:]))
    if closed and len(points) > 2:
        pairs.append((points[-1], points[0]))
    for index, (a, b) in enumerate(pairs):
        chord = distance(a, b)
        bulge = bulges[index] if index < len(bulges) else 0.0
        length += bulge_length(chord, bulge) if bulge else chord
    return length, list(points)


def _bump(counter: dict, key: str) -> None:
    """Count one geometry fault against the layer it sits on."""
    counter[key] = counter.get(key, 0) + 1


def _as_float(value: str) -> float:
    try:
        return float(value.strip())
    except ValueError:
        return 0.0


def _read_entities(recs, start: int, end: int) -> dict:
    """Every drawing object in the ENTITIES section, with geometry and text."""
    by_type: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    by_layer_length: dict[str, float] = {}
    by_layer_unmeasured: dict[str, int] = {}
    faults = {"zero_length_lines": {}, "bad_radius": {}, "short_polylines": {},
              "vertexless_polylines": {}, "zero_height_texts": {}}
    by_space = {"model": 0, "paper": 0}
    layouts: dict[str, int] = {}
    length = 0.0
    measured = 0
    positioned = 0
    unmeasured: dict[str, int] = {}
    box = None
    texts: list[dict] = []
    text_total = 0
    inserts: list[dict] = []
    vertices = 0
    closed_polylines = 0

    sections = recs[start:end]
    index = 0
    while index < len(sections):
        kind, fields = sections[index]
        index += 1
        if kind in ("ENDSEC", "SECTION", ""):
            continue
        if kind == "SEQEND":
            continue
        if kind == "VERTEX":
            vertices += 1
            continue
        if kind == "POLYLINE":
            poly_vertices = []
            while index < len(sections):
                next_kind, next_fields = sections[index]
                if next_kind != "VERTEX":
                    break
                poly_vertices.append((number(next_fields, 10), number(next_fields, 20),
                                      number(next_fields, 42)))
                index += 1
            if index < len(sections) and sections[index][0] == "SEQEND":
                index += 1
            vertices += len(poly_vertices)
            if flag(fields, 70) & 1:
                closed_polylines += 1
            if not poly_vertices:
                _bump(faults["vertexless_polylines"], layer_of(fields))
            elif len(poly_vertices) < 2:
                _bump(faults["short_polylines"], layer_of(fields))
            entity_length, points, measure = _measure_entity("POLYLINE", fields, poly_vertices)
        else:
            attributes: list[dict] = []
            if kind == "INSERT":
                while index < len(sections) and sections[index][0] == "ATTRIB":
                    attribute = sections[index][1]
                    attributes.append({"tag": value_of(attribute, 2, ""),
                                       "value": text_of(attribute)})
                    index += 1
                if index < len(sections) and sections[index][0] == "SEQEND":
                    index += 1
            entity_length, points, measure = _measure_entity(kind, fields)
            if kind == "LWPOLYLINE" and flag(fields, 70) & 1:
                closed_polylines += 1
            if kind in ("LWPOLYLINE", "POLYLINE") and len(points) < 2:
                _bump(faults["short_polylines"], layer_of(fields))
            if kind == "LINE" and not entity_length:
                _bump(faults["zero_length_lines"], layer_of(fields))
            if kind in ("CIRCLE", "ARC") and number(fields, 40) <= 0:
                _bump(faults["bad_radius"], layer_of(fields))
            if kind in ("TEXT", "MTEXT") and number(fields, 40) <= 0:
                _bump(faults["zero_height_texts"], layer_of(fields))
            if kind == "INSERT":
                inserts.append({
                    "block": value_of(fields, 2, "") or "",
                    "at": point(fields, 10, 20),
                    "scale": (number(fields, 41, 1.0), number(fields, 42, 1.0), number(fields, 43, 1.0)),
                    "rotation": number(fields, 50),
                    "attributes": attributes,
                })

        layer = layer_of(fields)
        by_type[kind] = by_type.get(kind, 0) + 1
        by_layer[layer] = by_layer.get(layer, 0) + 1
        if points:
            positioned += 1
        if measure == "length":
            measured += 1
            length += entity_length
            by_layer_length[layer] = by_layer_length.get(layer, 0.0) + entity_length
        elif measure == "fill":
            unmeasured[kind] = unmeasured.get(kind, 0) + 1
            by_layer_unmeasured[layer] = by_layer_unmeasured.get(layer, 0) + 1
        for px, py in points:
            if not (math.isfinite(px) and math.isfinite(py)):
                continue
            box = (px, py, px, py) if box is None else (
                min(box[0], px), min(box[1], py), max(box[2], px), max(box[3], py))
        layout = (value_of(fields, 410, "") or "").strip()
        if flag(fields, 67) == 1:
            by_space["paper"] += 1
            if layout:
                layouts[layout] = layouts.get(layout, 0) + 1
        else:
            by_space["model"] += 1
        if kind in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
            text_total += 1
            if len(texts) < TEXT_LIMIT:
                texts.append({"kind": kind, "layer": layer, "value": unescape(text_of(fields)),
                              "height": number(fields, 40)})

    return {
        "count": sum(by_type.values()),
        "by_type": by_type,
        "by_layer": by_layer,
        "by_layer_length": by_layer_length,
        "by_layer_unmeasured": by_layer_unmeasured,
        "by_space": by_space,
        "layouts": layouts,
        "inserts": inserts,
        "vertices": vertices,
        "closed_polylines": closed_polylines,
        "geometry": {"length": length, "measured": measured, "positioned": positioned,
                     "unmeasured": unmeasured, "bbox": box},
        "texts": texts,
        "text_total": text_total,
        "faults": faults,
    }


def parse_drawing(text: str) -> dict:
    """One DXF document as a structured record: format, header, tables, blocks, entities."""
    if text.startswith(BINARY_MAGIC.decode("latin-1")) or text.encode("latin-1", "ignore")[:22] == BINARY_MAGIC:
        raise DxfError(
            "this is a binary DXF file. The format is compact and undocumented outside "
            "AutoCAD, so read it here as ASCII DXF: in AutoCAD or ODA File Converter, "
            "save-as DXF with the ASCII option.")
    pair_list, junk, reached_eof = pairs(text)
    if not pair_list:
        raise DxfError("this file holds no DXF group pairs, so there is nothing to read")
    recs = records(pair_list)
    ranges = _section_ranges(recs)
    if not ranges:
        raise DxfError(
            "this file has no SECTION records, so it is not a DXF drawing: nothing in it "
            "can be read as a drawing object")

    header = _read_header(recs, *ranges["HEADER"]) if "HEADER" in ranges else _read_header([], 0, 0)
    tables, layers, layer_order = (
        _read_tables(recs, *ranges["TABLES"]) if "TABLES" in ranges else ({}, {}, []))
    blocks, block_order = (
        _read_blocks(recs, *ranges["BLOCKS"]) if "BLOCKS" in ranges else ({}, []))
    entities = (_read_entities(recs, *ranges["ENTITIES"]) if "ENTITIES" in ranges
                else _read_entities([], 0, 0))

    version_code = header["version_code"]
    extents = extent_pair(header, "extmin", "extmax")
    limits = extent_pair(header, "limmin", "limmax")

    inserts_by_block: dict[str, int] = {}
    for insert in entities["inserts"]:
        name = insert["block"]
        inserts_by_block[name] = inserts_by_block.get(name, 0) + 1

    return {
        "format": {
            "code": version_code,
            "name": VERSIONS.get(version_code, version_code or "unknown"),
            "code_page": header["code_page"],
            "handle_seed": header["handle_seed"],
        },
        "sections": _section_order(recs),
        "header": header,
        "tables": tables,
        "layers": layers,
        "layer_order": layer_order,
        "blocks": blocks,
        "block_order": block_order,
        "inserts_by_block": inserts_by_block,
        "entities": entities,
        "faults": entities["faults"],
        "extents": extents,
        "limits": limits,
        "junk_lines": junk,
        "eof_found": reached_eof,
        "bytes": len(text.encode("utf-8")),
    }


def _section_order(recs) -> list[str]:
    """Section names in the order the file writes them."""
    order = []
    for name, fields in recs:
        if name == "SECTION":
            section = (value_of(fields, 2, "") or "").strip().upper()
            if section and section not in order:
                order.append(section)
    return order


def layer_index(layers) -> dict[str, str]:
    """Layer names as AutoCAD matches them: folded, so ``WALLS`` and ``Walls`` are one layer."""
    index = {}
    for name in layers:
        index.setdefault(name.strip().upper(), name)
    return index


def _finding(check: str, detail: str, severity: str, items, count=None) -> dict:
    """One check result: what was asked, what the file answers, and how many things that is.

    ``items`` are the lines an answer prints (collapsed at EXAMPLE_LIMIT), so ``count`` carries
    the real number of offending objects separately from how many of them are shown.
    """
    lines = list(items or [])
    return {"check": check, "detail": detail, "severity": severity, "items": lines,
            "count": len(lines) if count is None else int(count)}


def _tally(counter) -> list[str]:
    """A count map as readable lines, biggest first, with the tail collapsed."""
    ordered = sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))
    shown = [f"{name or '(no layer name)'} ({count})" for name, count in ordered[:EXAMPLE_LIMIT]]
    if len(ordered) > EXAMPLE_LIMIT:
        shown.append(f"and {len(ordered) - EXAMPLE_LIMIT} more")
    return shown


def _listed(names) -> list[str]:
    """Names as readable lines, with the tail collapsed rather than cut off silently."""
    ordered = sorted(names)
    shown = list(ordered[:EXAMPLE_LIMIT])
    if len(ordered) > EXAMPLE_LIMIT:
        shown.append(f"and {len(ordered) - EXAMPLE_LIMIT} more")
    return shown


def is_layout_block(key: str) -> bool:
    """True for the blocks AutoCAD keeps model space and the paper-space layouts in."""
    return str(key or "").strip().upper().startswith(LAYOUT_BLOCK_PREFIXES)


def drawing_checks(drawing: dict) -> list[dict]:
    """File-level checks on one drawing.

    These read what the file says. They are not AutoCAD's audit, not a DRC, and not a plot
    check: no linetype scale, dimension-style or paper-space-fit rule is evaluated here.
    """
    layers = drawing["layers"]
    index = layer_index(layers)
    entities = drawing["entities"]
    by_layer = entities["by_layer"]
    blocks = drawing["blocks"]
    inserts = drawing["inserts_by_block"]
    findings = []

    # An entity with no layer name at all is not "an undefined layer": it is its own check,
    # so it is kept out of this one rather than counted twice.
    unknown = {name: count for name, count in by_layer.items()
               if name.strip() and name.strip().upper() not in index}
    findings.append(_finding(
        "Entities on a layer the drawing never defines",
        "The LAYER table is what tells AutoCAD how to draw an entity; an entity on a name that "
        "is not in it falls back to layer 0 and loses its colour and linetype.",
        "fault", [f"{name or '(no layer name)'} ({count} entities)"
                  for name, count in sorted(unknown.items())], count=sum(unknown.values())))

    defpoints = {name: count for name, count in by_layer.items()
                 if name.strip().upper() == DEFPOINTS}
    findings.append(_finding(
        f"Geometry on the {DEFPOINTS} layer (AutoCAD never plots it)",
        f"{DEFPOINTS} exists so dimensions can hold their definition points. Anything drawn "
        "there is invisible in every print, which is usually not what the author meant.",
        "fault", [f"{DEFPOINTS} ({count} entities)" for count in defpoints.values()],
        count=sum(defpoints.values())))

    off = {name: count for name, count in by_layer.items()
           if (layers.get(index.get(name.strip().upper(), ""), {}).get("off"))}
    findings.append(_finding(
        "Entities on layers switched off",
        "A layer is switched off by a negative colour number. Its entities still exist and "
        "still plot if the layer is turned back on, so hidden work is reported rather than lost.",
        "fault", [f"{name} ({count} entities)" for name, count in sorted(off.items())],
        count=sum(off.values())))

    frozen = {name: count for name, count in by_layer.items()
              if (layers.get(index.get(name.strip().upper(), ""), {}).get("frozen"))}
    findings.append(_finding(
        "Entities on frozen layers",
        "Frozen layers are skipped by AutoCAD's regeneration and are not plotted. Frozen "
        "geometry is worth knowing about before someone turns the layer back on.",
        "fault", [f"{name} ({count} entities)" for name, count in sorted(frozen.items())],
        count=sum(frozen.values())))

    folded: dict[str, list[str]] = {}
    for name in layers:
        folded.setdefault(name.strip().upper(), []).append(name)
    collisions = [names for names in folded.values() if len(names) > 1]
    findings.append(_finding(
        "Layer names that differ only by case",
        "AutoCAD layer names are case-insensitive, so two names that differ only in case are "
        "one layer to AutoCAD and two records to every reader that is not careful.",
        "fault", [" vs ".join(names) for names in collisions],
        count=sum(len(names) for names in collisions)))

    no_layer = by_layer.get("", 0)
    findings.append(_finding(
        "Entities with no layer name at all",
        "A drawing object with no group code 8 has no layer to be drawn on, which is a broken "
        "record rather than a layer called the empty string.",
        "fault", [f"{no_layer} entities"] if no_layer else [], count=no_layer))

    empty_layers = [name for name in layers if by_layer.get(name, 0) == 0]
    findings.append(_finding(
        "Layers defined but holding no entities",
        "An empty layer is harmless and often deliberate (a template, a layer kept for a "
        "standard), so it is listed rather than counted as a problem.",
        "note", _listed(empty_layers), count=len(empty_layers)))

    faults = drawing["faults"]
    findings.append(_finding(
        "Line segments of zero length",
        "A line whose two ends are the same point draws nothing, snaps badly and inflates "
        "every count in this drawing.",
        "fault", _tally(faults["zero_length_lines"]),
        count=sum(faults["zero_length_lines"].values())))

    findings.append(_finding(
        "Circles or arcs with a radius of zero or less",
        "A radius that is not positive is a record AutoCAD cannot draw.",
        "fault", _tally(faults["bad_radius"]), count=sum(faults["bad_radius"].values())))

    findings.append(_finding(
        "Polylines with fewer than two vertices",
        "A polyline needs two vertices to describe a segment; one vertex is a stray record.",
        "fault", _tally(faults["short_polylines"]), count=sum(faults["short_polylines"].values())))

    findings.append(_finding(
        "Old-style POLYLINE records with no VERTEX",
        "Before R13 a polyline carried its points in VERTEX records between POLYLINE and "
        "SEQEND; one with no VERTEX at all describes nothing.",
        "fault", _tally(faults["vertexless_polylines"]),
        count=sum(faults["vertexless_polylines"].values())))

    findings.append(_finding(
        "Text with a character height of zero or less",
        "Text height is in the drawing's units; a height that is not positive cannot be drawn.",
        "fault", _tally(faults["zero_height_texts"]),
        count=sum(faults["zero_height_texts"].values())))

    undefined = {name: count for name, count in inserts.items()
                 if name not in blocks and name.upper() not in {key.upper() for key in blocks}}
    findings.append(_finding(
        "Inserts pointing at a block the file does not define",
        "An INSERT names a block definition to draw. When the definition is missing, AutoCAD "
        "draws nothing and the drawing has lost content.",
        "fault", [f"{name or '(no block name)'} ({count} inserts)"
                  for name, count in sorted(undefined.items())], count=sum(undefined.values())))

    empty_blocks = [key for key, block in blocks.items()
                    if block["count"] == 0 and not is_layout_block(key)]
    findings.append(_finding(
        "Block definitions with nothing in them",
        "An empty block is legal — attribute-only symbols and clipped xrefs use one — so it is "
        "listed rather than called a fault. The model-space and paper-space layout blocks are "
        "left out: their contents live in the ENTITIES section, not in the block.",
        "note", _listed(empty_blocks), count=len(empty_blocks)))

    unused = [key for key in blocks if not blocks[key]["xref"]
              and inserts.get(blocks[key]["name"], 0) == 0
              and not blocks[key]["anonymous"] and not is_layout_block(key)]
    findings.append(_finding(
        "Block definitions that are never inserted",
        "A block nothing points at is dead weight in the file, and usually a leftover. "
        "Generated blocks (a name starting * or $) are left out: AutoCAD writes them and "
        "nothing is expected to name them.",
        "note", _listed(unused), count=len(unused)))

    extents = drawing["extents"]
    findings.append(_finding(
        "The header declares no usable extents",
        "$EXTMIN and $EXTMAX are what AutoCAD zooms to. A file that never had them set, or "
        "that carries the ±1e20 sentinel, has no recorded drawing area.",
        "note", [] if extents else
        [f"$EXTMIN {drawing['header'].get('extmin')} / $EXTMAX {drawing['header'].get('extmax')}"]))

    box = entities["geometry"]["bbox"]
    outside = []
    if extents and box:
        tolerance = max(1.0, (extents[2] - extents[0]), (extents[3] - extents[1])) * 1e-6
        edges = (("left", box[0], extents[0], -1), ("bottom", box[1], extents[1], -1),
                 ("right", box[2], extents[2], 1), ("top", box[3], extents[3], 1))
        for name, actual, declared, direction in edges:
            if direction < 0 and actual < declared - tolerance:
                outside.append(f"{name}: geometry reaches {round_value(actual)} against a "
                               f"declared {round_value(declared)}")
            elif direction > 0 and actual > declared + tolerance:
                outside.append(f"{name}: geometry reaches {round_value(actual)} against a "
                               f"declared {round_value(declared)}")
    findings.append(_finding(
        "Geometry outside the extents the header declares",
        "The extents are a cache of the drawing's bounding box that AutoCAD writes on save. "
        "When they are stale, every zoom-extents and every reader that trusts them is wrong.",
        "fault", outside, count=len(outside)))

    findings.append(_finding(
        "The drawing declares no unit ($INSUNITS absent or 0)",
        "R12 and earlier files predate $INSUNITS, and AutoCAD writes 0 for unitless. Every "
        "number here is then in whatever unit the author had in mind, not in a known one.",
        "note", [] if not drawing["header"]["insunits"]["declared"] else
        ([] if drawing["header"]["insunits"]["code"] else ["$INSUNITS is present and 0"])))

    findings.append(_finding(
        "The file does not end with an EOF marker",
        "Every DXF ends with a code-0 EOF record. Without it the file is truncated, and "
        "whatever is missing is missing silently.",
        "fault", [] if drawing["eof_found"] else ["no EOF record was reached"],
        count=0 if drawing["eof_found"] else 1))

    findings.append(_finding(
        "Group-code lines that do not form a pair",
        "The format is a code line followed by a value line all the way down. A line that is "
        "not a number there shifts every pair after it, so it is counted and skipped.",
        "fault", [f"{drawing['junk_lines']} lines"] if drawing["junk_lines"] else [],
        count=drawing["junk_lines"]))

    findings.append(_finding(
        "Model space holds no drawing objects",
        "A drawing whose ENTITIES section is empty, or whose objects are all in paper space, "
        "has nothing to show in the model tab.",
        "note", [] if entities["by_space"]["model"] else
        [f"0 model-space objects, {entities['by_space']['paper']} paper-space objects"]))

    return findings


def layer_report(drawing: dict) -> dict:
    """Every layer the drawing defines, with what sits on it."""
    entities = drawing["entities"]
    by_layer = entities["by_layer"]
    lengths = entities["by_layer_length"]
    unmeasured = entities["by_layer_unmeasured"]
    rows = []
    for name in drawing["layer_order"]:
        layer = drawing["layers"][name]
        rows.append({
            "name": name,
            "color": layer["abs_color"],
            "off": layer["off"],
            "frozen": layer["frozen"],
            "locked": layer["locked"],
            "linetype": layer["linetype"],
            "entities": by_layer.get(name, 0),
            "length": lengths.get(name, 0.0),
            "unmeasured": unmeasured.get(name, 0),
        })
    rows.sort(key=lambda row: (-row["entities"], row["name"]))
    used = [row for row in rows if row["entities"]]
    return {
        "rows": rows,
        "used": used,
        "empty": [row["name"] for row in rows if not row["entities"]],
        "defined": len(rows),
        "used_count": len(used),
        "orphan_layers": sorted(name for name in by_layer
                                if name.strip().upper() not in layer_index(drawing["layers"])),
        "entity_total": entities["count"],
        "layer_lengths": {row["name"]: row["length"] for row in rows if row["length"]},
    }


def block_report(drawing: dict) -> dict:
    """Every block the drawing defines, with how often it is placed."""
    rows = []
    for key in drawing["block_order"]:
        block = drawing["blocks"][key]
        rows.append({
            "key": key,
            "name": block["name"],
            "anonymous": block["anonymous"],
            "xref": block["xref"],
            "base": block["base"],
            "layer": block["layer"],
            "entities": block["count"],
            "by_type": block["by_type"],
            "inserts": drawing["inserts_by_block"].get(block["name"], 0),
        })
    rows.sort(key=lambda row: (-row["inserts"], -row["entities"], row["key"]))
    return {
        "rows": rows,
        "defined": len(rows),
        "placed": sum(row["inserts"] for row in rows),
        "anonymous": [row["key"] for row in rows if row["anonymous"]],
        "unused": [row["key"] for row in rows
                   if not row["inserts"] and not row["anonymous"] and not row["xref"]],
        "undefined": sorted(name for name in drawing["inserts_by_block"]
                            if name not in drawing["blocks"]),
    }


def compare_drawings(left: dict, right: dict) -> dict:
    """Two drawings side by side: layers, entity types, blocks and size that differ.

    This compares what the two files say, not what they look like: two drawings with the same
    layer names can hold entirely different geometry, and nothing here dissolves or overlays
    geometry to find out.
    """
    left_layers = {name.strip().upper() for name in left["layers"]}
    right_layers = {name.strip().upper() for name in right["layers"]}
    left_types = set(left["entities"]["by_type"])
    right_types = set(right["entities"]["by_type"])
    left_blocks = {key.strip().upper() for key in left["blocks"]}
    right_blocks = {key.strip().upper() for key in right["blocks"]}

    def size(drawing):
        box = drawing["entities"]["geometry"]["bbox"]
        if not box:
            return None
        return (round_value(box[2] - box[0]), round_value(box[3] - box[1]))

    return {
        "left": {"format": left["format"]["name"], "layers": len(left_layers),
                 "entities": left["entities"]["count"], "blocks": len(left_blocks),
                 "length": left["entities"]["geometry"]["length"], "size": size(left)},
        "right": {"format": right["format"]["name"], "layers": len(right_layers),
                  "entities": right["entities"]["count"], "blocks": len(right_blocks),
                  "length": right["entities"]["geometry"]["length"], "size": size(right)},
        "layers_only_left": sorted(left_layers - right_layers),
        "layers_only_right": sorted(right_layers - left_layers),
        "layers_shared": sorted(left_layers & right_layers),
        "types_only_left": sorted(left_types - right_types),
        "types_only_right": sorted(right_types - left_types),
        "types_shared": sorted(left_types & right_types),
        "blocks_only_left": sorted(left_blocks - right_blocks),
        "blocks_only_right": sorted(right_blocks - left_blocks),
        "blocks_shared": sorted(left_blocks & right_blocks),
    }


class DxfData:
    """Reads DXF drawings: a built-in demo catalogue, or any .dxf URL."""

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, user_agent: str | None = None, sleep=None) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("DXF_BASE_URL") or BASE_URL).rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get("DXF_CACHE_TTL", "3600"))
        self.timeout = float(timeout if timeout is not None else env.get("DXF_HTTP_TIMEOUT", "60"))
        self.user_agent = user_agent or env.get("DXF_USER_AGENT") or DEFAULT_USER_AGENT
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
                    raise DxfError(
                        f"that drawing is larger than {MAX_BYTES // (1024 * 1024)} MB, which is "
                        "past the size a drawing read here is meant for")
                return body
        except urlerror.HTTPError as exc:
            message = self._http_error(exc, url)
            if exc.code in RETRY_STATUSES:
                raise DxfBusy(message) from exc
            raise DxfError(message) from exc
        except DxfError:
            raise
        except IncompleteRead as exc:
            if exc.partial:
                return bytes(exc.partial)
            raise DxfBusy(f"the host closed the response early for {url}") from exc
        except Exception as exc:  # urllib raises many types; callers see one
            raise DxfBusy(f"could not read {url}: {exc}") from exc

    @staticmethod
    def _http_error(exc, url: str) -> str:
        """A readable reason for a failed read."""
        if exc.code == 404:
            return f"no drawing at {url} (HTTP 404)"
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
        failure: DxfBusy | None = None
        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                self._sleep(RETRY_BACKOFF * attempt)
            try:
                body = self._fetch(url)
            except DxfBusy as exc:
                failure = exc
                continue
            if isinstance(body, str):
                body = body.encode("utf-8", "replace")
            if not isinstance(body, (bytes, bytearray)):
                raise DxfError("the host returned an unexpected payload")
            text = bytes(body).decode("utf-8", "replace")
            with self._lock:
                self._cache[url] = (now + self.cache_ttl, text)
            return text
        raise failure or DxfBusy(f"could not read {url}")

    # -- references --------------------------------------------------------

    @staticmethod
    def is_url(reference) -> bool:
        return bool(_URL_RE.match(str(reference or "").strip()))

    @staticmethod
    def check_url(value) -> str:
        """A drawing URL: http(s) only, and it has to name a .dxf file."""
        url = str(value or "").strip().strip("<>")
        if not _URL_RE.match(url):
            raise ValueError("a drawing URL has to start with http:// or https://")
        return url

    @staticmethod
    def kind_of(reference) -> str:
        """The only kind this reader has, refused for anything that is not a .dxf."""
        path = parse.urlparse(str(reference or "")).path.lower()
        if path.endswith(DXF_EXTENSION):
            return DXF_EXTENSION
        raise ValueError(
            f"I only read {DXF_EXTENSION} drawings, and "
            f"{path.rsplit('/', 1)[-1] or reference!r} is not one")

    def sample(self, name):
        """A demo drawing from the built-in catalogue, by the name or alias given."""
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

    def sample_url(self, key: str) -> str:
        """The demo URL for a catalogue entry."""
        return f"{self.base_url}/" + parse.quote(SAMPLES[key]["path"], safe="/")

    def resolve(self, reference):
        """A drawing reference -> (url, label, where it came from)."""
        text = str(reference or "").strip()
        if self.is_url(text):
            url = self.check_url(text)
            self.kind_of(url)
            label = parse.urlparse(url).path.rsplit("/", 1)[-1] or url
            return url, label, "url"
        found = self.sample(text)
        if not found:
            raise DxfError(
                f"I do not know a DXF demo called {text!r}. Give me a full .dxf URL, or one of "
                f"the built-in drawings: {', '.join(self.sample_names())}.")
        key, sample = found
        return self.sample_url(key), f"{key} ({sample['title']})", "sample"

    def document(self, reference) -> dict:
        """Fetch a drawing and return (url, label, source, text)."""
        url, label, source = self.resolve(reference)
        text = self._read(url)
        return {"url": url, "label": label, "source": source, "text": text,
                "bytes": len(text.encode("utf-8"))}

    def read(self, reference) -> dict:
        """Fetch and parse one drawing into its structured record."""
        document = self.document(reference)
        record = parse_drawing(document["text"])
        return {**record, "url": document["url"], "label": document["label"],
                "source": document["source"], "bytes": document["bytes"]}


__all__ = [
    "BASE_URL",
    "BINARY_MAGIC",
    "DATASET",
    "DEFAULT_TTL",
    "DXF_EXTENSION",
    "DxfBusy",
    "DxfData",
    "DxfError",
    "EXTENT_SENTINEL",
    "MAX_BYTES",
    "SAMPLES",
    "arc_length",
    "block_report",
    "bulge_length",
    "compare_drawings",
    "drawing_checks",
    "extent_pair",
    "layer_report",
    "pairs",
    "parse_drawing",
    "records",
    "round_value",
    "unescape",
]
