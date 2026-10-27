#!/usr/bin/env python3
"""
Unit and integration tests for Phase 1: From Solid Stock Support.
"""

import math
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

# Ensure mock adsk module exists if running outside Fusion
if "adsk" not in sys.modules:
    adsk = types.ModuleType("adsk")
    adsk.cam = types.ModuleType("adsk.cam")
    adsk.cam.OperationTypes = types.SimpleNamespace(TurningOperation=1, MillingOperation=0)
    adsk.cam.CAM = types.SimpleNamespace(cast=lambda x: x)
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
else:
    adsk = sys.modules["adsk"]
    if not hasattr(adsk.cam, "CAM"):
        adsk.cam.CAM = types.SimpleNamespace(cast=lambda x: x)

if not hasattr(adsk.core, "MessageBoxIconTypes"):
    adsk.core.MessageBoxIconTypes = types.SimpleNamespace(
        InformationIconType=0,
        WarningIconType=1,
        CriticalIconType=2,
        QuestionIconType=3,
    )
if not hasattr(adsk.core, "MessageBoxButtonTypes"):
    adsk.core.MessageBoxButtonTypes = types.SimpleNamespace(
        OKButtonType=0,
        YesNoButtonType=1,
    )
if not hasattr(adsk.core, "DialogResults"):
    adsk.core.DialogResults = types.SimpleNamespace(
        DialogYes=0,
        DialogNo=1,
        DialogOK=2,
    )
if not hasattr(adsk.core, "ObjectCollection"):
    class MockObjectCollection(list):
        @classmethod
        def create(cls):
            return cls()
        def add(self, item):
            self.append(item)
    adsk.core.ObjectCollection = MockObjectCollection

if not hasattr(adsk.cam, "SetupStockModes"):
    adsk.cam.SetupStockModes = types.SimpleNamespace(
        FixedBoxStock=0,
        RelativeBoxStock=1,
        FixedCylinderStock=2,
        RelativeCylinderStock=3,
        FixedTubeStock=4,
        RelativeTubeStock=5,
        SolidStock=6,
        PreviousSetupStock=7,
    )
if not hasattr(adsk.cam, "PostOutputUnitOptions"):
    adsk.cam.PostOutputUnitOptions = types.SimpleNamespace(
        DocumentUnitsOutput=0,
        InchesOutput=1,
        MillimetersOutput=2,
    )
if not hasattr(adsk.fusion, "TriangleMeshQualityOptions"):
    adsk.fusion.TriangleMeshQualityOptions = types.SimpleNamespace(
        LowQualityTriangleMesh=8,
        NormalQualityTriangleMesh=11,
        HighQualityTriangleMesh=13,
        VeryHighQualityTriangleMesh=15,
    )
if not hasattr(adsk.cam, "PostProcessInput"):
    class MockPostProcessInput:
        @classmethod
        def create(cls, programName, postConfiguration, outputFolder, outputUnits):
            inst = cls()
            inst.programName = programName
            inst.postConfiguration = postConfiguration
            inst.outputFolder = outputFolder
            inst.outputUnits = outputUnits
            inst.isOpenInEditor = False
            return inst
    adsk.cam.PostProcessInput = MockPostProcessInput


from geometry.heightfield_engine import HeightfieldGrid, MillHeightfieldVerifier
from geometry.lathe_engine import Lathe2DIPWVerifier
from geometry.models import (
    AxisymmetricStock2D,
    BoxStock3D,
    MillToolGeometry,
    MotionKind,
    MotionSegment,
    SolidMeshStock3D,
    ToolAssembly,
    Vector3D,
    WorkpieceDefinition,
)
from geometry.solid_mesh import (
    extract_mesh_from_fusion_setup,
    extract_solid_workpiece_from_fusion_setup,
    rasterize_triangles_to_grid,
    revolve_mesh_to_lathe_stock,
    transform_design_points_to_setup_wcs,
)
import PostProcessAll


