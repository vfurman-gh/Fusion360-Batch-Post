#!/usr/bin/env python3
"""
Test Suite: PostProcessAll.py Integration with 6-Stage Pipeline.

Validates:
1. RunLatheVerifier correctly extracts workpiece and tools from initial post pass.
2. Locates intermediate Compact-NCI toolpath (.cnc).
3. Executes 6-stage PipelineOrchestrator and Lathe2DIPWVerifier.
4. Restores native G0 rapid moves via universal_wrapper.cps.
5. Populates verifyResults with structured metrics (material removed, G0 restored, zero collisions).
"""

import os
import shutil
import sys
import tempfile
import types
import unittest

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
else:
    adsk = sys.modules["adsk"]

if not hasattr(adsk.core, "MessageBoxIconTypes"):
    adsk.core.MessageBoxIconTypes = types.SimpleNamespace(
        InformationIconType=0,
        WarningIconType=1,
        CriticalIconType=2,
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

from PostProcessAll import RunLatheVerifier, verifyResults, _RecordVerifyResult


class DummySetup:
    """Mock adsk.cam.Setup for unit testing."""
    def __init__(self, name="Setup 1 - Turning", is_turning=True):
        self.name = name
        # Emulate adsk.cam.OperationTypes.TurningOperation
        self.operationType = 1 if is_turning else 0


class TestPostProcessAllPipelineIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")
        cls.base_cps = os.path.join(cls.repo_root, "centroid_turning.cps")

        if not os.path.isfile(cls.fixture_cnc) or not os.path.isfile(cls.base_cps):
            raise unittest.SkipTest("Prerequisites not found for integration test")

    def setUp(self):
        verifyResults.clear()

    def test_run_lathe_verifier_end_to_end(self):
        """Verify RunLatheVerifier executes pipeline and updates G-code and verifyResults."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cnc_output = os.path.join(tmp_dir, "roughing_output.cnc")
            sidecar_cnc = cnc_output + ".intermediate.cnc"

            # 1. Copy fixture binary Compact-NCI to sidecar location
            shutil.copyfile(self.fixture_cnc, sidecar_cnc)

            # 2. Emulate initial post output containing metadata comments
            meta_header = (
                '%\n'
                'O1001 (ROUGHING_TEST)\n'
                ';META_STOCK: {"zLower":-2.5,"zUpper":0,"rOuter":0.625,"rInner":0.25}\n'
                ';META_OP: {"id":0,"name":"Profile Roughing8","tool":6,"post":"REAR","orientDeg":0,'
                '"insertOrient":"left","hand":"R","noseR":0.008,"comp":[-0.008,0.008],"toolType":"boring turning",'
                '"cutterSVG":"M0.000697246 0.00796956 A0.008 0.008 0 0 1 -0.00796956 -0.000697246 L0.023802 -0.363848 A0.008 0.008 0 0 1 0.0310743 -0.371121 L0.394225 -0.402892 A0.008 0.008 0 0 1 0.402892 -0.394225 L0.371121 -0.0310743 A0.008 0.008 0 0 1 0.363848 -0.023802 L0.000697246 0.00796956 Z",'
                '"holderSVG":"M0.553699 -0.0565425 L0.0080831 -0.00809344 L0.0536995 -0.491199 L0.0536995 -4.9912 L0.553699 -4.9912 L0.553699 -0.0565425 Z"}\n'
                ';META_OP: {"id":1,"name":"Profile Roughing8 (2)","tool":6,"post":"REAR","orientDeg":0,'
                '"insertOrient":"left","hand":"R","noseR":0.008,"comp":[-0.008,0.008],"toolType":"boring turning",'
                '"cutterSVG":"M0.000697246 0.00796956 A0.008 0.008 0 0 1 -0.00796956 -0.000697246 L0.023802 -0.363848 A0.008 0.008 0 0 1 0.0310743 -0.371121 L0.394225 -0.402892 A0.008 0.008 0 0 1 0.402892 -0.394225 L0.371121 -0.0310743 A0.008 0.008 0 0 1 0.363848 -0.023802 L0.000697246 0.00796956 Z",'
                '"holderSVG":"M0.553699 -0.0565425 L0.0080831 -0.00809344 L0.0536995 -0.491199 L0.0536995 -4.9912 L0.553699 -4.9912 L0.553699 -0.0565425 Z"}\n'
                ';META_OP: {"id":2,"name":"Profile Finishing6","tool":6,"post":"REAR","orientDeg":0,'
                '"insertOrient":"left","hand":"R","noseR":0.008,"comp":[-0.008,0.008],"toolType":"boring turning",'
                '"cutterSVG":"M0.000697246 0.00796956 A0.008 0.008 0 0 1 -0.00796956 -0.000697246 L0.023802 -0.363848 A0.008 0.008 0 0 1 0.0310743 -0.371121 L0.394225 -0.402892 A0.008 0.008 0 0 1 0.402892 -0.394225 L0.371121 -0.0310743 A0.008 0.008 0 0 1 0.363848 -0.023802 L0.000697246 0.00796956 Z",'
                '"holderSVG":"M0.553699 -0.0565425 L0.0080831 -0.00809344 L0.0536995 -0.491199 L0.0536995 -4.9912 L0.553699 -4.9912 L0.553699 -0.0565425 Z"}\n'
                'G20\n'
                'G90\n'
                'T0606\n'
                'M30\n'
                '%\n'
            )
            with open(cnc_output, "w", encoding="utf-8") as f:
                f.write(meta_header)

            setup = DummySetup(name="Profile Roughing", is_turning=True)
            docSettings = {"verifyGcode": True, "restore_rapid": True}

            # 3. Execute RunLatheVerifier
            err = RunLatheVerifier(cnc_output, setup, docSettings)

            # Assertions
            self.assertIsNone(err, f"Unexpected error returned from verifier: {err}")
            self.assertEqual(len(verifyResults), 1, "Expected 1 recorded verification result")

            rec = verifyResults[0]
            self.assertEqual(rec["setup"], "Profile Roughing")
            self.assertIsNone(rec["error"], "Expected zero collision error")
            self.assertGreaterEqual(rec["promoted"], 10, "Expected at least 10 restored G0 rapids")
            self.assertGreater(rec["volume_mm3"], 7000.0, "Expected > 7000 mm3 material carved")

            # Verify final G-code was rewritten with native wrapper output
            with open(cnc_output, "r", encoding="utf-8") as f:
                final_gcode = f.read()

            self.assertIn("N32 G0 X0.47", final_gcode)
            self.assertIn("N34 G1 X0.49 Z-0.07 F0.002", final_gcode)

            # Verify report markdown exists
            report_md_path = os.path.splitext(cnc_output)[0] + "_report.md"
            self.assertTrue(os.path.isfile(report_md_path))
            with open(report_md_path, "r", encoding="utf-8") as f:
                md_content = f.read()
            self.assertIn("# Toolpath Verification & Rapid-Promotion Report", md_content)
            self.assertIn("Zero collisions detected", md_content)

            # Verify verification plot png exists
            plot_png_path = os.path.splitext(cnc_output)[0] + ".png"
            self.assertTrue(os.path.isfile(plot_png_path), "Expected dual-panel verification plot image (.png)")
            self.assertGreater(os.path.getsize(plot_png_path), 5000, "Expected non-trivial PNG plot image")

            # Verify sidecar intermediate file was cleaned up
            self.assertFalse(os.path.isfile(sidecar_cnc), "Sidecar intermediate should be removed after pipeline completion")

    def test_run_lathe_verifier_with_native_setup_stock_parameters(self):
        """Verify RunLatheVerifier extracts stock from setup.parameters when ;META_STOCK is absent."""
        class MockParam:
            def __init__(self, val):
                self.value = types.SimpleNamespace(value=val)

        class MockParams:
            def __init__(self, d_outer_cm, len_cm, d_inner_cm=0.0, offset_front_cm=0.0):
                self.params = {
                    "job_stockDiameter": MockParam(d_outer_cm),
                    "job_stockLength": MockParam(len_cm),
                    "job_stockDiameterInner": MockParam(d_inner_cm),
                    "job_stockOffsetFront": MockParam(offset_front_cm),
                }

            def itemByName(self, name):
                return self.params.get(name)

        with tempfile.TemporaryDirectory() as tmp_dir:
            cnc_output = os.path.join(tmp_dir, "native_stock_output.cnc")
            sidecar_cnc = cnc_output + ".intermediate.cnc"

            # 1. Copy fixture binary Compact-NCI to sidecar location
            shutil.copyfile(self.fixture_cnc, sidecar_cnc)

            # 2. Emulate post output with tool metadata but NO ;META_STOCK: comment
            header_no_stock = (
                '%\n'
                'O1001 (NATIVE_STOCK_TEST)\n'
                ';META_OP: {"id":0,"name":"Profile Roughing8","tool":6,"post":"REAR","orientDeg":0,'
                '"insertOrient":"left","hand":"R","noseR":0.008,"comp":[-0.008,0.008],"toolType":"boring turning",'
                '"cutterSVG":"M0.000697246 0.00796956 A0.008 0.008 0 0 1 -0.00796956 -0.000697246 L0.023802 -0.363848 A0.008 0.008 0 0 1 0.0310743 -0.371121 L0.394225 -0.402892 A0.008 0.008 0 0 1 0.402892 -0.394225 L0.371121 -0.0310743 A0.008 0.008 0 0 1 0.363848 -0.023802 L0.000697246 0.00796956 Z",'
                '"holderSVG":"M0.553699 -0.0565425 L0.0080831 -0.00809344 L0.0536995 -0.491199 L0.0536995 -4.9912 L0.553699 -4.9912 L0.553699 -0.0565425 Z"}\n'
                'G20\n'
                'G90\n'
                'T0606\n'
                'M30\n'
                '%\n'
            )
            with open(cnc_output, "w", encoding="utf-8") as f:
                f.write(header_no_stock)

            # Outer diameter = 1.25 in = 3.175 cm; Length = 2.50 in = 6.35 cm; Inner diameter = 0.50 in = 1.27 cm
            setup = DummySetup(name="Profile Roughing Native", is_turning=True)
            setup.parameters = MockParams(d_outer_cm=3.175, len_cm=6.35, d_inner_cm=1.27, offset_front_cm=0.0)

            docSettings = {"verifyGcode": True, "restore_rapid": True}

            # 3. Execute RunLatheVerifier
            err = RunLatheVerifier(cnc_output, setup, docSettings)

            # Assertions
            self.assertIsNone(err, f"Unexpected error returned from verifier: {err}")
            self.assertEqual(len(verifyResults), 1, "Expected 1 recorded verification result")
            rec = verifyResults[0]
            self.assertEqual(rec["setup"], "Profile Roughing Native")
            self.assertIsNone(rec["error"])
            self.assertGreater(rec["volume_mm3"], 7000.0)


    def test_run_lathe_verifier_with_object_base_post_does_not_crash(self):
        """Verify RunLatheVerifier safely handles base_post being a PostConfiguration object without stat TypeError."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cnc_output = os.path.join(tmp_dir, "test_obj_post.cnc")
            sidecar_cnc = cnc_output + ".intermediate.cnc"
            shutil.copyfile(self.fixture_cnc, sidecar_cnc)

            with open(cnc_output, "w", encoding="utf-8") as f:
                f.write('%\nO1001\nG20\nM30\n%\n')

            class MockPostConfigurationObject:
                pass

            setup = DummySetup(name="Obj Post Setup", is_turning=True)
            docSettings = {"verifyGcode": True, "restore_rapid": False}

            # Passing MockPostConfigurationObject instance instead of string path
            err = RunLatheVerifier(cnc_output, setup, docSettings, base_post=MockPostConfigurationObject())
            # Should not raise TypeError: stat: path should be string...
            # Since workpiece was not defined on DummySetup, it safely returns None or error, but does not crash
            self.assertIsNone(err)

    def test_dumper_and_replayer_scripts_exist(self):
        """Verify constDumperScript and constReplayerScript point to existing .cps scripts."""
        from PostProcessAll import constDumperScript, constReplayerScript
        self.assertTrue(os.path.isfile(constDumperScript))
        self.assertTrue(os.path.isfile(constReplayerScript))
        self.assertTrue(constDumperScript.endswith("universal_dumper.cps"))
        self.assertTrue(constReplayerScript.endswith("universal_replayer.cps"))

    def test_unified_pipeline_executes_mill_heightfield_verifier(self):
        """Verify PipelineOrchestrator default verifier for milling is MillHeightfieldVerifier."""
        from pipeline import PipelineOrchestrator
        from geometry import BoxStock3D, WorkpieceDefinition, MillHeightfieldVerifier

        stock_posts_dir = (
            "/Users/vadimf/Library/Application Support/Autodesk/webdeploy/production/"
            "c139e2731e42a0340acc3d34de2cbdb01237a27c/Autodesk Fusion.app/Contents/"
            "Libraries/Applications/CAM360/Data/Posts"
        )
        tormach_cps = os.path.join(stock_posts_dir, "tormach.cps")
        milling_cnc = os.path.join(self.repo_root, "tests", "fixtures", "milling_drill_test.cnc")
        if not os.path.isfile(tormach_cps) or not os.path.isfile(milling_cnc):
            raise unittest.SkipTest("Tormach post or milling fixture not available")

        orchestrator = PipelineOrchestrator(
            dumper_cps=os.path.join(self.repo_root, "universal_dumper.cps"),
            replayer_cps=os.path.join(self.repo_root, "universal_replayer.cps"),
        )
        workpiece = WorkpieceDefinition(
            stock=BoxStock3D(x_min=-50.0, x_max=50.0, y_min=-50.0, y_max=50.0, z_min=-25.0, z_max=0.0)
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_nc = os.path.join(tmp_dir, "tormach_milling.nc")
            report_md = os.path.join(tmp_dir, "report.md")
            plot_png = os.path.join(tmp_dir, "tormach_milling.png")
            rep, gcode = orchestrator.run(
                intermediate_cnc_path=milling_cnc,
                base_post_path=tormach_cps,
                workpiece=workpiece,
                final_cnc_path=out_nc,
                report_output_path=report_md,
                plot_output_path=plot_png,
            )

            # Assert report is generated by MillHeightfieldVerifier
            self.assertGreater(rep.total_volume_removed_mm3, 1000.0, "Expected 3D stock volume removed")
            self.assertGreater(len(gcode), 50, "Expected non-empty emitted G-code")
            self.assertTrue(os.path.isfile(report_md), "Verification markdown report must be written")
            self.assertTrue(os.path.isfile(plot_png), "Verification plot PNG must be written for milling")
            self.assertGreater(os.path.getsize(plot_png), 10000, "Expected PNG file to have size > 10KB")
            with open(report_md, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("Toolpath Verification", content)
            self.assertIn("Total Material Removed", content)

    def test_record_verify_result_with_warning(self):
        """Verify _RecordVerifyResult records warnings and collision kinds properly."""
        from PostProcessAll import _RecordVerifyResult, verifyResults
        from geometry.models import CollisionRecord, CollisionKind, Vector3D

        setup = DummySetup(name="Collision Setup", is_turning=True)
        col = CollisionRecord(
            segment_id=1,
            vector_key="v1",
            kind=CollisionKind.RAPID_INTO_STOCK,
            timestamp_sec=0.1,
            contact_point=Vector3D(0.0, 0.0, -5.0),
            penetration_volume_mm3=10.0,
            description="Rapid into uncut stock",
        )
        _RecordVerifyResult(
            setup=setup,
            cncPath="/tmp/test.nc",
            reportText="# Report",
            collisions=[col],
            warning="1 collision(s) detected!",
            error=None,
        )
        self.assertEqual(len(verifyResults), 1)
        rec = verifyResults[0]
        self.assertEqual(rec["setup"], "Collision Setup")
        self.assertIsNone(rec["error"])
        self.assertEqual(rec["warning"], "1 collision(s) detected!")
        self.assertEqual(rec["kinds"].get("RAPID_INTO_STOCK"), 1)

    def test_show_verify_results_does_not_popup_modal_dialog(self):
        """Verify ShowVerifyResults operates silently without modal dialog popups."""
        from PostProcessAll import _RecordVerifyResult, ShowVerifyResults, verifyResults
        from geometry.models import CollisionRecord, CollisionKind, Vector3D

        setup = DummySetup(name="Warning Setup", is_turning=False)
        col = CollisionRecord(
            segment_id=5,
            vector_key="v5",
            kind=CollisionKind.HOLDER_STRIKE,
            timestamp_sec=0.5,
            contact_point=Vector3D(10.0, 20.0, -10.0),
            penetration_volume_mm3=20.0,
            description="Holder strikes stock wall",
        )
        _RecordVerifyResult(
            setup=setup,
            cncPath="/tmp/warn.nc",
            reportText="# Warning Report",
            collisions=[col],
            warning="1 collision(s) detected",
            error=None,
        )

        popup_called = []
        class MockUI:
            def messageBox(self, *args, **kwargs):
                popup_called.append(args)
                return 0

        ShowVerifyResults(MockUI())
        self.assertEqual(len(popup_called), 0, "ShowVerifyResults must never pop up modal message boxes in the user's face")

    def test_format_summary_report_specifies_operation_collisions(self):
        """Verify _format_summary_report outputs markdown table and breakdown by operation."""
        from pipeline import PipelineOrchestrator
        from geometry.models import CollisionRecord, CollisionKind, Vector3D, VerificationReport

        orchestrator = PipelineOrchestrator(
            dumper_cps=os.path.join(self.repo_root, "universal_dumper.cps"),
            replayer_cps=os.path.join(self.repo_root, "universal_replayer.cps"),
        )

        col1 = CollisionRecord(
            segment_id=42,
            vector_key="v42",
            kind=CollisionKind.RAPID_INTO_STOCK,
            timestamp_sec=12.5,
            contact_point=Vector3D(10.0, -15.0, -2.0),
            penetration_volume_mm3=8.5,
            description="Rapid crashed into raw stock",
            op_id="3D Adaptive Roughing1",
        )
        col2 = CollisionRecord(
            segment_id=88,
            vector_key="v88",
            kind=CollisionKind.RAPID_INTO_STOCK,
            timestamp_sec=25.0,
            contact_point=Vector3D(12.0, -14.0, -3.0),
            penetration_volume_mm3=4.2,
            description="Rapid crashed into raw stock again",
            op_id="3D Adaptive Roughing1",
        )
        col3 = CollisionRecord(
            segment_id=150,
            vector_key="v150",
            kind=CollisionKind.HOLDER_STRIKE,
            timestamp_sec=60.0,
            contact_point=Vector3D(20.0, 5.0, 15.0),
            penetration_volume_mm3=12.0,
            description="Holder struck uncut wall",
            op_id="2D Contour1",
        )

        report = VerificationReport(
            total_machining_time_sec=120.0,
            total_volume_removed_mm3=15000.0,
            collisions=[col1, col2, col3],
            promoted_vectors={},
            mrr_timeline=[],
            mrr_spikes=[],
            operation_names=["3D Adaptive Roughing1", "2D Contour3", "2D Contour1"],
        )

        md = orchestrator._format_summary_report(
            report=report,
            final_gcode="",
            base_post_path="/path/to/tormach.cps",
            restored_count=10,
        )

        # 1. Table header exists
        self.assertIn("### Collisions by Operation", md)
        self.assertIn("| Operation | Status | Collisions | Details by Type |", md)

        # 2. 3D Adaptive Roughing1 row: 2 collisions, RAPID_INTO_STOCK (2)
        self.assertIn("| **3D Adaptive Roughing1** | ⚠️ **Collision** | **2** | `RAPID_INTO_STOCK` (2) |", md)

        # 3. 2D Contour3 row: clean/safe
        self.assertIn("| 2D Contour3 | ✅ Safe | 0 | None |", md)

        # 4. 2D Contour1 row: 1 collision, HOLDER_STRIKE (1)
        self.assertIn("| **2D Contour1** | ⚠️ **Collision** | **1** | `HOLDER_STRIKE` (1) |", md)

        # 5. Total row
        self.assertIn("| **Total** | | **3** | |", md)

        # 6. Detailed breakdown section
        self.assertIn("### Detailed Collision Breakdown", md)
        self.assertIn("#### ⚠️ Operation: 3D Adaptive Roughing1 (2 collisions)", md)
        self.assertIn("Move 42", md)
        self.assertIn("Move 88", md)
        self.assertIn("#### ⚠️ Operation: 2D Contour1 (1 collision)", md)
        self.assertIn("Move 150", md)

    def test_non_modal_notifications_success_warning_and_failure(self):
        """Verify _NotifyPostProcessResult provides non-modal statusMessage & HTML toast without modal dialogs."""
        from PostProcessAll import _NotifyPostProcessResult, constNotificationPaletteId

        created_palettes = {}
        class MockPalette:
            def __init__(self, id, name, html):
                self.id = id
                self.name = name
                self.html = html
                self.deleted = False
                self.dockingOption = None
                self.dockingState = None
                self.incomingFromHTML = type("Incoming", (), {"add": lambda s, h: None})()

            @property
            def htmlFileURL(self):
                return self.html

            @htmlFileURL.setter
            def htmlFileURL(self, val):
                self.html = val

            def deleteMe(self):
                self.deleted = True

        class MockPalettes:
            def itemById(self, id):
                return created_palettes.get(id)

            def add2(self, id, name, html_arg, isVisible, showCloseButton, isResizable, width, height):
                if not (html_arg.startswith("<html>") or html_arg.startswith("<HTML>") or os.path.isfile(html_arg)):
                    raise RuntimeError("3 : Invalid htmlFileURL argument")
                p = MockPalette(id, name, html_arg)
                created_palettes[id] = p
                return p

        class MockUI:
            def __init__(self):
                self.statusMessage = ""
                self.palettes = MockPalettes()
                self.message_box_calls = []

            def messageBox(self, *args, **kwargs):
                self.message_box_calls.append(args)
                return 0

        mock_ui = MockUI()

        # 1. Success notification
        _NotifyPostProcessResult(
            ui=mock_ui,
            cntFiles=1,
            cntSkipped=0,
            lstSkipped="",
            outputFolder="/tmp/nc_output",
            verifyResults=[{
                "setup": "OilLid",
                "cnc": "/tmp/nc_output/OilLid.nc",
                "promoted": 68,
                "kinds": {},
                "warning": None,
                "error": None,
            }]
        )
        self.assertEqual(len(mock_ui.message_box_calls), 0, "Must NEVER call modal messageBox on success")
        self.assertIn("Post Processing Completed Successfully", mock_ui.statusMessage)
        self.assertIn("OilLid", mock_ui.statusMessage)
        self.assertIn(constNotificationPaletteId, created_palettes)
        self.assertIn("Post Processing Completed Successfully", created_palettes[constNotificationPaletteId].html)

        # 2. Warning notification (skipped setups)
        _NotifyPostProcessResult(
            ui=mock_ui,
            cntFiles=2,
            cntSkipped=1,
            lstSkipped="SetupB failed",
            outputFolder="/tmp/nc_output",
            verifyResults=[]
        )
        self.assertEqual(len(mock_ui.message_box_calls), 0, "Must NEVER call modal messageBox on warning")
        self.assertIn("Completed with Warnings", mock_ui.statusMessage)
        self.assertIn("1 setup(s) skipped", mock_ui.statusMessage)
        self.assertIn("Post Processing Completed with Warnings", created_palettes[constNotificationPaletteId].html)

        # 3. Failure notification (0 files posted)
        _NotifyPostProcessResult(
            ui=mock_ui,
            cntFiles=0,
            cntSkipped=1,
            lstSkipped="Kernel failed to solve",
            outputFolder="/tmp/nc_output",
            verifyResults=[],
            is_fatal=True
        )
        self.assertEqual(len(mock_ui.message_box_calls), 0, "Must NEVER call modal messageBox on failure")
        self.assertIn("Post Processing Failed", mock_ui.statusMessage)
        self.assertIn("Kernel failed to solve", mock_ui.statusMessage)
        self.assertIn("Post Processing Failed", created_palettes[constNotificationPaletteId].html)


if __name__ == "__main__":
    unittest.main()

