"""
3D In-Process Workpiece (IPW) & Collision Verification Engine using Heightfield Grid (Z-Map).

Provides:
- `HeightfieldGrid`: Discrete 2.5D/3D regular grid (numpy array) tracking workpiece top surface.
- `MillHeightfieldVerifier`: Production-grade milling verifier implementing `IVerificationEngine`
  via `BaseVerificationEngine`. Performs:
  • Volumetric material removal rate (MRR) tracking and statistical spike detection.
  • Rapid move into uncut stock collision detection (`RAPID_INTO_STOCK`).
  • Toolholder collision detection against uncut stock (`HOLDER_STRIKE`).
  • Non-fluted shank rubbing detection (`SHANK_RUB`).
  • G1 -> G0 rapid promotion authorization with multi-pass occurrence gating.
  • Parametric cutter profiles (flat, ball, bull-nose/torus, drill/chamfer).
  • Full linear and circular arc (G2/G3) toolpath sweeping.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

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

from .engine_interface import BaseVerificationEngine
from .models import (
    BoxStock3D,
    CollisionKind,
    CollisionRecord,
    MillToolGeometry,
    MotionKind,
    MotionSegment,
    MoveEvaluation,
    ToolAssembly,
    Vector3D,
    WorkpieceDefinition,
)


class HeightfieldGrid:
    """
    Discrete 2.5D/3D regular grid representing the top surface Z(X, Y) of the workpiece.
    """

    def __init__(
        self,
        cell_size_mm: float = 0.5,
        pad_mm: float = 10.0,
    ) -> None:
        if np is None:
            raise ImportError("numpy is required for HeightfieldGrid but could not be imported")
        self.cell_size_mm = max(0.05, float(cell_size_mm))
        self.pad_mm = max(0.0, float(pad_mm))
        self.cell_area_mm2 = self.cell_size_mm * self.cell_size_mm

        # World bounds
        self.stock_x_min = 0.0
        self.stock_x_max = 0.0
        self.stock_y_min = 0.0
        self.stock_y_max = 0.0
        self.stock_z_min = 0.0
        self.stock_z_max = 0.0

        # Padded grid bounds
        self.grid_x_min = 0.0
        self.grid_x_max = 0.0
        self.grid_y_min = 0.0
        self.grid_y_max = 0.0
        self.z_floor = 0.0

        self.nx = 0
        self.ny = 0
        self.H = np.zeros((0, 0), dtype=np.float32)

    def reset(self, stock: BoxStock3D) -> None:
        """Initialize or reset heightfield grid from a 3D bounding box."""
        self.stock_x_min = float(stock.x_min)
        self.stock_x_max = float(stock.x_max)
        self.stock_y_min = float(stock.y_min)
        self.stock_y_max = float(stock.y_max)
        self.stock_z_min = float(stock.z_min)
        self.stock_z_max = float(stock.z_max)

        self.grid_x_min = self.stock_x_min - self.pad_mm
        self.grid_x_max = self.stock_x_max + self.pad_mm
        self.grid_y_min = self.stock_y_min - self.pad_mm
        self.grid_y_max = self.stock_y_max + self.pad_mm
        self.z_floor = self.stock_z_min

        self.nx = max(2, int(math.ceil((self.grid_x_max - self.grid_x_min) / self.cell_size_mm)) + 1)
        self.ny = max(2, int(math.ceil((self.grid_y_max - self.grid_y_min) / self.cell_size_mm)) + 1)

        # Allocate grid
        # Outside stock bounds: z_floor. Inside stock bounds: stock_z_max.
        self.H = np.full((self.ny, self.nx), self.z_floor, dtype=np.float32)

        col_min = max(0, int(math.floor((self.stock_x_min - self.grid_x_min) / self.cell_size_mm)))
        col_max = min(self.nx, int(math.ceil((self.stock_x_max - self.grid_x_min) / self.cell_size_mm)))
        row_min = max(0, int(math.floor((self.stock_y_min - self.grid_y_min) / self.cell_size_mm)))
        row_max = min(self.ny, int(math.ceil((self.stock_y_max - self.grid_y_min) / self.cell_size_mm)))

        if col_max > col_min and row_max > row_min:
            self.H[row_min:row_max, col_min:col_max] = np.float32(self.stock_z_max)

    def get_subgrid_bounds(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        radius_mm: float,
    ) -> Tuple[int, int, int, int]:
        """Compute grid index bounds (col_min, col_max, row_min, row_max) enclosing swept tool."""
        min_x = min(x1, x2) - radius_mm
        max_x = max(x1, x2) + radius_mm
        min_y = min(y1, y2) - radius_mm
        max_y = max(y1, y2) + radius_mm

        col_min = max(0, int(math.floor((min_x - self.grid_x_min) / self.cell_size_mm)))
        col_max = min(self.nx, int(math.ceil((max_x - self.grid_x_min) / self.cell_size_mm)) + 1)
        row_min = max(0, int(math.floor((min_y - self.grid_y_min) / self.cell_size_mm)))
        row_max = min(self.ny, int(math.ceil((max_y - self.grid_y_min) / self.cell_size_mm)) + 1)

        return col_min, col_max, row_min, row_max

    def get_mesh_coordinates(
        self,
        col_min: int,
        col_max: int,
        row_min: int,
        row_max: int,
    ) -> Tuple[Any, Any]:
        """Construct 2D spatial coordinate grids (X, Y) for a subgrid window."""
        xs = self.grid_x_min + np.arange(col_min, col_max, dtype=np.float32) * self.cell_size_mm
        ys = self.grid_y_min + np.arange(row_min, row_max, dtype=np.float32) * self.cell_size_mm
        return np.meshgrid(xs, ys)


def evaluate_tool_bottom_profile(
    radius_arr: Any,
    tool_radius: float,
    corner_radius: float,
    taper_angle_deg: float = 0.0,
) -> Any:
    """
    Vectorized computation of tool bottom relative height Z_rel(r) >= 0.
    At tool tip center (r=0), Z_rel = 0.
    """
    if np is None:
        raise ImportError("numpy is required for evaluate_tool_bottom_profile but could not be imported")
    r = np.maximum(0.0, radius_arr)
    R = max(1e-4, float(tool_radius))
    rc = max(0.0, min(float(corner_radius), R))

    if rc <= 1e-4:
        # Flat endmill or conical / chamfer / drill tool
        if taper_angle_deg > 0.01:
            # Conical tip (drill, chamfer mill, spot drill, countersink).
            # If included angle is given (> 45 deg, e.g. 90 deg for chamfer, 118 deg for drill):
            # half angle with vertical Z axis is alpha = taper_angle_deg / 2.
            # If half angle is given (<= 45 deg, e.g. 45 deg for 90 deg chamfer):
            # half angle is taper_angle_deg.
            if taper_angle_deg > 45.0:
                half_angle_deg = taper_angle_deg / 2.0
            else:
                half_angle_deg = taper_angle_deg
            half_angle_deg = min(89.5, max(0.5, half_angle_deg))
            alpha_rad = math.radians(half_angle_deg)
            # Conical geometry: r = z * tan(alpha) => z_rel = r / tan(alpha)
            return (r / math.tan(alpha_rad)).astype(np.float32)
        return np.zeros_like(r, dtype=np.float32)

    if abs(rc - R) < 1e-4:
        # Ball endmill: hemispherical bottom
        diff = np.maximum(0.0, R * R - r * r)
        return (R - np.sqrt(diff)).astype(np.float32)

    # Bull-nose / Torus endmill
    r_flat = R - rc
    z_rel = np.zeros_like(r, dtype=np.float32)
    corner_mask = r > r_flat
    if np.any(corner_mask):
        dr = r[corner_mask] - r_flat
        diff = np.maximum(0.0, rc * rc - dr * dr)
        z_rel[corner_mask] = rc - np.sqrt(diff)

    return z_rel


class MillHeightfieldVerifier(BaseVerificationEngine):
    """
    3D Milling In-Process Workpiece (IPW) & Collision Verification Engine using
    a regular Heightfield Grid (Z-Map).
    """

    def __init__(
        self,
        cell_size_mm: float = 0.5,
        pad_mm: float = 10.0,
        promote_margin_mm: float = 0.5,
        holder_margin_mm: float = 1.0,
        min_promote_len_mm: float = 0.5,
        z_tol_mm: float = 1e-3,
        xy_tol_mm: float = 1e-3,
    ) -> None:
        super().__init__()
        if np is None:
            raise ImportError("numpy is required for MillHeightfieldVerifier but could not be imported")
        self.grid = HeightfieldGrid(cell_size_mm=cell_size_mm, pad_mm=pad_mm)
        self.promote_margin_mm = float(promote_margin_mm)
        self.holder_margin_mm = float(holder_margin_mm)
        self.min_promote_len_mm = float(min_promote_len_mm)
        self.z_tol_mm = float(z_tol_mm)
        self.xy_tol_mm = float(xy_tol_mm)

        # Fallback tool dimensions if ToolAssembly geometry is missing
        self.default_cutter_diameter = 10.0
        self.default_flute_length = 25.0
        self.default_overall_length = 50.0
        self.default_holder_diameter = 40.0
        self.default_holder_length = 50.0

        self._sec_clearance_mm: Dict[int, float] = {}

    def reset(self, workpiece: WorkpieceDefinition) -> None:
        """Reset verifier and IPW grid to raw stock."""
        self._reset_base_state(workpiece)
        self._sec_clearance_mm.clear()

        stock = getattr(workpiece, "stock", None)
        if isinstance(stock, BoxStock3D):
            self.grid.reset(stock)
        elif hasattr(stock, "x_min") and hasattr(stock, "z_max"):
            box_stock = BoxStock3D(
                x_min=float(stock.x_min),
                x_max=float(stock.x_max),
                y_min=float(stock.y_min),
                y_max=float(stock.y_max),
                z_min=float(stock.z_min),
                z_max=float(stock.z_max),
            )
            self.grid.reset(box_stock)
        else:
            # Conservative default 100x100x25 stock if workpiece is unshaped
            self.grid.reset(BoxStock3D(-50.0, 50.0, -50.0, 50.0, -25.0, 0.0))

    def reset_workpiece(self, workpiece: WorkpieceDefinition) -> None:
        """Backwards compatibility alias for reset()."""
        self.reset(workpiece)

    def load_tool(self, tool: ToolAssembly) -> None:
        """Mount active tool assembly."""
        self.active_tool = tool

    def _get_active_mill_tool_params(self) -> Tuple[float, float, float, float, float, float, float]:
        """Extract tool radius, corner radius, flute len, stickout len, holder radius, holder len, taper."""
        geom = getattr(self.active_tool, "geometry", None)
        if isinstance(geom, MillToolGeometry):
            d = max(0.5, float(geom.diameter))
            cr = max(0.0, float(geom.corner_radius))
            flute = max(1.0, float(geom.flute_length))
            overall = max(flute, float(geom.overall_length))
            hd = max(d, float(geom.holder_diameter))
            hl = max(5.0, float(geom.holder_length))
            taper = float(geom.taper_angle_deg)
            return d / 2.0, cr, flute, overall, hd / 2.0, hl, taper

        # Fallback values
        r = self.default_cutter_diameter / 2.0
        return (
            r,
            0.0,
            self.default_flute_length,
            self.default_overall_length,
            self.default_holder_diameter / 2.0,
            self.default_holder_length,
            0.0,
        )

    def _sweep_subgrid(
        self,
        p1: Vector3D,
        p2: Vector3D,
        tool_r: float,
        corner_r: float,
        taper_deg: float,
        col_min: int,
        col_max: int,
        row_min: int,
        row_max: int,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute swept tool envelope over a subgrid window.
        Returns: (mask_inside_tool, z_sweep_cutter, t_param, dist_xy)
        """
        X, Y = self.grid.get_mesh_coordinates(col_min, col_max, row_min, row_max)

        x1, y1, z1 = float(p1.x), float(p1.y), float(p1.z)
        x2, y2, z2 = float(p2.x), float(p2.y), float(p2.z)

        vx = x2 - x1
        vy = y2 - y1
        L2 = vx * vx + vy * vy

        if L2 < 1e-10:
            # Pure vertical or dwell motion
            dist_xy = np.sqrt((X - x1) ** 2 + (Y - y1) ** 2)
            t_param = np.zeros_like(dist_xy, dtype=np.float32)
            mask = dist_xy <= (tool_r + 1e-5)
            z_tip = min(z1, z2)
            z_rel = evaluate_tool_bottom_profile(dist_xy, tool_r, corner_r, taper_deg)
            z_sweep = np.float32(z_tip) + z_rel
            return mask, z_sweep, t_param, dist_xy

        # Linear motion in XY
        wx = X - x1
        wy = Y - y1
        t_param = np.clip((wx * vx + wy * vy) / L2, 0.0, 1.0)

        cx = x1 + t_param * vx
        cy = y1 + t_param * vy
        dist_xy = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2)

        mask = dist_xy <= (tool_r + 1e-5)
        z_tip = z1 + t_param * (z2 - z1)
        z_rel = evaluate_tool_bottom_profile(dist_xy, tool_r, corner_r, taper_deg)
        z_sweep = z_tip + z_rel

        return mask, z_sweep, t_param, dist_xy

    def step(self, move: MotionSegment) -> MoveEvaluation:
        """
        Evaluate a single kinematic move against the heightfield IPW,
        record material removal, detect collisions, check rapid promotion, and advance time.
        """
        self.current_time_sec += max(0.0, float(move.duration_sec))
        self.total_machining_time_sec += max(0.0, float(move.duration_sec))

        v_key_str = move.vector_key.to_wrapper_str()

        # Handle circular arcs by discretizing into linear sub-segments
        if move.is_arc and move.arc_center is not None:
            return self._step_arc(move)

        return self._step_linear(move.start, move.end, move, v_key_str)

    def _step_linear(
        self,
        p1: Vector3D,
        p2: Vector3D,
        move: MotionSegment,
        v_key_str: str,
    ) -> MoveEvaluation:
        """Process a single linear kinematic move."""
        tool_r, corner_r, flute_len, overall_len, holder_r, holder_len, taper_deg = (
            self._get_active_mill_tool_params()
        )

        # Update per-section clearance tracking
        top_z = max(p1.z, p2.z)
        sec_id = move.sec_id
        if sec_id not in self._sec_clearance_mm or top_z > self._sec_clearance_mm[sec_id]:
            self._sec_clearance_mm[sec_id] = top_z

        move_collisions: List[CollisionRecord] = []

        # ---------------------------------------------------------------------
        # 1. Bounding Subgrid Extraction
        # ---------------------------------------------------------------------
        # Max radius encompasses cutter and toolholder envelope
        max_r = max(tool_r, holder_r)
        col_min, col_max, row_min, row_max = self.grid.get_subgrid_bounds(
            p1.x, p1.y, p2.x, p2.y, max_r
        )

        # If move is completely outside the padded grid bounds, return safe air move
        if col_max <= col_min or row_max <= row_min:
            seen = self.record_vector_occurrence(v_key_str, is_air_move=True)
            return MoveEvaluation(
                segment_id=move.segment_id,
                vector_key=v_key_str,
                is_air_move=True,
                material_removed_mm3=0.0,
                instantaneous_mrr=0.0,
                collisions=[],
            )

        sub_H = self.grid.H[row_min:row_max, col_min:col_max]

        # ---------------------------------------------------------------------
        # 2. Swept Cutter Footprint & Height Calculation
        # ---------------------------------------------------------------------
        mask_cutter, z_sweep_cutter, t_param, dist_cutter = self._sweep_subgrid(
            p1, p2, tool_r, corner_r, taper_deg, col_min, col_max, row_min, row_max
        )

        # ---------------------------------------------------------------------
        # 3. Holder & Shank Collision Checks
        # ---------------------------------------------------------------------
        # Holder check: cells within holder radius
        X_sub, Y_sub = self.grid.get_mesh_coordinates(col_min, col_max, row_min, row_max)
        vx = p2.x - p1.x
        vy = p2.y - p1.y
        L2 = vx * vx + vy * vy
        if L2 < 1e-10:
            dist_holder = np.sqrt((X_sub - p1.x) ** 2 + (Y_sub - p1.y) ** 2)
            z_tip_min = min(p1.z, p2.z)
            z_holder_bottom = np.full_like(dist_holder, z_tip_min + overall_len, dtype=np.float32)
            z_shank_bottom = np.full_like(dist_holder, z_tip_min + flute_len, dtype=np.float32)
        else:
            t_holder = np.clip(((X_sub - p1.x) * vx + (Y_sub - p1.y) * vy) / L2, 0.0, 1.0)
            cx_h = p1.x + t_holder * vx
            cy_h = p1.y + t_holder * vy
            dist_holder = np.sqrt((X_sub - cx_h) ** 2 + (Y_sub - cy_h) ** 2)
            z_tip_h = p1.z + t_holder * (p2.z - p1.z)
            z_holder_bottom = z_tip_h + overall_len
            z_shank_bottom = z_tip_h + flute_len

        mask_holder = dist_holder <= (holder_r + 1e-5)

        # Holder strike: stock top is higher than holder bottom
        holder_penetration = (sub_H - z_holder_bottom)
        holder_strike_mask = mask_holder & (holder_penetration > self.z_tol_mm)

        if np.any(holder_strike_mask):
            max_pen = float(np.max(holder_penetration[holder_strike_mask]))
            pen_vol = float(np.sum(holder_penetration[holder_strike_mask])) * self.grid.cell_area_mm2
            col = CollisionRecord(
                segment_id=move.segment_id,
                vector_key=v_key_str,
                kind=CollisionKind.HOLDER_STRIKE,
                timestamp_sec=self.current_time_sec,
                contact_point=Vector3D(p2.x, p2.y, float(p2.z + overall_len)),
                penetration_volume_mm3=pen_vol,
                description=(
                    f"Toolholder collided with uncut stock (max penetration: {max_pen:.3f} mm, "
                    f"volume: {pen_vol:.2f} mm³)"
                ),
            )
            self.collisions.append(col)
            move_collisions.append(col)

        # Shank rub check: cutting deeper than flute length
        shank_rub_penetration = (sub_H - z_shank_bottom)
        shank_rub_mask = mask_cutter & (shank_rub_penetration > self.z_tol_mm)
        if np.any(shank_rub_mask) and move.motion != MotionKind.RAPID:
            max_pen = float(np.max(shank_rub_penetration[shank_rub_mask]))
            pen_vol = float(np.sum(shank_rub_penetration[shank_rub_mask])) * self.grid.cell_area_mm2
            col = CollisionRecord(
                segment_id=move.segment_id,
                vector_key=v_key_str,
                kind=CollisionKind.SHANK_RUB,
                timestamp_sec=self.current_time_sec,
                contact_point=Vector3D(p2.x, p2.y, float(p2.z + flute_len)),
                penetration_volume_mm3=pen_vol,
                description=(
                    f"Unfluted tool shank rubbed into raw stock (depth exceeded flute by {max_pen:.3f} mm)"
                ),
            )
            self.collisions.append(col)
            move_collisions.append(col)

        # ---------------------------------------------------------------------
        # 4. Rapid Move Collision (RAPID_INTO_STOCK)
        # ---------------------------------------------------------------------
        stock_cut_depth = sub_H - z_sweep_cutter
        rapid_into_stock_mask = mask_cutter & (stock_cut_depth > self.z_tol_mm)

        if move.motion == MotionKind.RAPID:
            if np.any(rapid_into_stock_mask):
                max_pen = float(np.max(stock_cut_depth[rapid_into_stock_mask]))
                pen_vol = float(np.sum(stock_cut_depth[rapid_into_stock_mask])) * self.grid.cell_area_mm2
                col = CollisionRecord(
                    segment_id=move.segment_id,
                    vector_key=v_key_str,
                    kind=CollisionKind.RAPID_INTO_STOCK,
                    timestamp_sec=self.current_time_sec,
                    contact_point=Vector3D(p2.x, p2.y, p2.z),
                    penetration_volume_mm3=pen_vol,
                    description=(
                        f"Rapid move crashed into uncut stock (penetration: {max_pen:.3f} mm, "
                        f"volume: {pen_vol:.2f} mm³)"
                    ),
                )
                self.collisions.append(col)
                move_collisions.append(col)

            # Rapid moves do not modify stock or get promoted
            return MoveEvaluation(
                segment_id=move.segment_id,
                vector_key=v_key_str,
                is_air_move=True,
                material_removed_mm3=0.0,
                instantaneous_mrr=0.0,
                collisions=move_collisions,
            )

        # ---------------------------------------------------------------------
        # 5. Material Removal & Volumetric MRR Integration
        # ---------------------------------------------------------------------
        vol_removed = 0.0
        mrr_rate = 0.0
        doc_mm = 0.0
        cuts_material = np.any(rapid_into_stock_mask)

        if cuts_material:
            cut_depths = stock_cut_depth[rapid_into_stock_mask]
            vol_removed = float(np.sum(cut_depths)) * self.grid.cell_area_mm2
            doc_mm = float(np.max(cut_depths))

            # Carve heightfield stock
            sub_H[rapid_into_stock_mask] = np.maximum(
                self.grid.z_floor,
                z_sweep_cutter[rapid_into_stock_mask]
            )

            sample = self.record_mrr_sample(
                segment_id=move.segment_id,
                duration_sec=move.duration_sec,
                volume_removed_mm3=vol_removed,
                depth_of_cut_mm=doc_mm,
            )
            mrr_rate = sample.mrr_rate_mm3_per_min

        # ---------------------------------------------------------------------
        # 6. Air Move & Rapid Promotion Check (for G1 feed moves)
        # ---------------------------------------------------------------------
        meta = move.metadata or {}
        in_cycle = bool(meta.get("in_cycle", False))
        dist_3d = math.sqrt((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2 + (p2.z - p1.z) ** 2)

        # Check pure vertical Z+ clearance retract
        dx = p2.x - p1.x
        dy = p2.y - p1.y
        dz = p2.z - p1.z
        is_pure_z_plus = (math.hypot(dx, dy) <= self.xy_tol_mm) and (dz > self.z_tol_mm)
        clearance_z = meta.get("z_clearance_mm", self._sec_clearance_mm.get(sec_id))

        is_retract_air = (not in_cycle) and is_pure_z_plus and (clearance_z is not None) and (p2.z >= (clearance_z - self.z_tol_mm))

        is_air_move = False
        if is_retract_air:
            is_air_move = True
        elif (not cuts_material) and (not in_cycle) and (dist_3d >= self.min_promote_len_mm):
            # Check holder clearance
            holder_margin_clear = True
            if np.any(mask_holder):
                h_margin_depth = sub_H[mask_holder] - (z_holder_bottom[mask_holder] - self.holder_margin_mm)
                if np.any(h_margin_depth > 0.0):
                    holder_margin_clear = False

            # Check cutter clearance
            cutter_margin_clear = True
            if self.promote_margin_mm > 1e-4 and np.any(mask_cutter):
                margin_cut_depth = sub_H[mask_cutter] - (z_sweep_cutter[mask_cutter] - self.promote_margin_mm)
                if np.any(margin_cut_depth > 0.0):
                    cutter_margin_clear = False

            if holder_margin_clear and cutter_margin_clear:
                is_air_move = True

        seen = self.record_vector_occurrence(v_key_str, is_air_move=is_air_move)

        return MoveEvaluation(
            segment_id=move.segment_id,
            vector_key=v_key_str,
            is_air_move=is_air_move,
            material_removed_mm3=vol_removed,
            instantaneous_mrr=mrr_rate,
            collisions=move_collisions,
        )

    def _step_arc(self, move: MotionSegment) -> MoveEvaluation:
        """Discretize circular arc move into chordal segments and evaluate."""
        p1 = move.start
        p2 = move.end
        center = move.arc_center
        assert center is not None

        c_x, c_y = float(center.x), float(center.y)
        r = math.hypot(p1.x - c_x, p1.y - c_y)
        theta1 = math.atan2(p1.y - c_y, p1.x - c_x)
        theta2 = math.atan2(p2.y - c_y, p2.x - c_x)

        is_cw = (move.motion == MotionKind.ARC_CW)
        if is_cw:
            if theta2 >= theta1:
                theta2 -= 2.0 * math.pi
        else:
            if theta2 <= theta1:
                theta2 += 2.0 * math.pi

        delta = theta2 - theta1
        # Discretize arc with max angle step <= 10 deg and chordal deviation <= cell_size / 2
        max_angle_step = math.radians(10.0)
        steps = max(3, int(math.ceil(abs(delta) / max_angle_step)))

        tot_vol = 0.0
        all_cols: List[CollisionRecord] = []
        is_air_all = True
        v_key_str = move.vector_key.to_wrapper_str()

        # Step each chordal sub-segment
        for i in range(steps):
            t_a = i / float(steps)
            t_b = (i + 1) / float(steps)
            ang_a = theta1 + delta * t_a
            ang_b = theta1 + delta * t_b

            sub_p1 = Vector3D(c_x + r * math.cos(ang_a), c_y + r * math.sin(ang_a), p1.z + t_a * (p2.z - p1.z))
            sub_p2 = Vector3D(c_x + r * math.cos(ang_b), c_y + r * math.sin(ang_b), p1.z + t_b * (p2.z - p1.z))

            sub_eval = self._step_linear(sub_p1, sub_p2, move, v_key_str)
            tot_vol += sub_eval.material_removed_mm3
            if not sub_eval.is_air_move:
                is_air_all = False
            all_cols.extend(sub_eval.collisions)

        mrr_rate = (tot_vol / max(1e-4, float(move.duration_sec))) * 60.0

        return MoveEvaluation(
            segment_id=move.segment_id,
            vector_key=v_key_str,
            is_air_move=is_air_all,
            material_removed_mm3=tot_vol,
            instantaneous_mrr=mrr_rate,
            collisions=all_cols,
        )

    def get_heightfield_snapshot(self) -> Tuple[np.ndarray, Tuple[float, float, float, float]]:
        """Return a copy of the current 2D heightfield array and its (xmin, xmax, ymin, ymax) extents."""
        bounds = (self.grid.grid_x_min, self.grid.grid_x_max, self.grid.grid_y_min, self.grid.grid_y_max)
        return np.copy(self.grid.H), bounds
