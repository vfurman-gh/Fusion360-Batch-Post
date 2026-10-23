#!/usr/bin/env python3
"""
Test Suite: Extraction and Reconstruction of WorkpieceDefinition and ToolAssembly
from Autodesk Intermediate Toolpaths and Post Engine Metadata.

Proves:
1. Extraction of WorkpieceDefinition (AxisymmetricStock2D: z_lower, z_upper, r_outer, r_inner).
2. Extraction of ToolAssembly (LatheToolGeometry: insert shape, nose radius, compensation).
3. Transformation of raw SVG paths into valid 2D metric Shapely Polygons for cutter and holder.
4. Verification of geometric invariants (tip alignment at origin, valid non-intersecting geometries).
"""

import glob
import json
import math
import os
import re
import subprocess
import tempfile
import unittest

from shapely.geometry import Polygon

from verify_lathe_gcode import parse_svg_polygon, transform_tool_svg, StockConfig, OperationMeta


def find_post_executable():
    patterns = [
        "/Users/*/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/post",
        "/Applications/Autodesk Fusion 360.app/Contents/Libraries/Applications/CAM360/post",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


class TestWorkpieceAndToolExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.post_bin = find_post_executable()
        if not cls.post_bin or not os.path.isfile(cls.post_bin):
            raise unittest.SkipTest("Autodesk 'post' executable not found on this system.")

        cls.base_cps = os.path.join(cls.repo_root, "centroid_turning.cps")
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")
        if not os.path.isfile(cls.fixture_cnc):
            raise unittest.SkipTest(f"Test fixture not found: {cls.fixture_cnc}")

        # Post-process intermediate fixture to generate metadata comments
        with tempfile.NamedTemporaryFile(suffix=".cnc", delete=False) as tmp_out:
            cls.post_out_path = tmp_out.name

        cmd = [cls.post_bin, "--quiet", cls.base_cps, cls.fixture_cnc, cls.post_out_path]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Post failed while generating test fixture output: {res.stderr}")

        with open(cls.post_out_path, "r") as f:
            cls.gcode_lines = f.readlines()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "post_out_path") and os.path.isfile(cls.post_out_path):
            os.remove(cls.post_out_path)

    def test_01_extract_stock_definition(self):
        """Extract and validate WorkpieceDefinition (StockConfig) from ;META_STOCK comment."""
        meta_stock_line = None
        for line in self.gcode_lines:
            if line.startswith(";META_STOCK:"):
                meta_stock_line = line.strip()
                break

        self.assertIsNotNone(meta_stock_line, "Missing ;META_STOCK comment in post output")

        payload = json.loads(meta_stock_line.split(":", 1)[1])
        stock = StockConfig(
            z_lower=payload["zLower"],
            z_upper=payload["zUpper"],
            r_outer=payload["rOuter"],
            r_inner=payload.get("rInner", 0.0),
            part_z_lower=payload.get("partZLower"),
            part_z_upper=payload.get("partZUpper"),
            part_r_outer=payload.get("partROuter"),
        )

        # Invariant checks for lathe workpiece
        self.assertLess(stock.z_lower, stock.z_upper, "z_lower must be less than z_upper")
        self.assertGreater(stock.r_outer, 0.0, "r_outer must be positive")
        self.assertGreaterEqual(stock.r_inner, 0.0, "r_inner must be non-negative")
        self.assertLess(stock.r_inner, stock.r_outer, "r_inner must be less than r_outer")

        # Specific fixture checks
        self.assertAlmostEqual(stock.z_lower, -2.5, places=3)
        self.assertAlmostEqual(stock.z_upper, 0.0, places=3)
        self.assertAlmostEqual(stock.r_outer, 0.625, places=3)
        self.assertAlmostEqual(stock.r_inner, 0.25, places=3)

    def test_02_extract_tool_assemblies(self):
        """Extract and validate ToolAssembly geometries for all operations from ;META_OP comments."""
        meta_op_lines = [l.strip() for l in self.gcode_lines if l.startswith(";META_OP:")]
        self.assertGreater(len(meta_op_lines), 0, "No ;META_OP comments found")

        operations = []
        for line in meta_op_lines:
            payload = json.loads(line.split(":", 1)[1])
            comp = (payload["comp"][0], payload["comp"][1])
            ttype = payload.get("toolType", "")

            # Reconstruct Shapely geometries from raw SVG strings
            cutter = transform_tool_svg(
                payload["cutterSVG"], payload["orientDeg"], comp, payload["post"], tool_type=ttype
            )
            holder = transform_tool_svg(
                payload["holderSVG"], payload["orientDeg"], comp, payload["post"], tool_type=ttype
            )
            holder_clean = holder.difference(cutter.buffer(0.0015))

            op = OperationMeta(
                id=payload["id"],
                name=payload["name"],
                tool=payload["tool"],
                post=payload["post"],
                orient_deg=payload["orientDeg"],
                insert_orient=payload["insertOrient"],
                hand=payload["hand"],
                nose_r=payload["noseR"],
                comp=comp,
                cutter_poly=cutter,
                holder_poly=holder_clean,
                tool_type=ttype,
            )
            operations.append(op)

        # Validate extracted operations
        self.assertEqual(len(operations), 3)

        op0 = operations[0]
        self.assertEqual(op0.tool, 6)
        self.assertEqual(op0.name, "Profile Roughing8")
        self.assertEqual(op0.post, "REAR")
        self.assertAlmostEqual(op0.nose_r, 0.008, places=4)

        # Validate cutter polygon geometry
        self.assertFalse(op0.cutter_poly.is_empty, "Cutter polygon is empty")
        self.assertTrue(op0.cutter_poly.is_valid, "Cutter polygon is mathematically invalid")
        self.assertGreater(op0.cutter_poly.area, 0.05, "Cutter polygon has unexpectedly tiny area")

        # Validate holder polygon geometry
        self.assertFalse(op0.holder_poly.is_empty, "Holder polygon is empty")
        self.assertTrue(op0.holder_poly.is_valid, "Holder polygon is mathematically invalid")
        self.assertGreater(op0.holder_poly.area, 1.0, "Holder polygon has unexpectedly tiny area")

    def test_03_tool_tip_origin_alignment(self):
        """Prove that transformed cutter polygon tip accurately aligns with (Z=0, R=0)."""
        meta_op_lines = [l.strip() for l in self.gcode_lines if l.startswith(";META_OP:")]
        payload = json.loads(meta_op_lines[0].split(":", 1)[1])
        comp = (payload["comp"][0], payload["comp"][1])
        cutter = transform_tool_svg(
            payload["cutterSVG"], payload["orientDeg"], comp, payload["post"], tool_type=payload["toolType"]
        )

        min_z, min_r, max_z, max_r = cutter.bounds
        # The theoretical cutting tip should be tangent to the origin (within nose radius margin)
        self.assertAlmostEqual(min_z, 0.0, delta=0.015, msg="Cutting edge should touch Z=0 reference")
        self.assertAlmostEqual(max_r, 0.0, delta=0.015, msg="Cutting edge should touch R=0 reference")


if __name__ == "__main__":
    unittest.main()
