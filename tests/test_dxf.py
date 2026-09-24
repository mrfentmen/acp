"""Tests for the dxf ACP agent: the group-pair reader, checks, routing, permissions.

    python3 tests/test_dxf.py

No network: the reader runs against an injected drawing and the agent against a FakeData. The
fixture is a hand-written DXF drawing carrying one of every problem the checks look for, checked
against the live DXF files in the ezdxf repository on 2026-09-24.
"""

from __future__ import annotations

import math
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DXF_DIR = REPO_ROOT / "agents" / "dxf"
for path in (str(REPO_ROOT), str(DXF_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import DxfAgent, route, sample_from_text, samples_from_text, urls_from_text  # noqa: E402
from data import (  # noqa: E402
    DXF_EXTENSION,
    EXAMPLE_LIMIT,
    MAX_BYTES,
    SAMPLES,
    DxfBusy,
    DxfData,
    DxfError,
    arc_length,
    block_report,
    bulge_length,
    compare_drawings,
    drawing_checks,
    extent_pair,
    is_layout_block,
    layer_report,
    pairs,
    parse_drawing,
    records,
    round_value,
    text_of,
    unescape,
)

#: A drawing written the way AutoCAD writes one — tables, blocks, every object type — carrying
#: one of every problem the checks look for.
DRAWING_TEXT = """0
SECTION
2
HEADER
9
$ACADVER
1
AC1032
9
$DWGCODEPAGE
3
ANSI_1252
9
$INSUNITS
70
4
9
$MEASUREMENT
70
1
9
$EXTMIN
10
0.0
20
0.0
30
0.0
9
$EXTMAX
10
100.0
20
50.0
30
0.0
9
$CLAYER
8
WALLS
9
$CECOLOR
62
256
9
$LTSCALE
40
1.0
0
ENDSEC
0
SECTION
2
TABLES
0
TABLE
2
LAYER
70
5
0
LAYER
5
10
2
WALLS
70
0
62
7
6
Continuous
0
LAYER
5
11
2
DIMS
70
1
62
-3
6
DASHED
0
LAYER
5
12
2
walls
70
0
62
1
6
Continuous
0
LAYER
5
13
2
DEFPOINTS
70
0
62
8
6
Continuous
0
LAYER
5
14
2
EMPTY
70
0
62
9
6
Continuous
0
ENDTAB
0
TABLE
2
LTYPE
70
1
0
LTYPE
5
20
2
Continuous
70
0
3
Solid line
72
65
73
0
40
0.0
0
ENDTAB
0
ENDSEC
0
SECTION
2
BLOCKS
0
BLOCK
5
30
2
DETAIL
70
0
10
5.0
20
5.0
30
0.0
8
0
0
LINE
5
31
8
WALLS
10
0.0
20
0.0
11
10.0
21
0.0
0
ENDBLK
5
32
0
BLOCK
5
33
2
HOLDBLOCK
70
0
10
0.0
20
0.0
30
0.0
8
0
0
ENDBLK
5
34
0
BLOCK
5
35
2
*Model_Space
70
1
10
0.0
20
0.0
30
0.0
8
0
0
ENDBLK
5
36
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
5
40
8
WALLS
10
1.0
20
1.0
11
1.0
21
1.0
0
LINE
5
41
8
WALLS
10
0.0
20
0.0
11
20.0
21
0.0
0
LINE
5
42
8
GHOST
10
30.0
20
30.0
11
35.0
21
30.0
0
LINE
5
43
10
40.0
20
40.0
11
45.0
21
40.0
0
LWPOLYLINE
5
44
8
WALLS
90
3
70
1
10
5.0
20
5.0
10
15.0
20
5.0
42
1.0
10
15.0
20
15.0
0
LWPOLYLINE
5
45
8
WALLS
90
1
70
0
10
25.0
20
25.0
0
CIRCLE
5
46
8
WALLS
10
20.0
20
20.0
40
0.0
0
CIRCLE
5
47
8
WALLS
10
50.0
20
20.0
40
2.0
0
ARC
5
48
8
WALLS
10
70.0
20
20.0
40
5.0
50
0.0
51
90.0
0
ARC
5
49
8
WALLS
10
70.0
20
35.0
40
-2.0
50
0.0
51
180.0
0
POLYLINE
5
50
8
WALLS
66
1
70
0
0
VERTEX
5
51
8
WALLS
10
80.0
20
10.0
0
VERTEX
5
52
8
WALLS
10
90.0
20
10.0
42
0.5
0
VERTEX
5
53
8
WALLS
10
90.0
20
20.0
0
SEQEND
5
54
0
POLYLINE
5
55
8
WALLS
66
1
70
0
0
SEQEND
5
56
0
INSERT
5
57
8
WALLS
2
DETAIL
10
10.0
20
40.0
41
1.0
42
1.0
43
1.0
50
0.0
0
INSERT
5
58
8
WALLS
2
NOTHERE
10
30.0
20
45.0
41
1.0
42
1.0
43
1.0
0
TEXT
5
59
8
WALLS
10
5.0
20
30.0
40
2.5
1
wall thickness 240
0
TEXT
5
60
8
WALLS
10
7.0
20
32.0
40
0.0
1
zero height label
0
MTEXT
5
61
8
WALLS
10
5.0
20
35.0
40
2.5
1
general note
0
MTEXT
5
62
8
WALLS
10
5.0
20
38.0
40
0.0
1
another zero height note
0
HATCH
5
63
8
WALLS
10
0.0
20
0.0
30
0.0
2
SOLID
70
1
71
1
91
1
92
7
72
0
73
1
93
4
10
5.0
20
5.0
10
15.0
20
5.0
10
15.0
20
15.0
10
5.0
20
15.0
97
0
75
1
76
1
0
POINT
5
64
8
DIMS
10
60.0
20
40.0
0
POINT
5
65
8
DEFPOINTS
10
62.0
20
42.0
0
3DFACE
5
66
8
WALLS
10
0.0
20
45.0
11
5.0
21
45.0
12
5.0
22
50.0
13
0.0
23
50.0
0
ENDSEC
0
EOF
"""

#: The same drawing with a header whose extents no longer cover the geometry.
STALE_EXTENT_TEXT = DRAWING_TEXT.replace("100.0\n20\n50.0\n30\n0.0", "60.0\n20\n30.0\n30\n0.0")

#: A small second drawing, for the comparison skill.
DRAWING_B_TEXT = """0
SECTION
2
HEADER
9
$ACADVER
1
AC1009
9
$EXTMIN
10
0.0
20
0.0
9
$EXTMAX
10
10.0
20
10.0
0
ENDSEC
0
SECTION
2
TABLES
0
TABLE
2
LAYER
70
1
0
LAYER
5
10
2
STEEL
70
0
62
5
6
Continuous
0
ENDTAB
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
5
20
8
STEEL
10
0.0
20
0.0
11
10.0
21
0.0
0
CIRCLE
5
21
8
STEEL
10
5.0
20
5.0
40
3.0
0
ENDSEC
0
EOF
"""

#: The header the ezdxf fixtures write when a drawing has never been zoomed to.
SENTINEL_TEXT = """0
SECTION
2
HEADER
9
$ACADVER
1
AC1009
9
$EXTMIN
10
1e+20
20
1e+20
30
0.0
9
$EXTMAX
10
-1e+20
20
-1e+20
30
0.0
0
ENDSEC
0
SECTION
2
ENTITIES
0
ENDSEC
0
EOF
"""

#: Garbage after the EOF marker, the way one real fixture ships it.
TRASHED_TEXT = DRAWING_TEXT + "\nthis is not a group pair\nneither is this\n"

FIXTURES = {
    "https://fixture.test/drawing.dxf": DRAWING_TEXT,
    "https://fixture.test/stale.dxf": STALE_EXTENT_TEXT,
    "https://fixture.test/b.dxf": DRAWING_B_TEXT,
    # The demo catalogue resolves to the repository layout, so the sample tests can read one.
    "https://fixture.test/examples/addons/drawing/data/usa.dxf": DRAWING_TEXT,
    "https://fixture.test/tests/test_01_dxf_entities/"
    "houses_of_parliament_georeferenced.dxf": DRAWING_TEXT,
}


def fetch_exact(mapping, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if url not in mapping:
            raise DxfError(f"no fixture at {url}")
        payload = mapping[url]
        return payload() if callable(payload) else (payload.encode("utf-8")
                                                    if isinstance(payload, str) else payload)

    return fetch


def check_by_name(findings, name):
    for finding in findings:
        if finding["check"] == name:
            return finding
    raise AssertionError(f"no check named {name!r} in {[f['check'] for f in findings]}")


class PairTests(unittest.TestCase):
    def test_padded_group_codes_are_read_and_values_are_not_trimmed(self):
        reading, junk, eof = pairs("  0\nLINE\n   8\nWALLS \n")
        self.assertEqual(reading, [(0, "LINE"), (8, "WALLS ")])
        self.assertEqual(junk, 0)
        self.assertFalse(eof)

    def test_crlf_and_lone_cr_are_normalised(self):
        self.assertEqual(pairs("0\r\nLINE\r\n8\r\nWALLS\r\n")[0],
                         [(0, "LINE"), (8, "WALLS")])
        self.assertEqual(pairs("0\rLINE\r8\rWALLS\r")[0], [(0, "LINE"), (8, "WALLS")])

    def test_an_empty_value_is_a_real_value(self):
        reading, _junk, _eof = pairs("1000\n\n1001\nEZDXF\n")
        self.assertEqual(reading, [(1000, ""), (1001, "EZDXF")])

    def test_reading_stops_at_the_eof_record(self):
        reading, junk, eof = pairs("0\nSECTION\n0\nENDSEC\n0\nEOF\nrubbish\nmore rubbish\n")
        self.assertTrue(eof)
        self.assertEqual(junk, 0)
        self.assertEqual(reading[-1], (0, "EOF"))

    def test_lines_that_are_not_group_codes_are_counted_not_guessed(self):
        reading, junk, eof = pairs("0\nLINE\nnot a code\n8\nWALLS\n0\nEOF\n")
        self.assertEqual(junk, 1)
        self.assertTrue(eof)
        self.assertIn((8, "WALLS"), reading)

    def test_a_file_with_no_eof_is_reported_rather_than_assumed_complete(self):
        _reading, _junk, eof = pairs("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n")
        self.assertFalse(eof)

    def test_records_start_at_every_code_zero_and_nine(self):
        reading = records(pairs("0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1032\n0\nENDSEC\n")[0])
        self.assertEqual([name for name, _fields in reading], ["SECTION", "$ACADVER", "ENDSEC"])
        # The section name is a field of the SECTION record, and a code 9 opens its own record.
        self.assertEqual(reading[0][1], [(2, "HEADER")])
        self.assertEqual(reading[1][1], [(1, "AC1032")])


class HelperTests(unittest.TestCase):
    def test_long_mtext_joins_its_chunks_in_file_order(self):
        self.assertEqual(text_of([(3, "first half "), (3, "and "), (1, "the end")]),
                         "first half and the end")
        self.assertEqual(text_of([(1, "single")]), "single")

    def test_mtext_formatting_codes_are_stripped(self):
        self.assertEqual(unescape(r"{\fArial|b1;Title}\Pnext line"), "Title next line")

    def test_an_arc_length_wraps_the_autocad_way(self):
        self.assertAlmostEqual(arc_length(10.0, 350.0, 10.0), 10.0 * math.radians(20.0), places=6)
        self.assertAlmostEqual(arc_length(10.0, 0.0, 90.0), 10.0 * math.radians(90.0), places=6)
        self.assertAlmostEqual(arc_length(4.0, 175.0, 175.0), 2 * math.pi * 4.0, places=6)

    def test_a_bulge_is_an_arc_length(self):
        self.assertAlmostEqual(bulge_length(10.0, 1.0), math.pi * 5.0, places=6)
        self.assertEqual(bulge_length(10.0, 0.0), 0.0)
        self.assertEqual(bulge_length(0.0, 1.0), 0.0)

    def test_extent_sentinels_and_reversed_boxes_are_not_sizes(self):
        header = parse_drawing(SENTINEL_TEXT)["header"]
        self.assertIsNone(extent_pair(header, "extmin", "extmax"))
        reversed_pair = {"extmin": (10.0, 10.0), "extmax": (0.0, 0.0)}
        self.assertIsNone(extent_pair(reversed_pair, "extmin", "extmax"))
        missing = {"extmin": (0.0, 0.0), "extmax": None}
        self.assertIsNone(extent_pair(missing, "extmin", "extmax"))

    def test_layout_blocks_are_recognised(self):
        for name in ("*Model_Space", "$Paper_Space", "*Paper_Space0"):
            self.assertTrue(is_layout_block(name), name)
        self.assertFalse(is_layout_block("DETAIL"))

    def test_round_value_never_returns_negative_zero(self):
        self.assertEqual(round_value(-0.0001), 0.0)
        self.assertEqual(round_value(1.239), 1.24)


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drawing = parse_drawing(DRAWING_TEXT)

    def test_the_format_header_and_units_are_read(self):
        self.assertEqual(self.drawing["format"]["code"], "AC1032")
        self.assertEqual(self.drawing["format"]["name"], "2018")
        self.assertEqual(self.drawing["format"]["code_page"], "ANSI_1252")
        self.assertTrue(self.drawing["header"]["insunits"]["declared"])
        self.assertEqual(self.drawing["header"]["insunits"]["code"], 4)
        self.assertEqual(self.drawing["header"]["insunits"]["name"], "millimetres")
        self.assertEqual(self.drawing["header"]["measurement"], 1)
        self.assertEqual(self.drawing["header"]["clayer"], "WALLS")

    def test_the_sections_are_read_in_file_order(self):
        self.assertEqual(self.drawing["sections"], ["HEADER", "TABLES", "BLOCKS", "ENTITIES"])
        self.assertTrue(self.drawing["eof_found"])
        self.assertEqual(self.drawing["junk_lines"], 0)

    def test_the_layer_table_carries_colour_linetype_and_flags(self):
        walls = self.drawing["layers"]["WALLS"]
        self.assertEqual(walls["color"], 7)
        self.assertFalse(walls["off"])
        self.assertFalse(walls["frozen"])
        self.assertEqual(walls["linetype"], "Continuous")
        dims = self.drawing["layers"]["DIMS"]
        self.assertTrue(dims["off"])
        self.assertTrue(dims["frozen"])
        self.assertEqual(dims["abs_color"], 3)
        self.assertEqual(dims["linetype"], "DASHED")
        self.assertEqual(self.drawing["tables"]["LAYER"], 5)
        self.assertEqual(self.drawing["tables"]["LTYPE"], 1)

    def test_blocks_are_read_with_their_base_point_and_contents(self):
        detail = self.drawing["blocks"]["DETAIL"]
        self.assertEqual(detail["base"], (5.0, 5.0, 0.0))
        self.assertEqual(detail["count"], 1)
        self.assertEqual(detail["by_type"], {"LINE": 1})
        self.assertFalse(detail["anonymous"])
        hold = self.drawing["blocks"]["HOLDBLOCK"]
        self.assertEqual(hold["count"], 0)

    def test_the_layout_block_is_generated_and_keeps_its_name(self):
        model = self.drawing["blocks"]["*Model_Space"]
        self.assertTrue(model["anonymous"])
        self.assertEqual(model["name"], "*Model_Space")

    def test_the_entities_are_counted_by_type_and_space(self):
        entities = self.drawing["entities"]
        self.assertEqual(entities["by_type"]["LINE"], 4)
        self.assertEqual(entities["by_type"]["LWPOLYLINE"], 2)
        self.assertEqual(entities["by_type"]["CIRCLE"], 2)
        self.assertEqual(entities["by_type"]["ARC"], 2)
        self.assertEqual(entities["by_type"]["POLYLINE"], 2)
        self.assertEqual(entities["by_type"]["INSERT"], 2)
        self.assertEqual(entities["by_type"]["TEXT"], 2)
        self.assertEqual(entities["by_type"]["MTEXT"], 2)
        self.assertEqual(entities["by_type"]["HATCH"], 1)
        self.assertEqual(entities["by_type"]["POINT"], 2)
        self.assertEqual(entities["by_type"]["3DFACE"], 1)
        self.assertNotIn("VERTEX", entities["by_type"])
        self.assertNotIn("SEQEND", entities["by_type"])
        self.assertEqual(entities["vertices"], 3)
        self.assertEqual(entities["by_space"], {"model": entities["count"], "paper": 0})

    def test_the_drawn_length_adds_lines_arcs_and_bulges_up(self):
        geometry = self.drawing["entities"]["geometry"]
        # 20 (normal line) + 35-30 (GHOST) + 5 (no-layer line) + closed polyline 10 + arc
        # 10 + 5 (half circle on the bulge) + quarter arc pi/2*5 + the old-style polyline
        # 10 + 5 (its bulge) + circle circumference 4 pi.
        # Two straight lines and one with no layer, then the closed LWPOLYLINE (bulged first
        # segment, straight second, hypotenuse closing it), the normal circle, the quarter arc,
        # the negative-radius arc, and the old-style polyline whose second vertex carries a
        # bulge. The zero-length line, the one-vertex polyline, the vertexless polyline and the
        # zero-radius circle contribute nothing.
        expected = (20.0 + 5.0 + 5.0
                    + bulge_length(10.0, 1.0) + 10.0 + math.hypot(10.0, 10.0)
                    + 2.0 * math.pi * 2.0 + arc_length(5.0, 0.0, 90.0)
                    + arc_length(-2.0, 0.0, 180.0)
                    + 10.0 + bulge_length(10.0, 0.5))
        self.assertAlmostEqual(geometry["length"], expected, places=6)
        self.assertEqual(geometry["measured"], 12)
        self.assertEqual(geometry["unmeasured"], {"HATCH": 1, "3DFACE": 1})
        self.assertGreater(geometry["positioned"], geometry["measured"])

    def test_the_bounding_box_covers_the_geometry(self):
        box = self.drawing["entities"]["geometry"]["bbox"]
        self.assertEqual(box, (0.0, 0.0, 90.0, 50.0))

    def test_the_declared_extents_are_read(self):
        self.assertEqual(self.drawing["extents"], (0.0, 0.0, 100.0, 50.0))
        self.assertIsNone(parse_drawing(SENTINEL_TEXT)["extents"])

    def test_inserts_keep_their_block_name_point_scale_and_rotation(self):
        inserts = self.drawing["entities"]["inserts"]
        self.assertEqual(inserts[0]["block"], "DETAIL")
        self.assertEqual(inserts[0]["at"], (10.0, 40.0))
        self.assertEqual(inserts[0]["scale"], (1.0, 1.0, 1.0))
        self.assertEqual(self.drawing["inserts_by_block"], {"DETAIL": 1, "NOTHERE": 1})

    def test_text_is_collected_with_its_layer_and_height(self):
        texts = self.drawing["entities"]["texts"]
        self.assertEqual(self.drawing["entities"]["text_total"], 4)
        self.assertEqual(texts[0]["value"], "wall thickness 240")
        self.assertEqual(texts[0]["height"], 2.5)
        self.assertEqual(texts[0]["layer"], "WALLS")

    def test_a_drawing_with_no_header_or_tables_still_parses(self):
        empty = parse_drawing("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
        self.assertEqual(empty["format"]["code"], "")
        self.assertIsNone(empty["extents"])
        self.assertFalse(empty["header"]["insunits"]["declared"])
        self.assertEqual(empty["entities"]["count"], 0)

    def test_garbage_after_the_eof_marker_is_ignored_not_parsed(self):
        trashed = parse_drawing(TRASHED_TEXT)
        self.assertTrue(trashed["eof_found"])
        self.assertEqual(trashed["junk_lines"], 0)
        self.assertEqual(trashed["entities"]["count"], self.drawing["entities"]["count"])

    def test_faults_are_collected_with_the_layer_they_sit_on(self):
        faults = self.drawing["faults"]
        self.assertEqual(sum(faults["zero_length_lines"].values()), 1)
        self.assertEqual(sum(faults["bad_radius"].values()), 2)
        self.assertEqual(sum(faults["short_polylines"].values()), 1)
        self.assertEqual(sum(faults["vertexless_polylines"].values()), 1)
        self.assertEqual(sum(faults["zero_height_texts"].values()), 2)

    def test_a_binary_dxf_is_refused_with_what_to_do(self):
        with self.assertRaises(DxfError) as caught:
            parse_drawing("AutoCAD Binary DXF\r\n\x1a\x00\ngarbage")
        self.assertIn("binary DXF", str(caught.exception))
        self.assertIn("ASCII", str(caught.exception))

    def test_a_file_with_no_sections_is_refused_as_not_a_drawing(self):
        with self.assertRaises(DxfError) as caught:
            parse_drawing("0\nLINE\n8\nWALLS\n0\nEOF\n")
        self.assertIn("not a DXF drawing", str(caught.exception))

    def test_a_file_that_is_not_group_pairs_at_all_is_refused(self):
        with self.assertRaises(DxfError) as caught:
            parse_drawing("hello\nworld\n")
        self.assertIn("no DXF group pairs", str(caught.exception))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(DxfError):
            parse_drawing("")


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drawing = parse_drawing(DRAWING_TEXT)
        cls.findings = drawing_checks(cls.drawing)
        cls.faults = {finding["check"]: finding for finding in cls.findings
                      if finding["severity"] == "fault"}
        cls.notes = {finding["check"]: finding for finding in cls.findings
                     if finding["severity"] == "note"}

    def test_the_parts_of_the_fixture_each_show_up(self):
        self.assertEqual(self.faults["Line segments of zero length"]["count"], 1)
        self.assertEqual(self.faults["Circles or arcs with a radius of zero or less"]["count"], 2)
        self.assertEqual(self.faults["Polylines with fewer than two vertices"]["count"], 1)
        self.assertEqual(self.faults["Old-style POLYLINE records with no VERTEX"]["count"], 1)
        self.assertEqual(self.faults["Text with a character height of zero or less"]["count"], 2)
        self.assertEqual(self.faults["Inserts pointing at a block the file does not define"]["count"], 1)
        self.assertIn("NOTHERE", " ".join(
            self.faults["Inserts pointing at a block the file does not define"]["items"]))

    def test_a_layer_no_table_defines_is_a_fault(self):
        finding = self.faults["Entities on a layer the drawing never defines"]
        self.assertEqual(finding["count"], 1)
        self.assertIn("GHOST", finding["items"][0])

    def test_dimension_layer_geometry_and_switched_off_or_frozen_layers_are_faults(self):
        self.assertEqual(self.faults["Geometry on the DEFPOINTS layer (AutoCAD never plots it)"]["count"], 1)
        self.assertEqual(self.faults["Entities on layers switched off"]["count"], 1)
        self.assertEqual(self.faults["Entities on frozen layers"]["count"], 1)
        self.assertIn("DIMS", self.faults["Entities on layers switched off"]["items"][0])

    def test_layer_names_differing_only_by_case_are_a_fault(self):
        finding = self.faults["Layer names that differ only by case"]
        self.assertEqual(finding["count"], 2)
        self.assertIn("WALLS", finding["items"][0])

    def test_an_entity_with_no_layer_name_is_its_own_fault(self):
        finding = self.faults["Entities with no layer name at all"]
        self.assertEqual(finding["count"], 1)
        self.assertNotIn("(no layer name)",
                         " ".join(self.faults["Entities on a layer the drawing never defines"]["items"]))

    def test_a_clean_drawing_passes_the_extent_check(self):
        self.assertEqual(self.faults["Geometry outside the extents the header declares"]["count"], 0)

    def test_stale_extents_are_a_fault_naming_the_edge(self):
        stale = parse_drawing(STALE_EXTENT_TEXT)
        finding = check_by_name(drawing_checks(stale),
                                "Geometry outside the extents the header declares")
        self.assertGreater(finding["count"], 0)
        self.assertIn("declared 60", " ".join(finding["items"]))

    def test_empty_layers_blocks_and_unused_blocks_are_notes_not_faults(self):
        self.assertIn("EMPTY", self.notes["Layers defined but holding no entities"]["items"])
        self.assertIn("HOLDBLOCK", self.notes["Block definitions with nothing in them"]["items"])
        self.assertIn("HOLDBLOCK", self.notes["Block definitions that are never inserted"]["items"])
        self.assertNotIn("*Model_Space", self.notes["Block definitions with nothing in them"]["items"])
        self.assertNotIn("*Model_Space", self.notes["Block definitions that are never inserted"]["items"])

    def test_a_declared_unit_is_not_a_note_but_an_absent_one_is(self):
        self.assertEqual(self.notes["The drawing declares no unit ($INSUNITS absent or 0)"]["count"], 0)
        r12 = parse_drawing("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
        finding = check_by_name(drawing_checks(r12),
                                "The drawing declares no unit ($INSUNITS absent or 0)")
        self.assertEqual(finding["severity"], "note")

    def test_a_missing_eof_marker_is_a_fault(self):
        truncated = DRAWING_TEXT.replace("0\nEOF\n", "")
        finding = check_by_name(drawing_checks(parse_drawing(truncated)),
                                "The file does not end with an EOF marker")
        self.assertEqual(finding["count"], 1)

    def test_group_code_lines_that_are_not_pairs_are_a_fault(self):
        broken = DRAWING_TEXT.replace("10\n20.0\n20\n20.0", "10\n20.0\nbroken line\n20\n20.0")
        finding = check_by_name(drawing_checks(parse_drawing(broken)),
                                "Group-code lines that do not form a pair")
        self.assertEqual(finding["count"], 1)

    def test_an_empty_model_space_is_a_note(self):
        finding = check_by_name(drawing_checks(parse_drawing(SENTINEL_TEXT)),
                                "Model space holds no drawing objects")
        self.assertEqual(finding["severity"], "note")
        self.assertEqual(finding["count"], 1)

    def test_every_check_is_named_and_severity_is_always_one_of_two_words(self):
        for finding in self.findings:
            self.assertTrue(finding["check"])
            self.assertTrue(finding["detail"])
            self.assertIn(finding["severity"], ("fault", "note"))
            self.assertIsInstance(finding["count"], int)
            self.assertIsInstance(finding["items"], list)

    def test_example_lists_are_collapsed_rather_than_cut_off(self):
        layers = "".join(f"0\nLAYER\n5\n{10 + index}\n2\nEMPTY{index}\n70\n0\n62\n7\n"
                         "6\nContinuous\n" for index in range(8))
        text = ("0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLAYER\n70\n8\n" + layers
                + "0\nENDTAB\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
        finding = check_by_name(drawing_checks(parse_drawing(text)),
                                "Layers defined but holding no entities")
        self.assertEqual(finding["count"], 8)
        self.assertEqual(len(finding["items"]), EXAMPLE_LIMIT + 1)
        self.assertTrue(finding["items"][-1].endswith("3 more"))


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drawing = parse_drawing(DRAWING_TEXT)

    def test_layers_are_reported_in_order_of_use(self):
        report = layer_report(self.drawing)
        self.assertEqual(report["defined"], 5)
        self.assertEqual(report["rows"][0]["name"], "WALLS")
        self.assertEqual(report["rows"][0]["entities"], 18)
        self.assertEqual(report["empty"], ["EMPTY", "walls"])
        self.assertGreater(report["rows"][0]["length"], 0)
        # GHOST is drawn on and the file never defines it; the empty name is one too.
        self.assertEqual(report["orphan_layers"], ["", "GHOST"])

    def test_a_layer_reported_off_keeps_its_colour_number(self):
        report = layer_report(self.drawing)
        dims = next(row for row in report["rows"] if row["name"] == "DIMS")
        self.assertTrue(dims["off"])
        self.assertTrue(dims["frozen"])
        self.assertEqual(dims["color"], 3)

    def test_blocks_are_reported_with_their_insert_counts(self):
        report = block_report(self.drawing)
        self.assertEqual(report["defined"], 3)
        self.assertEqual(report["rows"][0]["key"], "DETAIL")
        self.assertEqual(report["rows"][0]["inserts"], 1)
        self.assertEqual(report["placed"], 1)
        self.assertIn("NOTHERE", report["undefined"])
        self.assertIn("HOLDBLOCK", report["unused"])

    def test_two_drawings_compare_on_layers_types_blocks_and_totals(self):
        left = parse_drawing(DRAWING_TEXT)
        right = parse_drawing(DRAWING_B_TEXT)
        comparison = compare_drawings(left, right)
        self.assertEqual(comparison["left"]["format"], "2018")
        self.assertEqual(comparison["right"]["format"], "R11/R12")
        self.assertEqual(comparison["layers_only_right"], ["STEEL"])
        self.assertIn("WALLS", comparison["layers_only_left"])
        self.assertIn("HATCH", comparison["types_only_left"])
        self.assertIn("CIRCLE", comparison["types_shared"])
        self.assertEqual(comparison["blocks_only_left"], ["*MODEL_SPACE", "DETAIL", "HOLDBLOCK"])
        self.assertEqual(comparison["blocks_only_right"], [])
        self.assertGreater(comparison["left"]["entities"], comparison["right"]["entities"])


class ReaderTests(unittest.TestCase):
    def reader(self, mapping=None, calls=None, **kwargs):
        return DxfData(fetch=fetch_exact(mapping or FIXTURES, calls), base_url="https://fixture.test",
                       **kwargs)

    def test_a_sample_resolves_to_a_demo_url(self):
        calls: list[str] = []
        reader = self.reader(calls=calls)
        record = reader.read("usa")
        self.assertEqual(record["source"], "sample")
        self.assertIn("examples/addons/drawing/data/usa.dxf", record["url"])
        self.assertEqual(len(calls), 1)

    def test_sample_names_are_matched_loosely_and_longest_first(self):
        reader = DxfData(base_url="https://fixture.test")
        cases = {"usa": "usa", "the USA map": "usa", "houses of parliament": "houses-of-parliament",
                 "Leica Disto": "leica", "uncommon": "uncommon", "minimal r10": "minimal-r10"}
        for given, expected in cases.items():
            found = reader.sample(given)
            self.assertIsNotNone(found, given)
            self.assertEqual(found[0], expected)
        self.assertIsNone(reader.sample("gotham"))
        self.assertIsNone(reader.sample("ab"))

    def test_a_drawing_url_is_used_as_given(self):
        reader = self.reader(mapping={"https://elsewhere.test/plan.dxf": DRAWING_TEXT})
        record = reader.read("https://elsewhere.test/plan.dxf")
        self.assertEqual(record["source"], "url")
        self.assertEqual(record["bytes"], len(DRAWING_TEXT.encode("utf-8")))
        self.assertEqual(record["url"], "https://elsewhere.test/plan.dxf")

    def test_urls_must_be_http_and_a_dxf(self):
        reader = DxfData(base_url="https://fixture.test")
        with self.assertRaises(ValueError):
            reader.check_url("ftp://x/plan.dxf")
        with self.assertRaises(ValueError) as caught:
            reader.kind_of("https://fixture.test/plan.dwg")
        self.assertIn(".dxf", str(caught.exception))
        with self.assertRaises(ValueError):
            reader.kind_of("https://fixture.test/board.kicad_pcb")

    def test_an_uppercase_extension_is_a_dxf(self):
        reader = DxfData(base_url="https://fixture.test")
        self.assertEqual(reader.kind_of("https://x.test/POLI-ALL210_12.DXF"), DXF_EXTENSION)

    def test_an_unknown_demo_name_is_refused_with_the_list(self):
        reader = DxfData(base_url="https://fixture.test", fetch=fetch_exact({}))
        with self.assertRaises(DxfError) as caught:
            reader.read("gotham")
        self.assertIn("gotham", str(caught.exception))
        self.assertIn("usa", str(caught.exception))

    def test_a_drawing_is_cached_and_read_once(self):
        calls: list[str] = []
        reader = DxfData(fetch=fetch_exact(FIXTURES, calls), base_url="https://fixture.test")
        reader.read("usa")
        reader.read("usa")
        self.assertEqual(len(calls), 1)

    def test_a_busy_host_is_retried_then_succeeds(self):
        attempts: list[str] = []

        def flaky(url):
            attempts.append(url)
            if len(attempts) == 1:
                raise DxfBusy("the host is busy")
            return DRAWING_TEXT.encode("utf-8")

        reader = DxfData(fetch=flaky, base_url="https://fixture.test", sleep=lambda _seconds: None)
        record = reader.read("https://fixture.test/drawing.dxf")
        self.assertEqual(record["entities"]["count"], 22)
        self.assertEqual(len(attempts), 2)

    def test_a_host_that_stays_busy_fails_after_the_retries(self):
        attempts: list[str] = []

        def always(url):
            attempts.append(url)
            raise DxfBusy("the host is busy")

        reader = DxfData(fetch=always, base_url="https://fixture.test", sleep=lambda _seconds: None)
        with self.assertRaises(DxfBusy):
            reader.read("https://fixture.test/drawing.dxf")
        self.assertEqual(len(attempts), 3)

    def test_a_bad_payload_is_refused_rather_than_decoded(self):
        reader = DxfData(fetch=lambda url: 1234, base_url="https://fixture.test")
        with self.assertRaises(DxfError):
            reader.read("https://fixture.test/drawing.dxf")

    def test_the_size_cap_is_named_in_megabytes(self):
        self.assertEqual(MAX_BYTES // (1024 * 1024), 12)

    def test_every_demo_in_the_catalogue_has_a_path_and_a_title(self):
        for key, sample in SAMPLES.items():
            self.assertTrue(sample["path"].lower().endswith(".dxf"), key)
            self.assertTrue(sample["title"], key)
            self.assertTrue(sample["aliases"], key)


class RouteTests(unittest.TestCase):
    def test_help_words_route_to_help(self):
        for text in ("help", "what can you do?", "", "commands?"):
            self.assertEqual(route(text)[0], "help")

    def test_a_plain_question_is_a_summary(self):
        skill, params = route("what is in the usa drawing?")
        self.assertEqual(skill, "drawing-summary")
        self.assertEqual(params["samples"], ["usa"])

    def test_check_words_route_to_the_checks(self):
        for text in ("check the uncommon drawing", "anything wrong with leica?",
                     "audit minimal r10", "are there empty layers?"):
            self.assertEqual(route(text)[0], "drawing-check", text)

    def test_layer_questions_route_to_the_layer_report(self):
        self.assertEqual(route("how many layers does the text drawing have?")[0], "layer-report")
        self.assertEqual(route("what colours are in the colors drawing?")[0], "layer-report")

    def test_block_questions_route_to_the_block_report(self):
        self.assertEqual(route("what blocks are in the text drawing?")[0], "block-report")
        self.assertEqual(route("how many inserts in ascii r12?")[0], "block-report")

    def test_comparison_words_route_to_compare(self):
        skill, params = route("compare the usa and leica drawings")
        self.assertEqual(skill, "compare")
        self.assertEqual(params["samples"], ["usa", "leica"])

    def test_two_urls_are_read_even_when_nothing_else_is_said(self):
        skill, params = route("https://a.test/x.dxf https://a.test/y.dxf")
        self.assertEqual(skill, "drawing-summary")
        self.assertEqual(len(params["urls"]), 2)

    def test_a_check_word_beats_a_layer_word(self):
        self.assertEqual(route("check the layers of the usa drawing")[0], "drawing-check")

    def test_urls_are_found_in_order_without_trailing_punctuation(self):
        found = urls_from_text("look at https://a.test/x.dxf, and https://b.test/y.dxf.")
        self.assertEqual(found, ["https://a.test/x.dxf", "https://b.test/y.dxf"])

    def test_two_demos_in_one_sentence_are_both_found(self):
        self.assertEqual(samples_from_text("compare usa with leica"), ["usa", "leica"])
        self.assertEqual(sample_from_text("check the hatches 1 drawing"), "hatches")


class FakeData(DxfData):
    """The reader the agent talks to: the same interface, no network."""

    def __init__(self, drawing=None, second=None, raise_error: bool = False,
                 raise_value_error: bool = False):
        super().__init__(base_url="https://fixture.test", fetch=fetch_exact({}),
                         sleep=lambda _seconds: None)
        self._drawing = parse_drawing(drawing or DRAWING_TEXT)
        self._second = parse_drawing(second or DRAWING_B_TEXT)
        self.raise_error = raise_error
        self.raise_value_error = raise_value_error
        self.calls: list[str] = []

    def read(self, reference):
        if self.raise_error:
            raise DxfError("the host is offline")
        if self.raise_value_error:
            raise ValueError("not a DXF drawing")
        self.calls.append(reference)
        text = str(reference)
        if "second" in text:
            record, name, label = self._second, "second", "second drawing"
        else:
            record, name, label = self._drawing, "drawing", "fixture drawing"
        record = dict(record)
        record.update({"url": f"https://fixture.test/{name}.dxf", "label": label,
                       "source": "sample", "bytes": 4096})
        return record


class QueueReader:
    def __init__(self):
        import queue
        self._items = queue.Queue()

    def push(self, text):
        self._items.put(text)

    def close(self):
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if item is None:
            raise StopIteration
        return item


class WiredWriter:
    def __init__(self, reader):
        self.reader = reader

    def write(self, text):
        self.reader.push(text)

    def flush(self):
        pass

    def close(self):
        self.reader.close()


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (Connection(to_agent, WiredWriter(to_client), name="agent"),
            Connection(to_client, WiredWriter(to_agent), name="client"))


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = DxfAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def tools(self, result):
        return [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]

    def test_the_agent_declares_its_name_and_version(self):
        agent_conn, _client_conn = connected_pair()
        agent = DxfAgent(agent_conn, FakeData())
        self.assertEqual(agent.name, "dxf")
        self.assertEqual(agent.version, "1.0.0")
        self.assertIn("DXF", agent.title)

    def test_help_needs_no_permission(self):
        result, client = self.turn("help")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("DXF", result["text"])
        self.assertIn("check the uncommon drawing", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_a_summary_counts_what_the_file_says(self):
        result, client = self.turn("what is in https://fixture.test/drawing.dxf?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        text = result["text"]
        self.assertIn("DXF drawing, format 2018 (AC1032)", text)
        self.assertIn("Units: millimetres", text)
        self.assertIn("Layers: 5 layers defined, 3 holding geometry, 2 empty", text)
        self.assertIn("Blocks: 3 block definitions, 1 insert placing them", text)
        self.assertIn("bounding box of", text)
        self.assertIn("Drawn length:", text)
        self.assertIn("wall thickness 240", text)
        self.assertIn("Extents: the header declares", text)
        self.assertIn("counted out of the file itself", text)
        self.assertIn("https://fixture.test/drawing.dxf", text)
        self.assertEqual(len(client.permission_requests), 1)
        tool = self.tools(result)[0]
        self.assertEqual(tool["name"], "drawing-summary")
        self.assertEqual(tool["kind"], "read")

    def test_a_check_lists_faults_and_notes_apart(self):
        result, _client = self.turn("check https://fixture.test/drawing.dxf")
        text = result["text"]
        self.assertIn("Checks (file-level, not AutoCAD AUDIT):", text)
        self.assertIn("✗ Line segments of zero length: 1", text)
        self.assertIn("✗ Inserts pointing at a block the file does not define: 1", text)
        self.assertIn("• Layers defined but holding no entities: 2", text)
        self.assertIn("to look at across", text)
        self.assertIn("not AutoCAD AUDIT", text)

    def test_a_layer_report_lists_every_layer_with_its_colour(self):
        result, _client = self.turn("what layers does https://fixture.test/drawing.dxf have?")
        text = result["text"]
        self.assertIn("layers defined", text)
        self.assertIn("WALLS: 18 objects", text)
        self.assertIn("colour 7", text)
        self.assertIn("switched off", text)
        self.assertIn("Empty layers (2): EMPTY, walls", text)

    def test_a_block_report_names_the_definitions_and_the_inserts(self):
        result, _client = self.turn("what blocks are in https://fixture.test/drawing.dxf?")
        text = result["text"]
        self.assertIn("DETAIL: 1 object (LINE 1), placed 1 time", text)
        self.assertIn("HOLDBLOCK: 0 objects (nothing), placed 0 times", text)
        self.assertIn("Defined but never inserted (1): HOLDBLOCK", text)
        self.assertIn("not expanded here", text)

    def test_a_comparison_reads_both_drawings_in_one_tool_call(self):
        result, client = self.turn("compare https://fixture.test/drawing.dxf with "
                                   "https://fixture.test/second.dxf")
        text = result["text"]
        self.assertIn("fixture drawing vs second drawing", text)
        self.assertIn("Layers only on the", text)
        self.assertIn("Shared:", text)
        self.assertEqual(len(client.permission_requests), 1)
        tool = self.tools(result)[0]
        self.assertEqual(tool["name"], "compare")

    def test_a_comparison_with_one_url_asks_for_the_second(self):
        result, client = self.turn("compare https://fixture.test/a.dxf")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("needs both drawings", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(self.tools(result), [])

    def test_a_non_dxf_url_is_refused_before_permission(self):
        result, client = self.turn("inspect https://fixture.test/plan.dwg")
        self.assertIn(".dxf", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(self.tools(result), [])

    def test_no_drawing_at_all_asks_for_one_without_reading(self):
        data = FakeData()
        result, client = self.turn("tell me about this drawing", data=data)
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Tell me which drawing to read", result["text"])
        self.assertEqual(data.calls, [])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(self.tools(result), [])

    def test_a_denied_permission_is_a_refusal_and_skips_the_read(self):
        data = FakeData()
        result, _client = self.turn("check https://fixture.test/drawing.dxf", data=data,
                                   permission="reject-once")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("permission", result["text"])
        self.assertEqual(data.calls, [])

    def test_a_reader_failure_is_reported_not_swallowed(self):
        result, _client = self.turn("what is in https://fixture.test/drawing.dxf?",
                                    data=FakeData(raise_error=True))
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("could not read that drawing", result["text"])
        self.assertIn("offline", result["text"])

    def test_a_bad_reference_is_reported_too(self):
        result, _client = self.turn("what is in https://fixture.test/drawing.dxf?",
                                    data=FakeData(raise_value_error=True))
        self.assertIn("I could not read that drawing", result["text"])

    def test_the_turn_streams_a_plan_and_closes_the_tool_call_with_a_summary(self):
        result, _client = self.turn("what is in https://fixture.test/drawing.dxf?")
        plan = [update for update in result["updates"] if update.get("sessionUpdate") == "plan"]
        self.assertTrue(plan)
        self.assertEqual(plan[0]["entries"][0]["content"], "Route the request (drawing-summary)")
        updates = [update for update in result["updates"]
                   if update.get("sessionUpdate") == "tool_call_update"]
        completed = [update for update in updates if update.get("status") == "completed"]
        self.assertEqual(len(completed), 1)
        summary = completed[0]["content"][0]["content"]["text"]
        self.assertIn("DXF 2018", summary)
        self.assertIn("layers", summary)

    def test_every_skill_in_the_list_is_routable(self):
        from agent import SKILLS
        self.assertEqual(set(SKILLS), {"drawing-summary", "layer-report", "block-report",
                                       "drawing-check", "compare", "help"})
        self.assertEqual(route("help")[0], "help")
        self.assertEqual(route("check it")[0], "drawing-check")
        self.assertEqual(route("compare a and b")[0], "compare")


if __name__ == "__main__":
    unittest.main(verbosity=2)
