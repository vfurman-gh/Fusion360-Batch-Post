"""
End-to-End Toolpath Verification, Multi-Batch Context Merging & Replay Pipeline.

Orchestrates the G-code-dialect-independent flow:
1. Context Extraction (`universal_dumper.cps`): Dumps structured JSON context and motion events
   from one or more single-tool Compact-NCI (`.cnc`) intermediate files while inheriting the
   target vendor post's tolerance and circular arc parameters.
2. Context Merging & Normalization (`context_parser.py`): Merges multiple single-tool batch
   JSON dumps into a unified multi-section context (`sections[0..N-1]`) and normalizes motions
   into canonical metric `MotionSegment` sequences (`mm`, `mm/min`, `sec`).
3. Unified IPW Verification (`IVerificationEngine`): Simulates stock removal, detects collisions,
   and computes occurrence-gated rapid-promotion rules across the combined setup in a single pass.
4. Direct Promotion Annotation: Annotates authorized air moves in the combined JSON context
   (`ev["promote"] = True`) and exports `promoted_vectors.json`.
5. Single-Pass Vendor Post Replay (`universal_replayer.cps`): Executes the unmodified vendor
   `.cps` post processor (`BASE_POST`) once across all merged sections, producing a single
   dialect-native multi-tool NC program with native tool changes and rapid promotions.
6. Report & Visualization Generation: Compiles markdown reports and high-res verification plots.
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple, Union

from geometry.models import (
    MotionSegment,
    ToolAssembly,
    WorkpieceDefinition,
    VerificationReport,
)
from geometry.engine_interface import IVerificationEngine
from context_parser import (
    merge_context_dumps,
    parse_context_dump,
    apply_promotions_to_context,
)


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
        dumper_cps: Optional[str] = None,
        replayer_cps: Optional[str] = None,
        apt_cps: Optional[str] = None,
        wrapper_cps: Optional[str] = None,
    ):
        self.post_bin = post_bin or find_autodesk_post_binary()
        if not self.post_bin or not os.path.isfile(self.post_bin):
            raise FileNotFoundError(f"Autodesk post executable not found: {self.post_bin}")
        self.post_bin_path = self.post_bin

        repo_root = os.path.dirname(os.path.abspath(__file__))
        self.dumper_cps = dumper_cps or os.path.join(repo_root, "universal_dumper.cps")
        if not os.path.isfile(self.dumper_cps):
            raise FileNotFoundError(f"universal_dumper.cps not found: {self.dumper_cps}")

        self.replayer_cps = replayer_cps or os.path.join(repo_root, "universal_replayer.cps")
        if not os.path.isfile(self.replayer_cps):
            raise FileNotFoundError(f"universal_replayer.cps not found: {self.replayer_cps}")

        self.apt_cps = apt_cps
        self.wrapper_cps = wrapper_cps

    def dump_intermediate_to_json(
        self,
        intermediate_cnc_path: str,
        base_post_path: str,
        output_json_path: str,
    ) -> str:
        """Run Pass 1 (`universal_dumper.cps`) on a binary Compact-NCI `.cnc` file."""
        env = os.environ.copy()
        env["BASE_POST"] = os.path.abspath(base_post_path)
        cmd = [self.post_bin, "--quiet", self.dumper_cps, intermediate_cnc_path, output_json_path]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if res.returncode != 0:
            err_detail = f"exit code {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
            raise RuntimeError(f"Context dump extraction failed: {err_detail}")
        return output_json_path

    def dump_context(
        self,
        intermediate_cnc_path: str,
        base_post_path: str,
        output_json_path: str,
    ) -> Dict:
        """Run Pass 1 (`universal_dumper.cps`) and return the parsed context dictionary."""
        self.dump_intermediate_to_json(intermediate_cnc_path, base_post_path, output_json_path)
        with open(output_json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def replay_combined_context(
        self,
        combined_context: Union[str, Dict],
        bootstrap_cnc_path: str,
        base_post_path: str,
        final_cnc_path: str,
        promoted_json_path: Optional[str] = None,
    ) -> str:
        """
        Run Pass 2 (`universal_replayer.cps`) with the unmodified `base_post_path`
        against `combined_context` (path or dict) and write the native machine program
        to `final_cnc_path`.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            if isinstance(combined_context, dict):
                ctx_path = os.path.join(tmp_dir, "combined_context.json")
                with open(ctx_path, "w", encoding="utf-8") as f:
                    json.dump(combined_context, f)
            else:
                ctx_path = combined_context

            env = os.environ.copy()
            env["BASE_POST"] = os.path.abspath(base_post_path)
            env["CONTEXT_JSON"] = os.path.abspath(ctx_path)
            if promoted_json_path and os.path.isfile(promoted_json_path):
                env["PROMOTED_MOVES_JSON"] = os.path.abspath(promoted_json_path)

            cmd = [self.post_bin, "--quiet", self.replayer_cps, bootstrap_cnc_path, final_cnc_path]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if res.returncode != 0:
                err_detail = f"exit code {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
                raise RuntimeError(f"Universal post replayer execution failed: {err_detail}")

            with open(final_cnc_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

    def run(
        self,
        intermediate_cnc_path: Union[str, Sequence[str]],
        base_post_path: str,
        workpiece: Optional[WorkpieceDefinition] = None,
        tools_by_op: Optional[Dict[str, ToolAssembly]] = None,
        verifier: Optional[IVerificationEngine] = None,
        final_cnc_path: str = "",
        report_output_path: Optional[str] = None,
        plot_output_path: Optional[str] = None,
        context_dump_paths: Optional[Sequence[str]] = None,
        promote: bool = True,
        **kwargs,
    ) -> Tuple[VerificationReport, str]:
        """
        Execute the complete dump -> merge -> verify -> replay pipeline on one or more
        intermediate `.cnc` files (or pre-extracted `context_dump_paths`).
        Returns `(VerificationReport, final_gcode_content)`.
        """
        if isinstance(intermediate_cnc_path, str):
            cnc_paths = [intermediate_cnc_path]
        else:
            cnc_paths = list(intermediate_cnc_path)

        if not cnc_paths or not os.path.isfile(cnc_paths[0]):
            raise FileNotFoundError(f"Intermediate NC file not found: {intermediate_cnc_path}")
        if not os.path.isfile(base_post_path):
            raise FileNotFoundError(f"Base machine post not found: {base_post_path}")

        bootstrap_cnc = cnc_paths[0]
        tools_by_op = tools_by_op or {}

        for out_p in (final_cnc_path, report_output_path, plot_output_path):
            if out_p:
                out_dir = os.path.dirname(os.path.abspath(out_p))
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            # -----------------------------------------------------------------
            # Step 1: Dump each intermediate .cnc to JSON (if not pre-dumped)
            # -----------------------------------------------------------------
            if context_dump_paths:
                dump_files = list(context_dump_paths)
            else:
                dump_files = []
                for idx, cnc_p in enumerate(cnc_paths):
                    dmp_p = os.path.join(tmp_dir, f"batch_{idx}.json")
                    self.dump_intermediate_to_json(cnc_p, base_post_path, dmp_p)
                    dump_files.append(dmp_p)

            # -----------------------------------------------------------------
            # Step 2: Merge JSON Contexts & Normalize Metric MotionSegments
            # -----------------------------------------------------------------
            combined_ctx = merge_context_dumps(dump_files)
            segments = parse_context_dump(combined_ctx)
            if not segments:
                raise ValueError("Parsed context dump yielded 0 kinematic segments")

            # -----------------------------------------------------------------
            # Step 3: Run Unified Verification Engine on Combined Setup
            # -----------------------------------------------------------------
            if verifier is None:
                is_turning = any(s.get("type", 0) == 1 for s in combined_ctx.get("sections", []))
                if is_turning:
                    from geometry.lathe_engine import Lathe2DIPWVerifier
                    verifier = Lathe2DIPWVerifier(tool_scale_to_mm=1.0)
                else:
                    from geometry.heightfield_engine import MillHeightfieldVerifier
                    verifier = MillHeightfieldVerifier()

            report = verifier.verify_toolpath(segments, tools_by_op, workpiece)

            # -----------------------------------------------------------------
            # Step 4: Annotate Promoted Air Moves in Combined Context & JSON
            # -----------------------------------------------------------------
            promoted_json_path = None
            restored_count = 0
            if promote:
                restored_count = apply_promotions_to_context(combined_ctx, report.promoted_vectors)
                promoted_json_path = os.path.join(tmp_dir, "promoted_vectors.json")
                json_payload = {
                    key: {"minOccur": rule.min_occur}
                    for key, rule in report.promoted_vectors.items()
                }
                with open(promoted_json_path, "w", encoding="utf-8") as f:
                    json.dump(json_payload, f, indent=2)

            # -----------------------------------------------------------------
            # Step 5: Execute Single-Pass Vendor Post Replay
            # -----------------------------------------------------------------
            effective_final_cnc = final_cnc_path or os.path.join(tmp_dir, "replayed_output.cnc")
            final_gcode = self.replay_combined_context(
                combined_context=combined_ctx,
                bootstrap_cnc_path=bootstrap_cnc,
                base_post_path=base_post_path,
                final_cnc_path=effective_final_cnc,
                promoted_json_path=promoted_json_path,
            )

            # -----------------------------------------------------------------
            # Step 6: Generate Summary Report & Verification Plot
            # -----------------------------------------------------------------
            if plot_output_path:
                try:
                    is_inch = (combined_ctx.get("unit", 0) == 0)
                    if getattr(verifier, "ipw", None) is not None:
                        from geometry.plotter import generate_verification_plot
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
                    elif getattr(verifier, "grid", None) is not None:
                        from geometry.plotter import generate_mill_verification_plot
                        generate_mill_verification_plot(
                            workpiece=workpiece,
                            grid=getattr(verifier, "grid", None),
                            segments=segments,
                            report=report,
                            tools_by_op=tools_by_op,
                            out_png_path=plot_output_path,
                            unit_is_inch=is_inch,
                        )
                except Exception as exc:
                    print(f"WARNING: Verification plot generation failed: {exc}", file=sys.stderr)

            report_summary = self._format_summary_report(
                report, final_gcode, base_post_path, restored_count=restored_count
            )
            if report_output_path:
                with open(report_output_path, "w", encoding="utf-8") as f:
                    f.write(report_summary)

            return report, final_gcode

    def _format_summary_report(
        self,
        report: VerificationReport,
        final_gcode: str,
        base_post_path: str,
        restored_count: Optional[int] = None,
    ) -> str:
        """Format a human-readable and structured markdown verification report."""
        promoted_count = len(report.promoted_vectors)
        collision_count = len(report.collisions)

        wrapper_restored_count = restored_count if restored_count is not None else 0
        if restored_count is None:
            for line in final_gcode.splitlines():
                if "UNIVERSAL WRAPPER" in line and "RESTORED" in line:
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
                lines.append(
                    f"- Line/Move {c.segment_id} ({c.kind.name}): {c.description} "
                    f"at contact ({c.contact_point.x:.2f}, {c.contact_point.y:.2f}, {c.contact_point.z:.2f})"
                )

        return "\n".join(lines)
