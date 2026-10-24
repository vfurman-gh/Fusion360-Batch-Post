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


if __name__ == "__main__":
    unittest.main()
