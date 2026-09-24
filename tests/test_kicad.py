"""Tests for the kicad ACP agent: the s-expression reader, checks, routing, permissions.

    python3 tests/test_kicad.py

No network: the reader runs against injected documents and the agent against a FakeData. The
fixtures are hand-written copies of the shapes KiCad writes, checked against the live demo
projects on 2026-09-24.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KICAD_DIR = REPO_ROOT / "agents" / "kicad"
for path in (str(REPO_ROOT), str(KICAD_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    KicadAgent,
    kind_from_text,
    route,
    sample_from_text,
    urls_from_text,
)
from data import (  # noqa: E402
    BOARD,
    KicadBusy,
    KicadData,
    KicadError,
    MAX_BYTES,
    SAMPLES,
    SCHEMATIC,
    arc_length,
    board_checks,
    bounds,
    net_key,
    net_name,
    parse_board,
    parse_schematic,
    parse_sexp,
    parity_checks,
    schematic_checks,
    tokenize,
)

#: A board written the way KiCad 9 writes one: numbered nets, courtyards, a filled zone — plus
#: one of every problem the checks look for.
BOARD_TEXT = """(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(title_block
\t\t(title "Fixture board")
\t\t(date "2026-01-02")
\t\t(rev "1.0")
\t\t(company "Test Ltd")
\t)
\t(layers
\t\t(0 "F.Cu" signal "top_copper")
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t\t(29 "B.CrtYd" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "VCC")
\t(net 3 "Net-(U1-Pad2)")
\t(net 4 "LONELY")
\t(footprint "Fixture:R_0603"
\t\t(layer "F.Cu")
\t\t(at 100 50)
\t\t(attr smd)
\t\t(property "Reference" "R1")
\t\t(property "Value" "10k")
\t\t(fp_line
\t\t\t(start -1 1)
\t\t\t(end 1 1)
\t\t\t(stroke (width 0.1) (type solid))
\t\t\t(layer "F.CrtYd")
\t\t)
\t\t(pad "1" smd roundrect
\t\t\t(at -0.8 0)
\t\t\t(size 0.9 0.95)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 2 "VCC")
\t\t)
\t\t(pad "2" smd roundrect
\t\t\t(at 0.8 0)
\t\t\t(size 0.9 0.95)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 3 "Net-(U1-Pad2)")
\t\t)
\t)
\t(footprint "R_0805_HandSolder"
\t\t(layer "F.Cu")
\t\t(at 110 50)
\t\t(attr smd)
\t\t(property "Reference" "R2")
\t\t(property "Value" "1k")
\t\t(pad "1" smd rect
\t\t\t(at -1 0)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net 1 "GND")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 1 0)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net 1 "GND")
\t\t)
\t)
\t(footprint "Fixture:SOIC-8"
\t\t(layer "F.Cu")
\t\t(at 106 54)
\t\t(attr smd)
\t\t(property "Reference" "U1")
\t\t(property "Value" "OpAmp")
\t\t(fp_rect
\t\t\t(start -2 2)
\t\t\t(end 2 -2)
\t\t\t(stroke (width 0.1) (type solid))
\t\t\t(layer "F.CrtYd")
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at -1.5 1)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net 1 "GND")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at -1.5 -1)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net 3 "Net-(U1-Pad2)")
\t\t)
\t\t(pad "3" smd rect
\t\t\t(at 1.5 0)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net 4 "LONELY")
\t\t)
\t)
\t(footprint "Fixture:PinHeader_1x02"
\t\t(layer "F.Cu")
\t\t(at 200 200)
\t\t(attr through_hole)
\t\t(property "Reference" "J1")
\t\t(property "Value" "Conn")
\t\t(pad "1" thru_hole circle
\t\t\t(at 0 0)
\t\t\t(size 2 2)
\t\t\t(drill 1.2)
\t\t\t(layers "*.Cu" "*.Mask")
\t\t\t(net 0 "")
\t\t)
\t\t(pad "2" thru_hole oval
\t\t\t(at 0 2.54)
\t\t\t(size 1.2 2)
\t\t\t(drill oval 0.6 1)
\t\t\t(layers "*.Cu" "*.Mask")
\t\t\t(net 2 "VCC")
\t\t)
\t)
\t(segment
\t\t(start 100 50)
\t\t(end 106 50)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net 1)
\t)
\t(segment
\t\t(start 106 50)
\t\t(end 110 50)
\t\t(width 0)
\t\t(layer "F.Cu")
\t\t(net 2)
\t)
\t(arc
\t\t(start 110 50)
\t\t(mid 111 51)
\t\t(end 110 52)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net 1)
\t)
\t(via
\t\t(at 102 52)
\t\t(size 0.8)
\t\t(drill 0.4)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net 1)
\t)
\t(via
\t\t(at 104 52)
\t\t(size 0.4)
\t\t(drill 0.4)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net 2)
\t)
\t(zone
\t\t(net 1)
\t\t(net_name "GND")
\t\t(layer "B.Cu")
\t\t(hatch edge 0.5)
\t\t(filled_polygon
\t\t\t(layer "B.Cu")
\t\t\t(pts (xy 90 40) (xy 120 40) (xy 120 60))
\t\t)
\t)
\t(zone
\t\t(net 2)
\t\t(net_name "VCC")
\t\t(layer "F.Cu")
\t\t(hatch edge 0.5)
\t)
\t(gr_rect
\t\t(start 90 40)
\t\t(end 120 60)
\t\t(stroke (width 0.05) (type default))
\t\t(fill none)
\t\t(layer "Edge.Cuts")
\t)
\t(gr_text "Fixture"
\t\t(at 100 45)
\t\t(layer "F.SilkS")
\t)
\t(dimension
\t\t(type aligned)
\t\t(layer "Dwgs.User")
\t)
)"""

#: A board written the way the newest KiCad writes one: no numbered net table, net names on
#: every pad and track, and a constraint list.
BOARD_NEW_TEXT = """(kicad_pcb (version 20260624) (generator "pcbnew") (generator_version "10.99")
\t(general
\t\t(thickness 1.6)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t)
\t(footprint "Fixture:R_0402"
\t\t(layer "F.Cu")
\t\t(at 10 10)
\t\t(property "Reference" "R9")
\t\t(property "Value" "100R")
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0)
\t\t\t(size 0.5 0.6)
\t\t\t(layers "F.Cu")
\t\t\t(net "VCC")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0)
\t\t\t(size 0.5 0.6)
\t\t\t(layers "F.Cu")
\t\t\t(net "GND")
\t\t)
\t)
\t(segment
\t\t(start 10 10)
\t\t(end 14 10)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(net "VCC")
\t)
\t(gr_rect
\t\t(start 0 0)
\t\t(end 20 20)
\t\t(stroke (width 0.05) (type default))
\t\t(fill none)
\t\t(layer "Edge.Cuts")
\t)
\t(constraint
\t\t(type clearance)
\t\t(min 0.2)
\t)
\t(constraint
\t\t(type track_width)
\t\t(min 0.15)
\t)
)"""

#: A schematic with a multi-unit part, an unannotated symbol, a DNP part and a bare sheet.
SCHEMATIC_TEXT = """(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(title_block
\t\t(title "Fixture schematic")
\t\t(date "2026-01-02")
\t\t(rev "1.0")
\t)
\t(lib_symbols
\t\t(symbol "Device:R"
\t\t\t(pin_names (offset 0))
\t\t)
\t\t(symbol "Amplifier_Operational:LM358"
\t\t\t(pin_names (offset 0.127))
\t\t)
\t\t(symbol "power:GND"
\t\t\t(power)
\t\t)
\t)
\t(junction
\t\t(at 100 50)
\t\t(diameter 0)
\t\t(color 0 0 0 0)
\t)
\t(no_connect
\t\t(at 108 50)
\t)
\t(wire
\t\t(pts (xy 100 50) (xy 110 50))
\t\t(stroke (width 0) (type default))
\t)
\t(wire
\t\t(pts (xy 110 50) (xy 110 60))
\t\t(stroke (width 0) (type default))
\t)
\t(label "SENSE"
\t\t(at 110 55 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t)
\t(global_label "VCC"
\t\t(shape input)
\t\t(at 120 50 180)
\t\t(effects (font (size 1.27 1.27)))
\t)
\t(hierarchical_label "OUT"
\t\t(shape output)
\t\t(at 130 50 0)
\t\t(effects (font (size 1.27 1.27)))
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-000000000001")
\t\t(property "Reference" "R1")
\t\t(property "Value" "10k")
\t\t(property "Footprint" "Fixture:R_0603")
\t\t(pin "1" (uuid "aaaa1111-0000-0000-0000-000000000002"))
\t\t(pin "2" (uuid "aaaa1111-0000-0000-0000-000000000003"))
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 104 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-000000000004")
\t\t(property "Reference" "R?")
\t\t(property "Value" "4k7")
\t\t(property "Footprint" "")
\t\t(pin "1" (uuid "aaaa1111-0000-0000-0000-000000000005"))
\t)
\t(symbol
\t\t(lib_id "Amplifier_Operational:LM358")
\t\t(at 108 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-000000000006")
\t\t(property "Reference" "U1")
\t\t(property "Value" "LM358")
\t\t(property "Footprint" "Fixture:SOIC-8")
\t\t(pin "1" (uuid "aaaa1111-0000-0000-0000-000000000007"))
\t)
\t(symbol
\t\t(lib_id "Amplifier_Operational:LM358")
\t\t(at 112 50 0)
\t\t(unit 2)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-000000000008")
\t\t(property "Reference" "U1")
\t\t(property "Value" "LM358")
\t\t(property "Footprint" "Fixture:SOIC-8")
\t\t(pin "5" (uuid "aaaa1111-0000-0000-0000-000000000009"))
\t)
\t(symbol
\t\t(lib_id "Amplifier_Operational:LM358")
\t\t(at 116 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-00000000000a")
\t\t(property "Reference" "U1")
\t\t(property "Value" "LM358")
\t\t(property "Footprint" "Fixture:SOIC-8")
\t\t(pin "1" (uuid "aaaa1111-0000-0000-0000-00000000000b"))
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 120 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp yes)
\t\t(uuid "aaaa1111-0000-0000-0000-00000000000c")
\t\t(property "Reference" "R5")
\t\t(property "Value" "0R")
\t\t(property "Footprint" "Fixture:R_0603")
\t\t(pin "1" (uuid "aaaa1111-0000-0000-0000-00000000000d"))
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 124 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom no)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-00000000000e")
\t\t(property "Reference" "MH1")
\t\t(property "Value" "MountingHole")
\t\t(property "Footprint" "Fixture:MountingHole")
\t\t(pin "1" (uuid "aaaa1111-0000-0000-0000-00000000000f"))
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 128 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-000000000010")
\t\t(property "Reference" "#PWR01")
\t\t(property "Value" "GND")
\t\t(property "Footprint" "")
\t\t(pin "1" (uuid "aaaa1111-0000-0000-0000-000000000011"))
\t)
\t(sheet
\t\t(at 150 50)
\t\t(size 20 15)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaa1111-0000-0000-0000-000000000012")
\t\t(property "Sheetname" "Power")
\t\t(property "Sheetfile" "")
\t\t(pin "VBUS" input
\t\t\t(at 150 55 180)
\t\t\t(uuid "aaaa1111-0000-0000-0000-000000000013")
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)"""

#: The URLs the built-in ecc83 demo resolves to against the fixture base URL.
DEMO_BOARD = "https://fixture.test/ecc83/ecc83-pp.kicad_pcb"
DEMO_SCH = "https://fixture.test/ecc83/ecc83-pp.kicad_sch"
FIXTURES = {
    DEMO_BOARD: BOARD_TEXT,
    DEMO_SCH: SCHEMATIC_TEXT,
    "https://fixture.test/new.kicad_pcb": BOARD_NEW_TEXT,
}



def fetch_exact(mapping, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if url not in mapping:
            raise KicadError(f"no fixture at {url}")
        payload = mapping[url]
        return payload() if callable(payload) else (payload.encode("utf-8")
                                                    if isinstance(payload, str) else payload)

    return fetch


class HelperTests(unittest.TestCase):
    def test_tokens_keep_empty_strings_and_unescape_quotes(self):
        self.assertEqual(tokenize('(net 0 "")'), ["(", "net", "0", "", ")"])
        self.assertEqual(tokenize('(property "Reference" "R1")'),
                         ["(", "property", "Reference", "R1", ")"])
        self.assertEqual(tokenize(r'(title "a \"quoted\" name")'),
                         ["(", "title", 'a "quoted" name', ")"])
        self.assertEqual(tokenize(r'(path "/VPP{slash}MCLR")'), ["(", "path", "/VPP{slash}MCLR", ")"])

    def test_a_board_parses_into_nested_lists(self):
        tree = parse_sexp("(kicad_pcb (version 20241229) (net 1 \"GND\"))")
        self.assertEqual(tree[0], "kicad_pcb")
        self.assertEqual(tree[1], ["version", "20241229"])
        self.assertEqual(tree[2], ["net", "1", "GND"])

    def test_unbalanced_brackets_are_refused(self):
        with self.assertRaises(KicadError):
            parse_sexp("(kicad_pcb (version 1)")
        with self.assertRaises(KicadError):
            parse_sexp("nothing here")
        with self.assertRaises(KicadError):
            parse_sexp('(title "unterminated')

    def test_a_net_is_read_from_either_shape(self):
        board = parse_board(BOARD_TEXT)
        numbered = {net["number"]: net["name"] for net in board["nets"]}
        self.assertEqual(numbered[1], "GND")
        self.assertEqual(numbered[3], "Net-(U1-Pad2)")
        fresh = parse_board(BOARD_NEW_TEXT)
        self.assertEqual({net["name"] for net in fresh["nets"]}, {"VCC", "GND"})
        self.assertEqual([net["number"] for net in fresh["nets"]], [None, None])

    def test_net_keys_and_names_normalise_the_way_the_board_writes_them(self):
        self.assertEqual(net_key(2, "GND"), "GND")
        self.assertEqual(net_key(2, ""), "#2")
        self.assertEqual(net_key(None, ""), "#0")
        self.assertEqual(net_name("/8MH-OUT"), "/8MH-OUT")
        self.assertEqual(net_name("/VPP{slash}MCLR"), "/VPP/MCLR")
        self.assertEqual(net_name("A{space}B"), "A B")

    def test_arc_length_measures_the_circle_and_survives_a_straight_line(self):
        # Half of the unit circle through the top: three points, midpoint on the arc.
        self.assertAlmostEqual(arc_length((1, 0), (0, 1), (-1, 0)), 3.14159, places=4)
        # A quarter of the unit circle.
        self.assertAlmostEqual(arc_length((1, 0), (0.7071, 0.7071), (0, 1)), 1.5708, places=3)
        # Points on one line: the two chords are added instead of dividing by zero.
        self.assertAlmostEqual(arc_length((0, 0), (1, 0), (2, 0)), 2.0, places=6)

    def test_bounds_reports_the_box_and_how_many_points_made_it(self):
        box = bounds([(0, 0), (10, 4), (2, 8)])
        self.assertEqual((box["width_mm"], box["height_mm"], box["points"]), (10, 8, 3))
        self.assertIsNone(bounds([]))


class BoardParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = parse_board(BOARD_TEXT)

    def test_header_layers_and_title_block(self):
        board = self.board
        self.assertEqual(board["kind"], BOARD)
        self.assertEqual(board["format_version"], "20241229")
        self.assertEqual(board["generator"], "pcbnew")
        self.assertEqual(board["generator_version"], "9.0")
        self.assertEqual(board["paper"], "A4")
        self.assertEqual(board["thickness_mm"], 1.6)
        self.assertEqual(len(board["layers"]), 5)
        self.assertEqual(board["copper_layers"], ["F.Cu", "B.Cu"])
        self.assertEqual(board["layers"][0]["user_name"], "top_copper")
        self.assertEqual(board["title_block"]["title"], "Fixture board")
        self.assertEqual(board["title_block"]["company"], "Test Ltd")

    def test_parts_pads_and_library_nicknames(self):
        board = self.board
        self.assertEqual(len(board["footprints"]), 4)
        self.assertEqual({footprint["reference"] for footprint in board["footprints"]},
                         {"R1", "R2", "U1", "J1"})
        self.assertEqual(board["pads"]["total"], 9)
        self.assertEqual(board["pads"]["by_type"], {"smd": 7, "thru_hole": 2})
        self.assertEqual(board["pads"]["min_drill_mm"], 0.6)
        self.assertEqual(board["pads"]["max_drill_mm"], 1.2)
        self.assertEqual(board["pads"]["drilled"], 2)
        r2 = next(footprint for footprint in board["footprints"] if footprint["reference"] == "R2")
        self.assertIsNone(r2["library_nickname"])
        j1 = next(footprint for footprint in board["footprints"] if footprint["reference"] == "J1")
        self.assertEqual(j1["pad_numbers"], ["1", "2"])
        self.assertEqual(j1["attributes"], ["through_hole"])

    def test_courtyard_needs_a_courtyard_layer(self):
        by_reference = {footprint["reference"]: footprint for footprint in self.board["footprints"]}
        self.assertTrue(by_reference["R1"]["courtyard"])
        self.assertTrue(by_reference["U1"]["courtyard"])
        self.assertFalse(by_reference["R2"]["courtyard"])
        self.assertFalse(by_reference["J1"]["courtyard"])

    def test_tracks_arcs_vias_zones_and_the_outline(self):
        board = self.board
        self.assertEqual(board["tracks"]["segments"], 2)
        self.assertEqual(board["tracks"]["arcs"], 1)
        self.assertEqual(board["tracks"]["min_width_mm"], 0.0)
        self.assertEqual(board["tracks"]["max_width_mm"], 0.25)
        self.assertEqual(len(board["tracks"]["zero_width"]), 1)
        self.assertEqual(board["tracks"]["by_layer"]["F.Cu"]["segments"], 3)
        # 6 mm straight + 5 mm straight + a quarter-ish arc of radius ~1.414
        self.assertGreater(board["tracks"]["length_mm"], 10.0)
        self.assertEqual(len(board["vias"]), 2)
        self.assertEqual(board["vias"][0]["layers"], ["F.Cu", "B.Cu"])
        self.assertEqual(len(board["zones"]), 2)
        self.assertEqual([zone["net"] for zone in board["zones"]], ["GND", "VCC"])
        self.assertEqual([zone["filled"] for zone in board["zones"]], [True, False])
        self.assertEqual(board["outline"]["width_mm"], 30.0)
        self.assertEqual(board["outline"]["height_mm"], 20.0)
        self.assertEqual(board["graphics"], {"gr_rect": 1, "gr_text": 1})
        self.assertEqual(board["graphics_by_layer"]["Edge.Cuts"], 1)
        self.assertEqual(board["counts"]["dimension"], 1)

    def test_nets_pads_and_routing_are_joined_across_both_shapes(self):
        board = self.board
        nets = {net["key"]: net for net in board["nets"]}
        self.assertEqual(nets["GND"]["pads"], 3)
        self.assertTrue(nets["GND"]["routed"])
        self.assertTrue(nets["GND"]["zoned"])
        self.assertTrue(nets["GND"]["named"])
        self.assertEqual(nets["LONELY"]["pads"], 1)
        self.assertFalse(nets["LONELY"]["routed"])
        self.assertTrue(nets["VCC"]["routed"], "a track that only numbers its net joins the named one")
        self.assertFalse(nets["Net-(U1-Pad2)"]["named"])
        self.assertEqual([net["name"] for net in board["named_nets"]], ["GND", "VCC", "LONELY"])

    def test_the_newest_format_has_no_net_table_and_a_constraint_list(self):
        board = parse_board(BOARD_NEW_TEXT)
        self.assertEqual(board["format_version"], "20260624")
        self.assertEqual(board["constraints"], ["clearance", "track_width"])
        self.assertEqual(len(board["footprints"]), 1)
        self.assertEqual(board["nets"][0]["pads"], 1)
        # The pad's net name and the track's net name are the same net, not two.
        self.assertEqual(len(board["nets"]), 2)
        vcc = next(net for net in board["nets"] if net["name"] == "VCC")
        self.assertTrue(vcc["routed"])

    def test_a_schematic_is_not_a_board_and_junk_is_not_kicad(self):
        with self.assertRaises(KicadError):
            parse_board(SCHEMATIC_TEXT)
        with self.assertRaises(KicadError):
            parse_board("(kicad_pro (version 1))")


class SchematicParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schematic = parse_schematic(SCHEMATIC_TEXT)

    def test_header_symbols_and_references(self):
        schematic = self.schematic
        self.assertEqual(schematic["kind"], SCHEMATIC)
        self.assertEqual(schematic["format_version"], "20250114")
        self.assertEqual(schematic["generator"], "eeschema")
        self.assertEqual(schematic["paper"], "A4")
        self.assertEqual(schematic["title_block"]["rev"], "1.0")
        self.assertEqual(len(schematic["symbols"]), 8)
        self.assertEqual(schematic["lib_symbols"], 3)
        self.assertEqual([symbol["reference"] for symbol in schematic["symbols"] if symbol["power"]],
                         ["#PWR01"])

    def test_unit_flags_and_values_are_read_per_symbol(self):
        schematic = self.schematic
        units = [(symbol["reference"], symbol["unit"]) for symbol in schematic["symbols"]]
        self.assertEqual(units, [("R1", 1), ("R?", 1), ("U1", 1), ("U1", 2), ("U1", 1),
                                 ("R5", 1), ("MH1", 1), ("#PWR01", 1)])
        r5 = next(symbol for symbol in schematic["symbols"] if symbol["reference"] == "R5")
        self.assertTrue(r5["dnp"])
        self.assertTrue(r5["in_bom"])
        mh1 = next(symbol for symbol in schematic["symbols"] if symbol["reference"] == "MH1")
        self.assertFalse(mh1["in_bom"])
        self.assertEqual(r5["pins"], 1)

    def test_wiring_labels_and_hierarchy(self):
        schematic = self.schematic
        self.assertEqual(schematic["wires"]["count"], 2)
        self.assertEqual(schematic["wires"]["length_mm"], 20.0)
        self.assertEqual(schematic["junctions"], 1)
        self.assertEqual(schematic["no_connects"], 1)
        self.assertEqual(schematic["labels"], ["SENSE"])
        self.assertEqual(schematic["global_labels"], ["VCC"])
        self.assertEqual(schematic["hierarchical_labels"], ["OUT"])
        self.assertEqual(len(schematic["sheets"]), 1)
        sheet = schematic["sheets"][0]
        self.assertEqual((sheet["name"], sheet["file"]), ("Power", ""))
        self.assertEqual(sheet["pins"], ["VBUS"])
        self.assertEqual(sheet["width"], 20.0)

    def test_a_board_is_not_a_schematic(self):
        with self.assertRaises(KicadError):
            parse_schematic(BOARD_TEXT)


class ReaderTests(unittest.TestCase):
    def reader(self, mapping=None, calls=None, **kwargs):
        return KicadData(fetch=fetch_exact(mapping or FIXTURES, calls), base_url="https://fixture.test",
                         **kwargs)

    def test_a_sample_resolves_to_a_demo_url_board_and_schematic(self):
        calls: list[str] = []
        reader = self.reader(calls=calls)
        board = reader.read("ecc83", BOARD)
        self.assertEqual(board["kind"], BOARD)
        self.assertIn("ecc83/ecc83-pp.kicad_pcb", board["url"])
        self.assertEqual(board["source"], "sample")
        schematic = reader.read("ecc83", SCHEMATIC)
        self.assertEqual(schematic["kind"], SCHEMATIC)
        self.assertIn("ecc83/ecc83-pp.kicad_sch", schematic["url"])
        self.assertEqual(len(calls), 2)

    def test_sample_names_are_matched_loosely_and_longest_first(self):
        reader = KicadData(base_url="https://fixture.test")
        cases = {"ecc83": "ecc83", "the ECC-83 board": "ecc83", "tiny tapeout": "tinytapeout",
                 "PIC Programmer": "pic-programmer", "cm5 minima": "cm5-minima"}
        for given, expected in cases.items():
            found = reader.sample(given)
            self.assertIsNotNone(found, given)
            self.assertEqual(found[0], expected)
        self.assertIsNone(reader.sample("gotham"))
        self.assertIsNone(reader.sample("ab"))

    def test_a_document_url_is_used_as_given_and_its_extension_says_the_kind(self):
        reader = self.reader(mapping={"https://elsewhere.test/board.kicad_pcb": BOARD_TEXT})
        record = reader.read("https://elsewhere.test/board.kicad_pcb")
        self.assertEqual(record["kind"], BOARD)
        self.assertEqual(record["source"], "url")
        self.assertEqual(record["bytes"], len(BOARD_TEXT.encode("utf-8")))

    def test_urls_must_be_http_and_a_kicad_document(self):
        reader = KicadData(base_url="https://fixture.test")
        with self.assertRaises(ValueError):
            reader.check_url("ftp://x/board.kicad_pcb")
        with self.assertRaises(ValueError):
            reader.kind_of("https://fixture.test/board.kicad_pro")
        with self.assertRaises(ValueError):
            reader.kind_of("https://fixture.test/drawing.dxf")

    def test_an_unknown_demo_name_is_refused_with_the_list(self):
        reader = KicadData(base_url="https://fixture.test", fetch=fetch_exact({}))
        with self.assertRaises(KicadError) as caught:
            reader.read("gotham")
        self.assertIn("gotham", str(caught.exception))
        for name in ("ecc83", "tinytapeout"):
            self.assertIn(name, str(caught.exception))

    def test_a_demo_url_encodes_the_space_in_a_path(self):
        reader = KicadData(base_url="https://fixture.test")
        self.assertTrue(reader.sample_url("sonde-xilinx", BOARD).endswith("sonde%20xilinx/sonde%20xilinx.kicad_pcb"))

    def test_a_busy_host_is_retried_then_succeeds(self):
        attempts: list[str] = []

        def flaky(url):
            attempts.append(url)
            if len(attempts) < 3:
                raise KicadBusy("busy")
            return BOARD_TEXT.encode("utf-8")

        reader = KicadData(fetch=flaky, base_url="https://fixture.test", sleep=lambda _: None)
        record = reader.read("ecc83", BOARD)
        self.assertEqual(record["kind"], BOARD)
        self.assertEqual(len(attempts), 3)

    def test_a_host_that_stays_busy_gives_up_with_the_reason(self):
        calls: list[str] = []

        def always_busy(url):
            calls.append(url)
            raise KicadBusy("GitLab is busy")

        reader = KicadData(fetch=always_busy, base_url="https://fixture.test", sleep=lambda _: None)
        with self.assertRaises(KicadBusy):
            reader.read("ecc83", BOARD)
        self.assertEqual(len(calls), 3)

    def test_a_rejected_read_is_not_retried(self):
        calls: list[str] = []

        def not_found(url):
            calls.append(url)
            raise KicadError("no document at that URL (HTTP 404)")

        reader = KicadData(fetch=not_found, base_url="https://fixture.test", sleep=lambda _: None)
        with self.assertRaises(KicadError):
            reader.read("ecc83", BOARD)
        self.assertEqual(len(calls), 1)

    def test_a_document_is_cached_so_a_second_read_is_free(self):
        calls: list[str] = []
        reader = self.reader(calls=calls)
        reader.read("ecc83", BOARD)
        reader.read("ecc83", BOARD)
        self.assertEqual(len(calls), 1)

    def test_an_oversized_document_is_refused_with_its_size(self):
        def huge(url):
            return b"(" * (MAX_BYTES + 10)

        reader = KicadData(fetch=huge, base_url="https://fixture.test", sleep=lambda _: None)
        with self.assertRaises(KicadError):
            reader.read("ecc83", BOARD)

    def test_the_reader_uses_its_own_settings(self):
        reader = KicadData(base_url="https://fixture.test/", cache_ttl=5, timeout=7,
                           user_agent="probe/9")
        self.assertEqual(reader.base_url, "https://fixture.test")
        self.assertEqual(reader.cache_ttl, 5.0)
        self.assertEqual(reader.timeout, 7.0)
        self.assertEqual(reader.user_agent, "probe/9")

    def test_parse_failures_are_reader_errors_not_silent_empties(self):
        reader = self.reader(mapping={"https://fixture.test/junk.kicad_pcb": "hello"})
        with self.assertRaises(KicadError):
            reader.read("https://fixture.test/junk.kicad_pcb")


class BoardCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = parse_board(BOARD_TEXT)
        cls.findings = {finding["id"]: finding for finding in board_checks(cls.board)}

    def test_every_check_is_reported_even_when_it_finds_nothing(self):
        expected = {"zero-width-track", "no-courtyard", "no-library", "duplicate-reference",
                    "via-drill", "single-pad-net", "off-board", "unfilled-zone", "unrouted-net",
                    "no-outline"}
        self.assertEqual(set(self.findings), expected)
        for finding in self.findings.values():
            self.assertIn("detail", finding)
            self.assertEqual(finding["severity"], "check")

    def test_the_problems_in_the_fixture_are_all_found(self):
        self.assertEqual(self.findings["zero-width-track"]["count"], 1)
        self.assertIn("F.Cu", self.findings["zero-width-track"]["examples"][0])
        self.assertEqual(self.findings["no-courtyard"]["count"], 2)
        self.assertEqual(self.findings["no-library"]["count"], 1)
        self.assertIn("R2", self.findings["no-library"]["examples"][0])
        self.assertEqual(self.findings["via-drill"]["count"], 1)
        self.assertEqual(self.findings["single-pad-net"]["count"], 1)
        self.assertEqual(self.findings["single-pad-net"]["examples"], ["LONELY"])
        self.assertEqual(self.findings["off-board"]["count"], 1)
        self.assertIn("J1", self.findings["off-board"]["examples"][0])
        self.assertEqual(self.findings["unfilled-zone"]["count"], 1)
        self.assertEqual(self.findings["unfilled-zone"]["examples"], ["VCC on F.Cu"])
        self.assertEqual(self.findings["no-outline"]["count"], 0)

    def test_a_net_poured_into_a_zone_is_not_called_unrouted(self):
        self.assertEqual(self.findings["unrouted-net"]["count"], 0)
        self.assertEqual(self.findings["unrouted-net"]["examples"], [])

    def test_a_board_with_no_outline_says_so(self):
        text = BOARD_TEXT.replace("(gr_rect\n\t\t(start 90 40)\n\t\t(end 120 60)", "(gr_rect\n\t\t(start 90 40)\n\t\t(end 90 40)")
        board = parse_board(text.replace("(layer \"Edge.Cuts\")", "(layer \"F.SilkS\")"))
        findings = {finding["id"]: finding for finding in board_checks(board)}
        self.assertIsNone(board["outline"])
        self.assertEqual(findings["no-outline"]["count"], 1)
        self.assertEqual(findings["off-board"]["count"], 0)


class SchematicCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings = {finding["id"]: finding
                        for finding in schematic_checks(parse_schematic(SCHEMATIC_TEXT))}

    def test_the_fixture_problems_are_found(self):
        self.assertEqual(len(self.findings), 7)
        self.assertEqual(self.findings["unannotated"]["count"], 1)
        self.assertEqual(self.findings["unannotated"]["examples"], ["R? (4k7)"])
        # R? is unannotated and MH1 is out of the BOM; R? still has to be placed, so it counts.
        self.assertEqual(self.findings["no-footprint"]["count"], 1)
        self.assertEqual(self.findings["no-footprint"]["examples"], ["R? (4k7)"])
        self.assertEqual(self.findings["sheet-without-file"]["count"], 1)
        self.assertEqual(self.findings["sheet-without-file"]["examples"], ["Power"])

    def test_a_repeated_reference_with_a_new_unit_is_not_a_duplicate(self):
        self.assertEqual(self.findings["duplicate-reference"]["count"], 1)
        self.assertEqual(self.findings["duplicate-reference"]["examples"], ["U1 unit 1 × 2"])
        self.assertEqual(self.findings["multi-unit"]["count"], 1)
        self.assertEqual(self.findings["multi-unit"]["examples"], ["U1 × 2 units"])
        self.assertEqual(self.findings["multi-unit"]["severity"], "note")

    def test_dnp_and_bom_exclusions_are_notes_not_faults(self):
        self.assertEqual(self.findings["dnp"]["count"], 1)
        self.assertEqual(self.findings["dnp"]["severity"], "note")
        self.assertEqual(self.findings["excluded-from-bom"]["count"], 1)
        self.assertEqual(self.findings["excluded-from-bom"]["severity"], "note")
        self.assertEqual(self.findings["excluded-from-bom"]["examples"], ["MH1 (MountingHole)"])

    def test_a_part_in_the_bom_with_no_footprint_is_a_fault(self):
        text = SCHEMATIC_TEXT.replace('(property "Footprint" "Fixture:R_0603")\n\t\t(pin "1"',
                                      '(property "Footprint" "")\n\t\t(pin "1"', 1)
        findings = {finding["id"]: finding for finding in schematic_checks(parse_schematic(text))}
        self.assertEqual(findings["no-footprint"]["count"], 2)
        self.assertIn("R1 (10k)", findings["no-footprint"]["examples"])


class ParityTests(unittest.TestCase):
    def test_references_and_net_names_are_compared_both_ways(self):
        parity = parity_checks(parse_schematic(SCHEMATIC_TEXT), parse_board(BOARD_TEXT))
        self.assertEqual(parity["schematic"]["references"], 4)
        self.assertEqual(parity["board"]["references"], 4)
        self.assertEqual(parity["matching_references"], 2)
        self.assertEqual(parity["references_only_in_schematic"], ["MH1", "R5"])
        self.assertEqual(parity["references_only_in_board"], ["J1", "R2"])

        self.assertEqual(parity["schematic"]["labels"], 3)
        self.assertEqual(parity["schematic"]["power_nets"], 1)
        self.assertEqual(parity["schematic"]["net_names"], 4)
        self.assertEqual(parity["matching_net_names"], 2)
        self.assertEqual(parity["net_names_not_a_board_net"], ["OUT", "SENSE"])
        self.assertEqual(parity["board_nets_not_in_schematic"], ["LONELY"])

    def test_the_board_scopes_local_labels_and_escapes_slashes(self):
        board = parse_board(BOARD_TEXT.replace('(net 4 "LONELY")', '(net 4 "/SENSE")'))
        parity = parity_checks(parse_schematic(SCHEMATIC_TEXT), board)
        self.assertNotIn("SENSE", parity["net_names_not_a_board_net"])
        self.assertEqual(parity["matching_net_names"], 3)
        # A label with a slash in it is escaped on the board and still lines up.
        escaped = parse_board(BOARD_TEXT.replace('(net 4 "LONELY")', '(net 4 "/8MH{slash}OUT")'))
        parity = parity_checks(parse_schematic(SCHEMATIC_TEXT.replace('"OUT"', '"8MH/OUT"')), escaped)
        self.assertNotIn("8MH/OUT", parity["net_names_not_a_board_net"])
        self.assertEqual(parity["matching_net_names"], 3)

    def test_a_power_symbol_names_a_net_the_board_carries(self):
        parity = parity_checks(parse_schematic(SCHEMATIC_TEXT), parse_board(BOARD_TEXT))
        self.assertNotIn("GND", parity["net_names_not_a_board_net"])

    def test_an_unannotated_part_is_not_compared(self):
        parity = parity_checks(parse_schematic(SCHEMATIC_TEXT), parse_board(BOARD_TEXT))
        self.assertNotIn("R?", parity["references_only_in_schematic"])
        self.assertNotIn("R?", parity["references_only_in_board"])


class RouteTests(unittest.TestCase):
    def test_reading_a_board_is_the_default(self):
        for text in ("what is in the ecc83 board?", "how many nets does the stickhub have?",
                     "show me the interf_u layout"):
            skill, params = route(text)
            self.assertEqual(skill, "board-summary", text)
            self.assertIn("sample", params)

    def test_the_schematic_words_ask_for_the_schematic(self):
        skill, params = route("how many symbols are in the tinytapeout schematic?")
        self.assertEqual(skill, "schematic-summary")
        self.assertEqual(params["sample"], "tinytapeout")

    def test_checking_a_board_or_a_schematic(self):
        self.assertEqual(route("check the stickhub board")[0], "board-check")
        self.assertEqual(route("any problems with the pic programmer pcb?")[0], "board-check")
        self.assertEqual(route("validate the ecc83 schematic")[0], "schematic-check")
        self.assertEqual(route("does the tinytapeout schematic have any errors?")[0], "schematic-check")

    def test_comparing_asks_for_parity(self):
        for text in ("does the interf_u schematic still match its board?",
                     "compare the ecc83 schematic to its board",
                     "is the video board in sync with the schematic?"):
            skill, params = route(text)
            self.assertEqual(skill, "parity", text)
            self.assertEqual(params["sample"], "video" if "video" in text else params["sample"])

    def test_a_bare_url_is_read_and_its_kind_comes_from_the_extension(self):
        skill, params = route("inspect https://example.test/my-board.kicad_pcb")
        self.assertEqual(skill, "board-summary")
        self.assertEqual(params["urls"], ["https://example.test/my-board.kicad_pcb"])
        self.assertEqual(route("read https://example.test/x.kicad_sch")[0], "schematic-summary")

    def test_urls_are_picked_out_of_prose_and_trimmed(self):
        self.assertEqual(urls_from_text("look at https://a.test/b.kicad_pcb, please."),
                         ["https://a.test/b.kicad_pcb"])
        self.assertEqual(urls_from_text("nothing here"), [])

    def test_an_unknown_demo_name_carries_no_reference(self):
        skill, params = route("what is in the gotham board?")
        self.assertEqual(skill, "board-summary")
        self.assertNotIn("sample", params)
        self.assertNotIn("urls", params)

    def test_kind_and_sample_readers(self):
        self.assertEqual(kind_from_text("the schematic"), SCHEMATIC)
        self.assertEqual(kind_from_text("the pcb"), BOARD)
        self.assertEqual(kind_from_text("the board and its schematic"), "both")
        self.assertIsNone(kind_from_text("that thing"))
        self.assertEqual(sample_from_text("check my sonde xilinx board"), "sonde-xilinx")
        self.assertEqual(sample_from_text("the cm5 carrier"), "cm5-minima")

    def test_help_and_empty(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("help")[0], "help")


class FakeData(KicadData):
    """The reader the agent talks to: the same interface, no network."""

    def __init__(self, board=None, schematic=None, raise_error: bool = False,
                 raise_value_error: bool = False):
        super().__init__(base_url="https://fixture.test", fetch=fetch_exact({}), sleep=lambda _: None)
        self._board = parse_board(board or BOARD_TEXT)
        self._schematic = parse_schematic(schematic or SCHEMATIC_TEXT)
        self.raise_error = raise_error
        self.raise_value_error = raise_value_error
        self.calls: list[tuple] = []

    def read(self, reference, kind=None):
        if self.raise_error:
            raise KicadError("GitLab offline")
        if self.raise_value_error:
            raise ValueError("not a KiCad document")
        self.calls.append((reference, kind))
        record = dict(self._board if kind == BOARD else self._schematic)
        record.update({"url": f"https://fixture.test/{reference or 'sample'}"
                              f".kicad_{'pcb' if kind == BOARD else 'sch'}",
                       "label": f"{reference} {kind}", "source": "sample", "bytes": 4096})
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
        agent = KicadAgent(agent_conn, data or FakeData())
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

    def test_a_board_summary_counts_what_the_file_says(self):
        result, client = self.turn("what is in the ecc83 board?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        text = result["text"]
        self.assertIn("format 20241229 (pcbnew 9.0)", text)
        self.assertIn("Size: 30 mm × 20 mm outline box", text)
        self.assertIn("4 footprints, 9 pads, 2 vias", text)
        self.assertIn("Nets: 5 nets (3 named, 2 auto-generated)", text)
        self.assertIn("Copper: 2 zones on GND, VCC", text)
        self.assertIn("Other items: 1 gr_text, 1 dimension", text)
        self.assertIn("Title block: title 'Fixture board'", text)
        self.assertIn("https://fixture.test/ecc83.kicad_pcb", text)
        self.assertIn("counted out of the file itself", text)
        self.assertEqual(len(client.permission_requests), 1)
        tool = self.tools(result)[0]
        self.assertEqual(tool["name"], "board-summary")
        self.assertEqual(tool["kind"], "read")

    def test_a_board_check_lists_faults_and_notes_apart(self):
        result, _ = self.turn("check the ecc83 board")
        text = result["text"]
        self.assertIn("Checks (file-level, not KiCad DRC):", text)
        self.assertIn("✗ Zero-width track segments: 1", text)
        self.assertIn("✗ Named nets a single pad sits on: 1", text)
        self.assertIn("LONELY", text)
        self.assertIn("✗ Footprints outside the board outline: 1", text)
        self.assertIn("✓ No Edge.Cuts outline: none", text)
        self.assertIn("to look at across", text)
        self.assertIn("not KiCad DRC", text)

    def test_a_schematic_check_calls_dnp_a_note(self):
        result, _ = self.turn("check the tinytapeout schematic")
        text = result["text"]
        self.assertIn("Checks (file-level, not KiCad ERC):", text)
        self.assertIn("✗ Unannotated symbols (reference ends in ?): 1", text)
        self.assertIn("• Symbols marked do-not-populate: 1", text)
        self.assertIn("• Parts with more than one unit placed: 1", text)
        self.assertIn("plus 3 notes", text)
        self.assertIn("to look at across 4 checks", text)
        self.assertIn("not KiCad ERC", text)

    def test_a_schematic_summary_names_the_wiring_and_the_hierarchy(self):
        result, _ = self.turn("how many symbols are in the tinytapeout schematic?")
        text = result["text"]
        self.assertIn("8 symbols placed (7 parts, 1 power/flag symbols)", text)
        self.assertIn("3 library symbols embedded", text)
        self.assertIn("References: 5 unique references, 1 unannotated", text)
        self.assertIn("Wiring: 2 wires (20 mm drawn), 1 junction, 1 no-connect flag", text)
        self.assertIn("Labels: 1 local, 1 global, 1 hierarchical", text)
        self.assertIn("Power → (no file)", text)

    def test_parity_reads_both_documents_in_one_tool_call(self):
        result, client = self.turn("does the ecc83 schematic still match its board?")
        text = result["text"]
        self.assertIn("References: 4 in the schematic, 4 on the board, 2 match.", text)
        self.assertIn("In the schematic but not on the board: 2", text)
        self.assertIn("MH1, R5", text)
        self.assertIn("On the board but not in the schematic: 2", text)
        self.assertIn("Net names: 4 in the schematic (3 labels, 1 power nets), 3 named nets on "
                      "the board, 2 match.", text)
        self.assertIn("not KiCad's own update-from-schematic", text)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertEqual([update["name"] for update in self.tools(result)], ["parity"])

    def test_help_needs_no_permission(self):
        result, client = self.turn("help")
        self.assertIn("built-in set of real KiCad demo projects", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(self.tools(result), [])

    def test_no_document_at_all_asks_for_one_without_reading(self):
        data = FakeData()
        result, client = self.turn("what is in the board?", data=data)
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Tell me which KiCad document to read", result["text"])
        self.assertIn("ecc83", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(data.calls, [])

    def test_a_comparison_with_one_url_asks_for_the_second(self):
        data = FakeData()
        result, _ = self.turn("compare https://example.test/a.kicad_pcb with the schematic", data=data)
        self.assertIn("a comparison needs both documents", result["text"])
        self.assertEqual(data.calls, [])

    def test_two_urls_of_the_same_kind_are_refused_before_reading(self):
        data = FakeData()
        result, _ = self.turn(
            "compare https://example.test/a.kicad_pcb to https://example.test/b.kicad_pcb", data=data)
        self.assertIn("one board and one schematic", result["text"])
        self.assertEqual(data.calls, [])

    def test_a_non_kicad_url_is_refused_before_permission(self):
        data = FakeData()
        result, client = self.turn("inspect https://example.test/drawing.dxf", data=data)
        self.assertIn("I only read KiCad", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(data.calls, [])

    def test_a_denied_permission_is_a_refusal_and_skips_the_read(self):
        data = FakeData()
        result, _ = self.turn("what is in the ecc83 board?", data=data, permission="reject-once")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])
        self.assertEqual(data.calls, [])
        updates = [update for update in result["updates"]
                   if update.get("sessionUpdate") == "tool_call_update"]
        self.assertEqual(updates[-1]["status"], "failed")

    def test_a_reader_failure_is_reported_not_swallowed(self):
        result, _ = self.turn("what is in the ecc83 board?", data=FakeData(raise_error=True))
        self.assertIn("GitLab offline", result["text"])
        self.assertIn("could not read", result["text"])

    def test_a_parse_failure_is_reported_too(self):
        result, _ = self.turn("what is in the ecc83 board?", data=FakeData(raise_value_error=True))
        self.assertIn("not a KiCad document", result["text"])

    def test_the_turn_streams_a_plan_and_closes_the_tool_call_with_a_summary(self):
        result, _ = self.turn("what is in the ecc83 board?")
        plans = [update for update in result["updates"] if update.get("sessionUpdate") == "plan"]
        self.assertTrue(plans)
        self.assertIn("board-summary", plans[0]["entries"][0]["content"])
        completed = [update for update in result["updates"]
                     if update.get("sessionUpdate") == "tool_call_update"
                     and update.get("status") == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertIn("4 footprints", completed[-1]["content"][0]["content"]["text"])

    def test_the_agent_declares_its_name_and_version(self):
        agent_conn, _ = connected_pair()
        agent = KicadAgent(agent_conn, FakeData())
        self.assertEqual(agent.name, "kicad")
        self.assertEqual(agent.version, "1.0.0")
        self.assertIn("KiCad", agent.title)

    def test_every_demo_in_the_catalog_has_both_documents_and_aliases(self):
        for key, sample in SAMPLES.items():
            self.assertTrue(sample["pcb"].endswith(".kicad_pcb"), key)
            self.assertTrue(sample["sch"].endswith(".kicad_sch"), key)
            self.assertTrue(sample["aliases"], key)
            self.assertTrue(sample["title"], key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
