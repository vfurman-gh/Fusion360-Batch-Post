"""
Lathe 2D Geometry & MRR Time-Series Verification Plotter.

Generates comprehensive high-resolution .png verification reports showing:
1. 2D Cross-section geometry: Initial Stock, Final IPW, Chuck Keep-out, Kinematic Moves,
   Insert/Holder silhouettes, and Collision/Spike locations.
2. MRR Time-Series: Instantaneous MRR, Operation timeline spans, Mean MRR, Spike thresholds,
   and Timeline collision markers.
"""

import math
import os
from typing import Dict, List, Optional, Set, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon
from shapely.affinity import translate
from shapely.geometry import Polygon

from .models import (
    CollisionKind,
    CollisionRecord,
    MotionKind,
    MotionSegment,
    ToolAssembly,
    VerificationReport,
    WorkpieceDefinition,
)

COLLISION_STYLES = {
    CollisionKind.RAPID_INTO_STOCK: ("#d62728", "X", "Rapid Into Stock"),
    CollisionKind.HOLDER_STRIKE: ("#e377c2", "s", "Holder Strike"),
    CollisionKind.FIXTURE_COLLISION: ("#ff7f0e", "^", "Chuck Collision"),
}


def draw_poly(ax, geom, **kwargs):
    """Draw a Shapely Polygon or MultiPolygon onto a Matplotlib Axes."""
    if geom is None or geom.is_empty:
        return
    polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms)
    for idx, p in enumerate(polys):
        kw = dict(kwargs)
        if idx > 0:
            kw.pop("label", None)
        ax.add_patch(MplPolygon(list(p.exterior.coords), closed=True, **kw))


