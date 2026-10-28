#!/usr/bin/env python3
"""
Unit and Integration Test Suite for 3D In-Process Workpiece (IPW) Heightfield Grid Engine.

Validates:
1. HeightfieldGrid discrete coordinate mapping and initial bounds allocation.
2. Mathematical tool bottom profile generation (flat, ball, bull-nose, conical).
3. 3D linear cutting moves and analytical volumetric MRR accuracy.
4. Multi-pass depth stepping, stock carving, and air move detection.
5. Rapid collision detection into uncut stock (RAPID_INTO_STOCK).
6. Toolholder collision detection into raw stock (HOLDER_STRIKE).
7. Unfluted tool shank rubbing detection (SHANK_RUB).
8. Occurrence-gated rapid (G1 -> G0) promotion rules.
9. Circular arc sweeping (G2/G3) in the XY plane.
10. Full toolpath verification workflow via verify_toolpath().
"""

import math
import unittest
import numpy as np

from geometry.engine_interface import BaseVerificationEngine, IVerificationEngine
from geometry.heightfield_engine import (
    HeightfieldGrid,
    MillHeightfieldVerifier,
    evaluate_tool_bottom_profile,
)
from geometry.models import (
    BoxStock3D,
    CollisionKind,
    MillToolGeometry,
    MotionKind,
    MotionSegment,
    ToolAssembly,
    Vector3D,
    VectorKey,
    WorkpieceDefinition,
)


def _make_segment(
    seg_id: int,
    motion: MotionKind,
    start: Vector3D,
    end: Vector3D,
    feedrate: float = 1000.0,
    duration_sec: float = 1.0,
    sec_id: int = 0,
    op_id: str = "Op1",
    is_arc: bool = False,
    arc_center: Vector3D = None,
    metadata: dict = None,
) -> MotionSegment:
    vkey = VectorKey(
        sec_id=sec_id,
        start_x=round(start.x, 3),
        start_y=round(start.y, 3),
        start_z=round(start.z, 3),
        end_x=round(end.x, 3),
        end_y=round(end.y, 3),
        end_z=round(end.z, 3),
    )
    return MotionSegment(
        segment_id=seg_id,
        op_id=op_id,
        sec_id=sec_id,
        motion=motion,
        start=start,
        end=end,
        feedrate_mmpm=feedrate,
        spindle_rpm=8000.0,
        duration_sec=duration_sec,
        vector_key=vkey,
        is_arc=is_arc,
        arc_center=arc_center,
        metadata=metadata or {},
    )


