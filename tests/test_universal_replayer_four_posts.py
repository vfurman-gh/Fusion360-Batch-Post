#!/usr/bin/env python3
"""
Comprehensive Test Suite: Universal Dumper & Universal Replayer across 4 Vendor Posts:
1. Centroid Turning (centroid_turning.cps)
2. Tormach PathPilot Mill (tormach.cps)
3. ShopBot OpenSBP Router (shopbot.cps)
4. Acu-Rite MillPwr G2 (acurite millpwr 2.cps)

Tests:
- Single-tool bit-for-bit passthrough parity (0 lines diff vs native Autodesk post execution).
- 2-tool merged setups with tool changes, multi-tool header tables, ATC tool preloading,
  circular arcs, canned/expanded cycles, rapid promotion (G0 / J3), and modal feedrate restoration.
"""

import copy
import difflib
import json
import os
import subprocess
import tempfile
import unittest

from context_parser import (
    apply_promotions_to_context,
    load_context_dump,
    merge_context_dumps,
    parse_context_dump,
)
from geometry.models import MotionKind, PromotedVectorRule
from pipeline import PipelineOrchestrator


STOCK_POSTS_DIR = (
    "/Users/vadimf/Library/Application Support/Autodesk/webdeploy/production/"
    "c139e2731e42a0340acc3d34de2cbdb01237a27c/Autodesk Fusion.app/Contents/"
    "Libraries/Applications/CAM360/Data/Posts"
)