class TestSolidMeshStock(unittest.TestCase):
    """Test suite for 3D solid mesh stock processing and simulation."""

    def test_transform_design_points_to_setup_wcs(self):
        """Verify coordinate frame conversion and cm -> mm scaling."""
        # 1. Identity transform with origin at (10, 20, 30) cm
        mock_wcs = MagicMock()
        mock_origin = MagicMock(x=10.0, y=20.0, z=30.0)
        mock_x = MagicMock(x=1.0, y=0.0, z=0.0)
        mock_y = MagicMock(x=0.0, y=1.0, z=0.0)
        mock_z = MagicMock(x=0.0, y=0.0, z=1.0)
        mock_wcs.getAsCoordinateSystem.return_value = (mock_origin, mock_x, mock_y, mock_z)

        # Points in design space: (10, 20, 30) cm and (15, 22, 33) cm
        pts_design_cm = np.array([
            [10.0, 20.0, 30.0],
            [15.0, 22.0, 33.0],
        ], dtype=np.float64)

        pts_setup_mm = transform_design_points_to_setup_wcs(pts_design_cm, mock_wcs)

        # First point should be at WCS origin: (0, 0, 0) mm
        np.testing.assert_allclose(pts_setup_mm[0], [0.0, 0.0, 0.0], atol=1e-5)
        # Second point: dx=5 cm -> 50 mm, dy=2 cm -> 20 mm, dz=3 cm -> 30 mm
        np.testing.assert_allclose(pts_setup_mm[1], [50.0, 20.0, 30.0], atol=1e-5)

        # 2. 90-degree Z rotation: setup X points along design Y, setup Y points along design -X
        mock_wcs_rot = MagicMock()
        del mock_wcs_rot.copy  # Force strategy 2
        mock_x_rot = MagicMock(x=0.0, y=1.0, z=0.0)
        mock_y_rot = MagicMock(x=-1.0, y=0.0, z=0.0)
        mock_z_rot = MagicMock(x=0.0, y=0.0, z=1.0)
        # Test 5-tuple SWIG return format (success, origin, x, y, z)
        mock_wcs_rot.getAsCoordinateSystem.return_value = (True, mock_origin, mock_x_rot, mock_y_rot, mock_z_rot)

        pts_rot_mm = transform_design_points_to_setup_wcs(pts_design_cm, mock_wcs_rot)
        np.testing.assert_allclose(pts_rot_mm[0], [0.0, 0.0, 0.0], atol=1e-5)
        # (15, 22, 33) - (10, 20, 30) = (5, 2, 3) cm
        # dot with X_rot (0, 1, 0) = 2 cm -> 20 mm
        # dot with Y_rot (-1, 0, 0) = -5 cm -> -50 mm
        # dot with Z_rot (0, 0, 1) = 3 cm -> 30 mm
        np.testing.assert_allclose(pts_rot_mm[1], [20.0, -50.0, 30.0], atol=1e-5)

        # 3. Strategy 1: Matrix3D copy().invert() with getCell (native Fusion 360 C++ Matrix3D)
        class MockMatrix3D:
            def __init__(self, m_cells):
                self.m = np.array(m_cells, dtype=np.float64)
            def copy(self):
                return MockMatrix3D(self.m.copy())
            def invert(self):
                self.m = np.linalg.inv(self.m)
                return True
            def getCell(self, r, c):
                return float(self.m[r, c])

        # Matrix transforming Setup WCS -> Design Space:
        # Rotated 90 deg around Z, translated by (10, 20, 30) cm
        wcs_matrix = MockMatrix3D([
            [0.0, -1.0, 0.0, 10.0],
            [1.0,  0.0, 0.0, 20.0],
            [0.0,  0.0, 1.0, 30.0],
            [0.0,  0.0, 0.0,  1.0],
        ])
        pts_mat_mm = transform_design_points_to_setup_wcs(pts_design_cm, wcs_matrix)
        np.testing.assert_allclose(pts_mat_mm[0], [0.0, 0.0, 0.0], atol=1e-5)
        np.testing.assert_allclose(pts_mat_mm[1], [20.0, -50.0, 30.0], atol=1e-5)

    def test_solid_mesh_stock_3d_model(self):
        """Verify SolidMeshStock3D bounds and initialization."""
        verts = np.array([
            [-10.0, -20.0, -5.0],
            [30.0, 40.0, 15.0],
            [10.0, 10.0, 0.0],
        ], dtype=np.float32)
        idx = np.array([[0, 1, 2]], dtype=np.int32)

        stock = SolidMeshStock3D.from_mesh(verts, idx)
        self.assertEqual(stock.x_min, -10.0)
        self.assertEqual(stock.x_max, 30.0)
        self.assertEqual(stock.y_min, -20.0)
        self.assertEqual(stock.y_max, 40.0)
        self.assertEqual(stock.z_min, -5.0)
        self.assertEqual(stock.z_max, 15.0)

        with self.assertRaises(ValueError):
            SolidMeshStock3D.from_mesh(np.empty((0, 3)), np.empty((0, 3)))

    def test_rasterize_triangles_to_heightfield_flat_box(self):
        """Verify rasterizing a flat rectangular top surface into HeightfieldGrid."""
        # 100 x 50 mm box from X=[0, 100], Y=[0, 50], Z=[-20, 10]
        # Top surface at Z=10 made of 2 triangles
        verts = np.array([
            [0.0, 0.0, 10.0],
            [100.0, 0.0, 10.0],
            [100.0, 50.0, 10.0],
            [0.0, 50.0, 10.0],
            [0.0, 0.0, -20.0],
            [100.0, 50.0, -20.0],
        ], dtype=np.float32)
        tris = np.array([
            [0, 1, 2],  # Top triangle 1
            [0, 2, 3],  # Top triangle 2
        ], dtype=np.int32)

        grid = HeightfieldGrid(cell_size_mm=1.0, pad_mm=5.0)
        grid.reset_from_mesh(verts, tris, tol_pad_z_mm=0.0)

        # Outside the [0, 100] x [0, 50] stock box (e.g. at grid padding), height should be z_floor (-20.0)
        pad_col = 1
        pad_row = 1
        self.assertAlmostEqual(grid.H[pad_row, pad_col], -20.0, places=2)

        # Inside the stock box (e.g. X=50, Y=25), height should be 10.0
        c_mid = int((50.0 - grid.grid_x_min) / grid.cell_size_mm)
        r_mid = int((25.0 - grid.grid_y_min) / grid.cell_size_mm)
        self.assertAlmostEqual(grid.H[r_mid, c_mid], 10.0, places=2)

    def test_rasterize_triangles_to_heightfield_sloped_wedge(self):
        """Verify rasterizing an inclined wedge correctly computes continuous plane heights."""
        # Wedge from X=[0, 40], Y=[0, 20], sloping from Z=0 at X=0 to Z=20 at X=40
        verts = np.array([
            [0.0, 0.0, 0.0],
            [40.0, 0.0, 20.0],
            [40.0, 20.0, 20.0],
            [0.0, 20.0, 0.0],
        ], dtype=np.float32)
        tris = np.array([
            [0, 1, 2],
            [0, 2, 3],
        ], dtype=np.int32)

        grid = HeightfieldGrid(cell_size_mm=1.0, pad_mm=5.0)
        grid.reset_from_mesh(verts, tris, tol_pad_z_mm=0.0)

        # Check height at X=10: cell center is at (10.5, 10.5), so height is 0.5 * 10.5 = 5.25 mm
        c_10 = int((10.0 - grid.grid_x_min) / grid.cell_size_mm)
        r_10 = int((10.0 - grid.grid_y_min) / grid.cell_size_mm)
        self.assertAlmostEqual(grid.H[r_10, c_10], 5.25, places=2)

        # Check height at X=30: cell center is at (30.5, 10.5), so height is 0.5 * 30.5 = 15.25 mm
        c_30 = int((30.0 - grid.grid_x_min) / grid.cell_size_mm)
        r_30 = int((10.0 - grid.grid_y_min) / grid.cell_size_mm)
        self.assertAlmostEqual(grid.H[r_30, c_30], 15.25, places=2)

    def test_mill_heightfield_verifier_with_solid_stock(self):
        """Verify MillHeightfieldVerifier executes toolpath verification on SolidMeshStock3D."""
        verts = np.array([
            [0.0, 0.0, 10.0],
            [60.0, 0.0, 10.0],
            [60.0, 40.0, 10.0],
            [0.0, 40.0, 10.0],
            [0.0, 0.0, -10.0],
            [60.0, 40.0, -10.0],
        ], dtype=np.float32)
        tris = np.array([
            [0, 1, 2],
            [0, 2, 3],
        ], dtype=np.int32)

        stock = SolidMeshStock3D.from_mesh(verts, tris)
        wp = WorkpieceDefinition(stock=stock)

        verifier = MillHeightfieldVerifier(cell_size_mm=1.0, pad_mm=5.0)
        verifier.reset(wp)
        self.assertTrue(verifier.has_valid_stock)

        # Mount 10 mm endmill
        tool = ToolAssembly(
            tool_id=1,
            name="10mm Flat Endmill",
            geometry=MillToolGeometry(
                diameter=10.0,
                corner_radius=0.0,
                flute_length=30.0,
                overall_length=50.0,
            ),
        )
        verifier.load_tool(tool)

        # Linear cutting move at Z=5 (cutting 5 mm into the 10 mm top stock)
        start_pt = Vector3D(10.0, 20.0, 5.0)
        end_pt = Vector3D(50.0, 20.0, 5.0)
        from geometry.models import VectorKey
        seg = MotionSegment(
            segment_id=1,
            op_id="op1",
            sec_id=1,
            motion=MotionKind.LINEAR,
            start=start_pt,
            end=end_pt,
            feedrate_mmpm=1000.0,
            spindle_rpm=5000.0,
            duration_sec=2.4,
            vector_key=VectorKey.from_floats(1, start_pt, end_pt),
        )
        eval_res = verifier.step(seg)
        self.assertFalse(eval_res.is_air_move)
        self.assertGreater(eval_res.material_removed_mm3, 0.0)

        # Verify stock height was carved to Z=5
        c_mid = int((30.0 - verifier.grid.grid_x_min) / verifier.grid.cell_size_mm)
        r_mid = int((20.0 - verifier.grid.grid_y_min) / verifier.grid.cell_size_mm)
        self.assertAlmostEqual(verifier.grid.H[r_mid, c_mid], 5.0, delta=0.2)

    def test_revolve_mesh_to_lathe_stock_centered(self):
        """Verify revolving a centered cylinder mesh produces a valid AxisymmetricStock2D."""
        # Generate 16-sided cylinder of radius 20 mm, length 60 mm (Z=-50 to Z=10) centered on Z
        theta = np.linspace(0, 2 * np.pi, 16, endpoint=False)
        x_ring = 20.0 * np.cos(theta)
        y_ring = 20.0 * np.sin(theta)

        verts_bottom = np.column_stack([x_ring, y_ring, np.full(16, -50.0)])
        verts_top = np.column_stack([x_ring, y_ring, np.full(16, 10.0)])
        verts = np.vstack([verts_bottom, verts_top])

        # Generate side quads as triangles
        tris = []
        for i in range(16):
            nxt = (i + 1) % 16
            # bottom i, top i, top nxt
            tris.append([i, i + 16, nxt + 16])
            # bottom i, top nxt, bottom nxt
            tris.append([i, nxt + 16, nxt])
        tris = np.array(tris, dtype=np.int32)

        wp = revolve_mesh_to_lathe_stock(verts, tris, tol_pad_r_mm=0.0)
        self.assertIsInstance(wp.stock, AxisymmetricStock2D)
        self.assertAlmostEqual(wp.stock.z_min, -50.0, places=2)
        self.assertAlmostEqual(wp.stock.z_max, 10.0, places=2)
        self.assertAlmostEqual(wp.stock.outer_radius, 20.0, places=1)
        self.assertIsNotNone(wp.stock.polygon)
        self.assertFalse(wp.stock.polygon.is_empty)

        # Verify with Lathe2DIPWVerifier.reset
        lathe_verifier = Lathe2DIPWVerifier()
        lathe_verifier.reset(wp)
        self.assertFalse(lathe_verifier.ipw.is_empty)
        # Bounding box of IPW: min Z=-50, max Z=10, max R=20
        min_z, min_r, max_z, max_r = lathe_verifier.ipw.bounds
        self.assertAlmostEqual(min_z, -50.0, places=2)
        self.assertAlmostEqual(max_z, 10.0, places=2)
        self.assertAlmostEqual(max_r, 20.0, places=1)

    def test_revolve_mesh_to_lathe_stock_off_center_rejected(self):
        """Verify revolving an off-center solid stock is rejected with an explicit error."""
        # Cylinder offset in X by 10.0 mm
        theta = np.linspace(0, 2 * np.pi, 8, endpoint=False)
        x_ring = 10.0 + 20.0 * np.cos(theta)
        y_ring = 20.0 * np.sin(theta)
        verts = np.vstack([
            np.column_stack([x_ring, y_ring, np.full(8, -50.0)]),
            np.column_stack([x_ring, y_ring, np.full(8, 10.0)]),
        ])
        tris = np.array([[0, 1, 8], [1, 9, 8]], dtype=np.int32)

        with self.assertRaises(ValueError) as err_ctx:
            revolve_mesh_to_lathe_stock(verts, tris)
        self.assertIn("not centered on the turning spindle axis", str(err_ctx.exception))

    def test_fusion_setup_solid_stock_extraction_and_pipeline(self):
        """Verify full pipeline integration extracting mock solid stock from Setup."""
        mock_setup = MagicMock()
        mock_setup.name = "Op1SolidMilling"
        mock_setup.stockMode = adsk.cam.SetupStockModes.SolidStock
        mock_setup.operationType = 0  # Milling

        # Mock WCS: identity matrix
        mock_wcs = MagicMock()
        mock_origin = MagicMock(x=0.0, y=0.0, z=0.0)
        mock_x = MagicMock(x=1.0, y=0.0, z=0.0)
        mock_y = MagicMock(x=0.0, y=1.0, z=0.0)
        mock_z = MagicMock(x=0.0, y=0.0, z=1.0)
        mock_wcs.getAsCoordinateSystem.return_value = (mock_origin, mock_x, mock_y, mock_z)
        mock_setup.workCoordinateSystem = mock_wcs

        # Mock BRepBody with meshManager
        mock_body = MagicMock()
        mock_calc = MagicMock()
        mock_mesh = MagicMock()
        # Flat surface 50 x 30 mm, Z=10 mm (in cm: 5.0 x 3.0 x 1.0)
        mock_mesh.nodeCoordinatesAsDouble = [
            0.0, 0.0, 1.0,
            5.0, 0.0, 1.0,
            5.0, 3.0, 1.0,
            0.0, 3.0, 1.0,
        ]
        mock_mesh.nodeIndices = [0, 1, 2, 0, 2, 3]
        mock_calc.calculate.return_value = mock_mesh
        mock_body.meshManager.createMeshCalculator.return_value = mock_calc

        mock_setup.stockSolids = [mock_body]

        # 1. extract_mesh_from_fusion_setup returns vertices in mm
        v_mm, idx = extract_mesh_from_fusion_setup(mock_setup)
        self.assertEqual(len(v_mm), 4)
        np.testing.assert_allclose(v_mm[1], [50.0, 0.0, 10.0])

        # 2. extract_solid_workpiece_from_fusion_setup returns SolidMeshStock3D
        wp = extract_solid_workpiece_from_fusion_setup(mock_setup, is_turning=False)
        self.assertIsInstance(wp.stock, SolidMeshStock3D)
        self.assertEqual(wp.stock.x_max, 50.0)
        self.assertEqual(wp.stock.y_max, 30.0)
        self.assertEqual(wp.stock.z_max, 10.0)

        # 3. PostProcessUnifiedPipeline executes with solid stock
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            dummy_cps = os.path.join(tmp_dir, "dummy.cps")
            with open(dummy_cps, "w", encoding="utf-8") as f:
                f.write('description = "Dummy Post";\ncapabilities = CAPABILITY_MILLING;\n')

            mock_prog = MagicMock()
            mock_prog.parameters.itemByName.return_value = MagicMock(value=MagicMock(value="tormach"))

            mock_op = MagicMock()
            mock_op.isSuppressed = False
            mock_op.hasToolpath = True
            p_tool = MagicMock()
            p_tool.value.value = 1
            mock_op.tool.parameters.itemByName.return_value = p_tool
            mock_setup.allOperations = [mock_op]

            def fake_post_process(target_ops, post_input):
                out_json = os.path.join(post_input.outputFolder, f"{post_input.programName}.json")
                with open(out_json, "w", encoding="utf-8") as f:
                    json.dump({"unit": 1, "sections": []}, f)
                return True

            mock_cam = MagicMock()
            mock_cam.postProcess.side_effect = fake_post_process

            mock_app = MagicMock()
            mock_app.activeDocument.products.itemByProductType.return_value = mock_cam

            # Mock pipeline orchestrator run
            mock_report = MagicMock()
            mock_report.collisions = []
            mock_report.promoted_count = 0
            mock_report.spike_count = 0
            mock_report.peak_mrr = 0.0
            mock_report.total_machining_time_sec = 10.0
            mock_report.total_volume_removed_mm3 = 500.0
            mock_report.promoted_vectors = []
            mock_report.mrr_spikes = []
            mock_report.summary_text = "Verification Success"

            with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
                 patch("PostProcessAll._ResolveBasePostPath", return_value=dummy_cps), \
                 patch("pipeline.PipelineOrchestrator.run", return_value=(mock_report, "G0 X0 Y0")):
                res = PostProcessAll._PostProcessUnifiedPipeline(
                    fname="Op1SolidMilling",
                    setup=mock_setup,
                    setupFolder=tmp_dir,
                    docSettings={},
                    program=mock_prog,
                    path=os.path.join(tmp_dir, "solid_milling.nc"),
                    fileExt=".nc",
                )
                self.assertIsNone(res)  # Clean success returns None (no error)


if __name__ == "__main__":
    unittest.main()
