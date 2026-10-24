#!/usr/bin/env python3
"""
CLI entry point for running the 6-Stage Lathe IPW Verification and Rapid-Promotion Pipeline.
Can be executed standalone or via subprocess from Fusion 360 Add-In when macOS library
validation restricts in-process C-extensions.
"""

import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from geometry import (
    Lathe2DIPWVerifier,
    ToolAssembly,
    AxisymmetricStock2D,
    WorkpieceDefinition,
    parse_legacy_stock,
    parse_legacy_tool_op,
)
from pipeline import PipelineOrchestrator


def main():
    parser = argparse.ArgumentParser(description="Run 6-Stage Lathe IPW Verification Pipeline")
    parser.add_argument("--intermediate", required=True, help="Path to intermediate Compact-NCI (.cnc)")
    parser.add_argument("--base-post", required=True, help="Path to base machine post processor (.cps)")
    parser.add_argument("--final-cnc", required=True, help="Path to final output machine G-code (.cnc)")
    parser.add_argument("--report", required=True, help="Path to verification markdown report (.md)")
    parser.add_argument("--plot", help="Path to verification plot image (.png)")
    parser.add_argument("--wrapper", default=os.path.join(_SCRIPT_DIR, "universal_wrapper.cps"), help="Path to universal_wrapper.cps")
    parser.add_argument("--meta-gcode", help="Path to G-code file containing metadata comments")
    parser.add_argument("--stock-json", help="JSON string for WorkpieceDefinition")
    parser.add_argument("--no-promote", action="store_true", help="Disable G0 rapid promotion")

    args = parser.parse_args()

    workpiece = None
    tools_by_op = {}

    if args.stock_json:
        try:
            data = json.loads(args.stock_json)
            workpiece = WorkpieceDefinition(
                stock=AxisymmetricStock2D(
                    z_min=float(data["z_min"]),
                    z_max=float(data["z_max"]),
                    outer_radius=float(data["outer_radius"]),
                    inner_radius=float(data.get("inner_radius", 0.0)),
                )
            )
        except Exception as e:
            print(f"WARNING: Failed to parse stock JSON: {e}", file=sys.stderr)

    gcode_content = ""
    candidate_files = [args.meta_gcode, args.final_cnc]
    for cand in candidate_files:
        if cand and os.path.isfile(cand):
            with open(cand, "r", encoding="utf8", errors="replace") as f:
                gcode_content = f.read()
            if ";META_OP:" in gcode_content:
                break

    is_inch = ("G20" in gcode_content) or not ("G21" in gcode_content)
    tool_scale = 25.4 if is_inch else 1.0

    for line in gcode_content.splitlines():
        if not workpiece and line.startswith(";META_STOCK:"):
            workpiece = parse_legacy_stock(line)
            if is_inch and workpiece.stock and isinstance(workpiece.stock, AxisymmetricStock2D):
                workpiece = WorkpieceDefinition(
                    stock=AxisymmetricStock2D(
                        z_min=workpiece.stock.z_min * 25.4,
                        z_max=workpiece.stock.z_max * 25.4,
                        outer_radius=workpiece.stock.outer_radius * 25.4,
                        inner_radius=workpiece.stock.inner_radius * 25.4,
                    ),
                    fixtures=workpiece.fixtures,
                )
        elif line.startswith(";META_OP:"):
            geom = parse_legacy_tool_op(line)
            try:
                payload = json.loads(line.split(":", 1)[1])
                tool_asm = ToolAssembly(
                    tool_id=payload.get("tool", 0),
                    name=payload.get("name", ""),
                    geometry=geom,
                )
                if tool_asm.name:
                    tools_by_op[tool_asm.name] = tool_asm
            except Exception:
                pass

    if not workpiece:
        print("ERROR: Workpiece stock definition could not be determined", file=sys.stderr)
        sys.exit(1)

    verifier = Lathe2DIPWVerifier(tool_scale_to_mm=tool_scale)
    orchestrator = PipelineOrchestrator(wrapper_cps=args.wrapper)

    report, final_gcode = orchestrator.run(
        intermediate_cnc_path=args.intermediate,
        base_post_path=args.base_post,
        workpiece=workpiece,
        tools_by_op=tools_by_op,
        verifier=verifier,
        final_cnc_path=args.final_cnc,
        report_output_path=args.report,
        plot_output_path=args.plot,
    )

    result_summary = {
        "volume_mm3": report.total_volume_removed_mm3,
        "promoted": len(report.promoted_vectors) if not args.no_promote else 0,
        "spikes": len(report.mrr_spikes),
        "collisions": len(report.collisions),
    }
    print(f"VERIFY_RESULT:{json.dumps(result_summary)}")


if __name__ == "__main__":
    main()