class TestHeightfieldGrid(unittest.TestCase):
    """Test low-level discrete regular grid geometry."""

    def test_grid_initialization_and_reset(self):
        stock = BoxStock3D(x_min=-20.0, x_max=20.0, y_min=-30.0, y_max=30.0, z_min=-15.0, z_max=0.0)
        grid = HeightfieldGrid(cell_size_mm=0.5, pad_mm=5.0)
        grid.reset(stock)

        # Expected dimensions with 5mm padding:
        # X: [-25, 25] -> width 50 -> ~101 cells
        # Y: [-35, 35] -> height 70 -> ~141 cells
        self.assertGreater(grid.nx, 95)
        self.assertGreater(grid.ny, 135)

        # Center inside stock should be z_max (0.0)
        col_c = int((0.0 - grid.grid_x_min) / grid.cell_size_mm)
        row_c = int((0.0 - grid.grid_y_min) / grid.cell_size_mm)
        self.assertAlmostEqual(float(grid.H[row_c, col_c]), 0.0, places=3)

        # Corner outside stock should be z_floor (-15.0)
        self.assertAlmostEqual(float(grid.H[0, 0]), -15.0, places=3)

    def test_tool_bottom_profile_formulas(self):
        # 1. Flat endmill (D=10, rc=0)
        r = np.array([0.0, 2.5, 5.0], dtype=np.float32)
        flat_z = evaluate_tool_bottom_profile(r, tool_radius=5.0, corner_radius=0.0)
        np.testing.assert_allclose(flat_z, [0.0, 0.0, 0.0])

        # 2. Ball endmill (D=10, rc=5.0)
        ball_z = evaluate_tool_bottom_profile(r, tool_radius=5.0, corner_radius=5.0)
        # At r=0: z=0; at r=5.0: z=5.0; at r=3.0: 5 - sqrt(25-9) = 1.0
        r_test = np.array([0.0, 3.0, 5.0], dtype=np.float32)
        ball_test = evaluate_tool_bottom_profile(r_test, tool_radius=5.0, corner_radius=5.0)
        np.testing.assert_allclose(ball_test, [0.0, 1.0, 5.0], atol=1e-5)

        # 3. Bull-nose endmill (D=10, R=5, rc=1.0 -> flat center r_flat=4.0)
        r_bull = np.array([0.0, 2.0, 4.0, 5.0], dtype=np.float32)
        bull_z = evaluate_tool_bottom_profile(r_bull, tool_radius=5.0, corner_radius=1.0)
        self.assertAlmostEqual(bull_z[0], 0.0)
        self.assertAlmostEqual(bull_z[2], 0.0)  # at r=4.0 (end of flat)
        self.assertAlmostEqual(bull_z[3], 1.0)  # at r=5.0 (outer corner)

        # 4. 90-degree Chamfer tool / Spot drill (D=6.35, R=3.175, included angle 90 deg -> half angle 45 deg)
        # z(r) = r / tan(45) = r
        r_chamfer = np.array([0.0, 1.0, 2.0, 3.175], dtype=np.float32)
        chamfer_z = evaluate_tool_bottom_profile(r_chamfer, tool_radius=3.175, corner_radius=0.0, taper_angle_deg=90.0)
        np.testing.assert_allclose(chamfer_z, [0.0, 1.0, 2.0, 3.175], atol=1e-5)

        # 5. 118-degree Drill (D=10, included angle 118 deg -> half angle 59 deg)
        # z(r) = r / tan(59 deg) = 0.60086 * r
        r_drill = np.array([0.0, 5.0], dtype=np.float32)
        drill_z = evaluate_tool_bottom_profile(r_drill, tool_radius=5.0, corner_radius=0.0, taper_angle_deg=118.0)
        np.testing.assert_allclose(drill_z, [0.0, 5.0 / math.tan(math.radians(59.0))], atol=1e-5)


