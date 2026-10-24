#!/usr/bin/env python3
"""
Test Suite: Extraction and Validation of WorkpieceDefinition (Stock & Fixtures)
via Fusion 360 Setup API, Analytical Models, and apt_extractor_wrapper.cps.

Proves:
1. apt_extractor_wrapper.cps generates ISO 4343 .apt output and captures .intermediate.cnc in a single pass.
2. Analytical WorkpieceDefinition construction and validation (AxisymmetricStock2D and AxisymmetricChuckObstacle).
3. _ExtractWorkpieceFromSetup accurately extracts stock diameter, length, ID, and clamping fixtures from native Fusion 360 setup parameters.
4. _ResolveBasePostPath safely resolves the machine post configuration.
"""

import glob
import json
import math
import os
import subprocess
import tempfile
import unittest

from geometry import (
    AxisymmetricStock2D,
    AxisymmetricChuckObstacle,
    WorkpieceDefinition,
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


class TestWorkpieceExtraction(unittest.TestCase):
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

    def test_03_mock_fusion_setup_workpiece_extraction(self):
        """Prove _ExtractWorkpieceFromSetup parses parameters accurately from native Setup."""
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

        class MockSetup:
            def __init__(self, stock_d):
                self.parameters = MockParams(stock_d)

        stock_params = {
            "job_stockDiameter": 3.175,       # 3.175 cm = 31.75 mm = 1.25 in
            "job_stockLength": 6.35,          # 6.35 cm = 63.5 mm = 2.5 in
            "job_stockDiameterInner": 1.27,   # 1.27 cm = 12.7 mm = 0.5 in
            "job_stockOffsetFront": 0.1,      # 0.1 cm = 1.0 mm
            "job_stockClampingLength": 1.5,   # 1.5 cm = 15.0 mm
        }

        setup = MockSetup(stock_params)

        from PostProcessAll import _ExtractWorkpieceFromSetup

        wp = _ExtractWorkpieceFromSetup(setup)
        self.assertIsNotNone(wp)
        self.assertAlmostEqual(wp.stock.outer_radius, 15.875, places=3)
        self.assertAlmostEqual(wp.stock.inner_radius, 6.35, places=3)
        self.assertAlmostEqual(wp.stock.z_max, 1.0, places=3)
        self.assertAlmostEqual(wp.stock.z_min, -62.5, places=3)
        self.assertEqual(len(wp.fixtures), 1)

    def test_04_resolve_base_post_path(self):
        """Prove _ResolveBasePostPath safely returns existing vendor post."""
        from PostProcessAll import _ResolveBasePostPath
        base_post = _ResolveBasePostPath(None)
        self.assertTrue(os.path.isfile(base_post), f"Resolved base post must exist: {base_post}")


if __name__ == "__main__":
    unittest.main()
