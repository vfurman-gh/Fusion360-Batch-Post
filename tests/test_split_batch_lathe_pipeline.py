"""
Unit tests for the redesigned split-batch lathe pipeline:
1. Multi-batch intermediate APT parsing via parse_apt_batches.
2. Global promotion evaluation with per-batch partitioning.
3. Correct vector key mapping for universal_wrapper.cps.
4. Deterministic G-code program stitching via _StitchGcodeFiles.
"""

import json
import os
import sys
import tempfile
import types
import unittest

if "adsk" not in sys.modules:
    adsk = types.ModuleType("adsk")
    adsk.cam = types.ModuleType("adsk.cam")
    adsk.cam.OperationTypes = types.SimpleNamespace(TurningOperation=1, MillingOperation=0)
    adsk.core = types.ModuleType("adsk.core")
    adsk.core.CommandCreatedEventHandler = type("CommandCreatedEventHandler", (), {})
    adsk.core.InputChangedEventHandler = type("InputChangedEventHandler", (), {})
    adsk.core.ValidateInputsEventHandler = type("ValidateInputsEventHandler", (), {})
    adsk.core.CommandEventHandler = type("CommandEventHandler", (), {})
    adsk.core.Application = types.SimpleNamespace(get=lambda: types.SimpleNamespace(log=lambda msg: None))
    adsk.fusion = types.ModuleType("adsk.fusion")
    sys.modules["adsk"] = adsk
    sys.modules["adsk.cam"] = adsk.cam
    sys.modules["adsk.core"] = adsk.core
    sys.modules["adsk.fusion"] = adsk.fusion

from apt_parser import parse_apt_batches, APTParser
from geometry import Lathe2DIPWVerifier, ToolAssembly, AxisymmetricStock2D, WorkpieceDefinition
from geometry.tool_generator import generate_rhombic_insert, generate_turning_holder
from geometry.models import LatheToolGeometry, ToolCompensationQuadrant
from PostProcessAll import _StitchGcodeFiles


class TestSplitBatchLathePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.fixture_apt = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.apt")

    def test_01_parse_apt_batches_tags_metadata(self):
        """Verify parse_apt_batches sets batch_idx and batch_sec_id monotonically."""
        batches = [self.fixture_apt, self.fixture_apt]
        segments = parse_apt_batches(batches)

        self.assertGreater(len(segments), 10, "Expected non-trivial motion segments")
        
        # Verify first half has batch_idx == 0 and second half has batch_idx == 1
        b0_segs = [s for s in segments if s.metadata.get("batch_idx") == 0]
        b1_segs = [s for s in segments if s.metadata.get("batch_idx") == 1]
        
        self.assertEqual(len(b0_segs), len(segments) // 2)
        self.assertEqual(len(b1_segs), len(segments) // 2)
        
        # Verify segment_id is strictly monotonically increasing across all batches
        for i in range(1, len(segments)):
            self.assertEqual(segments[i].segment_id, segments[i - 1].segment_id + 1)

    def test_02_global_promotion_partitioning_by_batch(self):
        """Verify global promotion decisions are correctly partitioned into per-batch rules."""
        cutter = generate_rhombic_insert(edge_length=9.525, nose_radius=0.4)
        holder = generate_turning_holder(shank_width=16.0, shank_length=100.0)
        tool_geom = LatheToolGeometry(
            insert_shape="rhombic",
            nose_radius=0.4,
            hand="R",
            quadrant=ToolCompensationQuadrant.P3,
            cutter_polygon=cutter,
            holder_polygon=holder,
            is_boring=False,
        )
        tool_asm = ToolAssembly(tool_id=6, name="Profile Roughing8", geometry=tool_geom)
        tools_by_op = {
            "Profile Roughing8": tool_asm,
            "Profile Roughing8 (2)": tool_asm,
        }

        workpiece = WorkpieceDefinition(
            stock=AxisymmetricStock2D(z_min=-75.0, z_max=0.0, outer_radius=16.5, inner_radius=0.0)
        )

        segments = parse_apt_batches([self.fixture_apt, self.fixture_apt])
        verifier = Lathe2DIPWVerifier(tool_scale_to_mm=1.0)
        report = verifier.verify_toolpath(segments, tools_by_op, workpiece)

        self.assertGreater(len(report.promoted_vectors), 0, "Expected authorized air promotions")

        # Partition promotion rules per batch
        batch_promoted = {0: {}, 1: {}}
        for seg in segments:
            b_idx = seg.metadata.get("batch_idx", 0)
            v_key_str = seg.vector_key.to_wrapper_str()
            if v_key_str in report.promoted_vectors:
                rule = report.promoted_vectors[v_key_str]
                batch_promoted[b_idx][v_key_str] = {"minOccur": rule.min_occur}

        # Verify each batch gets non-empty promotion rules
        self.assertGreater(len(batch_promoted[0]), 0)
        self.assertGreater(len(batch_promoted[1]), 0)

        # Verify vector keys start with local section IDs matching universal_wrapper format
        for v_key in batch_promoted[0]:
            self.assertRegex(v_key, r"^\d+:-?\d+,-?\d+,-?\d+->-?\d+,-?\d+,-?\d+$")

    def test_03_stitch_gcode_files(self):
        """Verify _StitchGcodeFiles combines single-tool files with headers, tool changes, and tails."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            f1 = os.path.join(tmp_dir, "batch_0.cnc")
            f2 = os.path.join(tmp_dir, "batch_1.cnc")
            f_out = os.path.join(tmp_dir, "final.cnc")

            gcode_b0 = (
                "%\n"
                ";SPINNER TURNING TEST\n"
                ";T0101 NR=0.0156\n"
                "G20\n"
                "G50 S2000\n"
                "T0100\n"
                "G54\n"
                "N10 G0 X1.3 Z0.04 T0101\n"
                "N20 G1 X1.22 Z0. F0.005\n"
                "N30 G0 X1.3\n"
                "M30\n"
                "%\n"
            )
            gcode_b1 = (
                "%\n"
                ";SPINNER OP 2\n"
                ";T0202 NR=0.008\n"
                "G20\n"
                "T0200\n"
                "G54\n"
                "N10 G0 X0.5 Z0.04 T0202\n"
                "N20 G1 Z-1.0 F0.003\n"
                "N30 G0 Z0.04\n"
                "M30\n"
                "%\n"
            )

            with open(f1, "w", encoding="utf-8") as f:
                f.write(gcode_b0)
            with open(f2, "w", encoding="utf-8") as f:
                f.write(gcode_b1)

            docSettings = {
                "endCodes": "M30:M2",
                "toolChange": "M0 (CHANGE TO NEXT TOOL)",
            }

            _StitchGcodeFiles([f1, f2], f_out, "Spinner", docSettings)

            self.assertTrue(os.path.isfile(f_out))
            with open(f_out, "r", encoding="utf-8") as f:
                stitched = f.read()

            # Header from Batch 0 is preserved
            self.assertIn(";SPINNER TURNING TEST", stitched)
            self.assertIn("G50 S2000", stitched)

            # Tool 1 motions are present
            self.assertIn("G0 X1.3 Z0.04 T0101", stitched)

            # Tool change block is injected
            self.assertIn("M0 (CHANGE TO NEXT TOOL)", stitched)

            # Tool 2 motions are present
            self.assertIn("G0 X0.5 Z0.04 T0202", stitched)
            self.assertIn("G1 Z-1.0 F0.003", stitched)

            # Only ONE program tail (M30, %) at the end
            self.assertEqual(stitched.count("M30"), 1)
            self.assertEqual(stitched.count("%"), 2)  # One at start, one at end


if __name__ == "__main__":
    unittest.main()
