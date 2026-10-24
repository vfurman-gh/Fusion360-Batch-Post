#!/usr/bin/env python3
"""
Test Suite: End-to-End Pipeline Integration with Mock Verification Engine.

Tests:
1. Full 6-stage pipeline execution:
   Intermediate .cnc -> apt.cps -> APTParser -> MockEngine -> promoted.json -> universal_wrapper.cps -> final .cnc + report.md.
2. Verified restoration of G0 rapid retracts and re-arming of G1 and feedrates in final output.
3. Verification report generation and metric accuracy.
"""

import os
import tempfile
import unittest
from typing import Dict, List

from geometry.models import (
    MotionSegment,
    MoveEvaluation,
    ToolAssembly,
    LatheToolGeometry,
    ToolCompensationQuadrant,
    AxisymmetricStock2D,
    WorkpieceDefinition,
    VerificationReport,
    PromotedVectorRule,
)
from geometry.engine_interface import IVerificationEngine
from pipeline import PipelineOrchestrator
from shapely.geometry import Polygon


class PipelineMockVerifier(IVerificationEngine):
    """
    Mock engine that flags known clearance retracts as air moves:
    Specifically, any retract where tool moves from outer cut (X > 10 mm)
    inward towards clearance (X ~ 5.969 mm) at constant Z.
    """

    def __init__(self):
        self.moves: List[MotionSegment] = []

    def reset(self, workpiece: WorkpieceDefinition) -> None:
        self.moves = []

    def load_tool(self, tool: ToolAssembly) -> None:
        pass

    def step(self, move: MotionSegment) -> MoveEvaluation:
        self.moves.append(move)
        # Retract move signature in roughing_test fixture:
        # Move starts at X > 10.0 and ends at X < 7.0 at same Z
        is_air = move.start.x > 10.0 and move.end.x < 7.0 and abs(move.start.z - move.end.z) < 1e-3
        return MoveEvaluation(
            segment_id=move.segment_id,
            vector_key=move.vector_key.to_wrapper_str(),
            is_air_move=is_air,
            material_removed_mm3=0.0 if is_air else 25.0,
            instantaneous_mrr=0.0 if is_air else 250.0,
        )

    def generate_report(self) -> VerificationReport:
        promoted: Dict[str, PromotedVectorRule] = {}
        for m in self.moves:
            # Check retract signature
            if m.start.x > 10.0 and m.end.x < 7.0 and abs(m.start.z - m.end.z) < 1e-3:
                promoted[m.vector_key.to_wrapper_str()] = PromotedVectorRule(min_occur=1)

        return VerificationReport(
            total_machining_time_sec=142.5,
            total_volume_removed_mm3=1250.0,
            collisions=[],
            promoted_vectors=promoted,
            mrr_timeline=[],
            mrr_spikes=[],
        )


class TestFullPipelineMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")
        cls.base_cps = os.path.join(cls.repo_root, "centroid_turning.cps")

        if not os.path.isfile(cls.fixture_cnc) or not os.path.isfile(cls.base_cps):
            raise unittest.SkipTest("Prerequisites not found for pipeline test")

    def test_end_to_end_pipeline_with_mock_engine(self):
        """Execute complete pipeline from intermediate .cnc to final .cnc and report."""
        orchestrator = PipelineOrchestrator()

        stock = AxisymmetricStock2D(z_min=-2.5, z_max=0.0, outer_radius=0.625)
        workpiece = WorkpieceDefinition(stock=stock)

        dummy_poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        tool6 = ToolAssembly(
            tool_id=6,
            name="Profile Boring Tool",
            geometry=LatheToolGeometry(
                insert_shape="C",
                nose_radius=0.008,
                hand="R",
                quadrant=ToolCompensationQuadrant.P3,
                cutter_polygon=dummy_poly,
                holder_polygon=dummy_poly,
            ),
        )
        tools_by_op = {
            "Profile Roughing8": tool6,
            "Profile Roughing8 (2)": tool6,
            "Profile Finishing6": tool6,
        }

        verifier = PipelineMockVerifier()

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_cnc = os.path.join(tmp_dir, "final_machined.cnc")
            out_report = os.path.join(tmp_dir, "verification_report.md")

            report, gcode = orchestrator.run(
                intermediate_cnc_path=self.fixture_cnc,
                base_post_path=self.base_cps,
                workpiece=workpiece,
                tools_by_op=tools_by_op,
                verifier=verifier,
                final_cnc_path=out_cnc,
                report_output_path=out_report,
            )

            # 1. Assertions on VerificationReport
            self.assertEqual(len(report.collisions), 0, "Expected zero collisions")
            self.assertGreater(len(report.promoted_vectors), 0, "Expected air moves to be promoted")
            self.assertIn("0:133870,0,2540->59690,0,2540", report.promoted_vectors)

            # 2. Assertions on Final G-Code
            self.assertTrue(os.path.isfile(out_cnc))
            self.assertIn("N32 G0 X0.47 ;RAPID RESTORED", gcode, "Expected promoted G0 move with inline comment in block N32")
            self.assertIn("N33 G1 Z-0.06 F0.002", gcode, "Expected restored modal G1 on block N33")
            self.assertIn(";UNIVERSAL WRAPPER RESTORED", gcode)

            # 3. Assertions on Markdown Report
            self.assertTrue(os.path.isfile(out_report))
            with open(out_report, "r") as f:
                report_content = f.read()

            self.assertIn("# Toolpath Verification & Rapid-Promotion Report", report_content)
            self.assertIn("Zero collisions detected", report_content)
            self.assertNotIn("Rapid-Promotion Authorization Rules", report_content)


if __name__ == "__main__":
    unittest.main()
