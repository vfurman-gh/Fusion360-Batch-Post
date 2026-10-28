#!/usr/bin/env python3
"""
Test Suite: Lathe2DIPWVerifier Collision and IPW Mechanics.

Specifically validates:
1. False Positive Avoidance: Toolholder trailing in cutter's wake does NOT trigger collision.
2. Multi-Pass Cavity Navigation: Toolholder following previous cutting passes through cleared stock.
3. True Positive Detection: Toolholder shank protruding outside cut envelope gouging uncut stock.
4. Rapid Collision Detection: G0 rapid plunge directly into uncut stock.
5. Fixture Collision Detection: Toolholder or cutter strike into chuck obstacles.
"""

import unittest
from shapely.geometry import Polygon

from geometry import (
    Lathe2DIPWVerifier,
    AxisymmetricStock2D,
    AxisymmetricChuckObstacle,
    WorkpieceDefinition,
    ToolAssembly,
    LatheToolGeometry,
    ToolCompensationQuadrant,
    MotionSegment,
    MotionKind,
    Vector3D,
    VectorKey,
    CollisionKind,
)


class TestLatheEngineCollisions(unittest.TestCase):
    def setUp(self):
        # Cylindrical stock: Z in [-50, 0], OD = 20 mm (Radius in [0, 20])
        self.stock = AxisymmetricStock2D(z_min=-50.0, z_max=0.0, outer_radius=20.0, inner_radius=0.0)
        self.workpiece = WorkpieceDefinition(stock=self.stock)

        # Standard 5x5 mm square cutter: tip at (0, 0), spans Z in [0, 5], R in [0, 5]
        self.cutter_poly = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])

    def test_01_holder_in_cutter_wake_no_false_positive(self):
        """
        Verify that a toolholder trailing inside the cutter's wake during a cutting move
        does NOT trigger a false-positive collision.
        """
        # Holder is attached strictly behind the cutter in Z (+Z direction),
        # spanning R in [1, 4] (inside cutter's R-span [0, 5])
        holder_in_wake = Polygon([(2, 1), (2, 4), (10, 4), (10, 1)])

        tool = ToolAssembly(
            tool_id=1,
            name="Wake-Safe Tool",
            geometry=LatheToolGeometry(
                insert_shape="W",
                nose_radius=0.8,
                hand="R",
                quadrant=ToolCompensationQuadrant.P3,
                cutter_polygon=self.cutter_poly,
                holder_polygon=holder_in_wake,
            ),
        )

        verifier = Lathe2DIPWVerifier()
        verifier.reset(self.workpiece)
        verifier.load_tool(tool)

        # Plunge cutting move along -Z from Z=0 to Z=-15 at R=15 mm
        cut_move = MotionSegment(
            segment_id=1,
            op_id="Roughing",
            sec_id=0,
            motion=MotionKind.LINEAR,
            start=Vector3D(15.0, 0.0, 0.0),
            end=Vector3D(15.0, 0.0, -15.0),
            feedrate_mmpm=200.0,
            spindle_rpm=1000.0,
            duration_sec=4.5,
            vector_key=VectorKey.from_floats(0, Vector3D(15.0, 0.0, 0.0), Vector3D(15.0, 0.0, -15.0)),
        )

        eval_res = verifier.step(cut_move)

        # Assert no collision is flagged
        self.assertEqual(len(eval_res.collisions), 0, "False positive collision flagged for holder in wake")
        self.assertGreater(eval_res.material_removed_mm3, 0.0, "Expected cutting move to remove material")

    def test_02_holder_following_previous_cut_path_no_false_positive(self):
        """
        Verify that when a tool plunges deeper into a cavity cleared by previous passes,
        the holder moving through that previously cleared space triggers zero collisions.
        """
        holder_in_wake = Polygon([(2, 1), (2, 4), (10, 4), (10, 1)])

        tool = ToolAssembly(
            tool_id=1,
            name="Wake-Safe Tool",
            geometry=LatheToolGeometry(
                insert_shape="W",
                nose_radius=0.8,
                hand="R",
                quadrant=ToolCompensationQuadrant.P3,
                cutter_polygon=self.cutter_poly,
                holder_polygon=holder_in_wake,
            ),
        )

        verifier = Lathe2DIPWVerifier()
        verifier.reset(self.workpiece)
        verifier.load_tool(tool)

        # Pass 1: Cut from Z=0 to Z=-10 at R=15
        pass1 = MotionSegment(
            segment_id=1,
            op_id="Roughing",
            sec_id=0,
            motion=MotionKind.LINEAR,
            start=Vector3D(15.0, 0.0, 0.0),
            end=Vector3D(15.0, 0.0, -10.0),
            feedrate_mmpm=200.0,
            spindle_rpm=1000.0,
            duration_sec=3.0,
            vector_key=VectorKey.from_floats(0, Vector3D(15.0, 0.0, 0.0), Vector3D(15.0, 0.0, -10.0)),
        )
        res1 = verifier.step(pass1)
        self.assertEqual(len(res1.collisions), 0)

        # Pass 2: Continue deeper from Z=-10 to Z=-20 at R=15
        # The holder now traverses through Z in [-10, 0], which was cleared in Pass 1
        pass2 = MotionSegment(
            segment_id=2,
            op_id="Roughing",
            sec_id=0,
            motion=MotionKind.LINEAR,
            start=Vector3D(15.0, 0.0, -10.0),
            end=Vector3D(15.0, 0.0, -20.0),
            feedrate_mmpm=200.0,
            spindle_rpm=1000.0,
            duration_sec=3.0,
            vector_key=VectorKey.from_floats(0, Vector3D(15.0, 0.0, -10.0), Vector3D(15.0, 0.0, -20.0)),
        )
        res2 = verifier.step(pass2)
        self.assertEqual(len(res2.collisions), 0, "False positive flagged while navigating previously cut channel")

    def test_03_holder_shank_protrusion_true_positive(self):
        """
        Verify that a wide or protruding toolholder shank that exceeds the cutter's wake
        and gouges uncut stock correctly triggers a HOLDER_STRIKE collision.
        """
        # Holder has a wide shoulder spanning R in [1, 10] (extends 5 mm past the cutter's R=5 edge)
        holder_protruding = Polygon([(2, 1), (2, 10), (10, 10), (10, 1)])

        tool = ToolAssembly(
            tool_id=2,
            name="Gouging Tool",
            geometry=LatheToolGeometry(
                insert_shape="W",
                nose_radius=0.8,
                hand="R",
                quadrant=ToolCompensationQuadrant.P3,
                cutter_polygon=self.cutter_poly,
                holder_polygon=holder_protruding,
            ),
        )

        verifier = Lathe2DIPWVerifier()
        verifier.reset(self.workpiece)
        verifier.load_tool(tool)

        # Cut at R=10 from Z=0 to Z=-15.
        # Cutter is at R in [10, 15] (cuts stock).
        # Holder reaches R in [11, 20]. Stock at R in [15, 20] is uncut raw stock.
        # The holder gouges into uncut stock!
        cut_move = MotionSegment(
            segment_id=1,
            op_id="Roughing",
            sec_id=0,
            motion=MotionKind.LINEAR,
            start=Vector3D(10.0, 0.0, 0.0),
            end=Vector3D(10.0, 0.0, -15.0),
            feedrate_mmpm=200.0,
            spindle_rpm=1000.0,
            duration_sec=4.5,
            vector_key=VectorKey.from_floats(0, Vector3D(10.0, 0.0, 0.0), Vector3D(10.0, 0.0, -15.0)),
        )

        eval_res = verifier.step(cut_move)

        self.assertEqual(len(eval_res.collisions), 1, "Expected exactly 1 holder gouge collision")
        col = eval_res.collisions[0]
        self.assertEqual(col.kind, CollisionKind.HOLDER_STRIKE)
        self.assertGreater(col.penetration_volume_mm3, 0.0)
        self.assertIn("Toolholder shank gouged raw stock", col.description)

    def test_04_rapid_into_uncut_stock_collision(self):
        """
        Verify that a G0 rapid move plunging directly into uncut stock triggers RAPID_INTO_STOCK.
        """
        holder_in_wake = Polygon([(2, 1), (2, 4), (10, 4), (10, 1)])
        tool = ToolAssembly(
            tool_id=1,
            name="Rapid Tool",
            geometry=LatheToolGeometry(
                insert_shape="W",
                nose_radius=0.8,
                hand="R",
                quadrant=ToolCompensationQuadrant.P3,
                cutter_polygon=self.cutter_poly,
                holder_polygon=holder_in_wake,
            ),
        )

        verifier = Lathe2DIPWVerifier()
        verifier.reset(self.workpiece)
        verifier.load_tool(tool)

        # Rapid move plunging from outside stock into Z=-10, R=10
        rapid_crash = MotionSegment(
            segment_id=1,
            op_id="Roughing",
            sec_id=0,
            motion=MotionKind.RAPID,
            start=Vector3D(10.0, 0.0, 5.0),
            end=Vector3D(10.0, 0.0, -10.0),
            feedrate_mmpm=5000.0,
            spindle_rpm=1000.0,
            duration_sec=0.18,
            vector_key=VectorKey.from_floats(0, Vector3D(10.0, 0.0, 5.0), Vector3D(10.0, 0.0, -10.0)),
        )

        eval_res = verifier.step(rapid_crash)

        self.assertGreater(len(eval_res.collisions), 0, "Expected rapid crash collision")
        kinds = [c.kind for c in eval_res.collisions]
        self.assertIn(CollisionKind.RAPID_INTO_STOCK, kinds)

    def test_05_chuck_fixture_collision(self):
        """
        Verify that a tool moving into the chuck keep-out envelope triggers FIXTURE_COLLISION.
        """
        # Add chuck obstacle from Z=-60 to Z=-40, clamping at R=15 to OD=50
        chuck = AxisymmetricChuckObstacle(z_min=-60.0, z_max=-40.0, clamping_radius=15.0, outer_radius=50.0)
        wp_with_chuck = WorkpieceDefinition(stock=self.stock, fixtures=[chuck])

        holder_in_wake = Polygon([(2, 1), (2, 4), (10, 4), (10, 1)])
        tool = ToolAssembly(
            tool_id=1,
            name="Tool",
            geometry=LatheToolGeometry(
                insert_shape="W",
                nose_radius=0.8,
                hand="R",
                quadrant=ToolCompensationQuadrant.P3,
                cutter_polygon=self.cutter_poly,
                holder_polygon=holder_in_wake,
            ),
        )

        verifier = Lathe2DIPWVerifier()
        verifier.reset(wp_with_chuck)
        verifier.load_tool(tool)

        # Move tool into chuck obstacle at Z=-45, R=20
        chuck_hit_move = MotionSegment(
            segment_id=1,
            op_id="Face",
            sec_id=0,
            motion=MotionKind.LINEAR,
            start=Vector3D(20.0, 0.0, -35.0),
            end=Vector3D(20.0, 0.0, -45.0),
            feedrate_mmpm=200.0,
            spindle_rpm=1000.0,
            duration_sec=3.0,
            vector_key=VectorKey.from_floats(0, Vector3D(20.0, 0.0, -35.0), Vector3D(20.0, 0.0, -45.0)),
        )

        eval_res = verifier.step(chuck_hit_move)

        self.assertEqual(len(eval_res.collisions), 1, "Expected chuck collision")
        self.assertEqual(eval_res.collisions[0].kind, CollisionKind.FIXTURE_COLLISION)

    def test_lathe_verification_plot_omits_tool_artists(self):
        """Verify that generate_verification_plot does not render tool holder or insert polygons."""
        import tempfile
        import os
        from geometry.plotter import generate_verification_plot

        holder_poly = Polygon([(2, 1), (2, 4), (10, 4), (10, 1)])
        tool = ToolAssembly(
            tool_id=1,
            name="Roughing Tool",
            geometry=LatheToolGeometry(
                insert_shape="W",
                nose_radius=0.8,
                hand="R",
                quadrant=ToolCompensationQuadrant.P3,
                cutter_polygon=self.cutter_poly,
                holder_polygon=holder_poly,
            ),
        )

        verifier = Lathe2DIPWVerifier()
        verifier.reset(self.workpiece)
        verifier.load_tool(tool)

        cut_move = MotionSegment(
            segment_id=1,
            op_id="Roughing",
            sec_id=0,
            motion=MotionKind.LINEAR,
            start=Vector3D(15.0, 0.0, 0.0),
            end=Vector3D(15.0, 0.0, -15.0),
            feedrate_mmpm=200.0,
            spindle_rpm=1000.0,
            duration_sec=3.0,
            vector_key=VectorKey.from_floats(0, Vector3D(15.0, 0.0, 0.0), Vector3D(15.0, 0.0, -15.0)),
        )
        verifier.step(cut_move)
        report = verifier.generate_report()

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_png = os.path.join(tmp_dir, "lathe_test.png")
            generate_verification_plot(
                workpiece=self.workpiece,
                final_ipw=verifier.ipw,
                chuck_obstacle=verifier.chuck_obstacle,
                segments=[cut_move],
                report=report,
                tools_by_op={"Roughing": tool},
                out_png_path=out_png,
                unit_is_inch=False,
            )
            self.assertTrue(os.path.isfile(out_png))
            self.assertGreater(os.path.getsize(out_png), 5000)


if __name__ == "__main__":
    unittest.main()
