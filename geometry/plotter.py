"""
2D Lathe & 3D Isometric Milling Geometry and MRR Verification Plotter.

Generates comprehensive high-resolution .png verification reports:
1. Turning: 2D Cross-section geometry (Initial Stock, Final IPW, Chuck Keep-out, Kinematic Moves,
   Insert/Holder silhouettes, and Collision/Spike locations).
2. Milling: 3D Isometric projection from X+ Y- Z+ (forward-top-right) of Remaining Stock (IPW Heightfield),
   Initial Stock Wireframe, Kinematic Moves (G0/G1/G2/G3), Tool Cylinder, and Collision/Spike markers.
3. Dual-Panel MRR Time-Series: Instantaneous MRR, Operation timeline spans, Mean MRR, Spike thresholds,
   and Timeline collision markers.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure packages directory is discoverable for Fusion 360's Python 3.14 environment
if sys.version_info >= (3, 14):
    _addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _packages_dir = os.path.join(_addon_dir, "packages")
    if os.path.isdir(_packages_dir) and _packages_dir not in sys.path:
        sys.path.insert(0, _packages_dir)

try:
    import numpy as np
except Exception:
    np = None

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Polygon as MplPolygon
from mpl_toolkits.mplot3d import Axes3D
from shapely.affinity import translate
from shapely.geometry import Polygon

from .models import (
    BoxStock3D,
    CollisionKind,
    CollisionRecord,
    MillToolGeometry,
    MotionKind,
    MotionSegment,
    ToolAssembly,
    VerificationReport,
    WorkpieceDefinition,
)

COLLISION_STYLES = {
    CollisionKind.RAPID_INTO_STOCK: ("#d62728", "X", "Rapid Into Stock"),
    CollisionKind.HOLDER_STRIKE: ("#e377c2", "s", "Holder Strike"),
    CollisionKind.FIXTURE_COLLISION: ("#ff7f0e", "^", "Fixture / Chuck Collision"),
    CollisionKind.SHANK_RUB: ("#9467bd", "v", "Shank Rub"),
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


def _plot_mrr_timeline(
    ax_mrr,
    report: VerificationReport,
    segments: List[MotionSegment],
    vol_scale: float,
    vol_unit: str,
) -> None:
    """Plot Panel 2: MRR Time-Series with Operation Spans and Spike/Collision annotations."""
    # 1. Build complete, gapless operation spans from all kinematic segments
    op_spans: List[Tuple[str, float, float]] = []
    t_curr = 0.0
    for seg in segments:
        dur = max(0.0, float(seg.duration_sec))
        if dur <= 0:
            continue
        op_name = seg.op_id or f"Section {seg.sec_id}"
        t_next = t_curr + dur
        if not op_spans or op_spans[-1][0] != op_name:
            op_spans.append((op_name, t_curr, t_next))
        else:
            name, t_start, _ = op_spans[-1]
            op_spans[-1] = (name, t_start, t_next)
        t_curr = t_next

    # Fallback if segments was empty or all durations were 0
    if not op_spans:
        for s in report.mrr_timeline:
            t0 = max(0.0, s.timestamp_sec)
            t1 = max(t0, s.timestamp_sec + s.duration_sec)
            op_name = f"Tool {s.tool_id}"
            if not op_spans or op_spans[-1][0] != op_name:
                op_spans.append((op_name, t0, t1))
            else:
                name, t_start, _ = op_spans[-1]
                op_spans[-1] = (name, t_start, t1)
        t_curr = op_spans[-1][2] if op_spans else 0.0

    total_time = max(1.0, report.total_machining_time_sec, t_curr)
    # Ensure final span reaches total_time so there are no trailing unshaded margins
    if op_spans and op_spans[-1][2] < total_time:
        name, t_start, _ = op_spans[-1]
        op_spans[-1] = (name, t_start, total_time)

    # 2. Shaded operation spans across 100% of the timeline
    span_colors = ["#e8f1f8", "#f7f9fc"]
    for idx, (op_name, t0, t1) in enumerate(op_spans):
        ax_mrr.axvspan(t0, t1, facecolor=span_colors[idx % 2], alpha=0.7, zorder=0)
        if idx > 0:
            ax_mrr.axvline(t0, color="#666666", linestyle="--", linewidth=0.85, alpha=0.75, zorder=2)

    # 3. Build instantaneous MRR timeline with zero-level steps during air/rapid intervals
    t_vals: List[float] = []
    mrr_vals: List[float] = []
    last_t = 0.0

    for s in report.mrr_timeline:
        t0 = max(0.0, s.timestamp_sec)
        t1 = max(t0, s.timestamp_sec + s.duration_sec)
        mrr_scaled = s.mrr_rate_mm3_per_min * vol_scale

        if t0 > last_t + 1e-4:
            t_vals.extend([last_t, t0])
            mrr_vals.extend([0.0, 0.0])

        t_vals.extend([t0, t1])
        mrr_vals.extend([mrr_scaled, mrr_scaled])
        last_t = t1

    if t_vals and last_t < total_time:
        t_vals.extend([last_t, total_time])
        mrr_vals.extend([0.0, 0.0])

    if t_vals:
        ax_mrr.fill_between(t_vals, mrr_vals, color="#1f77b4", alpha=0.30, zorder=3)
        ax_mrr.plot(t_vals, mrr_vals, color="#1f77b4", linewidth=0.8, label=f"Instantaneous MRR ({vol_unit}/min)", zorder=4)

    # 4. Plot ALL MRR Spikes (clean markers without unhelpful text labels)
    spike_t: List[float] = []
    spike_mrr: List[float] = []
    for sp in report.mrr_spikes:
        t_mid = sp.timestamp_sec + 0.5 * sp.duration_sec
        mrr_s = sp.mrr_rate_mm3_per_min * vol_scale
        spike_t.append(t_mid)
        spike_mrr.append(mrr_s)

    if spike_t:
        ax_mrr.scatter(
            spike_t,
            spike_mrr,
            color="#e65100",
            s=45,
            edgecolors="black",
            linewidths=1.0,
            zorder=6,
            label="Flagged MRR Spike",
        )

    # 5. Timeline collision markers
    for col in report.collisions:
        color, marker, _ = COLLISION_STYLES.get(col.kind, ("#d62728", "X", col.kind.name))
        ax_mrr.axvline(col.timestamp_sec, color=color, linestyle=":", linewidth=1.2, alpha=0.8, zorder=5)

    # Ensure y_max accommodates all spikes and curve peaks
    max_curve_mrr = max(mrr_vals) if mrr_vals else 0.0
    max_spike_mrr = max([sp.mrr_rate_mm3_per_min * vol_scale for sp in report.mrr_spikes], default=0.0)
    y_max = max(max_curve_mrr, max_spike_mrr, 0.01) * 1.25

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


def generate_verification_plot(
    workpiece: WorkpieceDefinition,
    final_ipw: Polygon,
    chuck_obstacle: Polygon,
    segments: List[MotionSegment],
    report: VerificationReport,
    tools_by_op: Dict[str, ToolAssembly],
    out_png_path: str,
    unit_is_inch: bool = True,
) -> None:
    """Generate the dual-panel 2D geometry and MRR verification plot for turning setups."""
    scale = 1.0 / 25.4 if unit_is_inch else 1.0
    u_label = "in" if unit_is_inch else "mm"
    vol_scale = (1.0 / (25.4 ** 3)) if unit_is_inch else 1.0
    vol_unit = "in³" if unit_is_inch else "mm³"

    fig, (ax_geo, ax_mrr) = plt.subplots(
        2, 1, figsize=(13, 10), gridspec_kw={"height_ratios": [1.15, 1.0]}
    )

    # Panel 1: 2D Turning Cross-Section
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

    seg_map = {s.segment_id: s for s in segments}

    # Flag MRR spikes and Collisions
    for sp in report.mrr_spikes:
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

    # Panel 2: MRR Time-Series
    _plot_mrr_timeline(ax_mrr, report, segments, vol_scale, vol_unit)

    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(out_png_path)), exist_ok=True)
    fig.savefig(out_png_path, bbox_inches="tight", dpi=150)
    plt.close(fig)


def generate_mill_verification_plot(
    workpiece: WorkpieceDefinition,
    grid: Any,  # HeightfieldGrid
    segments: List[MotionSegment],
    report: VerificationReport,
    tools_by_op: Dict[str, ToolAssembly],
    out_png_path: str,
    unit_is_inch: bool = True,
) -> None:
    """
    Generate dual-panel 3D isometric IPW geometry and MRR verification plot for milling setups.
    Panel 1: 3D Isometric View from X+ Y- Z+ (forward-top-right) of Remaining Stock (IPW Heightfield),
             Initial Stock Wireframe, Kinematic Moves, Collision Markers, and Tool.
    Panel 2: MRR Time-Series with Operation Spans and Spike/Collision annotations.
    """
    scale = 1.0 / 25.4 if unit_is_inch else 1.0
    u_label = "in" if unit_is_inch else "mm"
    vol_scale = (1.0 / (25.4 ** 3)) if unit_is_inch else 1.0
    vol_unit = "in³" if unit_is_inch else "mm³"

    fig = plt.figure(figsize=(14, 11), dpi=200)
    gs = fig.add_gridspec(2, 1, height_ratios=[1.5, 1.0])
    ax_geo = fig.add_subplot(gs[0], projection="3d")
    ax_mrr = fig.add_subplot(gs[1])

    # 1. Isometric camera orientation: X+ Y- Z+ (forward-top-right)
    ax_geo.view_init(elev=30, azim=-60)
    ax_geo.computed_zorder = False

    seen_labels: Set[str] = set()

    # 2. Initial Stock Bounding Box Outline
    stock = getattr(workpiece, "stock", None)
    if isinstance(stock, BoxStock3D):
        sx_min, sx_max = stock.x_min * scale, stock.x_max * scale
        sy_min, sy_max = stock.y_min * scale, stock.y_max * scale
        sz_min, sz_max = stock.z_min * scale, stock.z_max * scale
    elif grid is not None and getattr(grid, "stock_x_max", 0.0) != 0.0:
        sx_min, sx_max = grid.stock_x_min * scale, grid.stock_x_max * scale
        sy_min, sy_max = grid.stock_y_min * scale, grid.stock_y_max * scale
        sz_min, sz_max = grid.stock_z_min * scale, grid.stock_z_max * scale
    else:
        sx_min, sx_max, sy_min, sy_max, sz_min, sz_max = -50.0 * scale, 50.0 * scale, -50.0 * scale, 50.0 * scale, -25.0 * scale, 0.0

    dx = max(1e-3, sx_max - sx_min)
    dy = max(1e-3, sy_max - sy_min)
    dz = max(1e-3, sz_max - sz_min)
    try:
        # True 1:1 metric physical aspect ratio matching CAD, scaled up to utilize canvas
        ax_geo.set_box_aspect((dx, dy, dz), zoom=1.28)
    except Exception:
        try:
            ax_geo.set_box_aspect((dx, dy, dz))
        except Exception:
            pass

    corners = [
        (sx_min, sy_min, sz_min), (sx_max, sy_min, sz_min),
        (sx_max, sy_max, sz_min), (sx_min, sy_max, sz_min),
        (sx_min, sy_min, sz_max), (sx_max, sy_min, sz_max),
        (sx_max, sy_max, sz_max), (sx_min, sy_max, sz_max),
    ]
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    for e1, e2 in edges:
        c1, c2 = corners[e1], corners[e2]
        lbl = "Initial Stock" if "Initial Stock" not in seen_labels else None
        seen_labels.add("Initial Stock")
        ax_geo.plot([c1[0], c2[0]], [c1[1], c2[1]], [c1[2], c2[2]], color="#666666", linestyle="--", linewidth=1.1, zorder=2, label=lbl)

    # 3. Remaining Stock IPW 3D Surface
    has_surface = False
    if np is not None and grid is not None and hasattr(grid, "H") and grid.H is not None and grid.H.size > 0:
        try:
            col_min = max(0, int(math.floor((grid.stock_x_min - grid.grid_x_min) / grid.cell_size_mm)))
            col_max = min(grid.nx, int(math.ceil((grid.stock_x_max - grid.grid_x_min) / grid.cell_size_mm)))
            row_min = max(0, int(math.floor((grid.stock_y_min - grid.grid_y_min) / grid.cell_size_mm)))
            row_max = min(grid.ny, int(math.ceil((grid.stock_y_max - grid.grid_y_min) / grid.cell_size_mm)))

            if col_max > col_min and row_max > row_min:
                from mpl_toolkits.mplot3d.art3d import Poly3DCollection
                from .mesh_utils import greedy_mesh_heightfield, compute_cam_palette_facecolors

                x_min_sub = (grid.grid_x_min + col_min * grid.cell_size_mm) * scale
                y_min_sub = (grid.grid_y_min + row_min * grid.cell_size_mm) * scale
                Z = grid.H[row_min:row_max, col_min:col_max] * scale
                cell_mm = grid.cell_size_mm * scale

                polygons, normals, z_levels = greedy_mesh_heightfield(
                    Z,
                    x_min=float(x_min_sub),
                    y_min=float(y_min_sub),
                    cell_size=float(cell_mm),
                    z_floor=float(sz_min),
                    omit_floor=True,
                    tol_mm=0.05 * scale,
                )

                if polygons:
                    facecolors = compute_cam_palette_facecolors(
                        normals,
                        z_levels,
                        z_stock_min=float(sz_min),
                        z_stock_max=float(sz_max),
                        light_azdeg=315.0,
                        light_altdeg=45.0,
                    )
                    edgecolors = np.copy(facecolors)
                    edgecolors[:, :3] *= 0.95  # Match reference CAM edge tint exactly

                    mesh_col = Poly3DCollection(
                        polygons,
                        facecolors=facecolors,
                        edgecolors=edgecolors,
                        linewidths=0.2,
                        antialiased=True,
                    )
                    mesh_col.set_zorder(1)
                    ax_geo.add_collection3d(mesh_col)
                    has_surface = True
                    seen_labels.add("Remaining Stock (IPW)")
        except Exception:
            pass

    # Viewport bounds: slight margin around stock prevents edge clipping while maintaining maximum zoom
    pad_x = max(1.5 * scale, 0.04 * (sx_max - sx_min))
    pad_y = max(1.5 * scale, 0.04 * (sy_max - sy_min))
    ax_geo.set_xlim(sx_min - pad_x, sx_max + pad_x)
    ax_geo.set_ylim(sy_min - pad_y, sy_max + pad_y)
    ax_geo.set_zlim(sz_min - 0.5 * scale, max(sz_max + 1.5 * scale, 3.0 * scale))

    # Clean transparent panes and completely remove 3D grid lines
    ax_geo.grid(False)
    try:
        ax_geo.xaxis._axinfo["grid"]["color"] = (0, 0, 0, 0)
        ax_geo.yaxis._axinfo["grid"]["color"] = (0, 0, 0, 0)
        ax_geo.zaxis._axinfo["grid"]["color"] = (0, 0, 0, 0)
    except Exception:
        pass
    ax_geo.xaxis.pane.fill = False
    ax_geo.yaxis.pane.fill = False
    ax_geo.zaxis.pane.fill = False
    ax_geo.xaxis.pane.set_edgecolor("#cbd5e1")
    ax_geo.yaxis.pane.set_edgecolor("#cbd5e1")
    ax_geo.zaxis.pane.set_edgecolor("#cbd5e1")
    ax_geo.tick_params(colors="#475569", labelsize=8)

    # 4. Kinematic Rapid Positioning Moves (Air Toolpath)
    # Avoid drawing dense internal cutting passes over solid part to preserve sharp surface boundaries
    for seg in segments:
        x1, y1, z1 = seg.start.x * scale, seg.start.y * scale, seg.start.z * scale
        x2, y2, z2 = seg.end.x * scale, seg.end.y * scale, seg.end.z * scale
        v_key = seg.vector_key.to_wrapper_str()
        is_promoted = v_key in report.promoted_vectors

        if is_promoted:
            lbl = "G0 Promoted Rapid Move" if "G0 Promoted Rapid Move" not in seen_labels else None
            seen_labels.add("G0 Promoted Rapid Move")
            ax_geo.plot([x1, x2], [y1, y2], [z1, z2], color="#dc2626", linestyle="--", linewidth=0.85, alpha=0.75, zorder=5, label=lbl)
        elif seg.motion == MotionKind.RAPID:
            lbl = "G0 Initial Positioning" if "G0 Initial Positioning" not in seen_labels else None
            seen_labels.add("G0 Initial Positioning")
            ax_geo.plot([x1, x2], [y1, y2], [z1, z2], color="#16a34a", linestyle=":", linewidth=0.8, alpha=0.65, zorder=4, label=lbl)

    seg_map = {s.segment_id: s for s in segments}

    # 5. Flagged Collisions (Positioned at Stock Contact Surface with Explicit zorder)
    for col in report.collisions:
        color, marker, desc = COLLISION_STYLES.get(col.kind, ("#dc2626", "X", col.kind.name))
        col_lbl_key = f"Collision: {desc}"
        col_lbl = col_lbl_key if col_lbl_key not in seen_labels else None
        seen_labels.add(col_lbl_key)

        cx = float(np.clip(col.contact_point.x * scale, sx_min - pad_x, sx_max + pad_x))
        cy = float(np.clip(col.contact_point.y * scale, sy_min - pad_y, sy_max + pad_y))
        cz = float(col.contact_point.z * scale)
        if grid is not None and hasattr(grid, "H") and grid.H is not None:
            c_idx = int(round((cx / scale - grid.grid_x_min) / grid.cell_size_mm))
            r_idx = int(round((cy / scale - grid.grid_y_min) / grid.cell_size_mm))
            if 0 <= r_idx < grid.ny and 0 <= c_idx < grid.nx:
                cz = max(cz, float(grid.H[r_idx, c_idx] * scale))

        ax_geo.scatter(
            cx,
            cy,
            cz + 0.15 * scale,
            marker=marker,
            color=color,
            s=65,
            edgecolors="#7f1d1d",
            linewidths=1.3,
            zorder=20,
            label=col_lbl,
        )

    # 6. Flagged Prominent MRR Spikes (Anchored to True Heightfield Surface with Explicit zorder)
    for sp in report.mrr_spikes:
        seg = seg_map.get(sp.segment_id)
        if seg:
            lbl = "Flagged MRR Spike" if "Flagged MRR Spike" not in seen_labels else None
            seen_labels.add("Flagged MRR Spike")
            mx = float((seg.start.x + seg.end.x) / 2.0 * scale)
            my = float((seg.start.y + seg.end.y) / 2.0 * scale)

            # Query the actual surface depth in the heightfield grid at this cut location:
            if grid is not None and hasattr(grid, "H") and grid.H is not None:
                c_idx = int(round((mx / scale - grid.grid_x_min) / grid.cell_size_mm))
                r_idx = int(round((my / scale - grid.grid_y_min) / grid.cell_size_mm))
                if 0 <= r_idx < grid.ny and 0 <= c_idx < grid.nx:
                    surf_z = float(grid.H[r_idx, c_idx] * scale)
                    mz = max(float(seg.end.z * scale), surf_z)
                else:
                    mz = float(seg.end.z * scale)
            else:
                mz = float(seg.end.z * scale)

            ax_geo.scatter(
                mx,
                my,
                mz + 0.15 * scale,
                color="#f97316",
                s=28,
                edgecolors="#7c2d12",
                linewidths=0.7,
                zorder=10,
                label=lbl,
            )

    ax_geo.set_xlabel(f"X Axis ({u_label})", labelpad=8)
    ax_geo.set_ylabel(f"Y Axis ({u_label})", labelpad=8)
    ax_geo.set_zlabel(f"Z Axis ({u_label})", labelpad=8)
    ax_geo.set_title(
        f"3D Mill IPW Verification — Volume Removed: {report.total_volume_removed_mm3 * vol_scale:.3f} {vol_unit} | "
        f"Promoted G0 Moves: {len(report.promoted_vectors)} | Collisions: {len(report.collisions)}",
        pad=10,
        fontsize=10.5,
        weight="bold",
    )

    # Proxy artist for Remaining Stock IPW in legend if drawn
    handles, labels = ax_geo.get_legend_handles_labels()
    if has_surface:
        handles.insert(1, Patch(facecolor="#22c55e", edgecolor="#15803d", label="Remaining Stock (IPW)"))
        labels.insert(1, "Remaining Stock (IPW)")
    ax_geo.legend(
        handles,
        labels,
        loc="upper left",
        bbox_to_anchor=(0.015, 0.98),
        bbox_transform=fig.transFigure,
        fontsize=8.5,
        framealpha=0.95,
        edgecolor="#cbd5e1",
        title="Verifier Annotations",
        title_fontsize=9.5,
    )

    # 6. Panel 2: MRR Time-Series
    _plot_mrr_timeline(ax_mrr, report, segments, vol_scale, vol_unit)

    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(out_png_path)), exist_ok=True)
    fig.savefig(out_png_path, bbox_inches="tight", dpi=200)
    plt.close(fig)
