#!/usr/bin/env python3
"""
Unit and Integration Tests for Milling Verification Engine (`MillClearanceVerifier`)
and Milling Domain Extraction (`context_parser.py`).
"""

import unittest
from typing import Dict, List

from geometry.models import (
    BoxStock3D,
    MillToolGeometry,
    MotionKind,
    MotionSegment,
    ToolAssembly,
    Vector3D,
    VectorKey,
    WorkpieceDefinition,
)
from geometry.mill_engine import MillClearanceVerifier
from context_parser import extract_workpiece_from_context, extract_tools_from_context


class TestMillClearanceVerifier(unittest.TestCase):
    def setUp(self):
        self.verifier = MillClearanceVerifier(xy_tol_mm=1e-3, z_tol_mm=1e-3)
        self.stock = BoxStock3D(
            x_min=-50.0, x_max=50.0,
            y_min=-50.0, y_max=50.0,
            z_min=-25.0, z_max=0.0,
        )
        self.workpiece = WorkpieceDefinition(stock=self.stock, fixtures=[])
        self.verifier.reset_workpiece(self.workpiece)

    def _make_segment(
        self,
        seg_id: int,
        start: Vector3D,
        end: Vector3D,
        motion: MotionKind = MotionKind.LINEAR,
        sec_id: int = 0,
        op_id: str = "Op1",
        in_cycle: bool = False,
        z_clearance_mm: float = 5.0,
        feed_mmpm: float = 1000.0,
    ) -> MotionSegment:
        vkey = VectorKey.from_floats(sec_id=sec_id, start=start, end=end)
        return MotionSegment(
            segment_id=seg_id,
            sec_id=sec_id,
            op_id=op_id,
            motion=motion,
            start=start,
            end=end,
            feedrate_mmpm=feed_mmpm,
            spindle_rpm=5000.0,
            duration_sec=0.1,
            vector_key=vkey,
            metadata={
                "in_cycle": in_cycle,
                "z_clearance_mm": z_clearance_mm,
            },
        )

    def test_pure_z_plus_retract_reaching_clearance_is_promoted(self):
        """Pure vertical Z+ move reaching or exceeding clearance height is marked air move."""
        seg = self._make_segment(
            seg_id=1,
            start=Vector3D(10.0, 20.0, -5.0),
            end=Vector3D(10.0, 20.0, 5.0),
            z_clearance_mm=5.0,
        )
        res = self.verifier.step_segment(seg)
        self.assertTrue(res.is_air_move)
        self.assertEqual(len(res.collisions), 0)

    def test_pure_z_plus_retract_exceeding_clearance_is_promoted(self):
        """Pure vertical Z+ move exceeding clearance height is marked air move."""
        seg = self._make_segment(
            seg_id=2,
            start=Vector3D(10.0, 20.0, 0.0),
            end=Vector3D(10.0, 20.0, 15.0),
            z_clearance_mm=5.0,
        )
        res = self.verifier.step_segment(seg)
        self.assertTrue(res.is_air_move)

    def test_partial_z_plus_below_clearance_is_not_promoted(self):
        """Pure vertical Z+ move that does NOT reach clearance height is NOT marked air move."""
        seg = self._make_segment(
            seg_id=3,
            start=Vector3D(10.0, 20.0, -10.0),
            end=Vector3D(10.0, 20.0, -2.0),
            z_clearance_mm=5.0,
        )
        res = self.verifier.step_segment(seg)
        self.assertFalse(res.is_air_move)

    def test_z_minus_downward_plunge_is_not_promoted(self):
        """Downward vertical Z- plunge must never be promoted to rapid."""
        seg = self._make_segment(
            seg_id=4,
            start=Vector3D(10.0, 20.0, 5.0),
            end=Vector3D(10.0, 20.0, -5.0),
            z_clearance_mm=5.0,
        )
        res = self.verifier.step_segment(seg)
        self.assertFalse(res.is_air_move)

    def test_horizontal_or_diagonal_move_is_not_promoted(self):
        """Moves with lateral XY components (ramps, diagonals, planar cuts) are NOT promoted."""
        # Pure planar move
        seg_xy = self._make_segment(
            seg_id=5,
            start=Vector3D(0.0, 0.0, 5.0),
            end=Vector3D(10.0, 0.0, 5.0),
            z_clearance_mm=5.0,
        )
        self.assertFalse(self.verifier.step_segment(seg_xy).is_air_move)

        # Diagonal ramp move (dx > 0, dz > 0)
        seg_ramp = self._make_segment(
            seg_id=6,
            start=Vector3D(0.0, 0.0, 0.0),
            end=Vector3D(10.0, 0.0, 10.0),
            z_clearance_mm=5.0,
        )
        self.assertFalse(self.verifier.step_segment(seg_ramp).is_air_move)

    def test_canned_cycle_motion_is_not_promoted(self):
        """Linear vertical move occurring inside a canned drilling cycle is NOT promoted."""
        seg = self._make_segment(
            seg_id=7,
            start=Vector3D(0.0, 0.0, -10.0),
            end=Vector3D(0.0, 0.0, 5.0),
            in_cycle=True,
            z_clearance_mm=5.0,
        )
        res = self.verifier.step_segment(seg)
        self.assertFalse(res.is_air_move)

    def test_rapid_motion_updates_clearance_and_is_air_move(self):
        """G0 rapid move records section clearance height and evaluates as air move."""
        rapid_seg = self._make_segment(
            seg_id=8,
            start=Vector3D(0.0, 0.0, 20.0),
            end=Vector3D(10.0, 10.0, 20.0),
            motion=MotionKind.RAPID,
            sec_id=42,
        )
        res = self.verifier.step_segment(rapid_seg)
        self.assertTrue(res.is_air_move)
        # Verify cached clearance for sec_id 42 is 20.0
        self.assertEqual(self.verifier._sec_clearance_mm.get(42), 20.0)

    def test_fallback_clearance_from_workpiece_stock(self):
        """When segment metadata lacks clearance, fall back to workpiece stock.z_max."""
        seg = self._make_segment(
            seg_id=9,
            start=Vector3D(0.0, 0.0, -5.0),
            end=Vector3D(0.0, 0.0, 0.0),  # reaches stock.z_max = 0.0
            z_clearance_mm=None,
        )
        # Clear metadata z_clearance_mm
        seg.metadata.pop("z_clearance_mm", None)
        res = self.verifier.step_segment(seg)
        self.assertTrue(res.is_air_move)

    def test_verify_toolpath_multi_pass_promoted_vectors(self):
        """Test full toolpath verification and vector occurrence gating."""
        # Construct a sequence:
        # Pass 1: Cut down, retract up to clearance (Move A)
        # Pass 2: Cut down, retract up to clearance (Move A again)
        # Move A occurs twice; both are air moves -> min_occur = 1
        seg1 = self._make_segment(1, Vector3D(0, 0, 0), Vector3D(0, 0, -5)) # plunge
        seg2 = self._make_segment(2, Vector3D(0, 0, -5), Vector3D(10, 0, -5)) # cut
        seg3 = self._make_segment(3, Vector3D(10, 0, -5), Vector3D(10, 0, 5)) # retract A (air)
        seg4 = self._make_segment(4, Vector3D(10, 0, 5), Vector3D(20, 0, 5)) # rapid/position
        seg5 = self._make_segment(5, Vector3D(20, 0, 5), Vector3D(20, 0, -5)) # plunge
        seg6 = self._make_segment(6, Vector3D(20, 0, -5), Vector3D(30, 0, -5)) # cut
        seg7 = self._make_segment(7, Vector3D(30, 0, -5), Vector3D(30, 0, 5)) # retract A (air)

        segments = [seg1, seg2, seg3, seg4, seg5, seg6, seg7]
        report = self.verifier.verify_toolpath(
            segments=segments,
            tools_by_op={},
            workpiece=self.workpiece,
        )
        self.assertEqual(len(report.collisions), 0)
        # seg3 and seg7 share the same vector key: pure Z+ dz=+10.0, feed=1000
        vkey_str = seg3.vector_key.to_wrapper_str()
        self.assertIn(vkey_str, report.promoted_vectors)
        self.assertEqual(report.promoted_vectors[vkey_str].min_occur, 1)


