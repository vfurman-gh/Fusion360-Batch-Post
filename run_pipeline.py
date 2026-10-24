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
    parser.add_argument("--fixtures-json", help="JSON string for fixtures / chuck obstacles")
    parser.add_argument("--tools-file", help="Path to JSON file containing analytical tool definitions")
    parser.add_argument("--no-promote", action="store_true", help="Disable G0 rapid promotion")

    args = parser.parse_args()

    workpiece = None
    tools_by_op = {}

    if args.stock_json:
        try:
            data = json.loads(args.stock_json)
            stock = AxisymmetricStock2D(
                z_min=float(data["z_min"]),
                z_max=float(data["z_max"]),
                outer_radius=float(data["outer_radius"]),
                inner_radius=float(data.get("inner_radius", 0.0)),
            )
            fixtures = []
            if args.fixtures_json:
                from geometry import AxisymmetricChuckObstacle
                f_data = json.loads(args.fixtures_json)
                for f_item in f_data:
                    fixtures.append(
                        AxisymmetricChuckObstacle(
                            z_min=float(f_item["z_min"]),
                            z_max=float(f_item["z_max"]),
                            outer_radius=float(f_item["outer_radius"]),
                            clamping_radius=float(f_item["clamping_radius"]),
                        )
                    )
            workpiece = WorkpieceDefinition(stock=stock, fixtures=fixtures)
        except Exception as e:
            print(f"WARNING: Failed to parse stock JSON: {e}", file=sys.stderr)

    analytical_tools_loaded = False
    if args.tools_file and os.path.isfile(args.tools_file):
        try:
            with open(args.tools_file, "r", encoding="utf8") as tf:
                tools_data = json.load(tf)
            from shapely.geometry import Polygon
            from geometry.tool_generator import (
                generate_rhombic_insert,
                generate_grooving_insert,
                generate_drill_cutter,
                generate_turning_holder,
            )
            from geometry import LatheToolGeometry, ToolCompensationQuadrant

            for op_name, t_info in tools_data.items():
                shape = t_info.get("insert_shape", "rhombic")
                tool_id = t_info.get("tool_id", 0)
                is_boring = t_info.get("is_boring", False)
                shank_w = t_info.get("shank_width", 16.0)
                shank_l = t_info.get("shank_length", 100.0)
                nose_r = t_info.get("nose_radius", 0.4)

                if shape == "drill":
                    cutter = generate_drill_cutter(
                        diameter=t_info.get("diameter", 6.35),
                        point_angle_deg=t_info.get("point_angle", 118.0),
                        flute_length=t_info.get("flute_length", 30.0),
                        overall_length=t_info.get("overall_length", 60.0),
                    )
                    holder = Polygon()
                elif shape == "groove":
                    cutter = generate_grooving_insert(
                        blade_width=t_info.get("blade_width", 3.0),
                        blade_depth=t_info.get("blade_depth", 12.0),
                        corner_radius=nose_r,
                    )
                    try:
                        holder = generate_turning_holder(
                            shank_width=shank_w,
                            shank_length=shank_l,
                            clearance_x=nose_r,
                            clearance_z=nose_r,
                            is_boring=is_boring,
                        )
                    except TypeError:
                        holder = generate_turning_holder(
                            shank_width=shank_w,
                            shank_length=shank_l,
                            clearance_x=nose_r,
                            clearance_z=nose_r,
                        )
                else:  # rhombic / general turning
                    cutter = generate_rhombic_insert(
                        edge_length=t_info.get("edge_length", 9.525),
                        nose_radius=nose_r,
                        lead_angle_deg=t_info.get("lead_angle", 5.0),
                    )
                    clr_x = max(1.25, nose_r * 3.0) if is_boring else max(0.4, nose_r * 1.5)
                    clr_z = max(1.25, nose_r * 3.0) if is_boring else max(0.4, nose_r * 1.5)
                    try:
                        holder = generate_turning_holder(
                            shank_width=shank_w,
                            shank_length=shank_l,
                            clearance_x=clr_x,
                            clearance_z=clr_z,
                            is_boring=is_boring,
                        )
                    except TypeError:
                        holder = generate_turning_holder(
                            shank_width=shank_w,
                            shank_length=shank_l,
                            clearance_x=clr_x,
                            clearance_z=clr_z,
                        )


                clean_holder = holder.difference(cutter.buffer(0.001)) if not holder.is_empty else holder
                geom = LatheToolGeometry(
                    insert_shape=shape,
                    nose_radius=nose_r,
                    hand=t_info.get("hand", "R"),
                    quadrant=ToolCompensationQuadrant.P3,
                    cutter_polygon=cutter,
                    holder_polygon=clean_holder,
                )
                tools_by_op[op_name] = ToolAssembly(
                    tool_id=tool_id,
                    name=op_name,
                    geometry=geom,
                )
            analytical_tools_loaded = bool(tools_by_op)
        except Exception as e:
            print(f"WARNING: Failed to parse tools file {args.tools_file}: {e}", file=sys.stderr)

    gcode_content = ""
    candidate_files = [args.meta_gcode, args.final_cnc]
    for cand in candidate_files:
        if cand and os.path.isfile(cand):
            with open(cand, "r", encoding="utf8", errors="replace") as f:
                gcode_content = f.read()
            if ";META_OP:" in gcode_content:
                break

    is_inch = ("G20" in gcode_content) or not ("G21" in gcode_content)
    tool_scale = 1.0 if analytical_tools_loaded else (25.4 if is_inch else 1.0)

    if workpiece and workpiece.stock:
        if math.isnan(workpiece.stock.outer_radius) or workpiece.stock.outer_radius <= 0.0:
            workpiece = None

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
        elif not analytical_tools_loaded and line.startswith(";META_OP:"):
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

    try:
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
    except TypeError:
        report, final_gcode = orchestrator.run(
            intermediate_cnc_path=args.intermediate,
            base_post_path=args.base_post,
            workpiece=workpiece,
            tools_by_op=tools_by_op,
            verifier=verifier,
            final_cnc_path=args.final_cnc,
            report_output_path=args.report,
        )
        if args.plot:
            try:
                from geometry.plotter import generate_verification_plot
                generate_verification_plot(
                    stock=workpiece.stock,
                    fixtures=workpiece.fixtures,
                    tools_by_op=tools_by_op,
                    motion_segments=report.evaluated_moves,
                    collisions=report.collisions,
                    output_path=args.plot,
                )
            except Exception:
                pass


    result_summary = {
        "volume_mm3": report.total_volume_removed_mm3,
        "promoted": len(report.promoted_vectors) if not args.no_promote else 0,
        "spikes": len(report.mrr_spikes),
        "collisions": len(report.collisions),
    }
    print(f"VERIFY_RESULT:{json.dumps(result_summary)}")


if __name__ == "__main__":
    main()
