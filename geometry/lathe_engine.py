"""
Real 2D Lathe In-Process Workpiece (IPW) & Collision Verification Engine.

Implements IVerificationEngine using optimized Shapely 2D polygon clipping:
- Single-quadrant (Z, Radius >= 0) Axisymmetric stock representation with tube/bore support.
- Convex-hull optimized swept kinematics for standard turning inserts and toolholders.
- Discretized circular arc sweeping (MOVARC, G2/G3) for exact contour envelope fidelity.
- Reflected radial envelope checking for internal boring bars and holders crossing the spindle centerline.
- Fast AABB (Axis-Aligned Bounding Box) spatial pre-filtering and inverted buffer checking.
- Pappus's centroid theorem for 3D volumetric material removal (mm³).
- Collision detection for chuck fixtures, rapid moves into stock, and holder rubbing.
- Monotonic occurrence-gated rapid move authorization for universal_wrapper.cps.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple
from shapely.affinity import translate, scale as shapely_scale
from shapely.geometry import Polygon, MultiPolygon, box
from shapely.ops import unary_union

from .models import (
    AxisymmetricStock2D,
    AxisymmetricChuckObstacle,
    CollisionKind,
    CollisionRecord,
    LatheToolGeometry,
    MotionKind,
    MotionSegment,
    MoveEvaluation,
    MRRSample,
    PromotedVectorRule,
    ToolAssembly,
    Vector3D,
    VerificationReport,
    WorkpieceDefinition,
)
from .engine_interface import BaseVerificationEngine, IVerificationEngine


# Domain Constants
ESTIMATED_PENETRATION_DEPTH_MM = 10.0
HOLDER_CUTTER_TRANSITION_TOL_MM = 0.05


def aabb_overlap(
    b1: Tuple[float, float, float, float],
    b2: Tuple[float, float, float, float],
) -> bool:
    """Fast O(1) Axis-Aligned Bounding Box (AABB) overlap check (minx, miny, maxx, maxy)."""
    return not (b1[2] < b2[0] or b1[0] > b2[2] or b1[3] < b2[1] or b1[1] > b2[3])


def sweep_linear_polygon(
    poly: Polygon,
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    is_convex: bool = False,
) -> Polygon:
    """Compute the 2D area swept by translating poly linearly from p1 to p2 in (Z, R)."""
    if poly.is_empty:
        return Polygon()
    poly1 = translate(poly, xoff=p1[0], yoff=p1[1])
    poly2 = translate(poly, xoff=p2[0], yoff=p2[1])
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    if math.hypot(dx, dy) < 1e-6:
        return poly1

    # Fast path for convex cutters (rhombic, triangular, round inserts):
    # Minkowski sum of convex poly and linear translation segment equals convex hull.
    if is_convex:
        return MultiPolygon([poly1, poly2]).convex_hull

    # General path for non-convex toolholders
    coords = list(poly1.exterior.coords)
    quads = [
        Polygon([
            coords[i],
            coords[i + 1],
            (coords[i + 1][0] + dx, coords[i + 1][1] + dy),
            (coords[i][0] + dx, coords[i][1] + dy),
        ])
        for i in range(len(coords) - 1)
    ]
    return unary_union([poly1, poly2] + quads).buffer(0)


def sweep_arc_polygon(
    poly: Polygon,
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    center: Tuple[float, float],
    is_cw: bool,
    is_convex: bool = False,
    max_step_deg: float = 10.0,
) -> Polygon:
    """Sweep a polygon along a circular arc in the (Z, R) plane by chordal discretization."""
    if poly.is_empty:
        return Polygon()
    c_z, c_r = center
    r = math.hypot(p1[0] - c_z, p1[1] - c_r)
    theta1 = math.atan2(p1[1] - c_r, p1[0] - c_z)
    theta2 = math.atan2(p2[1] - c_r, p2[0] - c_z)

    if is_cw:
        if theta2 >= theta1:
            theta2 -= 2.0 * math.pi
    else:
        if theta2 <= theta1:
            theta2 += 2.0 * math.pi

    delta = theta2 - theta1
    steps = max(3, int(math.ceil(abs(math.degrees(delta)) / max_step_deg)))

    pts = [
        (c_z + r * math.cos(theta1 + (delta * i) / steps), c_r + r * math.sin(theta1 + (delta * i) / steps))
        for i in range(steps + 1)
    ]
    swept_pieces = [
        sweep_linear_polygon(poly, pts[i], pts[i + 1], is_convex=is_convex)
        for i in range(steps)
    ]
    return unary_union(swept_pieces)


def sweep_polygon(poly: Polygon, p1: Tuple[float, float], p2: Tuple[float, float]) -> Polygon:
    """Backward compatibility wrapper for linear polygon sweep."""
    return sweep_linear_polygon(poly, p1, p2, is_convex=False)


class Lathe2DIPWVerifier(BaseVerificationEngine):
    """
    High-fidelity 2D Lathe IPW Verifier.
    All internal calculations use standardized metric units (mm, mm/min, mm³, sec).
    Stock is modeled strictly in the radial half-plane (R >= 0) for optimal GEOS performance.
    """

    def __init__(
        self,
        clearance_tol_mm: float = 0.05,     # Tolerance before flagging stock collision (mm)
        promote_margin_mm: float = 0.15,    # Clearance required from stock to authorize G0 (mm)
        min_promote_len_mm: float = 0.35,   # Minimum travel distance to justify G0 promotion (mm)
        tool_scale_to_mm: float = 1.0,      # Legacy tool polygon scale factor to mm
    ):
        super().__init__()
        self.clearance_tol_mm = clearance_tol_mm
        self.promote_margin_mm = promote_margin_mm
        self.min_promote_len_mm = min_promote_len_mm
        self.tool_scale_to_mm = tool_scale_to_mm

        self.ipw: Polygon = Polygon()
        self.chuck_obstacle: Polygon = Polygon()
        self.active_cutter: Polygon = Polygon()
        self.active_holder: Polygon = Polygon()

        self.cutter_is_convex: bool = False
        self.holder_is_convex: bool = False
        self.holder_has_neg_y: bool = False
        self.is_boring: bool = False

    def reset(self, workpiece: WorkpieceDefinition) -> None:
        """Initialize single-quadrant (R >= 0) IPW stock model and fixtures in metric coordinates."""
        self._reset_base_state(workpiece)

        stock = workpiece.stock
        if not isinstance(stock, AxisymmetricStock2D) and not (hasattr(stock, "z_min") and hasattr(stock, "outer_radius")):
            raise TypeError(f"Lathe2DIPWVerifier requires AxisymmetricStock2D, got {type(stock)}")

        # Build axisymmetric 2D stock in half-plane R >= 0
        z_min, z_max = stock.z_min, stock.z_max
        r_out = stock.outer_radius
        r_in = max(0.0, stock.inner_radius)

        if math.isnan(r_out) or r_out <= 0.0 or math.isnan(z_min) or math.isnan(z_max) or z_min >= z_max:
            raise ValueError(f"Invalid stock dimensions for IPW: z_min={z_min}, z_max={z_max}, r_out={r_out}")

        self.ipw = box(z_min, r_in, z_max, r_out)

        # Chuck keep-out geometry in R >= 0
        chuck_polys = []
        for fixture in workpiece.fixtures:
            if isinstance(fixture, AxisymmetricChuckObstacle):
                min_z, max_z = min(fixture.z_min, fixture.z_max), max(fixture.z_min, fixture.z_max)
                min_r, max_r = min(fixture.clamping_radius, fixture.outer_radius), max(fixture.clamping_radius, fixture.outer_radius)
                if min_z < max_z and min_r < max_r:
                    chuck_polys.append(box(min_z, min_r, max_z, max_r))

        self.chuck_obstacle = unary_union(chuck_polys) if chuck_polys else Polygon()

    def load_tool(self, tool: ToolAssembly) -> None:
        """Mount the active tool, precompute convexity and radial extension properties."""
        self.active_tool = tool
        if isinstance(tool.geometry, LatheToolGeometry):
            cutter = tool.geometry.cutter_polygon
            holder = tool.geometry.holder_polygon
            if abs(self.tool_scale_to_mm - 1.0) > 1e-4:
                cutter = shapely_scale(cutter, xfact=self.tool_scale_to_mm, yfact=self.tool_scale_to_mm, origin=(0, 0))
                holder = shapely_scale(holder, xfact=self.tool_scale_to_mm, yfact=self.tool_scale_to_mm, origin=(0, 0))

            self.active_cutter = cutter
            self.active_holder = holder
            self.cutter_is_convex = not cutter.is_empty and (abs(cutter.area - cutter.convex_hull.area) < 1e-4)
            self.holder_is_convex = not holder.is_empty and (abs(holder.area - holder.convex_hull.area) < 1e-4)
            self.holder_has_neg_y = (holder.bounds[1] < -1e-4) if not holder.is_empty else False
            self.is_boring = getattr(tool.geometry, "is_boring", False)
        else:
            self.active_cutter = Polygon()
            self.active_holder = Polygon()
            self.cutter_is_convex = False
            self.holder_is_convex = False
            self.holder_has_neg_y = False
            self.is_boring = False

    def step(self, move: MotionSegment) -> MoveEvaluation:
        """
        Atomically evaluate a single move in the (Z, R >= 0) plane:
        - Sweeps cutter and holder along linear or circular arc trajectories.
        - Checks fixtures and collision hazards using fast AABB pruning and inverted buffering.
        - Evaluates air move promotion safety.
        - Updates IPW stock (carves swept material in R >= 0).
        - Computes exact 3D MRR via Pappus's theorem.
        """
        self.current_time_sec += move.duration_sec
        self.total_machining_time_sec += move.duration_sec

        # Lathe kinematics: Horizontal = Z, Vertical = Radius (X in APT)
        p1 = (move.start.z, move.start.x)
        p2 = (move.end.z, move.end.x)
        dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        v_key_str = move.vector_key.to_wrapper_str()

        # Handle zero-displacement moves (e.g. dwells)
        if dist < 1e-6:
            return MoveEvaluation(
                segment_id=move.segment_id,
                vector_key=v_key_str,
                is_air_move=False,
                material_removed_mm3=0.0,
                instantaneous_mrr=0.0,
            )

        # ---------------------------------------------------------------------
        # Kinematic Sweeping (Linear or Circular Arc)
        # ---------------------------------------------------------------------
        if move.is_arc and move.arc_center is not None:
            c = (move.arc_center.z, move.arc_center.x)
            is_cw = (move.motion == MotionKind.ARC_CW)
            swept_cutter = sweep_arc_polygon(
                self.active_cutter, p1, p2, c, is_cw, is_convex=self.cutter_is_convex
            )
            swept_holder = sweep_arc_polygon(
                self.active_holder, p1, p2, c, is_cw, is_convex=self.holder_is_convex
            )
        else:
            swept_cutter = sweep_linear_polygon(
                self.active_cutter, p1, p2, is_convex=self.cutter_is_convex
            )
            swept_holder = sweep_linear_polygon(
                self.active_holder, p1, p2, is_convex=self.holder_is_convex
            )

        # For internal boring bars crossing the center axis (Y < 0),
        # reflect negative Y coordinates into R >= 0 to check the opposite bore wall
        if self.is_boring and self.holder_has_neg_y and not swept_holder.is_empty and swept_holder.bounds[1] < -1e-4:
            sh_refl = Polygon([(z, -y) for z, y in swept_holder.exterior.coords])
            swept_holder_radial = unary_union([swept_holder, sh_refl]).intersection(box(-1e6, 0.0, 1e6, 1e6))
        else:
            swept_holder_radial = swept_holder

        move_collisions: List[CollisionRecord] = []
        stock_bounds = self.ipw.bounds if not self.ipw.is_empty else (0.0, 0.0, 0.0, 0.0)

        # ---------------------------------------------------------------------
        # 1. Fixture (Chuck) Collision Check
        # ---------------------------------------------------------------------
        if not self.chuck_obstacle.is_empty:
            tool_union = unary_union([swept_cutter, swept_holder_radial])
            if aabb_overlap(tool_union.bounds, self.chuck_obstacle.bounds):
                chuck_hit = tool_union.intersection(self.chuck_obstacle)
                if not chuck_hit.is_empty and chuck_hit.area > 1e-4:
                    col = CollisionRecord(
                        segment_id=move.segment_id,
                        vector_key=v_key_str,
                        kind=CollisionKind.FIXTURE_COLLISION,
                        timestamp_sec=self.current_time_sec,
                        contact_point=Vector3D(p2[1], 0.0, p2[0]),
                        penetration_volume_mm3=chuck_hit.area * ESTIMATED_PENETRATION_DEPTH_MM,
                        description=f"Tool collided with chuck fixture (overlap area: {chuck_hit.area:.3f} mm²)",
                        op_id=move.op_id,
                    )
                    self.collisions.append(col)
                    move_collisions.append(col)

        # ---------------------------------------------------------------------
        # 2. Rapid Move Verification (G0)
        # ---------------------------------------------------------------------
        if move.motion == MotionKind.RAPID:
            if not self.ipw.is_empty:
                if aabb_overlap(swept_cutter.bounds, stock_bounds):
                    rapid_hit = swept_cutter.intersection(self.ipw)
                    if not rapid_hit.is_empty and rapid_hit.area > 1e-4:
                        col = CollisionRecord(
                            segment_id=move.segment_id,
                            vector_key=v_key_str,
                            kind=CollisionKind.RAPID_INTO_STOCK,
                            timestamp_sec=self.current_time_sec,
                            contact_point=Vector3D(p2[1], 0.0, p2[0]),
                            penetration_volume_mm3=rapid_hit.area * ESTIMATED_PENETRATION_DEPTH_MM,
                            description=f"Rapid move crashed into uncut stock (overlap area: {rapid_hit.area:.3f} mm²)",
                            op_id=move.op_id,
                        )
                        self.collisions.append(col)
                        move_collisions.append(col)

                if aabb_overlap(swept_holder_radial.bounds, stock_bounds):
                    holder_hit = swept_holder_radial.intersection(self.ipw)
                    if not holder_hit.is_empty and holder_hit.area > 1e-4:
                        col = CollisionRecord(
                            segment_id=move.segment_id,
                            vector_key=v_key_str,
                            kind=CollisionKind.HOLDER_STRIKE,
                            timestamp_sec=self.current_time_sec,
                            contact_point=Vector3D(p2[1], 0.0, p2[0]),
                            penetration_volume_mm3=holder_hit.area * ESTIMATED_PENETRATION_DEPTH_MM,
                            description=f"Toolholder rapid strike into stock (overlap area: {holder_hit.area:.3f} mm²)",
                            op_id=move.op_id,
                        )
                        self.collisions.append(col)
                        move_collisions.append(col)

            return MoveEvaluation(
                segment_id=move.segment_id,
                vector_key=v_key_str,
                is_air_move=False,
                material_removed_mm3=0.0,
                instantaneous_mrr=0.0,
                collisions=move_collisions,
            )

        # ---------------------------------------------------------------------
        # 3. Holder Strike on Cutting Moves (G1/G2/G3)
        # ---------------------------------------------------------------------
        if not self.ipw.is_empty and aabb_overlap(swept_holder_radial.bounds, stock_bounds):
            holder_hit = swept_holder_radial.intersection(self.ipw)
            if not holder_hit.is_empty and holder_hit.area > 1e-4:
                outside_wake = holder_hit.difference(swept_cutter.buffer(HOLDER_CUTTER_TRANSITION_TOL_MM))
                if not outside_wake.is_empty and outside_wake.area > 1e-4:
                    col = CollisionRecord(
                        segment_id=move.segment_id,
                        vector_key=v_key_str,
                        kind=CollisionKind.HOLDER_STRIKE,
                        timestamp_sec=self.current_time_sec,
                        contact_point=Vector3D(p2[1], 0.0, p2[0]),
                        penetration_volume_mm3=outside_wake.area * ESTIMATED_PENETRATION_DEPTH_MM,
                        description=f"Toolholder shank gouged raw stock (overlap area: {outside_wake.area:.3f} mm²)",
                        op_id=move.op_id,
                    )
                    self.collisions.append(col)
                    move_collisions.append(col)

        # ---------------------------------------------------------------------
        # 4. Air Move & Rapid Promotion Check (Inverted Buffering)
        # ---------------------------------------------------------------------
        if not move.is_arc and dist >= self.min_promote_len_mm:
            # Inverted buffering: buffer the simple tool instead of buffering the complex IPW
            sc_pad = swept_cutter.buffer(self.promote_margin_mm)
            sh_pad = swept_holder_radial.buffer(self.promote_margin_mm)

            c_hit = sc_pad.intersection(self.ipw) if aabb_overlap(sc_pad.bounds, stock_bounds) else Polygon()
            cutter_air = (not aabb_overlap(sc_pad.bounds, stock_bounds)) or c_hit.is_empty or (c_hit.area < 1e-4)

            h_hit = sh_pad.intersection(self.ipw) if aabb_overlap(sh_pad.bounds, stock_bounds) else Polygon()
            holder_air = (not aabb_overlap(sh_pad.bounds, stock_bounds)) or h_hit.is_empty or (h_hit.area < 1e-4)

            if cutter_air and holder_air:
                self.record_vector_occurrence(v_key_str, is_air_move=True)
                return MoveEvaluation(
                    segment_id=move.segment_id,
                    vector_key=v_key_str,
                    is_air_move=True,
                    material_removed_mm3=0.0,
                    instantaneous_mrr=0.0,
                    collisions=move_collisions,
                )

        # ---------------------------------------------------------------------
        # 5. Cutting Move: Material Removal & Volumetric MRR
        # ---------------------------------------------------------------------
        self.record_vector_occurrence(v_key_str, is_air_move=False)

        vol_3d_mm3 = 0.0
        area_2d = 0.0

        if not self.ipw.is_empty and aabb_overlap(swept_cutter.bounds, stock_bounds):
            removed = self.ipw.intersection(swept_cutter)
            area_2d = removed.area
            if area_2d > 1e-6:
                r_cent = abs(removed.centroid.y)
                vol_3d_mm3 = 2.0 * math.pi * r_cent * area_2d
                self.ipw = self.ipw.difference(swept_cutter)

        sample = self.record_mrr_sample(
            segment_id=move.segment_id,
            duration_sec=move.duration_sec,
            volume_removed_mm3=vol_3d_mm3,
            depth_of_cut_mm=math.sqrt(area_2d),
        )

        return MoveEvaluation(
            segment_id=move.segment_id,
            vector_key=v_key_str,
            is_air_move=False,
            material_removed_mm3=vol_3d_mm3,
            instantaneous_mrr=sample.mrr_rate_mm3_per_min,
            collisions=move_collisions,
        )
