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



if __name__ == "__main__":
    unittest.main()

