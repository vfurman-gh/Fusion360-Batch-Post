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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

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
    extract_workpiece_from_context,
    extract_tools_from_context,
)


def find_autodesk_post_binary() -> Optional[str]:
    """Locate the standalone Autodesk CAM post processor binary (macOS post or Windows post.exe)."""
    env_post = os.environ.get("AUTODESK_POST_BINARY")
    if env_post and os.path.isfile(env_post):
        return env_post

    candidates = []
    patterns = [
        # macOS paths
        "/Users/*/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/post",
        "/Applications/Autodesk Fusion 360.app/Contents/Libraries/Applications/CAM360/post",
        os.path.expanduser("~/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/post"),
    ]

    # Windows paths
    localappdata = os.environ.get("LOCALAPPDATA", "")
    programfiles = os.environ.get("ProgramFiles", "")
    programfiles_x86 = os.environ.get("ProgramFiles(x86)", "")
    for base in (localappdata, programfiles, programfiles_x86):
        if base:
            patterns.append(os.path.join(base, "Autodesk", "webdeploy", "production", "*", "Applications", "CAM360", "post.exe"))
            patterns.append(os.path.join(base, "Autodesk", "webdeploy", "production", "*", "post.exe"))

    for pattern in patterns:
        if pattern:
            for match in glob.glob(pattern):
                if os.path.isfile(match):
                    candidates.append(match)

    if not candidates:
        return None

    # Sort descending by modification time to pick the newest active webdeploy installation
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def find_autodesk_apt_cps() -> Optional[str]:
    """Locate Autodesk's standard apt.cps configuration."""
    env_apt = os.environ.get("AUTODESK_APT_CPS")
    if env_apt and os.path.isfile(env_apt):
        return env_apt

    candidates = []
    patterns = [
        # macOS paths
        "/Users/*/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/Data/Posts/apt.cps",
        "/Applications/Autodesk Fusion 360.app/Contents/Libraries/Applications/CAM360/Data/Posts/apt.cps",
        os.path.expanduser("~/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/Data/Posts/apt.cps"),
    ]

    localappdata = os.environ.get("LOCALAPPDATA", "")
    programfiles = os.environ.get("ProgramFiles", "")
    programfiles_x86 = os.environ.get("ProgramFiles(x86)", "")
    for base in (localappdata, programfiles, programfiles_x86):
        if base:
            patterns.append(os.path.join(base, "Autodesk", "webdeploy", "production", "*", "Applications", "CAM360", "Data", "Posts", "apt.cps"))
            patterns.append(os.path.join(base, "Autodesk", "webdeploy", "production", "*", "Data", "Posts", "apt.cps"))

    for pattern in patterns:
        if pattern:
            for match in glob.glob(pattern):
                if os.path.isfile(match):
                    candidates.append(match)

    if not candidates:
        return None

    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


