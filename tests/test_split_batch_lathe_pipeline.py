"""
Unit tests for the split-batch lathe pipeline (Context-Dump + Merge + Unified Verification + Replay):
1. Multi-batch context merging and re-indexing via merge_context_dumps.
2. Continuous MotionSegment parsing across batches via parse_context_dump.
3. Global promotion evaluation and direct event annotation via apply_promotions_to_context.
4. Single-pass replay of a multi-tool merged context via universal_replayer.cps.
"""

import copy
import json
import os
import sys
import tempfile
import unittest

from context_parser import (
    apply_promotions_to_context,
    merge_context_dumps,
    parse_context_dump,
)
from geometry import Lathe2DIPWVerifier, ToolAssembly, AxisymmetricStock2D, WorkpieceDefinition
from geometry.tool_generator import generate_rhombic_insert, generate_turning_holder
from geometry.models import LatheToolGeometry, ToolCompensationQuadrant
from pipeline import PipelineOrchestrator


class TestSplitBatchLathePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")
        cls.centroid_cps = os.path.join(cls.repo_root, "centroid_turning.cps")
        cls.orchestrator = PipelineOrchestrator(
            dumper_cps=os.path.join(cls.repo_root, "universal_dumper.cps"),
            replayer_cps=os.path.join(cls.repo_root, "universal_replayer.cps"),
        )

    def test_01_merge_and_parse_context_batches_tags_metadata(self):
        """Verify merge_context_dumps and parse_context_dump set batch_idx and sec_id monotonically."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_path = os.path.join(tmp_dir, "batch_0.json")
            b0_ctx = self.orchestrator.dump_context(self.fixture_cnc, self.centroid_cps, b0_path)

            combined_ctx = merge_context_dumps([b0_ctx, b0_ctx])
            self.assertEqual(len(combined_ctx["sections"]), len(b0_ctx["sections"]) * 2)

            # Verify section IDs are monotonically re-indexed 0..N-1
            for idx, sec in enumerate(combined_ctx["sections"]):
                self.assertEqual(sec["id"], idx)

            segments = parse_context_dump(combined_ctx)
            self.assertGreater(len(segments), 20, "Expected non-trivial motion segments")

            b0_segs = [s for s in segments if s.metadata.get("batch_idx") == 0]
            b1_segs = [s for s in segments if s.metadata.get("batch_idx") == 1]

            self.assertEqual(len(b0_segs), len(segments) // 2)
            self.assertEqual(len(b1_segs), len(segments) // 2)

            for i in range(1, len(segments)):
                self.assertEqual(segments[i].segment_id, segments[i - 1].segment_id + 1)

    def test_02_global_promotion_annotation_across_batches(self):
        """Verify global promotion decisions are stamped onto events across all merged batches."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_path = os.path.join(tmp_dir, "batch_0.json")
            b0_ctx = self.orchestrator.dump_context(self.fixture_cnc, self.centroid_cps, b0_path)

            b1_ctx = copy.deepcopy(b0_ctx)
            for sec in b1_ctx["sections"]:
                sec["tool"]["number"] = 8
                sec["tool"]["_toolId"] = "8"
                sec["tool"]["compensationOffset"] = 8

            combined_ctx = merge_context_dumps([b0_ctx, b1_ctx])
            segments = parse_context_dump(combined_ctx)

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
                "Profile Finishing6": tool_asm,
            }

            workpiece = WorkpieceDefinition(
                stock=AxisymmetricStock2D(z_min=-75.0, z_max=0.0, outer_radius=16.5, inner_radius=0.0)
            )

            verifier = Lathe2DIPWVerifier(tool_scale_to_mm=1.0)
            report = verifier.verify_toolpath(segments, tools_by_op, workpiece)
            self.assertGreater(len(report.promoted_vectors), 0, "Expected authorized air promotions")

            promoted_cnt = apply_promotions_to_context(combined_ctx, report.promoted_vectors)
            self.assertGreater(promoted_cnt, 0)

            # Verify both batch 0 and batch 1 sections received promoted events
            b0_promoted = sum(
                1
                for sec in combined_ctx["sections"]
                if sec.get("batchIdx") == 0
                for ev in sec.get("events", [])
                if ev.get("promote")
            )
            b1_promoted = sum(
                1
                for sec in combined_ctx["sections"]
                if sec.get("batchIdx") == 1
                for ev in sec.get("events", [])
                if ev.get("promote")
            )
            self.assertGreater(b0_promoted, 0)
            self.assertGreater(b1_promoted, 0)

    def test_03_single_pass_replay_combines_multi_tool_batches(self):
        """Verify replay_combined_context emits a unified multi-tool program without text stitching."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            b0_path = os.path.join(tmp_dir, "batch_0.json")
            b0_ctx = self.orchestrator.dump_context(self.fixture_cnc, self.centroid_cps, b0_path)

            b1_ctx = copy.deepcopy(b0_ctx)
            b1_ctx["sections"] = [copy.deepcopy(b0_ctx["sections"][-1])]
            b1_sec = b1_ctx["sections"][0]
            b1_sec["id"] = 0
            b1_sec["tool"]["number"] = 2
            b1_sec["tool"]["_toolId"] = "2"
            b1_sec["tool"]["compensationOffset"] = 2
            b1_sec["tool"]["lengthOffset"] = 2
            b1_sec["tool"]["diameterOffset"] = 2

            combined_ctx = merge_context_dumps([b0_ctx, b1_ctx])
            final_nc = os.path.join(tmp_dir, "combined_out.nc")
            gcode = self.orchestrator.replay_combined_context(
                combined_context=combined_ctx,
                base_post_path=self.centroid_cps,
                bootstrap_cnc_path=self.fixture_cnc,
                final_cnc_path=final_nc,
            )

            self.assertIn("T0606", gcode)
            self.assertIn("T0202", gcode)
            self.assertLess(gcode.find("T0606"), gcode.find("T0202"))

    def test_04_dynamic_css_feed_and_rpm_scaling_and_constant_mrr(self):
        """Verify dynamic CSS calculates varying RPM/feedrate per radius and maintains constant MRR."""
        context = {
            "unit": 1,  # Metric (mm)
            "highFeedrate": 0,
            "maximumSpindleSpeed": 2000.0,
            "sections": [
                {
                    "id": 0,
                    "type": 1,  # TYPE_TURNING
                    "feedMode": 1,  # FEED_PER_REVOLUTION
                    "strategy": "turningProfileRoughing",
                    "params": {
                        "operation-comment": "Roughing CSS",
                        "operation:tool_useConstantSurfaceSpeed": 1,
                        "operation:tool_surfaceSpeed": 76000.0,  # 76 m/min = 76000 mm/min
                        "operation:tool_maximumSpindleSpeed": 2000.0,
                        "operation:tool_feedCuttingRel": 0.127,  # mm/rev
                    },
                    "tool": {
                        "number": 1,
                        "_isTurningTool": True,
                        "_spindleMode": 1,  # SPINDLE_CONSTANT_SURFACE_SPEED
                        "surfaceSpeed": 76000.0,
                        "maximumSpindleSpeed": 2000.0,
                    },
                    "events": [
                        # Pass 1: R=14.0 mm (unclamped: N = 76000 / (2*pi*14) ≈ 863.99 RPM)
                        {"type": "onLinear", "sx": 14.0, "sy": 0.0, "sz": 0.0, "x": 14.0, "y": 0.0, "z": -20.0, "feed": 0.127},
                        # Pass 2: R=7.0 mm (unclamped: N = 76000 / (2*pi*7) ≈ 1727.97 RPM)
                        {"type": "onLinear", "sx": 7.0, "sy": 0.0, "sz": 0.0, "x": 7.0, "y": 0.0, "z": -20.0, "feed": 0.127},
                        # Pass 3: R=2.0 mm (clamped: ideal N ≈ 6047.88 RPM -> clamped to 2000.0 RPM)
                        {"type": "onLinear", "sx": 2.0, "sy": 0.0, "sz": 0.0, "x": 2.0, "y": 0.0, "z": -20.0, "feed": 0.127},
                    ],
                }
            ],
        }

        segments = parse_context_dump(context)
        self.assertEqual(len(segments), 3)

        # Pass 1: R = 14 mm
        self.assertAlmostEqual(segments[0].spindle_rpm, 76000.0 / (2 * 3.1415926535 * 14.0), delta=1.0)
        self.assertAlmostEqual(segments[0].feedrate_mmpm, 0.127 * segments[0].spindle_rpm, delta=0.5)

        # Pass 2: R = 7 mm
        self.assertAlmostEqual(segments[1].spindle_rpm, 76000.0 / (2 * 3.1415926535 * 7.0), delta=1.0)
        self.assertAlmostEqual(segments[1].feedrate_mmpm, 0.127 * segments[1].spindle_rpm, delta=0.5)
        self.assertGreater(segments[1].spindle_rpm, segments[0].spindle_rpm)
        self.assertGreater(segments[1].feedrate_mmpm, segments[0].feedrate_mmpm)

        # Pass 3: R = 2 mm (clamped to 2000 RPM)
        self.assertEqual(segments[2].spindle_rpm, 2000.0)
        self.assertAlmostEqual(segments[2].feedrate_mmpm, 0.127 * 2000.0, delta=0.5)

    def test_05_max_rpm_propagation_hierarchy(self):
        """Verify max spindle RPM resolves from NCProgram postProperties, root context, or tool/op params."""
        # 1. Resolved from NCProgram postProperties
        ctx_nc = {
            "unit": 1,
            "postProperties": {"maximumSpindleSpeed": 1850.0},
            "sections": [
                {
                    "id": 0, "type": 1, "feedMode": 1,
                    "params": {"operation:tool_surfaceSpeed": 76000.0},
                    "tool": {"_isTurningTool": True, "_spindleMode": 1},
                    "events": [{"type": "onLinear", "sx": 1.0, "sy": 0.0, "sz": 0.0, "x": 1.0, "y": 0.0, "z": -10.0, "feed": 0.1}],
                }
            ],
        }
        segs_nc = parse_context_dump(ctx_nc)
        self.assertEqual(segs_nc[0].spindle_rpm, 1850.0)

        # 2. Resolved from root maximumSpindleSpeed
        ctx_root = {
            "unit": 1,
            "maximumSpindleSpeed": 2250.0,
            "sections": [
                {
                    "id": 0, "type": 1, "feedMode": 1,
                    "params": {"operation:tool_surfaceSpeed": 76000.0},
                    "tool": {"_isTurningTool": True, "_spindleMode": 1},
                    "events": [{"type": "onLinear", "sx": 1.0, "sy": 0.0, "sz": 0.0, "x": 1.0, "y": 0.0, "z": -10.0, "feed": 0.1}],
                }
            ],
        }
        segs_root = parse_context_dump(ctx_root)
        self.assertEqual(segs_root[0].spindle_rpm, 2250.0)

        # 3. Operation-specific parameter overrides global post property
        ctx_op_override = {
            "unit": 1,
            "maximumSpindleSpeed": 3000.0,
            "sections": [
                {
                    "id": 0, "type": 1, "feedMode": 1,
                    "params": {"operation:tool_surfaceSpeed": 76000.0, "operation:tool_maximumSpindleSpeed": 1500.0},
                    "tool": {"_isTurningTool": True, "_spindleMode": 1},
                    "events": [{"type": "onLinear", "sx": 1.0, "sy": 0.0, "sz": 0.0, "x": 1.0, "y": 0.0, "z": -10.0, "feed": 0.1}],
                }
            ],
        }
        segs_op = parse_context_dump(ctx_op_override)
        self.assertEqual(segs_op[0].spindle_rpm, 1500.0)


if __name__ == "__main__":
    unittest.main()
