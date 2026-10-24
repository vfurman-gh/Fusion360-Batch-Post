"""
End-to-End Toolpath Verification & Rapid-Promotion Pipeline.

Orchestrates the complete flow:
1. Intermediate Pass: Generates ISO 4343 APT (.apt) from Compact-NCI (.cnc) using Autodesk apt.cps.
2. APT Parsing: Converts APT statements into normalized metric MotionSegments.
3. Verification Simulation: Evaluates collisions, IPW material removal, and air promotion rules using IVerificationEngine.
4. Rule Serialization: Exports promoted vector rules (with occurrence gating) to JSON.
5. Final Universal Wrap: Executes universal_wrapper.cps with target machine post (BASE_POST) to emit native G0 rapid moves and restored G1 cutting moves.
6. Report Generation: Compiles performance metrics, cycle time savings, and collision logs.
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from typing import Dict, Optional, Tuple

from geometry.models import (
    MotionSegment,
    ToolAssembly,
    WorkpieceDefinition,
    VerificationReport,
)
from geometry.engine_interface import IVerificationEngine
from apt_parser import parse_apt_file


def find_autodesk_post_binary() -> Optional[str]:
    """Locate the standalone Autodesk CAM post processor binary."""
    patterns = [
        "/Users/*/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/post",
        "/Applications/Autodesk Fusion 360.app/Contents/Libraries/Applications/CAM360/post",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def find_autodesk_apt_cps() -> Optional[str]:
    """Locate Autodesk's standard apt.cps configuration."""
    patterns = [
        "/Users/*/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/Data/Posts/apt.cps",
        "/Applications/Autodesk Fusion 360.app/Contents/Libraries/Applications/CAM360/Data/Posts/apt.cps",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


class PipelineOrchestrator:
    def __init__(
        self,
        post_bin: Optional[str] = None,
        apt_cps: Optional[str] = None,
        wrapper_cps: Optional[str] = None,
    ):
        self.post_bin = post_bin or find_autodesk_post_binary()
        if not self.post_bin or not os.path.isfile(self.post_bin):
            raise FileNotFoundError(f"Autodesk post executable not found: {self.post_bin}")

        self.apt_cps = apt_cps or find_autodesk_apt_cps()
        if not self.apt_cps or not os.path.isfile(self.apt_cps):
            raise FileNotFoundError(f"apt.cps configuration not found: {self.apt_cps}")

        repo_root = os.path.dirname(os.path.abspath(__file__))
        self.wrapper_cps = wrapper_cps or os.path.join(repo_root, "universal_wrapper.cps")
        if not os.path.isfile(self.wrapper_cps):
            raise FileNotFoundError(f"universal_wrapper.cps not found: {self.wrapper_cps}")

    def run(
        self,
        intermediate_cnc_path: str,
        base_post_path: str,
        workpiece: WorkpieceDefinition,
        tools_by_op: Dict[str, ToolAssembly],
        verifier: IVerificationEngine,
        final_cnc_path: str,
        report_output_path: Optional[str] = None,
        plot_output_path: Optional[str] = None,
    ) -> Tuple[VerificationReport, str]:
        """
        Execute the full 6-stage pipeline.
        Returns (VerificationReport, final_gcode_content).
        """
        if not os.path.isfile(intermediate_cnc_path):
            raise FileNotFoundError(f"Intermediate NC file not found: {intermediate_cnc_path}")
        if not os.path.isfile(base_post_path):
            raise FileNotFoundError(f"Base machine post not found: {base_post_path}")

        final_dir = os.path.dirname(os.path.abspath(final_cnc_path))
        if final_dir:
            os.makedirs(final_dir, exist_ok=True)
        if report_output_path:
            report_dir = os.path.dirname(os.path.abspath(report_output_path))
            if report_dir:
                os.makedirs(report_dir, exist_ok=True)
        if plot_output_path:
            plot_dir = os.path.dirname(os.path.abspath(plot_output_path))
            if plot_dir:
                os.makedirs(plot_dir, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            # -----------------------------------------------------------------
            # Step 1: Intermediate APT Extraction
            # -----------------------------------------------------------------
            apt_output_path = os.path.join(tmp_dir, "toolpath.apt")
            cmd_apt = [self.post_bin, "--quiet", self.apt_cps, intermediate_cnc_path, apt_output_path]
            res_apt = subprocess.run(cmd_apt, capture_output=True, text=True)
            if res_apt.returncode != 0:
                err_detail = f"exit code {res_apt.returncode}\nSTDOUT:\n{res_apt.stdout}\nSTDERR:\n{res_apt.stderr}"
                raise RuntimeError(f"Intermediate APT generation failed: {err_detail}")

            # -----------------------------------------------------------------
            # Step 2: Parse APT into Metric MotionSegments
            # -----------------------------------------------------------------
            segments = parse_apt_file(apt_output_path)
            if not segments:
                raise ValueError("Parsed APT file yielded 0 kinematic segments")

            # -----------------------------------------------------------------
            # Step 3: Run Verification Engine
            # -----------------------------------------------------------------
            report = verifier.verify_toolpath(segments, tools_by_op, workpiece)

            # -----------------------------------------------------------------
            # Step 4: Serialize Promoted Vectors to JSON
            # -----------------------------------------------------------------
            promoted_json_path = os.path.join(tmp_dir, "promoted_vectors.json")
            json_payload = {
                key: {"minOccur": rule.min_occur}
                for key, rule in report.promoted_vectors.items()
            }
            with open(promoted_json_path, "w", encoding="utf-8") as f:
                json.dump(json_payload, f, indent=2)

            # -----------------------------------------------------------------
            # Step 5: Execute Universal Wrapper Post Pass
            # -----------------------------------------------------------------
            env = os.environ.copy()
            env["BASE_POST"] = os.path.abspath(base_post_path)
            env["PROMOTED_MOVES_JSON"] = os.path.abspath(promoted_json_path)

            cmd_wrap = [self.post_bin, "--quiet", self.wrapper_cps, intermediate_cnc_path, final_cnc_path]
            res_wrap = subprocess.run(cmd_wrap, env=env, capture_output=True, text=True)
            if res_wrap.returncode != 0:
                err_detail = f"exit code {res_wrap.returncode}\nSTDOUT:\n{res_wrap.stdout}\nSTDERR:\n{res_wrap.stderr}"
                raise RuntimeError(f"Universal post wrapper execution failed: {err_detail}")

            with open(final_cnc_path, "r", encoding="utf-8", errors="replace") as f:
                final_gcode = f.read()

            # -----------------------------------------------------------------
            # Step 6: Generate Summary Report & Verification Plot
            # -----------------------------------------------------------------
            if plot_output_path:
                try:
                    from geometry.plotter import generate_verification_plot
                    is_inch = ("G20" in final_gcode) or not ("G21" in final_gcode)
                    final_ipw = getattr(verifier, "ipw", None)
                    chuck_obs = getattr(verifier, "chuck_obstacle", None)
                    generate_verification_plot(
                        workpiece=workpiece,
                        final_ipw=final_ipw,
                        chuck_obstacle=chuck_obs,
                        segments=segments,
                        report=report,
                        tools_by_op=tools_by_op,
                        out_png_path=plot_output_path,
                        unit_is_inch=is_inch,
                    )
                except Exception as exc:
                    print(f"WARNING: Verification plot generation failed: {exc}", file=sys.stderr)

            report_summary = self._format_summary_report(report, final_gcode, base_post_path)
            if report_output_path:
                with open(report_output_path, "w", encoding="utf-8") as f:
                    f.write(report_summary)

            return report, final_gcode

    def _format_summary_report(
        self,
        report: VerificationReport,
        final_gcode: str,
        base_post_path: str,
    ) -> str:
        """Format a human-readable and structured markdown verification report."""
        promoted_count = len(report.promoted_vectors)
        collision_count = len(report.collisions)

        # Count wrapper confirmation comments in output
        wrapper_restored_count = 0
        for line in final_gcode.splitlines():
            if "UNIVERSAL WRAPPER RESTORED" in line:
                try:
                    parts = line.split("RESTORED")[1].split("G0")[0].strip()
                    wrapper_restored_count = int(parts)
                except (ValueError, IndexError):
                    pass

        lines = [
            "# Toolpath Verification & Rapid-Promotion Report",
            "",
            "## Summary Metrics",
            f"- **Target Base Post**: `{os.path.basename(base_post_path)}`",
            f"- **Total Machining Time**: {report.total_machining_time_sec:.2f} s ({report.total_machining_time_sec/60:.2f} min)",
            f"- **Total Material Removed**: {report.total_volume_removed_mm3:.2f} mm³",
            f"- **Collisions Detected**: {collision_count}",
            f"- **Authorized Air Moves**: {promoted_count}",
            f"- **G0 Moves Restored in G-code**: {wrapper_restored_count}",
            "",
            "## Collision Status",
        ]

        if collision_count == 0:
            lines.append("✅ **Zero collisions detected.** All rapid moves and holder clearances are verified safe.")
        else:
            lines.append(f"⚠️ **{collision_count} Collisions Detected:**")
            for c in report.collisions:
                lines.append(f"- Line/Move {c.segment_id} ({c.kind.name}): {c.description} at contact ({c.contact_point.x:.2f}, {c.contact_point.y:.2f}, {c.contact_point.z:.2f})")

        lines.extend([
            "",
            "## Rapid-Promotion Authorization Rules",
        ])
        if promoted_count == 0:
            lines.append("No air moves were authorized for rapid promotion.")
        else:
            lines.append("| Vector Key (secId:start->end) | Min Occurrence Threshold |")
            lines.append("| :--- | :---: |")
            for k, rule in list(report.promoted_vectors.items())[:20]:
                lines.append(f"| `{k}` | `{rule.min_occur}` |")
            if promoted_count > 20:
                lines.append(f"| *... and {promoted_count - 20} more* | |")

        return "\n".join(lines)
