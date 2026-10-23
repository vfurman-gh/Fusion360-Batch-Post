#!/usr/bin/env python3
"""
Test Suite: End-to-End Pipeline Integration with Real Lathe2DIPWVerifier.

Validates:
1. Complete 6-stage pipeline using genuine 2D Shapely In-Process Workpiece simulation.
2. 3D volumetric material removal calculation via Pappus's centroid theorem (> 7000 mm³).
3. Physics-based clearance identification of retract moves across roughing passes.
4. Native G0 restoration and modal G1/feedrate re-arming in final G-code output.
5. Markdown verification report compilation with zero collisions.
"""

import os
import tempfile
import unittest

from geometry import (
    Lathe2DIPWVerifier,
    AxisymmetricStock2D,
    WorkpieceDefinition,
    ToolAssembly,
    parse_legacy_tool_op,
)
from pipeline import PipelineOrchestrator


class TestFullPipelineRealEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")
        cls.base_cps = os.path.join(cls.repo_root, "centroid_turning.cps")

        if not os.path.isfile(cls.fixture_cnc) or not os.path.isfile(cls.base_cps):
            raise unittest.SkipTest("Prerequisites not found for real pipeline test")

        # Load tool 6 (Profile Boring Tool) geometry
        meta_op_tool6 = (
            ';META_OP: {"id":0,"name":"Profile Roughing8","tool":6,"post":"REAR","orientDeg":0,'
            '"insertOrient":"left","hand":"R","noseR":0.008,"comp":[-0.008,0.008],"toolType":"boring turning",'
            '"cutterSVG":"M0.000697246 0.00796956 A0.008 0.008 0 0 1 -0.00796956 -0.000697246 L0.023802 -0.363848 A0.008 0.008 0 0 1 0.0310743 -0.371121 L0.394225 -0.402892 A0.008 0.008 0 0 1 0.402892 -0.394225 L0.371121 -0.0310743 A0.008 0.008 0 0 1 0.363848 -0.023802 L0.000697246 0.00796956 Z",'
            '"holderSVG":"M0.553699 -0.0565425 L0.0080831 -0.00809344 L0.0536995 -0.491199 L0.0536995 -4.9912 L0.553699 -4.9912 L0.553699 -0.0565425 Z"}'
        )
        geom = parse_legacy_tool_op(meta_op_tool6)
        cls.tool6 = ToolAssembly(tool_id=6, name="Profile Roughing8", geometry=geom)
        cls.tools_by_op = {
            "Profile Roughing8": cls.tool6,
            "Profile Roughing8 (2)": cls.tool6,
            "Profile Finishing6": cls.tool6,
        }

        # Metric stock definition: 2.5" length = 63.5 mm, 0.625" OD = 15.875 mm, 0.25" ID = 6.35 mm
        stock = AxisymmetricStock2D(z_min=-63.5, z_max=0.0, outer_radius=15.875, inner_radius=6.35)
        cls.workpiece = WorkpieceDefinition(stock=stock)

    def test_end_to_end_pipeline_with_real_shapely_engine(self):
        """Execute complete pipeline using real Lathe2DIPWVerifier."""
        verifier = Lathe2DIPWVerifier(tool_scale_to_mm=25.4)
        orchestrator = PipelineOrchestrator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_cnc = os.path.join(tmp_dir, "machined_output.cnc")
            out_report = os.path.join(tmp_dir, "report.md")

            report, gcode = orchestrator.run(
                intermediate_cnc_path=self.fixture_cnc,
                base_post_path=self.base_cps,
                workpiece=self.workpiece,
                tools_by_op=self.tools_by_op,
                verifier=verifier,
                final_cnc_path=out_cnc,
                report_output_path=out_report,
            )

            # 1. Physics & Simulation Assertions
            self.assertEqual(len(report.collisions), 0, "Real toolpath should have 0 collisions")
            self.assertGreater(report.total_volume_removed_mm3, 7000.0, "Expected >7000 mm3 material carved")
            self.assertGreaterEqual(len(report.promoted_vectors), 10, "Expected at least 10 promoted air moves")

            # Verify Move 11 retract promotion rule
            self.assertIn("0:133870,0,2540->59690,0,2540", report.promoted_vectors)
            self.assertEqual(report.promoted_vectors["0:133870,0,2540->59690,0,2540"].min_occur, 1)

            # 2. Final G-Code Output Assertions
            self.assertTrue(os.path.isfile(out_cnc))
            self.assertIn("N32 G0 X0.47", gcode, "Expected block N32 promoted to G0 X0.47")
            self.assertIn("N33 Z-0.06", gcode, "Expected block N33 promoted to modal G0 Z-0.06")
            self.assertIn("N34 G1 X0.49 Z-0.07 F0.002", gcode, "Expected block N34 restored to G1 and feedrate")
            self.assertIn(";UNIVERSAL WRAPPER RESTORED 10 G0 RAPID MOVES", gcode)

            # 3. Report Assertions
            self.assertTrue(os.path.isfile(out_report))
            with open(out_report, "r") as f:
                report_text = f.read()

            self.assertIn("7819.26 mm³", report_text)
            self.assertIn("**G0 Moves Restored in G-code**: 10", report_text)
            self.assertIn("Zero collisions detected", report_text)


if __name__ == "__main__":
    unittest.main()
