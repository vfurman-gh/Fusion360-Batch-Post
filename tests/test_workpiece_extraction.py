#!/usr/bin/env python3
"""
Test Suite: Extraction and Validation of WorkpieceDefinition (Stock & Fixtures)
via Fusion 360 Setup API and Analytical Models.

Proves:
1. Analytical WorkpieceDefinition construction and validation (AxisymmetricStock2D and AxisymmetricChuckObstacle).
2. _ExtractWorkpieceFromSetup accurately extracts stock diameter, length, ID, and clamping fixtures from native Fusion 360 setup parameters.
3. _ResolveBasePostPath safely resolves the machine post configuration.
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

from pipeline import find_autodesk_post_binary


class TestWorkpieceExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")

    def test_01_analytical_stock_and_chuck_geometry(self):
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

    def test_02_mock_fusion_setup_workpiece_extraction(self):
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

    def test_03_resolve_base_post_path(self):
        """Prove _ResolveBasePostPath safely returns existing vendor post for both turning and milling."""
        from PostProcessAll import _ResolveBasePostPath

        # When program is None, no fallback guessing is performed; returns empty string
        self.assertEqual(_ResolveBasePostPath(None), "")
        self.assertEqual(_ResolveBasePostPath(None, is_turning=True), "")
        self.assertEqual(_ResolveBasePostPath(None, is_turning=False), "")

        # Mock NCProgram with Centroid turning post for turning setup
        class MockTurningConfig:
            name = "centroid_turning"
            description = "Centroid Turning"
        class MockTurningProg:
            postConfiguration = MockTurningConfig()

        resolved_turn = _ResolveBasePostPath(MockTurningProg(), is_turning=True)
        self.assertTrue(os.path.isfile(resolved_turn))
        self.assertTrue(resolved_turn.endswith("centroid_turning.cps"))

        # Mock NCProgram with Tormach milling post for milling setup
        class MockPostConfig:
            name = "tormach"
            description = "Tormach PathPilot"
        class MockProg:
            postConfiguration = MockPostConfig()

        resolved_tormach = _ResolveBasePostPath(MockProg(), is_turning=False)
        self.assertTrue(os.path.isfile(resolved_tormach))
        self.assertTrue(resolved_tormach.endswith("tormach.cps"))

        # Incompatible turning post on milling setup rejected without guessing
        resolved_incompat = _ResolveBasePostPath(MockTurningProg(), is_turning=False)
        self.assertEqual(resolved_incompat, "")


if __name__ == "__main__":
    unittest.main()
