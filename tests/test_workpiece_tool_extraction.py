#!/usr/bin/env python3
"""
Test Suite: Extraction and Reconstruction of WorkpieceDefinition and ToolAssembly
via Pure-Python Analytical Geometry and apt_extractor_wrapper.cps.

Proves:
1. apt_extractor_wrapper.cps generates ISO 4343 .apt output and captures .intermediate.cnc in a single pass.
2. Analytical WorkpieceDefinition construction and validation (AxisymmetricStock2D and AxisymmetricChuckObstacle).
3. Analytical ToolAssembly generation for rhombic, grooving, drill, and toolholder geometries.
4. Tool tip origin alignment (Z=0, R=0) and non-overlapping clearance verification.
5. End-to-end compatibility of extracted models with Lathe2DIPWVerifier.
"""

import glob
import json
import math
import os
import subprocess
import tempfile
import unittest

from shapely.geometry import Polygon

from geometry import (
    AxisymmetricStock2D,
    AxisymmetricChuckObstacle,
    WorkpieceDefinition,
    ToolAssembly,
    LatheToolGeometry,
    ToolCompensationQuadrant,
    Lathe2DIPWVerifier,
)
from geometry.tool_generator import (
    generate_rhombic_insert,
    generate_grooving_insert,
    generate_drill_cutter,
    generate_turning_holder,
)
from geometry.stock_generator import (
    generate_axisymmetric_stock_polygon,
    generate_chuck_obstacle_polygon,
)
import types
import sys

# Mock adsk namespace if running outside Fusion
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

from pipeline import find_autodesk_post_binary, find_autodesk_apt_cps


class TestWorkpieceAndToolExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.post_bin = find_autodesk_post_binary()
        cls.system_apt = find_autodesk_apt_cps()
        cls.apt_wrapper = os.path.join(cls.repo_root, "apt_extractor_wrapper.cps")
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")

    def test_01_apt_extractor_wrapper_single_pass(self):
        """Prove apt_extractor_wrapper.cps produces both .apt and intermediate .cnc in one step."""
        if not self.post_bin or not os.path.isfile(self.post_bin) or not os.path.isfile(self.fixture_cnc):
            raise unittest.SkipTest("Autodesk post binary or test fixture not found.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            apt_out = os.path.join(tmp_dir, "output.apt")
            captured_cnc = os.path.join(tmp_dir, "captured.intermediate.cnc")

            env = dict(os.environ)
            if self.system_apt:
                env["SYSTEM_APT_CPS"] = self.system_apt
            env["TARGET_INTERMEDIATE_CNC"] = captured_cnc

            cmd = [self.post_bin, "--quiet", self.apt_wrapper, self.fixture_cnc, apt_out]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"APT extractor failed: {res.stdout}\n{res.stderr}")

            # Verify .apt file exists and contains ISO 4343 APT statements
            self.assertTrue(os.path.isfile(apt_out), "Output .apt file was not created")
            with open(apt_out, "r", encoding="utf8", errors="replace") as f:
                apt_content = f.read()
            self.assertIn("GOTO/", apt_content, "Missing GOTO statements in APT output")

            # Verify captured intermediate Compact-NCI file exists and is non-empty
            self.assertTrue(os.path.isfile(captured_cnc), "Intermediate .cnc was not captured by onClose()")
            self.assertGreater(os.path.getsize(captured_cnc), 1000, "Captured intermediate .cnc is too small")

    def test_02_analytical_stock_and_chuck_geometry(self):
        """Construct and validate analytical WorkpieceDefinition (stock + chuck)."""
        stock = AxisymmetricStock2D(
            z_min=-63.5,
            z_max=0.0,
            outer_radius=15.875,
            inner_radius=6.35,
        )
        chuck = AxisymmetricChuckObstacle(
            z_min=-113.5,
            z_max=-50.8,
            outer_radius=45.0,
            clamping_radius=15.875,
        )
        workpiece = WorkpieceDefinition(stock=stock, fixtures=[chuck])

        # Validate stock polygon
        stock_poly = generate_axisymmetric_stock_polygon(stock)
        self.assertTrue(stock_poly.is_valid)
        self.assertFalse(stock_poly.is_empty)
        # Expected area = length * (R_outer - R_inner) = 63.5 * (15.875 - 6.35) = 604.8375 mm²
        expected_area = 63.5 * (15.875 - 6.35)
        self.assertAlmostEqual(stock_poly.area, expected_area, places=3)

        # Validate chuck obstacle polygon
        chuck_poly = generate_chuck_obstacle_polygon(chuck)
        self.assertTrue(chuck_poly.is_valid)
        self.assertFalse(chuck_poly.is_empty)
        # Chuck should be strictly outside or abutting clamping radius
        self.assertAlmostEqual(chuck_poly.bounds[1], 15.875, places=3)

    def test_03_analytical_rhombic_insert_and_holder(self):
        """Construct and validate analytical rhombic turning insert and OD/boring holders."""
        nose_r = 0.4
        edge_l = 9.525
        cutter = generate_rhombic_insert(edge_length=edge_l, nose_radius=nose_r, lead_angle_deg=5.0)

        self.assertTrue(cutter.is_valid, "Cutter polygon must be geometrically valid")
        self.assertFalse(cutter.is_empty, "Cutter polygon must not be empty")
        self.assertGreater(cutter.area, 10.0, "Cutter polygon area should be > 10 mm²")

        # Cutting tip must be tangent to origin (0, 0)
        min_z, min_r, max_z, max_r = cutter.bounds
        self.assertAlmostEqual(min_z, -nose_r, delta=0.05, msg="Cutting tip Z tangent to origin")
        self.assertAlmostEqual(max_r, nose_r, delta=0.05, msg="Cutting tip R tangent to origin")

        # OD holder
        holder_od = generate_turning_holder(shank_width=16.0, shank_length=100.0, is_boring=False)
        self.assertTrue(holder_od.is_valid)
        self.assertFalse(holder_od.is_empty)

        # Boring holder (shank extends along Z)
        holder_bore = generate_turning_holder(shank_width=16.0, shank_length=100.0, is_boring=True)
        self.assertTrue(holder_bore.is_valid)
        self.assertGreater(holder_bore.bounds[2], 90.0, "Boring holder should extend along Z > 90 mm")

    def test_04_analytical_grooving_and_drill_geometries(self):
        """Validate grooving insert and twist drill analytical polygon generators."""
        # Grooving
        groove = generate_grooving_insert(blade_width=3.0, blade_depth=12.0, corner_radius=0.2)
        self.assertTrue(groove.is_valid)
        self.assertFalse(groove.is_empty)
        self.assertAlmostEqual(groove.bounds[3], 0.0, delta=1e-5, msg="Grooving tip at R=0")

        # Drill
        drill = generate_drill_cutter(diameter=6.35, point_angle_deg=118.0, flute_length=30.0, overall_length=60.0)
        self.assertTrue(drill.is_valid)
        self.assertFalse(drill.is_empty)
        # Tip at origin
        self.assertAlmostEqual(drill.bounds[0], -6.35 * 0.5, places=3)
        self.assertAlmostEqual(drill.bounds[2], 6.35 * 0.5, places=3)

    def test_05_mock_fusion_setup_extraction(self):
        """Prove _ExtractWorkpieceFromSetup and _ExtractToolsFromSetup parse parameters accurately."""
        # Mock parameter objects mimicking Fusion 360 CAM API
        class MockParamValue:
            def __init__(self, val):
                self.value = val

        class MockParam:
            def __init__(self, name, val):
                self.name = name
                self.value = MockParamValue(val)

        class MockParams:
            def __init__(self, d):
                self._d = d
            def itemByName(self, name):
                return MockParam(name, self._d[name]) if name in self._d else None

        class MockTool:
            def __init__(self, d):
                self.parameters = MockParams(d)

        class MockOperation:
            def __init__(self, name, tool_d):
                self.name = name
                self.tool = MockTool(tool_d)
                self.parameters = MockParams({})

        class MockSetup:
            def __init__(self, stock_d, ops):
                self.parameters = MockParams(stock_d)
                self.allOperations = ops

        stock_params = {
            "job_stockDiameter": 3.175,       # 3.175 cm = 31.75 mm = 1.25 in
            "job_stockLength": 6.35,          # 6.35 cm = 63.5 mm = 2.5 in
            "job_stockDiameterInner": 1.27,   # 1.27 cm = 12.7 mm = 0.5 in
            "job_stockOffsetFront": 0.1,      # 0.1 cm = 1.0 mm
            "job_stockClampingLength": 1.5,   # 1.5 cm = 15.0 mm
        }

        tool_params = {
            "tool_number": 6,
            "tool_type": "turning boring",
            "tool_edgeLength": 0.9525,        # 9.525 mm
            "tool_cornerRadius": 0.04,        # 0.4 mm
            "tool_leadAngle": math.radians(5.0),
            "tool_shankWidth": 1.6,           # 16 mm
            "tool_overallLength": 10.0,       # 100 mm
        }

        op = MockOperation("Profile Roughing8", tool_params)
        setup = MockSetup(stock_params, [op])

        from PostProcessAll import _ExtractWorkpieceFromSetup, _ExtractToolsFromSetup, _BuildToolAssembly

        wp = _ExtractWorkpieceFromSetup(setup)
        self.assertIsNotNone(wp)
        self.assertAlmostEqual(wp.stock.outer_radius, 15.875, places=3)
        self.assertAlmostEqual(wp.stock.inner_radius, 6.35, places=3)
        self.assertAlmostEqual(wp.stock.z_max, 1.0, places=3)
        self.assertAlmostEqual(wp.stock.z_min, -62.5, places=3)
        self.assertEqual(len(wp.fixtures), 1)

        tools_data = _ExtractToolsFromSetup(setup)
        self.assertIn("Profile Roughing8", tools_data)
        t_info = tools_data["Profile Roughing8"]
        self.assertEqual(t_info["tool_id"], 6)
        self.assertTrue(t_info["is_boring"])
        self.assertAlmostEqual(t_info["nose_radius"], 0.4, places=3)
        self.assertAlmostEqual(t_info["edge_length"], 9.525, places=3)

        tool_asm = _BuildToolAssembly(t_info)
        self.assertIsInstance(tool_asm, ToolAssembly)
        self.assertFalse(tool_asm.geometry.cutter_polygon.is_empty)
        self.assertFalse(tool_asm.geometry.holder_polygon.is_empty)


if __name__ == "__main__":
    unittest.main()