class PipelineOrchestrator:
    def __init__(
        self,
        post_bin: Optional[str] = None,
        dumper_cps: Optional[str] = None,
        replayer_cps: Optional[str] = None,
        *args,
        **kwargs,
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
                    json.dump(combined_context, f, separators=(',', ':'))
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
        intermediate_cnc_path: Optional[Union[str, Sequence[str]]] = None,
        base_post_path: str = "",
        workpiece: Optional[WorkpieceDefinition] = None,
        tools_by_op: Optional[Dict[str, ToolAssembly]] = None,
        verifier: Optional[IVerificationEngine] = None,
        final_cnc_path: str = "",
        report_output_path: Optional[str] = None,
        plot_output_path: Optional[str] = None,
        context_dump_paths: Optional[Sequence[str]] = None,
        promote: bool = True,
        bootstrap_cnc_path: Optional[str] = None,
        progress_cb: Optional[Any] = None,
        post_properties: Optional[dict] = None,
        program_name: Optional[str] = None,
        fallback_workpiece: Optional[WorkpieceDefinition] = None,
        is_turning: Optional[bool] = None,
        **kwargs,
    ) -> Tuple[VerificationReport, str]:
        """
        Execute the complete dump -> merge -> verify -> replay pipeline on one or more
        intermediate `.cnc` files (or pre-extracted `context_dump_paths`).
        Returns `(VerificationReport, final_gcode_content)`.
        """
        def _notify(msg: str, frac: float):
            if progress_cb and not progress_cb(msg, frac):
                raise RuntimeError("Operation cancelled by user.")

        if context_dump_paths:
            dump_files = list(context_dump_paths)
            bootstrap_cnc = bootstrap_cnc_path or ""
        elif intermediate_cnc_path:
            if isinstance(intermediate_cnc_path, str):
                cnc_paths = [intermediate_cnc_path]
            else:
                cnc_paths = list(intermediate_cnc_path)
            if not cnc_paths or not os.path.isfile(cnc_paths[0]):
                raise FileNotFoundError(f"Intermediate NC file not found: {intermediate_cnc_path}")
            dump_files = []
            bootstrap_cnc = bootstrap_cnc_path or cnc_paths[0]
        else:
            raise ValueError("Either context_dump_paths or intermediate_cnc_path must be provided")

        if not base_post_path or not os.path.isfile(base_post_path):
            raise FileNotFoundError(f"Base machine post not found: {base_post_path}")

        tools_by_op = dict(tools_by_op) if tools_by_op else {}

        for out_p in (final_cnc_path, report_output_path, plot_output_path):
            if out_p:
                out_dir = os.path.dirname(os.path.abspath(out_p))
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            # -----------------------------------------------------------------
            # Step 1: Dump each intermediate .cnc to JSON (if not pre-dumped)
            # -----------------------------------------------------------------
            if not context_dump_paths:
                for idx, cnc_p in enumerate(cnc_paths):
                    dmp_p = os.path.join(tmp_dir, f"batch_{idx}.json")
                    self.dump_intermediate_to_json(cnc_p, base_post_path, dmp_p)
                    dump_files.append(dmp_p)

            # -----------------------------------------------------------------
            # Step 2: Merge JSON Contexts & Domain Extraction
            # -----------------------------------------------------------------
            _notify("Pass 2: Merging toolpath context...", 0.42)
            combined_ctx = merge_context_dumps(dump_files)
            if program_name:
                combined_ctx["programName"] = str(program_name)
            if post_properties:
                combined_ctx["postProperties"] = post_properties

            if is_turning is None:
                is_turning = any(
                    s.get("type", 0) == 1 or bool(s.get("tool", {}).get("_isTurningTool", False))
                    for s in combined_ctx.get("sections", [])
                )
            if workpiece is None:
                workpiece = extract_workpiece_from_context(combined_ctx, fallback_workpiece=fallback_workpiece, is_turning=is_turning)
            if not tools_by_op:
                tools_by_op = extract_tools_from_context(combined_ctx, is_turning=is_turning)
            if is_turning and not tools_by_op:
                raise ValueError("No lathe operations with valid tools found in context dump.")

            segments = parse_context_dump(combined_ctx)

            # -----------------------------------------------------------------
            # Step 3: Run Unified Verification Engine on Combined Setup
            # -----------------------------------------------------------------
            _notify(f"Pass 2: Simulating stock & collisions ({'turning' if is_turning else 'milling'})...", 0.50)
            if verifier is None:
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
            has_stock = (workpiece is not None and getattr(workpiece, "stock", None) is not None)
            if promote and has_stock and report.promoted_vectors:
                # S3: Sometimes toolpath collisions exist in CAM setups (e.g. minor holder touches
                # or benign fixture/stock skim contacts that are difficult or impractical to eliminate
                # in complex parts). We permit rapid promotion of verified air moves even when
                # collisions are detected elsewhere in the setup, as long as the collisions do not
                # result from the promoted moves themselves (i.e. every promoted move is strictly
                # verified to be collision-free with zero stock removal and safe clearance).
                collision_vector_keys = {c.vector_key for c in report.collisions if c.vector_key}
                safe_promoted_vectors = {
                    key: rule for key, rule in report.promoted_vectors.items()
                    if key not in collision_vector_keys
                }
                if safe_promoted_vectors:
                    restored_count = apply_promotions_to_context(combined_ctx, safe_promoted_vectors)
                    promoted_json_path = os.path.join(tmp_dir, "promoted_vectors.json")
                    json_payload = {
                        key: {"minOccur": rule.min_occur}
                        for key, rule in safe_promoted_vectors.items()
                    }
                    with open(promoted_json_path, "w", encoding="utf-8") as f:
                        json.dump(json_payload, f, indent=2)

            # -----------------------------------------------------------------
            # Step 5: Execute Single-Pass Vendor Post Replay
            # -----------------------------------------------------------------
            _notify("Pass 2: Emitting final NC program...", 0.85)
            if not bootstrap_cnc or not os.path.isfile(bootstrap_cnc):
                bootstrap_cnc = os.path.join(tmp_dir, "bootstrap.intermediate.cnc")
                with open(bootstrap_cnc, "wb") as bf:
                    bf.write(b"")

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
                _notify("Pass 2: Generating verification plot & report...", 0.70)
                try:
                    is_inch = (combined_ctx.get("unit", 0) == 0)
                    if is_turning:
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
                    else:
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

            report_summary = self.format_summary_report(
                report, final_gcode, base_post_path, restored_count=restored_count
            )
            if report_output_path:
                with open(report_output_path, "w", encoding="utf-8") as f:
                    f.write(report_summary)

            return report, final_gcode

    def format_summary_report(
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

        # Collate collisions by operation
        cols_by_op: Dict[str, List[CollisionRecord]] = {}
        for c in report.collisions:
            op_label = c.op_id if c.op_id else "General"
            cols_by_op.setdefault(op_label, []).append(c)

        all_ops = list(report.operation_names) if getattr(report, "operation_names", None) else []
        for op in cols_by_op.keys():
            if op not in all_ops:
                all_ops.append(op)
        if not all_ops:
            all_ops = list(cols_by_op.keys()) or ["All Operations"]

        if collision_count == 0:
            lines.append("✅ **Zero collisions detected.** All rapid moves and holder clearances are verified safe across all operations.")
            lines.append("")
            lines.append("### Operations Verification Summary")
            lines.append("| Operation | Status | Collisions | Details by Type |")
            lines.append("| :--- | :--- | :--- | :--- |")
            for op in all_ops:
                lines.append(f"| {op} | ✅ Safe | 0 | None |")
        else:
            affected_ops = [op for op, clist in cols_by_op.items() if len(clist) > 0]
            lines.append(
                f"⚠️ **{collision_count} Collision(s) Detected across {len(affected_ops)} operation(s)!**  \n"
                "Final NC code generation completed with warnings. Review the table and detailed move listings below before machining."
            )
            lines.append("")
            lines.append("### Collisions by Operation")
            lines.append("| Operation | Status | Collisions | Details by Type |")
            lines.append("| :--- | :--- | :--- | :--- |")
            for op in all_ops:
                op_cols = cols_by_op.get(op, [])
                if op_cols:
                    type_counts: Dict[str, int] = {}
                    for c in op_cols:
                        k_name = c.kind.name if hasattr(c.kind, "name") else str(c.kind)
                        type_counts[k_name] = type_counts.get(k_name, 0) + 1
                    type_str = ", ".join(f"`{k}` ({n})" for k, n in sorted(type_counts.items()))
                    lines.append(f"| **{op}** | ⚠️ **Collision** | **{len(op_cols)}** | {type_str} |")
                else:
                    lines.append(f"| {op} | ✅ Safe | 0 | None |")
            lines.append(f"| **Total** | | **{collision_count}** | |")
            lines.append("")

            lines.append("### Detailed Collision Breakdown")
            for op in all_ops:
                op_cols = cols_by_op.get(op, [])
                if not op_cols:
                    continue
                lines.append(f"#### ⚠️ Operation: {op} ({len(op_cols)} collision{'s' if len(op_cols) != 1 else ''})")
                for c in op_cols:
                    k_name = c.kind.name if hasattr(c.kind, "name") else str(c.kind)
                    lines.append(
                        f"- **Move {c.segment_id}** (`{k_name}`): {c.description} "
                        f"at contact `({c.contact_point.x:.2f}, {c.contact_point.y:.2f}, {c.contact_point.z:.2f})`"
                    )
                lines.append("")

        return "\n".join(lines)

    _format_summary_report = format_summary_report