class TestUniversalReplayerFourPosts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.orchestrator = PipelineOrchestrator(
            dumper_cps=os.path.join(cls.repo_root, "universal_dumper.cps"),
            replayer_cps=os.path.join(cls.repo_root, "universal_replayer.cps"),
        )
        cls.turning_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")
        cls.milling_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "milling_drill_test.cnc")

        cls.centroid_cps = os.path.join(cls.repo_root, "centroid_turning.cps")
        cls.tormach_cps = os.path.join(STOCK_POSTS_DIR, "tormach.cps")
        cls.shopbot_cps = os.path.join(STOCK_POSTS_DIR, "shopbot.cps")
        cls.millpwr_cps = os.path.join(STOCK_POSTS_DIR, "acurite millpwr 2.cps")

        for p in (cls.turning_cnc, cls.milling_cnc, cls.centroid_cps, cls.tormach_cps, cls.shopbot_cps, cls.millpwr_cps):
            if not os.path.isfile(p):
                raise unittest.SkipTest(f"Required fixture or vendor post not found: {p}")

    def _run_native_post(self, cps_path: str, cnc_path: str, out_dir: str) -> str:
        out_prefix = os.path.join(out_dir, "native_out")
        cmd = [self.orchestrator.post_bin_path, "--quiet", cps_path, cnc_path, out_prefix]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(
            res.returncode,
            0,
            f"Native post failed for {cps_path}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}",
        )
        for fname in sorted(os.listdir(out_dir)):
            if fname.startswith("native_out") and not fname.endswith(".log"):
                with open(os.path.join(out_dir, fname), "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
        self.fail(f"Native post did not emit output file for {cps_path}")

    def _assert_bit_for_bit_passthrough(self, cps_path: str, cnc_path: str):
        with tempfile.TemporaryDirectory() as tmp_dir:
            native_out = self._run_native_post(cps_path, cnc_path, tmp_dir)
            dump_json = os.path.join(tmp_dir, "dump.json")
            ctx = self.orchestrator.dump_context(cnc_path, cps_path, dump_json)
            replay_out_path = os.path.join(tmp_dir, "replayed_out.nc")
            replayed_out = self.orchestrator.replay_combined_context(
                combined_context=ctx,
                base_post_path=cps_path,
                bootstrap_cnc_path=cnc_path,
                final_cnc_path=replay_out_path,
            )
            if native_out != replayed_out:
                diff = "\n".join(
                    difflib.unified_diff(
                        native_out.splitlines(),
                        replayed_out.splitlines(),
                        fromfile="native",
                        tofile="replayed",
                        lineterm="",
                    )
                )
                self.fail(f"Passthrough diff non-zero for {os.path.basename(cps_path)}:\n{diff}")

    # =========================================================================
    # 1. Single-Tool Bit-for-Bit Passthrough Parity Tests
    # =========================================================================

    def test_01_passthrough_centroid_turning(self):
        """Verify 100% bit-for-bit identical output on Centroid Turning (centroid_turning.cps)."""
        self._assert_bit_for_bit_passthrough(self.centroid_cps, self.turning_cnc)

    def test_02_passthrough_tormach_mill(self):
        """Verify 100% bit-for-bit identical output on Tormach PathPilot (tormach.cps)."""
        self._assert_bit_for_bit_passthrough(self.tormach_cps, self.milling_cnc)

    def test_03_passthrough_shopbot_router(self):
        """Verify 100% bit-for-bit identical output on ShopBot OpenSBP (shopbot.cps)."""
        self._assert_bit_for_bit_passthrough(self.shopbot_cps, self.milling_cnc)

    def test_04_passthrough_acurite_millpwr_g2(self):
        """Verify 100% bit-for-bit identical output on Acu-Rite MillPwr G2 (acurite millpwr 2.cps)."""
        self._assert_bit_for_bit_passthrough(self.millpwr_cps, self.milling_cnc)

    # =========================================================================
    # 2. Multi-Tool (2-Tool Setup) Merge, Promotion & Replay Tests
    # =========================================================================

    def _build_two_tool_milling_batches(self, base_post_path: str, tmp_dir: str):
        """
        Create two single-tool milling context dumps:
        - Batch 0 (Tool 15): Peck drilling cycle (G83 / expanded cycle) + rapid retract.
        - Batch 1 (Tool 20): 1/2" flat endmill contour with high-feed positioning move,
          linear cutting moves, and a 90-degree CW circular arc in XY plane.
        """
        b0_json = os.path.join(tmp_dir, "batch_0.json")
        b0_ctx = self.orchestrator.dump_context(self.milling_cnc, base_post_path, b0_json)

        # Construct Batch 1 (Tool 20 Endmill with high-feed move + linear cut + CW arc)
        b1_ctx = copy.deepcopy(b0_ctx)
        sec1 = b1_ctx["sections"][0]
        sec1["id"] = 0
        sec1["initialPosition"] = {"x": 0.0, "y": 0.0, "z": 1.0}
        sec1["globalInitialPosition"] = {"x": 0.0, "y": 0.0, "z": 1.0}
        sec1["finalPosition"] = {"x": 0.75, "y": -0.25, "z": 1.0}
        sec1["zRangeMin"] = -0.25
        sec1["zRangeMax"] = 1.0
        sec1["initSpindleSpeed"] = 8000.0
        sec1["endSpindleSpeed"] = 8000.0
        sec1["hasAnyCycle"] = False
        sec1["params"]["operation-comment"] = "Contour2D_T20"
        sec1["params"]["operation-strategy"] = "contour2d"

        t20 = sec1["tool"]
        t20["number"] = 20
        t20["_toolId"] = "20"
        t20["compensationOffset"] = 20
        t20["lengthOffset"] = 20
        t20["diameterOffset"] = 20
        t20["type"] = 3  # TOOL_MILLING_END_FLAT
        t20["diameter"] = 0.5
        t20["cornerRadius"] = 0.0
        t20["taperAngle"] = 0.0
        t20["_taperAngle"] = 0.0
        t20["spindleRPM"] = 8000.0
        t20["description"] = "1/2 Flat Endmill"
        t20["_description"] = "1/2 Flat Endmill"
        t20["comment"] = "Finish Contour"
        t20["_comment"] = "Finish Contour"

        # Events for Tool 20:
        # 1. Rapid to (0, 0, 1.0)
        # 2. High-feed positioning traverse (0, 0, 1.0) -> (0, 0, 0.1) at F100 (candidate for G0/J3 promotion)
        # 3. Linear plunge (0, 0, 0.1) -> (0, 0, -0.25) at F15
        # 4. Linear cut (0, 0, -0.25) -> (0.5, 0, -0.25) at F30
        # 5. CW 90-deg circular arc in XY plane: center (0.5, -0.25, -0.25), end (0.75, -0.25, -0.25)
        # 6. High-feed retract (0.75, -0.25, -0.25) -> (0.75, -0.25, 1.0) at F100 (promoted to G0/J3)
        sec1["events"] = [
            {"type": "onParameter", "name": "operation-comment", "value": "Contour2D_T20"},
            {"type": "onMovement", "m": 0},
            {"type": "onRapid", "x": 0.0, "y": 0.0, "z": 1.0, "sx": 0.0, "sy": 0.0, "sz": 1.0, "movement": 0, "rc": 0, "rpm": 8000.0},
            {"type": "onMovement", "m": 5},
            {"type": "onLinear", "x": 0.0, "y": 0.0, "z": 0.1, "feed": 100.0, "sx": 0.0, "sy": 0.0, "sz": 1.0, "movement": 5, "rc": 0, "rpm": 8000.0, "sync": False},
            {"type": "onMovement", "m": 1},
            {"type": "onLinear", "x": 0.0, "y": 0.0, "z": -0.25, "feed": 15.0, "sx": 0.0, "sy": 0.0, "sz": 0.1, "movement": 1, "rc": 0, "rpm": 8000.0, "sync": False},
            {"type": "onLinear", "x": 0.5, "y": 0.0, "z": -0.25, "feed": 30.0, "sx": 0.0, "sy": 0.0, "sz": -0.25, "movement": 1, "rc": 0, "rpm": 8000.0, "sync": False},
            {
                "type": "onCircular",
                "cw": True,
                "cx": 0.5,
                "cy": -0.25,
                "cz": -0.25,
                "x": 0.75,
                "y": -0.25,
                "z": -0.25,
                "feed": 30.0,
                "sx": 0.5,
                "sy": 0.0,
                "sz": -0.25,
                "plane": 0,
                "radius": 0.25,
                "sweep": 1.5707963267948966,
                "nx": 0.0,
                "ny": 0.0,
                "nz": 1.0,
                "fullCircle": False,
                "helical": False,
                "spiral": False,
                "movement": 1,
                "rc": 0,
                "rpm": 8000.0,
                "linearizedMoves": [
                    {"type": "onLinear", "x": 0.6768, "y": -0.0732, "z": -0.25, "feed": 30.0, "sx": 0.5, "sy": 0.0, "sz": -0.25, "movement": 1, "rc": 0, "rpm": 8000.0, "sync": False},
                    {"type": "onLinear", "x": 0.75, "y": -0.25, "z": -0.25, "feed": 30.0, "sx": 0.6768, "sy": -0.0732, "sz": -0.25, "movement": 1, "rc": 0, "rpm": 8000.0, "sync": False},
                ],
            },
            {"type": "onMovement", "m": 5},
            {"type": "onLinear", "x": 0.75, "y": -0.25, "z": 1.0, "feed": 100.0, "sx": 0.75, "sy": -0.25, "sz": -0.25, "movement": 5, "rc": 0, "rpm": 8000.0, "sync": False},
        ]
        b1_json = os.path.join(tmp_dir, "batch_1.json")
        with open(b1_json, "w", encoding="utf-8") as f:
            json.dump(b1_ctx, f, indent=2)

        return b0_json, b1_json

    def test_05_two_tool_setup_centroid_turning(self):
        """
        Test 2-tool turning setup on Centroid Turning (centroid_turning.cps):
        - Batch 0: Tool 6 (Roughing + Finishing)
        - Batch 1: Tool 8 (Grooving / Secondary Finishing)
        Verifies unified header, T606 -> T808 tool change, promoted G0 moves,
        modal G1 feed restoration, circular arcs, and single M30 footer.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_json = os.path.join(tmp_dir, "batch_0.json")
            b0_ctx = self.orchestrator.dump_context(self.turning_cnc, self.centroid_cps, b0_json)

            # Create Batch 1 with Tool 8 (take the finishing section and assign Tool 8)
            b1_ctx = copy.deepcopy(b0_ctx)
            b1_ctx["sections"] = [copy.deepcopy(b0_ctx["sections"][-1])]
            b1_sec = b1_ctx["sections"][0]
            b1_sec["id"] = 0
            b1_sec["tool"]["number"] = 8
            b1_sec["tool"]["_toolId"] = "8"
            b1_sec["tool"]["compensationOffset"] = 8
            b1_sec["tool"]["lengthOffset"] = 8
            b1_sec["tool"]["diameterOffset"] = 8
            b1_sec["tool"]["description"] = "VNMG 331 Finish T8"
            b1_sec["tool"]["_description"] = "VNMG 331 Finish T8"
            b1_sec["params"]["operation-comment"] = "Profile Finishing T8"
            for ev in b1_sec["events"]:
                if ev.get("type") == "onParameter" and ev.get("name") == "operation-comment":
                    ev["value"] = "Profile Finishing T8"

            b1_json = os.path.join(tmp_dir, "batch_1.json")
            with open(b1_json, "w", encoding="utf-8") as f:
                json.dump(b1_ctx, f, indent=2)

            combined_ctx = merge_context_dumps([b0_json, b1_json])
            self.assertEqual(len(combined_ctx["sections"]), 4)

            segments = parse_context_dump(combined_ctx)
            # Run unified 2D IPW verifier across the 2-tool setup (Tool 6 Roughing/Finishing + Tool 8 Finishing)
            from geometry import Lathe2DIPWVerifier, ToolAssembly, AxisymmetricStock2D, WorkpieceDefinition
            from geometry.tool_generator import generate_rhombic_insert, generate_turning_holder
            from geometry.models import LatheToolGeometry, ToolCompensationQuadrant

            cutter = generate_rhombic_insert(edge_length=9.525, nose_radius=0.2032)
            holder = generate_turning_holder(shank_width=12.7, shank_length=100.0, is_boring=True)
            t6_asm = ToolAssembly(
                tool_id=6,
                name="Profile Roughing8",
                geometry=LatheToolGeometry(
                    insert_shape="rhombic",
                    nose_radius=0.2032,
                    hand="R",
                    quadrant=ToolCompensationQuadrant.P3,
                    cutter_polygon=cutter,
                    holder_polygon=holder,
                    is_boring=True,
                ),
            )
            t8_asm = ToolAssembly(
                tool_id=8,
                name="Profile Finishing T8",
                geometry=t6_asm.geometry,
            )
            tools_by_op = {
                "Profile Roughing8": t6_asm,
                "Profile Roughing8 (2)": t6_asm,
                "Profile Finishing6": t6_asm,
                "Profile Finishing T8": t8_asm,
            }
            workpiece = WorkpieceDefinition(
                stock=AxisymmetricStock2D(z_min=-63.5, z_max=0.0, outer_radius=15.875, inner_radius=6.35)
            )
            verifier = Lathe2DIPWVerifier(tool_scale_to_mm=1.0)
            report = verifier.verify_toolpath(segments, tools_by_op, workpiece)
            promoted_count = apply_promotions_to_context(combined_ctx, report.promoted_vectors)
            self.assertGreaterEqual(promoted_count, 5)

            out_nc = os.path.join(tmp_dir, "centroid_2tool.nc")
            gcode = self.orchestrator.replay_combined_context(
                combined_context=combined_ctx,
                base_post_path=self.centroid_cps,
                bootstrap_cnc_path=self.turning_cnc,
                final_cnc_path=out_nc,
            )

            # Verify both Tool 6 (T0606) and Tool 8 (T0808) are present in order
            idx_t6 = gcode.find("T0606")
            idx_t8 = gcode.find("T0808")
            self.assertNotEqual(idx_t6, -1, "Expected T0606 tool call in Batch 0")
            self.assertNotEqual(idx_t8, -1, "Expected T0808 tool call in Batch 1")
            self.assertLess(idx_t6, idx_t8, "T0606 must precede T0808")

            # Verify promoted G0 rapids and modal G1 restoration
            self.assertIn("N32 G0 X0.47", gcode)
            self.assertIn("N33 G1 Z-0.06 F0.002", gcode)
            self.assertIn("N102 T0600", gcode)
            self.assertIn("N106 T0800", gcode)

            # Verify circular arcs (G2/G3) exist in both tool sections
            self.assertIn("G2 X", gcode[:idx_t8])
            self.assertIn("G3 X", gcode[idx_t8:])

            # Verify onClose() cancels Tool 8 offset (T0800) and retracts home (G28 U0. / G28 W0.) at program end
            self.assertIn("N131 T0800", gcode)
            self.assertTrue(gcode.strip().endswith("G28 W0."))

    def test_06_two_tool_setup_tormach_mill(self):
        """
        Test 2-tool milling setup on Tormach PathPilot (tormach.cps):
        - Batch 0: Tool 15 (Drill with G83 canned cycle)
        - Batch 1: Tool 20 (Endmill with promoted G0 rapid, linear cut, and CW arc)
        Verifies multi-tool header table, ZMIN ranges, G30 tool change retract,
        forceModals() G17 G90 restoration, G83 cycle, G2 arc, and promoted G0.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_json, b1_json = self._build_two_tool_milling_batches(self.tormach_cps, tmp_dir)
            combined_ctx = merge_context_dumps([b0_json, b1_json])
            segments = parse_context_dump(combined_ctx)

            promoted_rules = {}
            for seg in segments:
                if seg.motion == MotionKind.LINEAR and seg.metadata.get("movement") == 5:
                    promoted_rules[seg.vector_key.to_wrapper_str()] = PromotedVectorRule(min_occur=1)
            promoted_count = apply_promotions_to_context(combined_ctx, promoted_rules)
            self.assertEqual(promoted_count, 2)

            out_nc = os.path.join(tmp_dir, "tormach_2tool.nc")
            gcode = self.orchestrator.replay_combined_context(
                combined_context=combined_ctx,
                base_post_path=self.tormach_cps,
                bootstrap_cnc_path=self.milling_cnc,
                final_cnc_path=out_nc,
            )

            # 1. Header tool table includes both T15 and T20 with ZMIN ranges
            self.assertIn("(T15", gcode)
            self.assertIn("(T20", gcode)
            self.assertIn("ZMIN=-0.25", gcode)

            # 2. Both tool changes use Tormach G30 retract + T.. G43 H.. M6
            self.assertIn("T15 G43 H15 M6", gcode)
            self.assertIn("T20 G43 H20 M6", gcode)
            self.assertGreaterEqual(gcode.count("G30"), 2)

            # 3. Section 0 emits G83 peck drilling cycle
            self.assertIn("G83", gcode)

            # 4. Section 1 re-emits G17 G90 after tool change via forceModals()
            sec1_part = gcode.split("T20 G43 H20 M6", 1)[1]
            self.assertIn("G17 G90", sec1_part)

            # 5. Section 1 promotes high-feed Z0.1 move to G0 and restores G1 Z-0.25 F15.
            self.assertIn("G0 Z0.1", sec1_part)
            self.assertIn("G1 Z-0.25 F15.", sec1_part)

            # 6. Section 1 emits G2 CW circular arc
            self.assertIn("G2 X0.75 Y-0.25", sec1_part)

            # 7. Single M30 footer
            self.assertEqual(gcode.count("M30"), 1)

    def test_07_two_tool_setup_shopbot_router(self):
        """
        Test 2-tool router setup on ShopBot OpenSBP (shopbot.cps):
        - Batch 0: Tool 15 (Drill with omitted onCyclePoint auto-expanded via expandCyclePoint)
        - Batch 1: Tool 20 (Endmill with promoted J3 rapid, M3 linear cut, and CG arc)
        Verifies multi-tool header, &Tool = 15 / &Tool = 20, expanded cycle points,
        promoted J3 rapid + M3 cutting move, CG arc, and single END footer.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_json, b1_json = self._build_two_tool_milling_batches(self.shopbot_cps, tmp_dir)
            combined_ctx = merge_context_dumps([b0_json, b1_json])
            segments = parse_context_dump(combined_ctx)

            promoted_rules = {}
            for seg in segments:
                if seg.motion == MotionKind.LINEAR and seg.metadata.get("movement") == 5:
                    promoted_rules[seg.vector_key.to_wrapper_str()] = PromotedVectorRule(min_occur=1)
            promoted_count = apply_promotions_to_context(combined_ctx, promoted_rules)
            self.assertEqual(promoted_count, 2)

            out_sbp = os.path.join(tmp_dir, "shopbot_2tool.sbp")
            sbp = self.orchestrator.replay_combined_context(
                combined_context=combined_ctx,
                base_post_path=self.shopbot_cps,
                bootstrap_cnc_path=self.milling_cnc,
                final_cnc_path=out_sbp,
            )

            # 1. Both tools listed in tool change assignments
            self.assertIn("&Tool = 15", sbp)
            self.assertIn("&Tool = 20", sbp)

            # 2. Section 1 after &Tool = 20 has promoted J3 rapid to Z0.1, then M3 plunge to Z-0.25
            sec1_part = sbp.split("&Tool = 20", 1)[1]
            self.assertIn("J3, 0, 0, 0.1", sec1_part)
            self.assertIn("M3, 0, 0, -0.25", sec1_part)
            self.assertIn("M3, 0.5, 0, -0.25", sec1_part)

            # 3. Section 1 emits ShopBot CG circular arc command
            self.assertIn("CG, , 0.75, -0.25, 0, -0.25, , 1", sec1_part)

            # 4. Promoted retract to Z1.0 is J3
            self.assertIn("J3, 0.75, -0.25, 1", sec1_part)

            # 5. UNIT_ERROR label appears only once at the end of the program
            self.assertEqual(sbp.count("UNIT_ERROR:"), 1)

    def test_08_two_tool_setup_acurite_millpwr_g2(self):
        """
        Test 2-tool milling setup on Acu-Rite MillPwr G2 (acurite millpwr 2.cps):
        - Batch 0: Tool 15 (Drill with omitted onCyclePoint auto-expanded + T20 preload)
        - Batch 1: Tool 20 (Endmill with T20 M6, promoted G0, G2 arc)
        Verifies cross-section getNextTool() ATC preloading, forceModals() G17 G90,
        promoted G0 + G1 feed restoration, and single M2 footer.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_json, b1_json = self._build_two_tool_milling_batches(self.millpwr_cps, tmp_dir)
            combined_ctx = merge_context_dumps([b0_json, b1_json])
            segments = parse_context_dump(combined_ctx)

            promoted_rules = {}
            for seg in segments:
                if seg.motion == MotionKind.LINEAR and seg.metadata.get("movement") == 5:
                    promoted_rules[seg.vector_key.to_wrapper_str()] = PromotedVectorRule(min_occur=1)
            promoted_count = apply_promotions_to_context(combined_ctx, promoted_rules)
            self.assertEqual(promoted_count, 2)

            out_nc = os.path.join(tmp_dir, "millpwr_2tool.nc")
            gcode = self.orchestrator.replay_combined_context(
                combined_context=combined_ctx,
                base_post_path=self.millpwr_cps,
                bootstrap_cnc_path=self.milling_cnc,
                final_cnc_path=out_nc,
            )

            # 1. Section 0 calls T15 M6 and immediately preloads T20 via getNextTool()
            self.assertIn("T15 M6", gcode)
            self.assertIn("T20 M6", gcode)
            sec0_part, sec1_part = gcode.split("T20 M6", 1)
            self.assertRegex(sec0_part, r"N\d+\s+T15\s+M6\s*\nN\d+\s+T20\b")

            # 2. Section 1 re-emits G17 G90 after tool change via forceModals()
            self.assertIn("G17 G90", sec1_part)

            # 3. Section 1 promotes high-feed Z0.1 move to G0 and restores G1 Z-0.25 F15
            self.assertRegex(sec1_part, r"N\d+\s+G0\s+Z0\.1\b")
            self.assertRegex(sec1_part, r"N\d+\s+G1\s+Z-0\.25\s+F15\b")

            # 4. Section 1 emits G2 CW circular arc
            self.assertIn("G2 X0.75 Y-0.25 I0 J-0.25", sec1_part)

            # 5. Single M2 program end
            self.assertEqual(gcode.count("M2\n"), 1)

    def test_09_post_properties_show_sequence_numbers_false(self):
        """
        Verify custom postProperties (e.g. showSequenceNumbers='false' on centroid_turning.cps)
        are respected by universal_replayer.cps even when vendor post defines setProperty(.current).
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_json = os.path.join(tmp_dir, "batch_0.json")
            ctx = self.orchestrator.dump_context(self.turning_cnc, self.centroid_cps, b0_json)
            ctx["postProperties"] = {"showSequenceNumbers": "false"}
            out_nc = os.path.join(tmp_dir, "centroid_no_seq.cnc")
            gcode = self.orchestrator.replay_combined_context(
                combined_context=ctx,
                base_post_path=self.centroid_cps,
                bootstrap_cnc_path=self.turning_cnc,
                final_cnc_path=out_nc,
            )
            self.assertIn("G98 G90", gcode)
            self.assertNotRegex(gcode, r"(?m)^N\d+\s+")

    def test_10_multi_section_milling_tormach_mill(self):
        """
        Verify multi-section milling replay on Tormach PathPilot (tormach.cps):
        Tests a 6-operation milling setup (such as setup OilLid) where successive
        operations query previousSection.getGlobalFinalToolAxis(), getFinalToolAxis(),
        and isNewWorkPlane() without errors.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_json = os.path.join(tmp_dir, "batch_0.json")
            b0_ctx = self.orchestrator.dump_context(self.milling_cnc, self.tormach_cps, b0_json)

            multi_ctx = copy.deepcopy(b0_ctx)
            base_sec = b0_ctx["sections"][0]
            multi_ctx["sections"] = []
            for i in range(6):
                s = copy.deepcopy(base_sec)
                s["id"] = i
                tool_num = 15 if i < 3 else 20
                s["tool"]["number"] = tool_num
                s["tool"]["_toolId"] = str(tool_num)
                s["tool"]["description"] = f"Tool {tool_num}"
                s["tool"]["_description"] = f"Tool {tool_num}"
                s["params"]["operation-comment"] = f"Milling_Op_{i}_T{tool_num}"
                multi_ctx["sections"].append(s)

            out_nc = os.path.join(tmp_dir, "tormach_6sec.nc")
            gcode = self.orchestrator.replay_combined_context(
                combined_context=multi_ctx,
                bootstrap_cnc_path=self.milling_cnc,
                base_post_path=self.tormach_cps,
                final_cnc_path=out_nc,
            )

            # All 6 operation comments must be emitted
            for i in range(6):
                tool_num = 15 if i < 3 else 20
                self.assertIn(f"(Milling_Op_{i}_T{tool_num})", gcode)

            # Both T15 and T20 must appear in header and tool calls
            self.assertIn("(T15", gcode)
            self.assertIn("(T20", gcode)
            self.assertIn("T15 G43 H15 M6", gcode)
            self.assertIn("T20 G43 H15 M6", gcode)
            self.assertEqual(gcode.count("M30"), 1)

    def test_11_oillid_exact_setup_simulation(self):
        """
        Faithfully mimic the user's exact OilLid milling setup on Tormach PathPilot:
        - Setup: OilLid
        - 1 Batch with Tool 2, 6 operations (single tool multi-operation)
        - Post: tormach.cps
        - Post properties: metric=True, safePositionMethod='G30', useSmoothing='-1',
          showSequenceNumbers='false'
        - Multi-section bootstrap CNC file (where C++ kernel finishes at section index > 0,
          exercising getPreviousSection(), isNewWorkPlane(), and defineWorkPlane())
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_json = os.path.join(tmp_dir, "batch_0.json")
            b0_ctx = self.orchestrator.dump_context(self.milling_cnc, self.tormach_cps, b0_json)

            multi_ctx = copy.deepcopy(b0_ctx)
            base_sec = b0_ctx["sections"][0]
            multi_ctx["programName"] = "1 OilLid"
            multi_ctx["sections"] = []
            for i in range(6):
                s = copy.deepcopy(base_sec)
                s["id"] = i
                s["tool"]["number"] = 2
                s["tool"]["_toolId"] = "2"
                s["tool"]["compensationOffset"] = 2
                s["tool"]["lengthOffset"] = 2
                s["tool"]["description"] = "1/4 Flat Endmill T2"
                s["tool"]["_description"] = "1/4 Flat Endmill T2"
                s["params"]["operation-comment"] = f"OilLid_Op_{i}"
                multi_ctx["sections"].append(s)

            multi_ctx["postProperties"] = {
                "metric": True,
                "group_configuration": True,
                "safePositionMethod": "G30",
                "useSmoothing": "-1",
                "showSequenceNumbers": "false",
            }

            out_nc = os.path.join(tmp_dir, "1_OilLid.nc")
            # Using self.turning_cnc (which has 3 sections) simulates captured.intermediate.cnc
            # having multiple sections, ensuring getPreviousSection() and isNewWorkPlane()
            # execute without 'can't access property workPlane, _section is undefined'.
            gcode = self.orchestrator.replay_combined_context(
                combined_context=multi_ctx,
                bootstrap_cnc_path=self.turning_cnc,
                base_post_path=self.tormach_cps,
                final_cnc_path=out_nc,
            )

            # Verify all 6 operations are present
            for i in range(6):
                self.assertIn(f"(OilLid_Op_{i})", gcode)

            # Tool 2 call is emitted
            self.assertIn("T2 G43 H2 M6", gcode)
            # Safe position retract G30 is emitted
            self.assertIn("G30", gcode)
            # Clean M30 program termination
            self.assertEqual(gcode.count("M30"), 1)


if __name__ == "__main__":
    unittest.main()