class TestMillHeightfieldVerifier(unittest.TestCase):
    """Test complete 3D milling verifier against kinematic toolpaths."""

    def setUp(self):
        # Stock: 60 x 60 x 20 mm, top at Z=0.0
        self.stock = BoxStock3D(x_min=-30.0, x_max=30.0, y_min=-30.0, y_max=30.0, z_min=-20.0, z_max=0.0)
        self.workpiece = WorkpieceDefinition(stock=self.stock)

        # Flat Endmill: D=10mm (R=5mm), flute=20mm, overall=40mm, holder_D=30mm, holder_L=40mm
        self.tool_geom = MillToolGeometry(
            diameter=10.0,
            corner_radius=0.0,
            flute_length=20.0,
            overall_length=40.0,
            holder_diameter=30.0,
            holder_length=40.0,
        )
        self.tool = ToolAssembly(tool_id=1, name="10mm Flat Endmill", geometry=self.tool_geom)

        self.verifier = MillHeightfieldVerifier(cell_size_mm=0.5, pad_mm=5.0, promote_margin_mm=0.0)
        self.verifier.reset(self.workpiece)
        self.verifier.load_tool(self.tool)

    def test_single_linear_slot_cut_volume_accuracy(self):
        """A linear slot cut from X=-10 to X=10 at Z=-2.0 should remove analytical volume."""
        # Slot: length = 20mm + tool_D (with end caps) = 30mm.
        # Rectangular middle = 20 * 10 * 2 = 400 mm3.
        # Two semi-circular end caps = pi * R^2 * depth = pi * 25 * 2 = ~157.08 mm3.
        # Total analytical volume ~ 557.08 mm3.
        move = _make_segment(
            seg_id=1,
            motion=MotionKind.LINEAR,
            start=Vector3D(-10.0, 0.0, -2.0),
            end=Vector3D(10.0, 0.0, -2.0),
            feedrate=600.0,
            duration_sec=2.0,
        )
        eval_res = self.verifier.step(move)

        self.assertFalse(eval_res.is_air_move)
        self.assertEqual(len(eval_res.collisions), 0)

        analytical_vol = 20.0 * 10.0 * 2.0 + math.pi * (5.0 ** 2) * 2.0
        # Grid discrete approximation is within 5% of continuous analytical volume
        self.assertAlmostEqual(eval_res.material_removed_mm3, analytical_vol, delta=analytical_vol * 0.05)
        self.assertGreater(eval_res.instantaneous_mrr, 0.0)

        # Re-stepping identical move must remove ZERO material and be detected as an air move
        move_pass2 = _make_segment(
            seg_id=2,
            motion=MotionKind.LINEAR,
            start=Vector3D(-10.0, 0.0, -2.0),
            end=Vector3D(10.0, 0.0, -2.0),
            feedrate=600.0,
            duration_sec=2.0,
        )
        eval2 = self.verifier.step(move_pass2)
        self.assertTrue(eval2.is_air_move)
        self.assertAlmostEqual(eval2.material_removed_mm3, 0.0, places=5)

    def test_multi_depth_pass_occurrence_gating(self):
        """Pass 1 cuts; Pass 2 at same depth clears air; verifier sets min_occur=2."""
        seg_pass1 = _make_segment(
            seg_id=1,
            motion=MotionKind.LINEAR,
            start=Vector3D(-5.0, 0.0, -1.0),
            end=Vector3D(5.0, 0.0, -1.0),
        )
        seg_pass2 = _make_segment(
            seg_id=2,
            motion=MotionKind.LINEAR,
            start=Vector3D(-5.0, 0.0, -1.0),
            end=Vector3D(5.0, 0.0, -1.0),
        )

        res1 = self.verifier.step(seg_pass1)
        self.assertFalse(res1.is_air_move)

        res2 = self.verifier.step(seg_pass2)
        self.assertTrue(res2.is_air_move)

        report = self.verifier.generate_report()
        vkey_str = seg_pass1.vector_key.to_wrapper_str()
        self.assertIn(vkey_str, report.promoted_vectors)
        self.assertEqual(report.promoted_vectors[vkey_str].min_occur, 2)

    def test_rapid_into_stock_collision(self):
        """A rapid (G0) move traversing into raw stock must trigger RAPID_INTO_STOCK."""
        # Rapid move traversing through stock at Z=-3.0
        rapid_crash = _make_segment(
            seg_id=10,
            motion=MotionKind.RAPID,
            start=Vector3D(-25.0, 0.0, -3.0),
            end=Vector3D(25.0, 0.0, -3.0),
        )
        eval_res = self.verifier.step(rapid_crash)

        self.assertEqual(len(eval_res.collisions), 1)
        col = eval_res.collisions[0]
        self.assertEqual(col.kind, CollisionKind.RAPID_INTO_STOCK)
        self.assertGreater(col.penetration_volume_mm3, 100.0)

    def test_holder_strike_collision(self):
        """Plunging deeper than tool overall stickout length causes toolholder to crash into stock."""
        # Tool stickout = 40mm. Stock is from Z=0 down to -20mm.
        # If tip plunges to Z=-50mm, the holder (starting at tip + 40 = -10mm) hits stock top (Z=0).
        deep_plunge = _make_segment(
            seg_id=20,
            motion=MotionKind.LINEAR,
            start=Vector3D(0.0, 0.0, 5.0),
            end=Vector3D(0.0, 0.0, -45.0),
            feedrate=200.0,
            duration_sec=15.0,
        )
        eval_res = self.verifier.step(deep_plunge)

        holder_cols = [c for c in eval_res.collisions if c.kind == CollisionKind.HOLDER_STRIKE]
        self.assertGreaterEqual(len(holder_cols), 1)
        self.assertIn("Toolholder collided with uncut stock", holder_cols[0].description)

    def test_shank_rub_collision(self):
        """Cutting with depth exceeding the flute length triggers SHANK_RUB."""
        # Flute length is 20mm. Plunge depth to Z=-25mm exceeds flute length by 5mm.
        deep_cut = _make_segment(
            seg_id=30,
            motion=MotionKind.LINEAR,
            start=Vector3D(0.0, 0.0, -25.0),
            end=Vector3D(10.0, 0.0, -25.0),
            feedrate=300.0,
            duration_sec=2.0,
        )
        eval_res = self.verifier.step(deep_cut)

        shank_cols = [c for c in eval_res.collisions if c.kind == CollisionKind.SHANK_RUB]
        self.assertGreaterEqual(len(shank_cols), 1)
        self.assertIn("shank rubbed into raw stock", shank_cols[0].description)

    def test_pure_z_plus_retract_air_promotion(self):
        """Pure vertical Z+ retract move reaching clearance height is promoted."""
        retract_move = _make_segment(
            seg_id=40,
            motion=MotionKind.LINEAR,
            start=Vector3D(5.0, 5.0, -2.0),
            end=Vector3D(5.0, 5.0, 15.0),
            metadata={"z_clearance_mm": 10.0},
        )
        eval_res = self.verifier.step(retract_move)
        self.assertTrue(eval_res.is_air_move)
        self.assertEqual(len(eval_res.collisions), 0)

    def test_circular_arc_sweeping(self):
        """Circular arc move (G2/G3) in XY plane correctly carves arc channel."""
        # 90-degree CCW arc from (10, 0) to (0, 10) around center (0, 0) at Z=-1.0
        arc_move = _make_segment(
            seg_id=50,
            motion=MotionKind.ARC_CCW,
            start=Vector3D(10.0, 0.0, -1.0),
            end=Vector3D(0.0, 10.0, -1.0),
            is_arc=True,
            arc_center=Vector3D(0.0, 0.0, -1.0),
            duration_sec=1.5,
        )
        eval_res = self.verifier.step(arc_move)

        self.assertFalse(eval_res.is_air_move)
        self.assertGreater(eval_res.material_removed_mm3, 50.0)
        self.assertEqual(len(eval_res.collisions), 0)

    def test_verify_toolpath_orchestration_workflow(self):
        """Run complete multi-segment toolpath through verify_toolpath convenience loop."""
        moves = [
            _make_segment(1, MotionKind.RAPID, Vector3D(0.0, 0.0, 20.0), Vector3D(-10.0, 0.0, 5.0)),
            _make_segment(2, MotionKind.LINEAR, Vector3D(-10.0, 0.0, 5.0), Vector3D(-10.0, 0.0, -2.0)),
            _make_segment(3, MotionKind.LINEAR, Vector3D(-10.0, 0.0, -2.0), Vector3D(10.0, 0.0, -2.0)),
            _make_segment(4, MotionKind.LINEAR, Vector3D(10.0, 0.0, -2.0), Vector3D(10.0, 0.0, 20.0), metadata={"z_clearance_mm": 15.0}),
        ]
        tools_map = {"Op1": self.tool}

        report = self.verifier.verify_toolpath(moves, tools_map, self.workpiece)

        self.assertIsInstance(report.total_volume_removed_mm3, float)
        self.assertGreater(report.total_volume_removed_mm3, 0.0)
        self.assertEqual(len(report.collisions), 0)
        self.assertGreater(len(report.mrr_timeline), 0)

        # Move 4 (vertical retract) should be promoted
        retract_key = moves[3].vector_key.to_wrapper_str()
        self.assertIn(retract_key, report.promoted_vectors)

    def test_ball_endmill_carving(self):
        """Ball endmill carves a hemispherical/curved bottom trench."""
        ball_geom = MillToolGeometry(
            diameter=10.0,
            corner_radius=5.0,  # Ball endmill: rc = D/2
            flute_length=20.0,
            overall_length=40.0,
        )
        ball_tool = ToolAssembly(tool_id=2, name="10mm Ball Endmill", geometry=ball_geom)
        self.verifier.reset(self.workpiece)
        self.verifier.load_tool(ball_tool)

        move = _make_segment(
            seg_id=1,
            motion=MotionKind.LINEAR,
            start=Vector3D(-5.0, 0.0, -2.0),
            end=Vector3D(5.0, 0.0, -2.0),
            duration_sec=1.0,
        )
        res = self.verifier.step(move)
        self.assertFalse(res.is_air_move)
        self.assertGreater(res.material_removed_mm3, 0.0)

        # Centerline bottom must be at -2.0
        # Edge (r=4mm) must be higher than -2.0 because of ball radius curve:
        # z(4) = 5 - sqrt(25 - 16) = 2.0 -> Z = -2.0 + 2.0 = 0.0!
        col_c = int((0.0 - self.verifier.grid.grid_x_min) / self.verifier.grid.cell_size_mm)
        row_c = int((0.0 - self.verifier.grid.grid_y_min) / self.verifier.grid.cell_size_mm)
        self.assertAlmostEqual(float(self.verifier.grid.H[row_c, col_c]), -2.0, places=2)

    def test_canned_cycle_moves_never_promoted(self):
        """Moves with metadata['in_cycle']=True are never authorized for rapid promotion."""
        cycle_move = _make_segment(
            seg_id=60,
            motion=MotionKind.LINEAR,
            start=Vector3D(0.0, 0.0, 15.0),
            end=Vector3D(0.0, 0.0, 25.0),
            metadata={"in_cycle": True},
        )
        res = self.verifier.step(cycle_move)
        self.assertFalse(res.is_air_move)
        report = self.verifier.generate_report()
        vkey_str = cycle_move.vector_key.to_wrapper_str()
        self.assertNotIn(vkey_str, report.promoted_vectors)

    def test_mrr_spike_detection(self):
        """Heavy step-down cut among light cuts triggers an MRR spike."""
        self.verifier.reset(self.workpiece)
        self.verifier.load_tool(self.tool)

        # Several light cuts (depth 0.1 mm)
        for i in range(10):
            m = _make_segment(
                seg_id=100 + i,
                motion=MotionKind.LINEAR,
                start=Vector3D(-10.0, float(i * 2 - 10), -0.1),
                end=Vector3D(10.0, float(i * 2 - 10), -0.1),
                duration_sec=2.0,
            )
            self.verifier.step(m)

        # One sudden deep plunge/slot (depth 5.0 mm, 50x volume)
        heavy_cut = _make_segment(
            seg_id=200,
            motion=MotionKind.LINEAR,
            start=Vector3D(-10.0, 20.0, -5.0),
            end=Vector3D(10.0, 20.0, -5.0),
            duration_sec=0.5,
        )
        self.verifier.step(heavy_cut)

        report = self.verifier.generate_report()
        self.assertGreater(len(report.mrr_spikes), 0)
        self.assertEqual(report.mrr_spikes[0].segment_id, 200)

    def test_generate_mill_verification_plot_end_to_end(self):
        """Verify that generate_mill_verification_plot creates a valid dual-panel 3D PNG."""
        import os
        import tempfile
        from geometry.plotter import generate_mill_verification_plot

        moves = [
            _make_segment(1, MotionKind.RAPID, Vector3D(-20.0, -20.0, 15.0), Vector3D(-15.0, 0.0, 5.0), op_id="Facing"),
            _make_segment(2, MotionKind.LINEAR, Vector3D(-15.0, 0.0, 5.0), Vector3D(-15.0, 0.0, -2.0), op_id="Facing"),
            _make_segment(3, MotionKind.LINEAR, Vector3D(-15.0, 0.0, -2.0), Vector3D(15.0, 0.0, -2.0), duration_sec=3.0, op_id="Facing"),
            _make_segment(4, MotionKind.LINEAR, Vector3D(15.0, 0.0, -2.0), Vector3D(15.0, 0.0, 15.0), metadata={"z_clearance_mm": 10.0}, op_id="Facing"),
        ]
        tools_map = {"Facing": self.tool}
        report = self.verifier.verify_toolpath(moves, tools_map, self.workpiece)

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_png = os.path.join(tmp_dir, "mill_verification.png")
            generate_mill_verification_plot(
                workpiece=self.workpiece,
                grid=self.verifier.grid,
                segments=moves,
                report=report,
                tools_by_op=tools_map,
                out_png_path=out_png,
                unit_is_inch=False,
            )
            self.assertTrue(os.path.exists(out_png))
            self.assertGreater(os.path.getsize(out_png), 10000)

    def test_plot_mrr_timeline_gapless_and_clean_spikes(self):
        """Verify _plot_mrr_timeline creates gapless operation spans, plots all spikes, and has no text labels."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from geometry.plotter import _plot_mrr_timeline

        # 2 operations: Op1 (1.0s rapid + 2.0s cut) and Op2 (1.0s cut + 1.0s rapid retract)
        moves = [
            _make_segment(1, MotionKind.RAPID, Vector3D(-20, 0, 10), Vector3D(-10, 0, 0), duration_sec=1.0, op_id="Facing"),
            _make_segment(2, MotionKind.LINEAR, Vector3D(-10, 0, -1), Vector3D(10, 0, -1), duration_sec=2.0, op_id="Facing"),
            _make_segment(3, MotionKind.LINEAR, Vector3D(10, 0, -3), Vector3D(10, 10, -3), duration_sec=1.0, op_id="Contour"),
            _make_segment(4, MotionKind.RAPID, Vector3D(10, 10, 10), Vector3D(0, 0, 20), duration_sec=1.0, op_id="Contour"),
        ]
        tools_map = {"Facing": self.tool, "Contour": self.tool}
        report = self.verifier.verify_toolpath(moves, tools_map, self.workpiece)

        fig, ax = plt.subplots()
        try:
            _plot_mrr_timeline(ax, report, moves, vol_scale=1.0, vol_unit="mm³")
            # 1. Check xlim covers entire sequence: 1+2+1+1 = 5.0 seconds
            self.assertAlmostEqual(ax.get_xlim()[0], 0.0)
            self.assertAlmostEqual(ax.get_xlim()[1], 5.0)

            # 2. Check no text annotations were placed on ax
            self.assertEqual(len(ax.texts), 0, "Spike text annotations should not be created")

            # 3. Check legend does not contain tool flute or holder
            handles, labels = ax.get_legend_handles_labels()
            for lbl in labels:
                self.assertNotIn("Flute", lbl)
                self.assertNotIn("Holder", lbl)
        finally:
            plt.close(fig)

    def test_chamfer_operation_mrr_is_virtually_zero(self):
        """Verify that a 2D chamfer operation using a conical chamfer tool produces virtually zero MRR."""
        # 1. Flat endmill cuts initial rough pocket at Z=-2.0
        rough_cut = _make_segment(
            1, MotionKind.LINEAR, Vector3D(-5, 0, -2.0), Vector3D(5, 0, -2.0),
            feedrate=600.0, duration_sec=2.0, op_id="Roughing"
        )
        self.verifier.step(rough_cut)

        # 2. Load 90-degree chamfer tool (Tool 9: D=6.35, R=3.175, taper_angle_deg=90.0)
        chamfer_geom = MillToolGeometry(
            diameter=6.35,
            corner_radius=0.0,
            flute_length=15.0,
            overall_length=50.0,
            taper_angle_deg=90.0,
            holder_diameter=30.0,
            holder_length=40.0,
        )
        chamfer_tool = ToolAssembly(tool_id=9, name="1/4 90deg Chamfer Mill", geometry=chamfer_geom)
        self.verifier.load_tool(chamfer_tool)

        # 3. Run 2D Chamfer move along the top edge beveling a 0.2mm edge break
        # Tip is at Z=-2.1, center is offset at X=7.0 (slot edge at X=5.0).
        # At slot edge (dr=2.0): z_cone = -2.1 + 2.0 = -0.1 mm, taking a tiny 0.1mm corner bevel.
        chamfer_move = _make_segment(
            2, MotionKind.LINEAR, Vector3D(7.0, -3.0, -2.1), Vector3D(7.0, 3.0, -2.1),
            feedrate=1000.0, duration_sec=1.0, op_id="2D Chamfer1"
        )
        res = self.verifier.step(chamfer_move)

        # Material removed by the chamfer cut must be virtually zero (< 1.0 mm³)
        self.assertLess(res.material_removed_mm3, 1.0)
        self.assertLess(res.instantaneous_mrr, 60.0)
        self.assertEqual(len(res.collisions), 0)

    def test_mrr_spikes_capped_at_top_3_per_operation(self):
        """Verify that BaseVerificationEngine.generate_report selects top 3 MRR spikes per operation."""
        engine = MillHeightfieldVerifier()
        engine.reset(self.workpiece)

        # Baseline cutting samples with MRR = 50 mm3/min (50 samples)
        for i in range(50):
            engine.current_time_sec = float(i)
            engine.record_mrr_sample(
                segment_id=i,
                duration_sec=1.0,
                volume_removed_mm3=50.0 / 60.0,
                op_id="Baseline",
            )

        # Op1: 5 spikes with rates 500, 600, 700, 800, 900 mm3/min (mean ~ 100, threshold ~ 250)
        for idx, rate in enumerate([500.0, 900.0, 600.0, 800.0, 700.0]):
            engine.current_time_sec = 20.0 + float(idx * 2)
            engine.record_mrr_sample(
                segment_id=100 + idx,
                duration_sec=1.0,
                volume_removed_mm3=rate / 60.0,
                op_id="Op1",
            )

        # Op2: 4 spikes with rates 400, 1000, 450, 950 mm3/min
        for idx, rate in enumerate([400.0, 1000.0, 450.0, 950.0]):
            engine.current_time_sec = 40.0 + float(idx * 2)
            engine.record_mrr_sample(
                segment_id=200 + idx,
                duration_sec=1.0,
                volume_removed_mm3=rate / 60.0,
                op_id="Op2",
            )

        report = engine.generate_report()

        # Exactly 3 spikes from Op1 and 3 spikes from Op2 = 6 spikes total
        op1_spikes = [s for s in report.mrr_spikes if s.op_id == "Op1"]
        op2_spikes = [s for s in report.mrr_spikes if s.op_id == "Op2"]

        self.assertEqual(len(op1_spikes), 3)
        self.assertEqual(len(op2_spikes), 3)
        self.assertEqual(len(report.mrr_spikes), 6)

        # For Op1, top 3 rates are 900, 800, 700
        op1_rates = sorted([s.mrr_rate_mm3_per_min for s in op1_spikes], reverse=True)
        self.assertAlmostEqual(op1_rates[0], 900.0, places=1)
        self.assertAlmostEqual(op1_rates[1], 800.0, places=1)
        self.assertAlmostEqual(op1_rates[2], 700.0, places=1)

        # For Op2, top 3 rates are 1000, 950, 450
        op2_rates = sorted([s.mrr_rate_mm3_per_min for s in op2_spikes], reverse=True)
        self.assertAlmostEqual(op2_rates[0], 1000.0, places=1)
        self.assertAlmostEqual(op2_rates[1], 950.0, places=1)
        self.assertAlmostEqual(op2_rates[2], 450.0, places=1)

        # Verify chronological ordering across report.mrr_spikes
        timestamps = [s.timestamp_sec for s in report.mrr_spikes]
        self.assertEqual(timestamps, sorted(timestamps))


if __name__ == "__main__":
    unittest.main()

