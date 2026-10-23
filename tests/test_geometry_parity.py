#!/usr/bin/env python3
"""
Parity Test Suite: Analytical Python Geometry Generators vs. Real-World Fusion Outputs.

Validates that pure-Python analytical generators in cam_geometry produce
geometrically valid, correctly positioned, and equivalent polygons matching
the ground-truth SVGs and metadata exported from Fusion 360 across all tool categories:
- Axisymmetric Stock (Solid and Hollow Tube)
- Twist Drill (Conical point + body)
- Grooving Insert (Flat nose + corner radii + relief)
- Rhombic Diamond Inserts (Tool 1 CNMG 80°, Tool 6 Boring 80°)
"""

import math
import os
import unittest
from shapely.geometry import Polygon

from geometry import (
    AxisymmetricStock2D,
    AxisymmetricChuckObstacle,
    generate_axisymmetric_stock_polygon,
    generate_chuck_obstacle_polygon,
    generate_rhombic_insert,
    generate_grooving_insert,
    generate_drill_cutter,
    generate_turning_holder,
    parse_svg_polygon,
    transform_tool_svg,
)


class TestGeometryParity(unittest.TestCase):
    # -------------------------------------------------------------------------
    # 1. Stock Parity Tests
    # -------------------------------------------------------------------------

    def test_stock_solid_cylinder_parity(self):
        """Test solid cylindrical stock matches bounding extents exactly."""
        stock = AxisymmetricStock2D(z_min=-2.5, z_max=0.0, outer_radius=0.625, inner_radius=0.0)
        poly = generate_axisymmetric_stock_polygon(stock)

        self.assertTrue(poly.is_valid)
        self.assertAlmostEqual(poly.bounds[0], -2.5, places=5)
        self.assertAlmostEqual(poly.bounds[1], 0.0, places=5)
        self.assertAlmostEqual(poly.bounds[2], 0.0, places=5)
        self.assertAlmostEqual(poly.bounds[3], 0.625, places=5)
        self.assertAlmostEqual(poly.area, 2.5 * 0.625, places=5)

    def test_stock_hollow_tube_parity(self):
        """Test hollow tube stock (with bored inner diameter) subtracts core cleanly."""
        stock = AxisymmetricStock2D(z_min=-2.5, z_max=0.0, outer_radius=0.625, inner_radius=0.25)
        poly = generate_axisymmetric_stock_polygon(stock)

        self.assertTrue(poly.is_valid)
        self.assertAlmostEqual(poly.bounds[0], -2.5, places=5)
        self.assertAlmostEqual(poly.bounds[1], 0.25, places=5)
        self.assertAlmostEqual(poly.bounds[2], 0.0, places=5)
        self.assertAlmostEqual(poly.bounds[3], 0.625, places=5)
        expected_area = 2.5 * (0.625 - 0.25)
        self.assertAlmostEqual(poly.area, expected_area, places=5)

    def test_chuck_obstacle_swept_bounds(self):
        """Test chuck obstacle keep-out geometry."""
        chuck = AxisymmetricChuckObstacle(z_min=-5.0, z_max=-2.5, outer_radius=3.0, clamping_radius=0.625)
        poly = generate_chuck_obstacle_polygon(chuck)

        self.assertTrue(poly.is_valid)
        self.assertAlmostEqual(poly.bounds[0], -5.0, places=5)
        self.assertAlmostEqual(poly.bounds[1], 0.625, places=5)
        self.assertAlmostEqual(poly.bounds[2], -2.5, places=5)
        self.assertAlmostEqual(poly.bounds[3], 3.0, places=5)

    # -------------------------------------------------------------------------
    # 2. Twist Drill Parity (Tool 15: Drill1)
    # -------------------------------------------------------------------------

    def test_drill_cutter_parity_with_fusion_svg(self):
        """
        Verify analytical drill generator matches Fusion Tool 15 Drill1 SVG
        with tip angle (118 deg), diameter (0.500 in), and flute length.
        """
        raw_svg = "M0 0 L0.25 0.150215 L0.25 4 L0.25 4 L0.25 4.5 L0 4.5 L-0.25 4.5 L-0.25 4 L-0.25 4 L-0.25 0.150215 L-0 0 Z"
        fusion_poly = parse_svg_polygon(raw_svg)
        self.assertTrue(fusion_poly.is_valid)

        analytical_poly = generate_drill_cutter(
            diameter=0.50,
            point_angle_deg=118.0,
            flute_length=4.0,
            overall_length=4.5,
        )
        self.assertTrue(analytical_poly.is_valid)

        # IoU (Intersection over Union) with Fusion polygon:
        intersection = analytical_poly.intersection(fusion_poly).area
        union = analytical_poly.union(fusion_poly).area
        iou = intersection / union
        self.assertGreater(iou, 0.9999, f"Drill IoU too low: {iou}")

    # -------------------------------------------------------------------------
    # 3. Grooving Insert Parity (Tool 3: Groove3)
    # -------------------------------------------------------------------------

    def test_grooving_insert_parity_with_fusion_svg(self):
        """
        Verify grooving generator matches Fusion Tool 3 (width=0.080 in, noseR=0.004 in).
        """
        raw_svg = "M0.0315 -0.11 L0.04 -0.004 A0.004 0.004 0 0 1 0.036 0 L-0.036 0 A0.004 0.004 0 0 1 -0.04 -0.004 L-0.0315 -0.11 L-0.0315 -0.5 L0.0315 -0.5 L0.0315 -0.11 Z"
        fusion_poly = parse_svg_polygon(raw_svg)
        self.assertTrue(fusion_poly.is_valid)

        analytical_poly = generate_grooving_insert(
            blade_width=0.080,
            blade_depth=0.11,
            corner_radius=0.004,
            shank_width=0.063,
            shank_depth=0.5,
        )
        self.assertTrue(analytical_poly.is_valid)

        intersection = analytical_poly.intersection(fusion_poly).area
        union = analytical_poly.union(fusion_poly).area
        iou = intersection / union
        self.assertGreater(iou, 0.999, f"Grooving IoU too low: {iou}")

    # -------------------------------------------------------------------------
    # 4. Diamond Insert Parity (Tool 1 & Tool 6)
    # -------------------------------------------------------------------------

    def test_tool1_diamond_insert_parity(self):
        """
        Verify analytical rhombic insert matches Fusion Tool 1 (CNMG 80°, edge=0.349 in, noseR=0.0156 in).
        """
        raw_svg = "M0.00136181 0.0155655 A0.015625 0.015625 0 0 1 -0.0155655 -0.00136181 L0.0148564 -0.349086 A0.015625 0.015625 0 0 1 0.0290602 -0.36329 L0.376785 -0.393712 A0.015625 0.015625 0 0 1 0.393712 -0.376785 L0.36329 -0.0290602 A0.015625 0.015625 0 0 1 0.349086 -0.0148564 L0.00136181 0.0155655 Z"
        fusion_poly = parse_svg_polygon(raw_svg)
        self.assertTrue(fusion_poly.is_valid)

        analytical_poly = generate_rhombic_insert(
            edge_length=0.349052,
            nose_radius=0.015625,
            lead_angle_deg=5.0,
        )
        self.assertTrue(analytical_poly.is_valid)

        intersection = analytical_poly.intersection(fusion_poly).area
        union = analytical_poly.union(fusion_poly).area
        iou = intersection / union
        self.assertGreater(iou, 0.995, f"Tool 1 IoU too low: {iou}")

    def test_tool6_boring_insert_parity(self):
        """
        Verify analytical rhombic insert matches Fusion Tool 6 (Boring 80°, edge=0.365 in, noseR=0.008 in).
        """
        raw_svg = "M0.000697246 0.00796956 A0.008 0.008 0 0 1 -0.00796956 -0.000697246 L0.023802 -0.363848 A0.008 0.008 0 0 1 0.0310743 -0.371121 L0.394225 -0.402892 A0.008 0.008 0 0 1 0.402892 -0.394225 L0.371121 -0.0310743 A0.008 0.008 0 0 1 0.363848 -0.023802 L0.000697246 0.00796956 Z"
        fusion_poly = parse_svg_polygon(raw_svg)
        self.assertTrue(fusion_poly.is_valid)

        analytical_poly = generate_rhombic_insert(
            edge_length=0.364538,
            nose_radius=0.008,
            lead_angle_deg=5.0,
        )
        self.assertTrue(analytical_poly.is_valid)

        intersection = analytical_poly.intersection(fusion_poly).area
        union = analytical_poly.union(fusion_poly).area
        iou = intersection / union
        self.assertGreater(iou, 0.998, f"Tool 6 IoU too low: {iou}")

    def test_convex_hull_sweep_equivalence(self):
        """Verify convex-hull sweeping matches quad union sweeping with IoU > 0.999."""
        from geometry.lathe_engine import sweep_linear_polygon
        insert = generate_rhombic_insert(edge_length=10.0, nose_radius=0.8, lead_angle_deg=5.0)
        p1 = (5.0, 10.0)
        p2 = (25.0, 15.0)

        poly_convex = sweep_linear_polygon(insert, p1, p2, is_convex=True)
        poly_quads = sweep_linear_polygon(insert, p1, p2, is_convex=False)

        self.assertTrue(poly_convex.is_valid)
        self.assertTrue(poly_quads.is_valid)

        iou = poly_convex.intersection(poly_quads).area / poly_convex.union(poly_quads).area
        self.assertGreater(iou, 0.999, f"Convex sweep mismatch: IoU={iou}")

    def test_circular_arc_sweep_curvature(self):
        """Verify sweep_arc_polygon accurately captures arc envelope bulge beyond chord."""
        from geometry.lathe_engine import sweep_arc_polygon, sweep_linear_polygon
        from shapely.geometry import Point
        unit_box = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        # 90 degree arc from (0, 10) to (10, 0) centered at (0, 0), CW
        arc_swept = sweep_arc_polygon(unit_box, p1=(0, 10), p2=(10, 0), center=(0, 0), is_cw=True, is_convex=True)
        chord_swept = sweep_linear_polygon(unit_box, p1=(0, 10), p2=(10, 0), is_convex=True)

        self.assertTrue(arc_swept.is_valid)
        self.assertTrue(chord_swept.is_valid)

        # The circular arc bulges out to radius ~10.6 at 45 deg, where the chord does not reach
        self.assertTrue(arc_swept.contains(Point(7.5, 7.5)))
        self.assertFalse(chord_swept.contains(Point(7.5, 7.5)))

        # Symmetric difference proves they are distinct geometric trajectories
        diff_area = arc_swept.symmetric_difference(chord_swept).area
        self.assertGreater(diff_area, 30.0)


if __name__ == "__main__":
    unittest.main()
