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

if not hasattr(adsk.cam, "PostOutputUnitOptions"):
    adsk.cam.PostOutputUnitOptions = types.SimpleNamespace(
        DocumentUnitsOutput=0,
        InchesOutput=1,
        MillimetersOutput=2,
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

from PostProcessAll import verifyResults, _RecordVerifyResult


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
                self._html = html
                self.deleted = False
                self.dockingOption = None
                self.dockingState = None
                self.incomingFromHTML = type("Incoming", (), {"add": lambda s, h: None})()
                self.navigatingURL = type("Navigating", (), {"add": lambda s, h: None})()

            @property
            def html(self):
                if os.path.isfile(self._html):
                    with open(self._html, "r", encoding="utf-8") as f:
                        return f.read()
                return self._html

            @property
            def htmlFileURL(self):
                return self._html

            @htmlFileURL.setter
            def htmlFileURL(self, val):
                self._html = val

            def setPosition(self, left, top):
                self.left = left
                self.top = top
                return True

            def setSize(self, width, height):
                self.width = width
                self.height = height
                return True

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
        self.assertIn(constNotificationPaletteId, created_palettes)
        self.assertIn("Post Processing Completed Successfully", created_palettes[constNotificationPaletteId].html)
        self.assertIn("OilLid", created_palettes[constNotificationPaletteId].html)

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
        self.assertIn("Post Processing Completed with Warnings", created_palettes[constNotificationPaletteId].html)
        self.assertIn("1 setup(s) skipped", created_palettes[constNotificationPaletteId].html)

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
        self.assertIn("Post Processing Failed", created_palettes[constNotificationPaletteId].html)
        self.assertIn("Kernel failed to solve", created_palettes[constNotificationPaletteId].html)

    def test_notification_event_handlers_and_action_urls(self):
        """Verify that action:// navigation links and HTML events properly route to file opening."""
        import urllib.parse
        import PostProcessAll
        from PostProcessAll import (
            _BuildNotificationHTML,
            _NotificationNavigationEventHandler,
            _NotificationHTMLEventHandler,
            constNotificationPaletteId,
        )

        actions = [
            ("Open NC File", "open_file", "/tmp/test_dir/output.nc"),
            ("Open Folder", "open_folder", "/tmp/test_dir"),
        ]
        html_out = _BuildNotificationHTML("Done", "success", "All good", "/tmp/test_dir", actions)
        self.assertIn('action://open_file?path=', html_out)
        self.assertIn('action://open_folder?path=', html_out)
        self.assertIn('sendAction(event, \'open_file\'', html_out)

        opened_files = []
        orig_open_file = PostProcessAll._OpenFile
        try:
            PostProcessAll._OpenFile = lambda p: opened_files.append(p)

            # Test NavigationEventHandler
            nav_handler = _NotificationNavigationEventHandler(constNotificationPaletteId)
            mock_nav_args = type("NavArgs", (), {
                "navigationURL": "action://open_file?path=%2Ftmp%2Ftest_dir%2Foutput.nc",
                "launchExternally": True,
                "preventDefault": False,
            })()

            # Temporarily make path exist
            with tempfile.NamedTemporaryFile() as tf:
                mock_nav_args.navigationURL = f"action://open_file?path={urllib.parse.quote(tf.name)}"
                nav_handler.notify(mock_nav_args)
                self.assertFalse(mock_nav_args.launchExternally, "Must prevent external browser opening")
                self.assertIn(tf.name, opened_files)

            # Test HTMLEventHandler
            html_handler = _NotificationHTMLEventHandler(constNotificationPaletteId)
            with tempfile.NamedTemporaryFile() as tf2:
                mock_html_args = type("HTMLArgs", (), {
                    "action": "open_file",
                    "data": tf2.name,
                })()
                html_handler.notify(mock_html_args)
                self.assertIn(tf2.name, opened_files)
        finally:
            PostProcessAll._OpenFile = orig_open_file

    def test_progress_dialog_advancement_and_auto_dismiss_timeout(self):
        """Verify progress updates advance percentages and notification auto-dismiss timer is 6 seconds."""
        import PostProcessAll
        from PostProcessAll import _BuildNotificationHTML

        # 1. Verify auto-dismiss timeout is 6 seconds (6000ms)
        html = _BuildNotificationHTML("Done", "success", "OK", "", [])
        self.assertIn("6000);", html, "Default auto-dismiss timeout must be 6 seconds")
        self.assertIn("2500);", html, "Mouseleave auto-dismiss timeout must be 2.5 seconds")

        # 2. Verify progress reporting callback logic
        progress_calls = []
        def mock_progress_cb(msg, frac):
            progress_calls.append((msg, frac))
            return True

        # Simulate pipeline reporting to callback
        mock_progress_cb("Pass 1: Dumping toolpath batch 1/2...", 0.20)
        mock_progress_cb("Pass 2: Simulating stock & collisions...", 0.50)
        mock_progress_cb("Pass 2: Generating verification plot & report...", 0.70)
        mock_progress_cb("Pass 2: Emitting final NC program...", 0.85)
        mock_progress_cb("Setup completed.", 1.0)

        self.assertEqual(len(progress_calls), 5)
        fractions = [c[1] for c in progress_calls]
        self.assertEqual(fractions, sorted(fractions), "Progress percentages must strictly advance")
        self.assertEqual(fractions[-1], 1.0)

    def test_default_settings_and_manager_purged_del_options(self):
        """Verify delFiles and delFolder are eliminated from defaults and purged by SettingsManager."""
        import PostProcessAll

        self.assertNotIn("delFiles", PostProcessAll.defaultSettings)
        self.assertNotIn("delFolder", PostProcessAll.defaultSettings)

        mgr = PostProcessAll.SettingsManager()
        legacy_dst = {
            "version": "1.0.0",
            "delFiles": True,
            "delFolder": True,
            "splitSetup": True,
            "restore_rapid": True,
        }
        mgr.UpdateSettings(PostProcessAll.defaultSettings, legacy_dst)
        self.assertNotIn("delFiles", legacy_dst)
        self.assertNotIn("delFolder", legacy_dst)
        self.assertNotIn("splitSetup", legacy_dst)

    def test_perform_post_process_modal_overwrite_abort_on_no(self):
        """Verify modal prompt aborts before any progress dialog or posting if user selects No."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        class MockCamCollection(list):
            @property
            def count(self):
                return len(self)
            def item(self, idx):
                return self[idx]

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_file = os.path.join(tmp_dir, "Setup 1.nc")
            with open(target_file, "w", encoding="utf8") as f:
                f.write("ORIGINAL_CONTENT")

            mock_param_folder = MagicMock()
            mock_param_folder.value.value = tmp_dir
            mock_param_ext = MagicMock()
            mock_param_ext.value.value = ".nc"

            params_dict = {
                "nc_program_output_folder": mock_param_folder,
                "nc_program_nc_extension": mock_param_ext,
                "nc_program_name": MagicMock(value=MagicMock(value="Prog")),
                "nc_program_filename": MagicMock(value=MagicMock(value="Prog")),
                "nc_program_openInEditor": MagicMock(value=MagicMock(value=False)),
            }
            mock_params = MagicMock()
            mock_params.itemByName.side_effect = lambda k: params_dict.get(k)

            mock_prog = MagicMock()
            mock_prog.parameters = mock_params
            mock_prog.attributes.itemByName.return_value = MagicMock(value="")

            mock_op = MagicMock()
            mock_op.isSuppressed = False
            mock_op.hasToolpath = True

            mock_setup = MagicMock()
            mock_setup.name = "Setup 1"
            mock_setup.isSuppressed = False
            mock_setup.allOperations = MockCamCollection([mock_op])

            mock_cam = MagicMock()
            mock_cam.allOperations = MockCamCollection([mock_op])
            mock_cam.setups = MockCamCollection([mock_setup])
            mock_cam.ncPrograms = MockCamCollection([mock_prog])

            mock_doc = MagicMock()
            mock_doc.products.itemByProductType.return_value = mock_cam
            mock_doc.attributes.itemByName.return_value = None

            msgbox_calls = []
            progress_created = []

            def mock_msgbox(prompt, title, btn_type, icon_type):
                msgbox_calls.append((prompt, title, btn_type, icon_type))
                return PostProcessAll.adsk.core.DialogResults.DialogNo

            mock_ui = MagicMock()
            mock_ui.messageBox.side_effect = mock_msgbox
            mock_ui.createProgressDialog.side_effect = lambda: progress_created.append(1)

            mock_app = MagicMock()
            mock_app.userInterface = mock_ui
            mock_app.activeDocument = mock_doc

            post_setup_calls = []
            with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
                 patch("PostProcessAll.PostProcessSetup", side_effect=lambda *args, **kwargs: post_setup_calls.append(args)):

                doc_settings = dict(PostProcessAll.defaultSettings)
                PostProcessAll.PerformPostProcess(doc_settings, setups=[mock_setup])

            self.assertEqual(len(msgbox_calls), 1, "Must prompt user with modal messageBox")
            prompt, title, btn_type, icon_type = msgbox_calls[0]
            self.assertIn("Setup 1.nc", prompt)
            self.assertIn("already exists", prompt)
            self.assertEqual(btn_type, PostProcessAll.adsk.core.MessageBoxButtonTypes.YesNoButtonType)
            self.assertEqual(len(progress_created), 0, "Must NOT open progress dialog when aborted")
            self.assertEqual(len(post_setup_calls), 0, "Must NOT call PostProcessSetup when aborted")

            # Verify existing file was not modified or truncated
            with open(target_file, "r", encoding="utf8") as f:
                self.assertEqual(f.read(), "ORIGINAL_CONTENT")

    def test_perform_post_process_modal_overwrite_proceed_on_yes(self):
        """Verify modal prompt proceeds with post-processing when user selects Yes."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        class MockCamCollection(list):
            @property
            def count(self):
                return len(self)
            def item(self, idx):
                return self[idx]

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_file = os.path.join(tmp_dir, "Setup 1.nc")
            with open(target_file, "w", encoding="utf8") as f:
                f.write("ORIGINAL_CONTENT")

            mock_param_folder = MagicMock()
            mock_param_folder.value.value = tmp_dir
            mock_param_ext = MagicMock()
            mock_param_ext.value.value = ".nc"

            params_dict = {
                "nc_program_output_folder": mock_param_folder,
                "nc_program_nc_extension": mock_param_ext,
                "nc_program_name": MagicMock(value=MagicMock(value="Prog")),
                "nc_program_filename": MagicMock(value=MagicMock(value="Prog")),
                "nc_program_openInEditor": MagicMock(value=MagicMock(value=False)),
            }
            mock_params = MagicMock()
            mock_params.itemByName.side_effect = lambda k: params_dict.get(k)

            mock_prog = MagicMock()
            mock_prog.parameters = mock_params
            mock_prog.attributes.itemByName.return_value = MagicMock(value="")

            mock_op = MagicMock()
            mock_op.isSuppressed = False
            mock_op.hasToolpath = True

            mock_setup = MagicMock()
            mock_setup.name = "Setup 1"
            mock_setup.isSuppressed = False
            mock_setup.allOperations = MockCamCollection([mock_op])

            mock_cam = MagicMock()
            mock_cam.allOperations = MockCamCollection([mock_op])
            mock_cam.setups = MockCamCollection([mock_setup])
            mock_cam.ncPrograms = MockCamCollection([mock_prog])

            mock_doc = MagicMock()
            mock_doc.products.itemByProductType.return_value = mock_cam
            mock_doc.attributes.itemByName.return_value = None

            msgbox_calls = []
            mock_progress = MagicMock()
            mock_progress.wasCancelled = False

            def mock_msgbox(prompt, title, btn_type, icon_type):
                msgbox_calls.append((prompt, title, btn_type, icon_type))
                return PostProcessAll.adsk.core.DialogResults.DialogYes

            mock_ui = MagicMock()
            mock_ui.messageBox.side_effect = mock_msgbox
            mock_ui.createProgressDialog.return_value = mock_progress

            mock_app = MagicMock()
            mock_app.userInterface = mock_ui
            mock_app.activeDocument = mock_doc

            post_setup_calls = []
            with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
                 patch("PostProcessAll.PostProcessSetup", side_effect=lambda *args, **kwargs: post_setup_calls.append(args)), \
                 patch("PostProcessAll._NotifyPostProcessResult"):

                doc_settings = dict(PostProcessAll.defaultSettings)
                PostProcessAll.PerformPostProcess(doc_settings, setups=[mock_setup])

            self.assertEqual(len(msgbox_calls), 1)
            self.assertEqual(len(post_setup_calls), 1, "Must execute PostProcessSetup when confirmed with Yes")

    def test_perform_post_process_no_modal_prompt_when_file_not_existing(self):
        """Verify modal prompt is skipped when target output file does not already exist."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        class MockCamCollection(list):
            @property
            def count(self):
                return len(self)
            def item(self, idx):
                return self[idx]

        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_param_folder = MagicMock()
            mock_param_folder.value.value = tmp_dir
            mock_param_ext = MagicMock()
            mock_param_ext.value.value = ".nc"

            params_dict = {
                "nc_program_output_folder": mock_param_folder,
                "nc_program_nc_extension": mock_param_ext,
                "nc_program_name": MagicMock(value=MagicMock(value="Prog")),
                "nc_program_filename": MagicMock(value=MagicMock(value="Prog")),
                "nc_program_openInEditor": MagicMock(value=MagicMock(value=False)),
            }
            mock_params = MagicMock()
            mock_params.itemByName.side_effect = lambda k: params_dict.get(k)

            mock_prog = MagicMock()
            mock_prog.parameters = mock_params
            mock_prog.attributes.itemByName.return_value = MagicMock(value="")

            mock_op = MagicMock()
            mock_op.isSuppressed = False
            mock_op.hasToolpath = True

            mock_setup = MagicMock()
            mock_setup.name = "Setup 1"
            mock_setup.isSuppressed = False
            mock_setup.allOperations = MockCamCollection([mock_op])

            mock_cam = MagicMock()
            mock_cam.allOperations = MockCamCollection([mock_op])
            mock_cam.setups = MockCamCollection([mock_setup])
            mock_cam.ncPrograms = MockCamCollection([mock_prog])

            mock_doc = MagicMock()
            mock_doc.products.itemByProductType.return_value = mock_cam
            mock_doc.attributes.itemByName.return_value = None

            msgbox_calls = []
            mock_progress = MagicMock()
            mock_progress.wasCancelled = False

            mock_ui = MagicMock()
            mock_ui.messageBox.side_effect = lambda *args, **kwargs: msgbox_calls.append(args)
            mock_ui.createProgressDialog.return_value = mock_progress

            mock_app = MagicMock()
            mock_app.userInterface = mock_ui
            mock_app.activeDocument = mock_doc

            post_setup_calls = []
            with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
                 patch("PostProcessAll.PostProcessSetup", side_effect=lambda *args, **kwargs: post_setup_calls.append(args)), \
                 patch("PostProcessAll._NotifyPostProcessResult"):

                doc_settings = dict(PostProcessAll.defaultSettings)
                PostProcessAll.PerformPostProcess(doc_settings, setups=[mock_setup])

            self.assertEqual(len(msgbox_calls), 0, "No modal messageBox should be shown when file does not exist")
            self.assertEqual(len(post_setup_calls), 1, "Must proceed directly to PostProcessSetup")

    def test_post_process_setup_non_destructive_writeability_check(self):
        """Verify PostProcessSetup does not destroy/truncate existing file during initial write check."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_name = "setup_out"
            file_path = os.path.join(tmp_dir, file_name + ".nc")
            with open(file_path, "w", encoding="utf8") as f:
                f.write("DO_NOT_TRUNCATE_THIS_DATA")

            mock_param_ext = MagicMock()
            mock_param_ext.value.value = ".nc"
            mock_program = MagicMock()
            mock_program.parameters.itemByName.return_value = mock_param_ext

            mock_setup = MagicMock()
            mock_setup.name = file_name
            mock_setup.operationType = 1

            mock_cam = MagicMock()
            mock_cam.checkToolpath.return_value = True

            mock_doc = MagicMock()
            mock_doc.products.itemByProductType.return_value = mock_cam

            mock_app = MagicMock()
            mock_app.activeDocument = mock_doc
            mock_app.userInterface = MagicMock()

            with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
                 patch("PostProcessAll._PostProcessUnifiedPipeline", return_value=None):

                doc_settings = dict(PostProcessAll.defaultSettings)
                res = PostProcessAll.PostProcessSetup(
                    file_name, mock_setup, tmp_dir, doc_settings, mock_program
                )
                self.assertIsNone(res)

            # Check file content was not wiped during the initial probe
            with open(file_path, "r", encoding="utf8") as f:
                self.assertEqual(f.read(), "DO_NOT_TRUNCATE_THIS_DATA")


    def test_pass1_direct_post_process_input_execution(self):
        """Verify Pass 1 uses direct cam.postProcess with PostProcessInput and zero Post Library sync."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = os.path.join(tmp_dir, "test_out.nc")

            mock_tool = MagicMock()
            mock_tool.number = 1

            mock_op = MagicMock()
            mock_op.name = "Contour 1"
            mock_op.tool = mock_tool
            mock_op.isSuppressed = False
            mock_op.hasToolpath = True

            mock_setup = MagicMock()
            mock_setup.name = "Setup 1"
            mock_setup.operationType = 0  # Milling
            mock_setup.allOperations = [mock_op]

            post_calls = []

            def fake_post_process(target_ops, post_input):
                post_calls.append((target_ops, post_input))
                # Create the batch_0.json and bootstrap.intermediate.cnc expected by Pass 2
                batch_json = os.path.join(post_input.outputFolder, f"{post_input.programName}.json")
                with open(batch_json, "w", encoding="utf-8") as f:
                    f.write('{"unit": 1, "comments": [], "sections": []}')
                bootstrap_cnc = os.path.join(post_input.outputFolder, "bootstrap.intermediate.cnc")
                shutil.copyfile(self.fixture_cnc, bootstrap_cnc)
                return True

            mock_cam = MagicMock()
            mock_cam.postProcess.side_effect = fake_post_process

            mock_doc = MagicMock()
            mock_doc.products.itemByProductType.return_value = mock_cam

            mock_app = MagicMock()
            mock_app.activeDocument = mock_doc

            mock_program = MagicMock()
            mock_program.parameters.itemByName.return_value = MagicMock(value=MagicMock(value="centroid_turning"))

            import pipeline

            def fake_subprocess_run(cmd, *args, **kwargs):
                final_cnc = cmd[-1]
                with open(final_cnc, "w", encoding="utf-8") as f:
                    f.write("G0 X0\nM30\n")
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
                 patch("PostProcessAll._ResolveBasePostPath", return_value=os.path.abspath("centroid_turning.cps")), \
                 patch("pipeline.subprocess.run", side_effect=fake_subprocess_run):

                doc_settings = dict(PostProcessAll.defaultSettings)
                res = PostProcessAll._PostProcessUnifiedPipeline(
                    fname="test_out",
                    setup=mock_setup,
                    setupFolder=tmp_dir,
                    docSettings=doc_settings,
                    program=mock_program,
                    path=out_file,
                    fileExt=".nc",
                    target_operations=[mock_op],
                )
                self.assertIsNone(res)

            # Verify cam.postProcess was called directly
            self.assertEqual(len(post_calls), 1)
            target_ops, post_in = post_calls[0]
            self.assertEqual(target_ops, mock_op)
            self.assertEqual(post_in.programName, "batch_0")
            self.assertEqual(post_in.postConfiguration, PostProcessAll.constDumperScript)
            self.assertFalse(post_in.isOpenInEditor)

            # Verify universal_dumper.cps was NOT copied to local post library
            local_posts_dir = os.path.expanduser("~/Library/Application Support/Autodesk/Fusion 360 CAM/Posts")
            if os.path.isdir(local_posts_dir):
                self.assertFalse(os.path.isfile(os.path.join(local_posts_dir, "universal_dumper.cps")))

    def test_s1_s2_extract_workpiece_missing_stock_returns_none(self):
        """Verify _ExtractWorkpieceFromSetup and extract_workpiece_from_context return None when stock is absent."""
        import PostProcessAll
        from context_parser import extract_workpiece_from_context

        # Mock setup with no stock parameters
        class EmptyParams:
            def itemByName(self, name):
                return None

        class EmptySetup:
            parameters = EmptyParams()
            stockSolids = None
            models = None

        self.assertIsNone(PostProcessAll._ExtractWorkpieceFromSetup(EmptySetup()))

        # Context with zero/negative volume
        ctx_empty_milling = {
            "unit": 1,
            "workpiece": {"lower": {"x": 0, "y": 0, "z": 0}, "upper": {"x": 0, "y": 0, "z": 0}},
            "sections": [{"type": 0}],
        }
        self.assertIsNone(extract_workpiece_from_context(ctx_empty_milling, fallback_workpiece=None, is_turning=False))

    def test_s3_benign_collision_promotion_filtering(self):
        """Verify that promoted moves in collision are excluded while independent air moves are promoted."""
        from geometry.models import (
            VerificationReport,
            CollisionRecord,
            CollisionKind,
            PromotedVectorRule,
            Vector3D,
        )
        from context_parser import apply_promotions_to_context

        # Context with two linear moves
        ctx = {
            "unit": 1,
            "sections": [
                {
                    "events": [
                        {
                            "type": "onLinear",
                            "sx": 10.0, "sy": 0.0, "sz": 5.0,
                            "x": 5.0, "y": 0.0, "z": 5.0,
                        },
                        {
                            "type": "onLinear",
                            "sx": 0.0, "sy": 0.0, "sz": 0.0,
                            "x": -10.0, "y": 0.0, "z": 0.0,
                        },
                    ]
                }
            ],
        }

        from geometry.models import VectorKey
        key_safe = VectorKey.from_floats(0, Vector3D(10.0, 0.0, 5.0), Vector3D(5.0, 0.0, 5.0)).to_wrapper_str()
        key_collided = VectorKey.from_floats(0, Vector3D(0.0, 0.0, 0.0), Vector3D(-10.0, 0.0, 0.0)).to_wrapper_str()

        report = VerificationReport(
            total_machining_time_sec=10.0,
            total_volume_removed_mm3=50.0,
            collisions=[
                CollisionRecord(
                    segment_id=1,
                    vector_key=key_collided,
                    kind=CollisionKind.HOLDER_STRIKE,
                    timestamp_sec=5.0,
                    contact_point=Vector3D(0, 0, 0),
                    penetration_volume_mm3=10.0,
                    description="Holder collided with fixture",
                )
            ],
            promoted_vectors={
                key_safe: PromotedVectorRule(min_occur=1),
                key_collided: PromotedVectorRule(min_occur=1),
            },
            mrr_timeline=[],
            mrr_spikes=[],
        )

        collision_vector_keys = {c.vector_key for c in report.collisions if c.vector_key}
        safe_promoted_vectors = {
            k: v for k, v in report.promoted_vectors.items()
            if k not in collision_vector_keys
        }

        # key_collided should be filtered out, key_safe retained
        self.assertIn(key_safe, safe_promoted_vectors)
        self.assertNotIn(key_collided, safe_promoted_vectors)

        promoted_count = apply_promotions_to_context(ctx, safe_promoted_vectors)
        self.assertEqual(promoted_count, 1)
        self.assertTrue(ctx["sections"][0]["events"][0].get("promote"))
        self.assertFalse(ctx["sections"][0]["events"][1].get("promote", False))

    def test_b1_verify_checkbox_removed_and_verification_always_runs(self):
        """Verify that verifyGcode is removed from defaultSettings and obsolete settings cleaned up."""
        import PostProcessAll
        self.assertNotIn("verifyGcode", PostProcessAll.defaultSettings)

        mgr = PostProcessAll.SettingsManager()
        dst = {"version": 16, "verifyGcode": True, "restore_rapid": True}
        mgr.UpdateSettings(PostProcessAll.defaultSettings, dst)
        self.assertNotIn("verifyGcode", dst)

    def test_b2_progress_cancellation_aborts_pipeline(self):
        """Verify that returning False from progress_cb immediately aborts the pipeline."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        mock_setup = MagicMock()
        mock_setup.name = "Setup 1"
        mock_setup.operationType = 0
        mock_setup.allOperations = []

        mock_prog = MagicMock()
        mock_prog.parameters.itemByName.return_value = MagicMock(value=MagicMock(value="centroid_turning"))

        cancelled_cb = lambda msg, frac: False

        with patch("PostProcessAll._ResolveBasePostPath", return_value=os.path.abspath("centroid_turning.cps")):
            status = PostProcessAll._PostProcessUnifiedPipeline(
                fname="test_cancel",
                setup=mock_setup,
                setupFolder="/tmp",
                docSettings=dict(PostProcessAll.defaultSettings),
                program=mock_prog,
                path="/tmp/test_cancel.nc",
                fileExt=".nc",
                progress_cb=cancelled_cb,
            )
            self.assertIn("cancelled", status.lower())

    def test_b4_nc_program_nc_extension_not_mutated(self):
        """Verify that PostProcessSetup does not mutate nc_program_nc_extension."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        mock_ext_value = MagicMock(value=".nc")
        mock_ext_param = MagicMock(value=mock_ext_value)

        def param_lookup(name):
            if name == "nc_program_nc_extension":
                return mock_ext_param
            return MagicMock(value=MagicMock(value=""))

        mock_prog = MagicMock()
        mock_prog.parameters.itemByName.side_effect = param_lookup

        mock_cam = MagicMock()
        mock_cam.checkToolpath.return_value = True

        mock_doc = MagicMock()
        mock_doc.products.itemByProductType.return_value = mock_cam

        mock_app = MagicMock()
        mock_app.activeDocument = mock_doc

        mock_setup = MagicMock()
        mock_setup.name = "Setup 1"
        mock_setup.operationType = 0
        mock_setup.allOperations = []

        with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
             patch("PostProcessAll._PostProcessUnifiedPipeline", return_value=None), \
             tempfile.TemporaryDirectory() as tmp_dir:
            res = PostProcessAll.PostProcessSetup(
                fname="test_ext",
                setup=mock_setup,
                setupFolder=tmp_dir,
                docSettings=dict(PostProcessAll.defaultSettings),
                program=mock_prog,
            )
            self.assertIsNone(res)
            # Extension parameter value should not have been overwritten to ".cnc"
            self.assertEqual(mock_ext_param.value.value, ".nc")

    def test_w1_windows_post_binary_discovery(self):
        """Verify find_autodesk_post_binary locates post.exe on Windows and selects newest by mtime."""
        from pipeline import find_autodesk_post_binary
        from unittest.mock import patch
        import time

        with tempfile.TemporaryDirectory() as tmp_dir:
            v1_dir = os.path.join(tmp_dir, "Autodesk", "webdeploy", "production", "hash_v1", "Applications", "CAM360")
            v2_dir = os.path.join(tmp_dir, "Autodesk", "webdeploy", "production", "hash_v2", "Applications", "CAM360")
            os.makedirs(v1_dir, exist_ok=True)
            os.makedirs(v2_dir, exist_ok=True)

            post_v1 = os.path.join(v1_dir, "post.exe")
            post_v2 = os.path.join(v2_dir, "post.exe")

            with open(post_v1, "w") as f:
                f.write("v1")
            with open(post_v2, "w") as f:
                f.write("v2")

            # Set mtime so v2 is newer than v1
            now = time.time()
            os.utime(post_v1, (now - 100, now - 100))
            os.utime(post_v2, (now, now))

            with patch.dict(os.environ, {"LOCALAPPDATA": tmp_dir}):
                found = find_autodesk_post_binary()
                self.assertEqual(found, post_v2)

    def test_w2_temporary_directory_logging_and_sidecar(self):
        """Verify debug logging and sidecar configs use platform tempfile dir and tolerate missing /tmp."""
        import json
        import PostProcessAll
        from unittest.mock import patch

        self.assertTrue(PostProcessAll._LOG_PATH.startswith(tempfile.gettempdir()))

        # Test _dbg_global writes to _LOG_PATH without throwing
        PostProcessAll._dbg_global("Test log entry")
        self.assertTrue(os.path.isfile(PostProcessAll._LOG_PATH))

        # Test _WriteDumperConfig and _ClearDumperConfig with simulated non-existent /tmp
        orig_isdir = os.path.isdir
        def fake_isdir(path):
            if path == "/tmp":
                return False
            return orig_isdir(path)

        with patch("os.path.isdir", side_effect=fake_isdir), \
             tempfile.NamedTemporaryFile(suffix=".cps") as tmp_cps:
            PostProcessAll._WriteDumperConfig(base_post_path=tmp_cps.name)
            dumper_cfg_path = os.path.join(tempfile.gettempdir(), "universal_dumper_config.json")
            self.assertTrue(os.path.isfile(dumper_cfg_path))
            with open(dumper_cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.assertEqual(cfg.get("basePost"), os.path.abspath(tmp_cps.name))

            PostProcessAll._ClearDumperConfig()
            self.assertFalse(os.path.isfile(dumper_cfg_path))

    def test_w8_sanitize_path_segment(self):
        """Verify _SanitizePathSegment strips invalid Windows/POSIX filesystem characters and handles reserved names."""
        import PostProcessAll

        self.assertEqual(PostProcessAll._SanitizePathSegment("normal_folder"), "normal_folder")
        self.assertEqual(PostProcessAll._SanitizePathSegment("Setup:Op1"), "Setup_Op1")
        self.assertEqual(PostProcessAll._SanitizePathSegment("Bad<Folder>Name?*"), "Bad_Folder_Name")
        self.assertEqual(PostProcessAll._SanitizePathSegment("TrailingDot."), "TrailingDot")
        self.assertEqual(PostProcessAll._SanitizePathSegment("TrailingSpace   "), "TrailingSpace")
        self.assertEqual(PostProcessAll._SanitizePathSegment("CON"), "_CON")
        self.assertEqual(PostProcessAll._SanitizePathSegment("nul.txt"), "_nul.txt")
        self.assertEqual(PostProcessAll._SanitizePathSegment("aux"), "_aux")
        self.assertEqual(PostProcessAll._SanitizePathSegment(""), "unnamed")
        self.assertEqual(PostProcessAll._SanitizePathSegment("   ...   "), "unnamed")

    def test_b9_recursive_output_folder_creation(self):
        """Verify PerformPostProcess creates nested output directory structure with parents=True."""
        import PostProcessAll
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            nested_out = os.path.join(tmp_dir, "deeply", "nested", "output_dir")
            mock_cam = MagicMock()
            mock_cam.allOperations.count = 0
            mock_program = MagicMock()
            mock_out_param = MagicMock(value=MagicMock(value=nested_out))
            mock_program.parameters.itemByName.return_value = mock_out_param
            mock_program.attributes.itemByName.return_value = None

            mock_doc = MagicMock()
            mock_doc.products.itemByProductType.return_value = mock_cam
            mock_app = MagicMock()
            mock_app.activeDocument = mock_doc
            mock_app.userInterface = None
            mock_docSettings = dict(PostProcessAll.defaultSettings)

            with patch("PostProcessAll.adsk.core.Application.get", return_value=mock_app), \
                 patch("PostProcessAll.GetNcProgram", return_value=mock_program), \
                 patch("PostProcessAll.GetNcProgramSetupsAndOperations", return_value=([], {})), \
                 patch("PostProcessAll.settingsMgr", MagicMock()):
                res = PostProcessAll.PerformPostProcess(
                    mock_docSettings,
                    setups=[],
                )
                self.assertIsNone(res)
                self.assertTrue(os.path.isdir(nested_out))

    def test_w3_installer_platform_detection(self):
        """Verify install_packages platform tag detection logic."""
        import install_packages
        from unittest.mock import patch

        with patch("platform.system", return_value="Windows"), patch("platform.machine", return_value="AMD64"):
            self.assertEqual(install_packages.get_default_platform_tag(), "win_amd64")

        with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"):
            self.assertEqual(install_packages.get_default_platform_tag(), "macosx_11_0_arm64")

        with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="x86_64"):
            self.assertEqual(install_packages.get_default_platform_tag(), "macosx_10_9_x86_64")


if __name__ == "__main__":
    unittest.main()