def generate_verification_plot(
    workpiece: WorkpieceDefinition,
    final_ipw: Polygon,
    chuck_obstacle: Polygon,
    segments: List[MotionSegment],
    report: VerificationReport,
    tools_by_op: Dict[str, ToolAssembly],
    out_png_path: str,
    unit_is_inch: bool = True,
):
    """
    Generate the dual-panel 2D geometry and MRR verification plot.
    """
    scale = 1.0 / 25.4 if unit_is_inch else 1.0
    u_label = "in" if unit_is_inch else "mm"
    vol_scale = (1.0 / (25.4 ** 3)) if unit_is_inch else 1.0
    vol_unit = "in³" if unit_is_inch else "mm³"

    fig, (ax_geo, ax_mrr) = plt.subplots(
        2, 1, figsize=(13, 10), gridspec_kw={"height_ratios": [1.15, 1.0]}
    )

    # -------------------------------------------------------------------------
    # Panel 1: 2D Turning Cross-Section
    # -------------------------------------------------------------------------
    # Rescale polygons for plotting display if inch
    from shapely.affinity import scale as shp_scale
    def to_plot(poly):
        if poly is None or poly.is_empty:
            return poly
        return shp_scale(poly, xfact=scale, yfact=scale, origin=(0, 0))

    initial_stock = to_plot(workpiece.initial_stock_polygon) if hasattr(workpiece, "initial_stock_polygon") else None
    if initial_stock is None and workpiece.stock:
        from .stock_generator import generate_axisymmetric_stock_polygon
        initial_stock = to_plot(generate_axisymmetric_stock_polygon(workpiece.stock))

    draw_poly(ax_geo, initial_stock, fill=False, edgecolor="#666666", linestyle="--", linewidth=1.2, label="Initial Stock")
    draw_poly(ax_geo, to_plot(final_ipw), facecolor="#cfe2f3", edgecolor="#2b579a", alpha=0.8, label="Remaining Stock (IPW)")
    draw_poly(ax_geo, to_plot(chuck_obstacle), facecolor="#555555", edgecolor="#222222", alpha=0.85, label="Chuck / Fixture")

    seen_labels: Set[str] = set()
    for seg in segments:
        z1, r1 = seg.start.z * scale, seg.start.x * scale
        z2, r2 = seg.end.z * scale, seg.end.x * scale
        v_key = seg.vector_key.to_wrapper_str()
        is_promoted = v_key in report.promoted_vectors

        if is_promoted:
            lbl = "G0 Promoted Rapid Move" if "G0 Promoted Rapid Move" not in seen_labels else None
            seen_labels.add("G0 Promoted Rapid Move")
            ax_geo.plot([z1, z2], [r1, r2], color="#d62728", linestyle="--", linewidth=1.0, alpha=0.85, label=lbl)
        elif seg.motion == MotionKind.RAPID:
            lbl = "G0 Initial Positioning" if "G0 Initial Positioning" not in seen_labels else None
            seen_labels.add("G0 Initial Positioning")
            ax_geo.plot([z1, z2], [r1, r2], color="#2ca02c", linestyle=":", linewidth=0.9, alpha=0.75, label=lbl)
        else:
            lbl = "G1/G2/G3 Cutting Move" if "G1/G2/G3 Cutting Move" not in seen_labels else None
            seen_labels.add("G1/G2/G3 Cutting Move")
            ax_geo.plot([z1, z2], [r1, r2], color="#1f77b4", linewidth=0.6, alpha=0.6, label=lbl)

    # Plot sample tools if present
    for tool_name, tool in list(tools_by_op.items())[:2]:
        if hasattr(tool.geometry, "cutter_polygon") and tool.geometry.cutter_polygon:
            c_poly = to_plot(tool.geometry.cutter_polygon)
            h_poly = to_plot(tool.geometry.holder_polygon)
            # Find a representative cutting position
            sample_pt = (segments[0].start.z * scale, segments[0].start.x * scale) if segments else (0, 0)
            draw_poly(ax_geo, translate(h_poly, sample_pt[0], sample_pt[1]), facecolor="#7f7f7f", edgecolor="black", alpha=0.5, label=f"Holder ({tool_name})")
            draw_poly(ax_geo, translate(c_poly, sample_pt[0], sample_pt[1]), facecolor="#f1c232", edgecolor="black", alpha=0.9, label=f"Insert ({tool_name})")
            break

    # Build segment lookup by segment_id
    seg_map = {s.segment_id: s for s in segments}

    # Flag MRR spikes and Collisions
    for sp in report.mrr_spikes[:8]:
        seg = seg_map.get(sp.segment_id)
        if seg:
            ax_geo.plot(seg.end.z * scale, seg.end.x * scale, "o", color="#e65100", markersize=5.5, markeredgecolor="black")

    for col in report.collisions:
        color, marker, desc = COLLISION_STYLES.get(col.kind, ("#d62728", "X", col.kind.name))
        col_lbl_key = f"Collision: {desc}"
        col_lbl = col_lbl_key if col_lbl_key not in seen_labels else None
        seen_labels.add(col_lbl_key)
        ax_geo.plot(col.contact_point.z * scale, col.contact_point.x * scale, marker, color=color, markersize=10, markeredgewidth=2.2, label=col_lbl, zorder=8)

    ax_geo.axhline(0, color="black", linestyle="-.", linewidth=0.8, label="Spindle Centerline (R=0)")
    ax_geo.set_aspect("equal", adjustable="datalim")
    ax_geo.set_xlabel(f"Z Axis ({u_label})", labelpad=5)
    ax_geo.set_ylabel(f"Radius R ({u_label})", labelpad=5)
    ax_geo.set_title(
        f"2D Lathe IPW Verification — Volume Removed: {report.total_volume_removed_mm3 * vol_scale:.3f} {vol_unit} | "
        f"Promoted G0 Moves: {len(report.promoted_vectors)} | Collisions: {len(report.collisions)}",
        pad=8,
    )
    ax_geo.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
    ax_geo.grid(True, linestyle=":", alpha=0.5)

    # -------------------------------------------------------------------------
    # Panel 2: MRR Time-Series
    # -------------------------------------------------------------------------
    t_vals: List[float] = []
    mrr_vals: List[float] = []
    op_spans: List[Tuple[str, float, float]] = []

    for s in report.mrr_timeline:
        t0 = s.timestamp_sec
        t1 = s.timestamp_sec + s.duration_sec
        mrr_scaled = s.mrr_rate_mm3_per_min * vol_scale
        t_vals.extend([t0, t1])
        mrr_vals.extend([mrr_scaled, mrr_scaled])

        seg = seg_map.get(s.segment_id)
        op_name = seg.op_id if seg else f"Tool {s.tool_id}"
        if not op_spans or op_spans[-1][0] != op_name:
            op_spans.append((op_name, t0, t1))
        else:
            name, start_t, _ = op_spans[-1]
            op_spans[-1] = (name, start_t, t1)

    y_max = max(mrr_vals or [0.01]) * 1.25

    # Shaded operation spans
    span_colors = ["#e8f1f8", "#f7f9fc"]
    for idx, (op_name, t0, t1) in enumerate(op_spans):
        ax_mrr.axvspan(t0, t1, facecolor=span_colors[idx % 2], alpha=0.7, zorder=0)
        if idx > 0:
            ax_mrr.axvline(t0, color="#666666", linestyle="--", linewidth=0.85, alpha=0.75, zorder=2)

    if t_vals:
        ax_mrr.fill_between(t_vals, mrr_vals, color="#1f77b4", alpha=0.30, zorder=3)
        ax_mrr.plot(t_vals, mrr_vals, color="#1f77b4", linewidth=0.8, label=f"Instantaneous MRR ({vol_unit}/min)", zorder=4)

    # Spike annotations
    for sp in report.mrr_spikes[:6]:
        t_mid = sp.timestamp_sec + 0.5 * sp.duration_sec
        mrr_s = sp.mrr_rate_mm3_per_min * vol_scale
        ax_mrr.plot(t_mid, mrr_s, "o", color="#e65100", markersize=5, markeredgecolor="black", zorder=6)
        ax_mrr.annotate(
            f"L{sp.segment_id} ({mrr_s:.2f})",
            xy=(t_mid, mrr_s),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            fontsize=7,
            color="#b71c1c",
            weight="bold",
            zorder=7,
        )

    total_time = max(1.0, report.total_machining_time_sec)
    ax_mrr.set_xlim(0, total_time)
    ax_mrr.set_ylim(0, y_max)
    ax_mrr.set_xlabel("Machining Time (seconds)", labelpad=6)
    ax_mrr.set_ylabel(f"MRR ({vol_unit} / min)")
    ax_mrr.set_title(
        f"Material Removal Rate (MRR) Time-Series — Flagged Spikes: {len(report.mrr_spikes)} | Collisions: {len(report.collisions)}",
        pad=8,
    )
    ax_mrr.legend(loc="upper right", fontsize=8, framealpha=0.9)
    ax_mrr.grid(True, linestyle=":", alpha=0.5, zorder=1)

    # Secondary span axis along the bottom
    if op_spans:
        ax_spans = ax_mrr.secondary_xaxis(-0.14)
        span_mids = [0.5 * (t0 + t1) for _, t0, t1 in op_spans]
        span_labels = [f"{op_name}\n[{t0:.0f}–{t1:.0f}s]" for op_name, t0, t1 in op_spans]
        ax_spans.set_xticks(span_mids)
        ax_spans.set_xticklabels(span_labels, rotation=25, ha="right", fontsize=7.5)
        ax_spans.tick_params(axis="x", length=0)
        ax_spans.spines["bottom"].set_visible(False)

    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(out_png_path)), exist_ok=True)
    fig.savefig(out_png_path, bbox_inches="tight", dpi=150)
    plt.close(fig)
