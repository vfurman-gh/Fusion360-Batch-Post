#!/usr/bin/env python3
"""
Test Suite: ISO 4343 APT Parser and Verification Engine Orchestration.

Validates:
1. Parsing of APT files into strongly-typed MotionSegment sequences.
2. Coordinate normalization and metric scale enforcement (mm, mm/min).
3. Section/operation grouping and tool tracking.
4. Exact spatial VectorKey generation matching universal_wrapper.cps.
5. Circular arc (MOVARC) parsing and normal determination.
6. End-to-end integration with IVerificationEngine via verify_apt_toolpath.
"""

import os
import unittest
from typing import Dict, List

from geometry.models import (
    MotionKind,
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
from apt_parser import APTParser, parse_apt_file, verify_apt_toolpath
from shapely.geometry import Polygon


class MockVerificationEngine(IVerificationEngine):
    """Mock engine recording moves and generating synthetic promotion report."""

    def __init__(self):
        self.reset_called = False
        self.loaded_tools: List[ToolAssembly] = []
        self.processed_moves: List[MotionSegment] = []

    def reset(self, workpiece: WorkpieceDefinition) -> None:
        self.reset_called = True
        self.workpiece = workpiece

    def load_tool(self, tool: ToolAssembly) -> None:
        self.loaded_tools.append(tool)

    def step(self, move: MotionSegment) -> MoveEvaluation:
        self.processed_moves.append(move)
        # Mock logic: if move starts at X > 10 and moves down to X ~ 5.969, mark as air
        is_air = move.start.x > 10.0 and move.end.x < 7.0 and abs(move.start.z - move.end.z) < 1e-3
        return MoveEvaluation(
            segment_id=move.segment_id,
            vector_key=move.vector_key.to_wrapper_str(),
            is_air_move=is_air,
            material_removed_mm3=0.0 if is_air else 15.0,
            instantaneous_mrr=0.0 if is_air else 150.0,
        )

    def generate_report(self) -> VerificationReport:
        promoted: Dict[str, PromotedVectorRule] = {}
        for m in self.processed_moves:
            if m.start.x > 10.0 and m.end.x < 7.0 and abs(m.start.z - m.end.z) < 1e-3:
                promoted[m.vector_key.to_wrapper_str()] = PromotedVectorRule(min_occur=1)

        return VerificationReport(
            total_machining_time_sec=120.0,
            total_volume_removed_mm3=500.0,
            collisions=[],
            promoted_vectors=promoted,
            mrr_timeline=[],
            mrr_spikes=[],
        )


class TestAPTParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.apt_fixture = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.apt")
        if not os.path.isfile(cls.apt_fixture):
            raise unittest.SkipTest(f"Fixture not found: {cls.apt_fixture}")

    def test_01_parse_apt_fixture(self):
        """Verify parsing of real-world APT fixture into MotionSegments."""
        parser = APTParser()
        segments = parser.parse_file(self.apt_fixture)

        self.assertGreater(len(segments), 50, "Expected at least 50 kinematic moves")

        # Verify operations detected
        operations = list(dict.fromkeys(s.op_id for s in segments))
        self.assertEqual(len(operations), 3)
        self.assertIn("Profile Roughing8", operations)
        self.assertIn("Profile Roughing8 (2)", operations)
        self.assertIn("Profile Finishing6", operations)

        # Verify tool tracking
        for seg in segments:
            self.assertEqual(seg.metadata.get("tool_id"), 6)

    def test_02_vector_key_metric_precision(self):
        """Verify Move 7 (retract move in roughing) matches exact wrapper vector key."""
        segments = parse_apt_file(self.apt_fixture)

        # Find the retract move from (13.38701, 0, 0.254) to (5.969, 0, 0.254)
        retract_move = None
        for s in segments:
            if s.sec_id == 0 and abs(s.start.x - 13.38701) < 1e-3 and abs(s.end.x - 5.969) < 1e-3:
                retract_move = s
                break

        self.assertIsNotNone(retract_move, "Retract move not found in parsed APT")
        wrapper_key = retract_move.vector_key.to_wrapper_str()

        # Must match 0:133870,0,2540->59690,0,2540 exactly
        self.assertEqual(wrapper_key, "0:133870,0,2540->59690,0,2540")

    def test_03_movarc_circular_interpolation(self):
        """Verify circular moves (MOVARC) parse center, radius, and arc motion kind."""
        segments = parse_apt_file(self.apt_fixture)
        arc_moves = [s for s in segments if s.is_arc]

        self.assertGreater(len(arc_moves), 5, "Expected at least 5 arc segments")

        arc0 = arc_moves[0]
        self.assertTrue(arc0.is_arc)
        self.assertIsNotNone(arc0.arc_center)
        self.assertAlmostEqual(arc0.arc_center.x, 14.122171, places=4)
        self.assertAlmostEqual(arc0.arc_center.z, -0.457225, places=4)
        self.assertEqual(arc0.arc_plane, "ZX")
        self.assertIn(arc0.motion, (MotionKind.ARC_CW, MotionKind.ARC_CCW))

    def test_04_verify_apt_toolpath_orchestration(self):
        """Verify verify_apt_toolpath successfully coordinates parser and engine."""
        mock_engine = MockVerificationEngine()
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

        report = verify_apt_toolpath(self.apt_fixture, mock_engine, workpiece, tools_by_op)

        self.assertTrue(mock_engine.reset_called)
        self.assertEqual(len(mock_engine.loaded_tools), 3)
        self.assertGreater(len(mock_engine.processed_moves), 50)

        # Verify promoted vectors output
        self.assertIn("0:133870,0,2540->59690,0,2540", report.promoted_vectors)
        rule = report.promoted_vectors["0:133870,0,2540->59690,0,2540"]
        self.assertEqual(rule.min_occur, 1)


if __name__ == "__main__":
    unittest.main()
