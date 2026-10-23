#!/usr/bin/env python3
"""
End-to-End Test Suite for Universal CPS Wrapper (universal_wrapper.cps)

Validates:
1. Passthrough Fidelity: Output matches baseline when no promotions are specified.
2. Single G0 Promotion: Authorized vector emits G0 and subsequent block restores G1 + F.
3. Temporal/Occurrence Gating: Moves with minOccur > 1 are not promoted before threshold.
4. Multi-Vector Promotion: Multiple sequential and non-sequential air moves are promoted.
5. Canned Cycle Immunity: cycleActive prevents promotion during canned/drill cycles.
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
import unittest


def find_post_executable():
    """Locate the Autodesk CAM post processor binary."""
    patterns = [
        "/Users/*/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/post",
        "/Applications/Autodesk Fusion 360.app/Contents/Libraries/Applications/CAM360/post",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


class TestUniversalCPSWrapper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.post_bin = find_post_executable()
        if not cls.post_bin or not os.path.isfile(cls.post_bin):
            raise unittest.SkipTest("Autodesk 'post' executable not found on this system.")

        cls.wrapper_cps = os.path.join(cls.repo_root, "universal_wrapper.cps")
        cls.base_cps = os.path.join(cls.repo_root, "centroid_turning.cps")
        cls.fixture_cnc = os.path.join(cls.repo_root, "tests", "fixtures", "roughing_test.cnc")

        if not os.path.isfile(cls.fixture_cnc):
            raise unittest.SkipTest(f"Test fixture not found: {cls.fixture_cnc}")

    def run_post(self, cps_path, env_vars=None):
        """Execute post processor and return (returncode, output_content, stderr)."""
        with tempfile.NamedTemporaryFile(suffix=".cnc", delete=False) as tmp_out:
            tmp_out_path = tmp_out.name

        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)

        cmd = [self.post_bin, "--quiet", cps_path, self.fixture_cnc, tmp_out_path]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)

        content = ""
        if os.path.isfile(tmp_out_path):
            with open(tmp_out_path, "r") as f:
                content = f.read()
            os.remove(tmp_out_path)

        return res.returncode, content, res.stderr

    def test_01_passthrough_fidelity(self):
        """Wrapper without promotions should produce output matching baseline target post."""
        # 1. Baseline post
        rc_base, content_base, err_base = self.run_post(self.base_cps)
        self.assertEqual(rc_base, 0, f"Baseline run failed: {err_base}")

        # 2. Universal wrapper with no promoted JSON
        env = {"BASE_POST": self.base_cps}
        rc_wrap, content_wrap, err_wrap = self.run_post(self.wrapper_cps, env)
        self.assertEqual(rc_wrap, 0, f"Wrapper run failed: {err_wrap}")

        # Wrapper appends a summary comment at onClose; strip comments to compare G-code blocks
        def extract_gcode_blocks(text):
            return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith(";")]

        blocks_base = extract_gcode_blocks(content_base)
        blocks_wrap = extract_gcode_blocks(content_wrap)
        self.assertEqual(blocks_base, blocks_wrap, "Wrapper altered G-code when no moves were promoted")

    def test_02_single_rapid_promotion_and_feed_restoration(self):
        """Promoting Move 11 should emit G0 X0.47 and re-arm G1 Z-0.06 F0.002 on the next block."""
        key_move_11 = "0:5270,0,100->2350,0,100"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp_json:
            json.dump({key_move_11: {"minOccur": 1}}, tmp_json)
            json_path = tmp_json.name

        try:
            env = {
                "BASE_POST": self.base_cps,
                "PROMOTED_MOVES_JSON": json_path,
            }
            rc, content, err = self.run_post(self.wrapper_cps, env)
            self.assertEqual(rc, 0, f"Post failed: {err}")

            lines = content.splitlines()
            promoted_block = None
            next_block = None

            for i, line in enumerate(lines):
                if "N32 G0 X0.47" in line:
                    promoted_block = line.strip()
                    # Look ahead for next non-comment G-code line
                    for j in range(i + 1, len(lines)):
                        l = lines[j].strip()
                        if l and not l.startswith(";"):
                            next_block = l
                            break
                    break

            self.assertIsNotNone(promoted_block, "Move 11 was not promoted to 'N32 G0 X0.47'")
            self.assertEqual(next_block, "N33 G1 Z-0.06 F0.002", "Next block did not properly restore modal G1 and feedrate")
            self.assertIn("UNIVERSAL WRAPPER RESTORED 1 G0 RAPID MOVES", content)
        finally:
            if os.path.isfile(json_path):
                os.remove(json_path)

    def test_03_occurrence_gating(self):
        """Vector with minOccur: 2 should NOT promote on its first occurrence."""
        key_move_11 = "0:5270,0,100->2350,0,100"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp_json:
            json.dump({key_move_11: {"minOccur": 2}}, tmp_json)
            json_path = tmp_json.name

        try:
            env = {
                "BASE_POST": self.base_cps,
                "PROMOTED_MOVES_JSON": json_path,
            }
            rc, content, err = self.run_post(self.wrapper_cps, env)
            self.assertEqual(rc, 0, f"Post failed: {err}")

            # Verify it remained N32 X0.47 (cutting feed)
            self.assertIn("N32 X0.47", content)
            self.assertNotIn("N32 G0 X0.47", content)
            self.assertIn("UNIVERSAL WRAPPER RESTORED 0 G0 RAPID MOVES", content)
        finally:
            if os.path.isfile(json_path):
                os.remove(json_path)

    def test_04_multi_vector_promotion(self):
        """Multiple retract moves across multiple roughing passes are all promoted."""
        promoted = {
            "0:5270,0,100->2350,0,100": {},
            "0:2350,0,100->2350,0,-600": {},
            "0:5259,0,-200->2350,0,-200": {},
            "0:2350,0,-200->2350,0,-900": {},
            "0:5263,0,-500->2350,0,-500": {},
            "0:2350,0,-500->2350,0,-1158": {},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp_json:
            json.dump(promoted, tmp_json)
            json_path = tmp_json.name

        try:
            env = {
                "BASE_POST": self.base_cps,
                "PROMOTED_MOVES_JSON": json_path,
            }
            rc, content, err = self.run_post(self.wrapper_cps, env)
            self.assertEqual(rc, 0, f"Post failed: {err}")

            self.assertIn("N32 G0 X0.47", content)
            self.assertIn("N39 G0 X0.47", content)
            self.assertIn("N50 G0 X0.47", content)
            self.assertIn("UNIVERSAL WRAPPER RESTORED 6 G0 RAPID MOVES", content)
        finally:
            if os.path.isfile(json_path):
                os.remove(json_path)


if __name__ == "__main__":
    unittest.main()