class TestMillDomainExtraction(unittest.TestCase):
    def test_extract_workpiece_from_context_for_milling(self):
        """Test extract_workpiece_from_context constructs BoxStock3D for is_turning=False."""
        context = {
            "is_turning": False,
            "unit": 1,  # mm
            "stock": {
                "x_min": -60.0,
                "x_max": 60.0,
                "y_min": -40.0,
                "y_max": 40.0,
                "z_min": -20.0,
                "z_max": 2.0,
            },
        }
        wp = extract_workpiece_from_context(context, is_turning=False)
        self.assertIsNotNone(wp)
        self.assertIsInstance(wp.stock, BoxStock3D)
        self.assertAlmostEqual(wp.stock.x_min, -60.0)
        self.assertAlmostEqual(wp.stock.x_max, 60.0)
        self.assertAlmostEqual(wp.stock.y_min, -40.0)
        self.assertAlmostEqual(wp.stock.y_max, 40.0)
        self.assertAlmostEqual(wp.stock.z_min, -20.0)
        self.assertAlmostEqual(wp.stock.z_max, 2.0)
        self.assertEqual(wp.fixtures, [])

    def test_extract_workpiece_from_context_inch_conversion(self):
        """Test extract_workpiece_from_context scales inches to mm for BoxStock3D."""
        context = {
            "is_turning": False,
            "unit": 0,  # INCHES
            "stock": {
                "x_min": -2.0,
                "x_max": 2.0,
                "y_min": -1.0,
                "y_max": 1.0,
                "z_min": -0.5,
                "z_max": 0.1,
            },
        }
        wp = extract_workpiece_from_context(context, is_turning=False)
        self.assertIsNotNone(wp)
        self.assertIsInstance(wp.stock, BoxStock3D)
        self.assertAlmostEqual(wp.stock.x_min, -2.0 * 25.4)
        self.assertAlmostEqual(wp.stock.x_max, 2.0 * 25.4)
        self.assertAlmostEqual(wp.stock.z_min, -0.5 * 25.4)
        self.assertAlmostEqual(wp.stock.z_max, 0.1 * 25.4)

    def test_extract_tools_from_context_for_milling(self):
        """Test extract_tools_from_context constructs ToolAssembly with MillToolGeometry."""
        context = {
            "is_turning": False,
            "unit": 1,  # mm
            "sections": [
                {
                    "sec_id": 0,
                    "op_id": "Op1_Adaptive",
                    "op_name": "Adaptive Clearing",
                    "params": {"operation-comment": "Op1_Adaptive"},
                    "tool": {
                        "number": 5,
                        "description": "10mm Flat Endmill",
                        "diameter": 10.0,
                        "flute_length": 25.0,
                        "body_length": 40.0,
                        "overall_length": 75.0,
                        "corner_radius": 0.0,
                        "holder_diameter": 35.0,
                        "holder_length": 45.0,
                    },
                }
            ],
        }
        tools = extract_tools_from_context(context, is_turning=False)
        self.assertIn("Op1_Adaptive", tools)
        t = tools["Op1_Adaptive"]
        self.assertIsInstance(t, ToolAssembly)
        self.assertEqual(t.tool_id, 5)
        self.assertIsInstance(t.geometry, MillToolGeometry)
        self.assertAlmostEqual(t.geometry.diameter, 10.0)
        self.assertAlmostEqual(t.geometry.flute_length, 25.0)
        self.assertAlmostEqual(t.geometry.corner_radius, 0.0)
        self.assertAlmostEqual(t.geometry.holder_diameter, 35.0)


if __name__ == "__main__":
    unittest.main()
