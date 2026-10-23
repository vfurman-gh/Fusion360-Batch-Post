"""
Real 2D Lathe In-Process Workpiece (IPW) & Collision Verification Engine.

Implements IVerificationEngine using Shapely 2D polygon clipping:
- 2D (Z, Radius) Axisymmetric stock representation with tube/bore support.
- Swept-polygon kinematics for cutters and toolholders.
- Pappus's centroid theorem for 3D volumetric material removal (mm³).
- Collision detection for chuck fixtures, rapid moves into stock, and holder rubbing.
- Monotonic occurrence-gated rapid move authorization for universal_wrapper.cps.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple
from shapely.affinity import translate, scale as shapely_scale
from shapely.geometry import Polygon, box
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
from .engine_interface import IVerificationEngine


def sweep_polygon(poly: Polygon, p1: Tuple[float, float], p2: Tuple[float, float]) -> Polygon:
    """Compute the 2D area swept by translating poly linearly from p1 to p2 in (Z, R)."""
    if poly.is_empty:
        return Polygon()
    poly1 = translate(poly, xoff=p1[0], yoff=p1[1])
    poly2 = translate(poly, xoff=p2[0], yoff=p2[1])
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    if math.hypot(dx, dy) < 1e-6:
        return poly1

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


class Lathe2DIPWVerifier(IVerificationEngine):
    """
    High-fidelity 2D Lathe IPW Verifier.
    All internal calculations use standardized metric units (mm, mm/min, mm³, sec).
    """

    def __init__(
        self,
        clearance_tol_mm: float = 0.05,     # Tolerance before flagging stock collision (mm)
        promote_margin_mm: float = 0.15,    # Clearance required from stock to authorize G0 (mm)
        min_promote_len_mm: float = 0.35,   # Minimum travel distance to justify G0 promotion (mm)
        tool_scale_to_mm: float = 1.0,      # Set to 25.4 if tool polygons are supplied in inches
    ):
        self.clearance_tol_mm = clearance_tol_mm
        self.promote_margin_mm = promote_margin_mm
        self.min_promote_len_mm = min_promote_len_mm
        self.tool_scale_to_mm = tool_scale_to_mm

        self.ipw: Polygon = Polygon()
        self.upper_half: Polygon = Polygon()
        self.chuck_obstacle: Polygon = Polygon()
        self.active_cutter: Polygon = Polygon()
        self.active_holder: Polygon = Polygon()
        self.active_tool: Optional[ToolAssembly] = None

        self.collisions: List[CollisionRecord] = []
        self.promoted_vectors: Dict[str, PromotedVectorRule] = {}
        self.vector_occurrences: Dict[str, int] = {}
        self.mrr_timeline: List[MRRSample] = []
        self.total_machining_time_sec: float = 0.0
        self.total_volume_removed_mm3: float = 0.0
        self.current_time_sec: float = 0.0

    def reset(self, workpiece: WorkpieceDefinition) -> None:
        """Initialize IPW stock model and fixtures in metric coordinates."""
        stock = workpiece.stock
        if not isinstance(stock, AxisymmetricStock2D):
            raise TypeError(f"Lathe2DIPWVerifier requires AxisymmetricStock2D, got {type(stock)}")

        # Build axisymmetric 2D stock mirrored across R=0
        z_min, z_max = stock.z_min, stock.z_max
        r_out = stock.outer_radius
        r_in = stock.inner_radius

        if r_in > 1e-4:
            upper = box(z_min, r_in, z_max, r_out)
            lower = box(z_min, -r_out, z_max, -r_in)
            self.ipw = unary_union([upper, lower])
        else:
            self.ipw = box(z_min, -r_out, z_max, r_out)

        # Reference clipping plane for upper half (R >= 0)
        self.upper_half = box(z_min - 100.0, 0.0, z_max + 100.0, r_out + 100.0)

        # Chuck keep-out geometry
        chuck_polys = []
        for fixture in workpiece.fixtures:
            if isinstance(fixture, AxisymmetricChuckObstacle):
                c_upper = box(fixture.z_min, fixture.clamping_radius, fixture.z_max, fixture.outer_radius)
                c_lower = box(fixture.z_min, -fixture.outer_radius, fixture.z_max, -fixture.clamping_radius)
                chuck_polys.extend([c_upper, c_lower])

        self.chuck_obstacle = unary_union(chuck_polys) if chuck_polys else Polygon()

        # Reset state accumulators
        self.collisions = []
        self.promoted_vectors = {}
        self.vector_occurrences = {}
        self.mrr_timeline = []
        self.total_machining_time_sec = 0.0
        self.total_volume_removed_mm3 = 0.0
        self.current_time_sec = 0.0

    def load_tool(self, tool: ToolAssembly) -> None:
        """Mount the active tool and scale polygons to metric if needed."""
        self.active_tool = tool
        if isinstance(tool.geometry, LatheToolGeometry):
            cutter = tool.geometry.cutter_polygon
            holder = tool.geometry.holder_polygon
            if abs(self.tool_scale_to_mm - 1.0) > 1e-4:
                cutter = shapely_scale(cutter, xfact=self.tool_scale_to_mm, yfact=self.tool_scale_to_mm, origin=(0, 0))
                holder = shapely_scale(holder, xfact=self.tool_scale_to_mm, yfact=self.tool_scale_to_mm, origin=(0, 0))
            self.active_cutter = cutter
            self.active_holder = holder
        else:
            self.active_cutter = Polygon()
            self.active_holder = Polygon()

    def step(self, move: MotionSegment) -> MoveEvaluation:
        """
        Atomically evaluate a single move in the (Z, R) plane:
        - Checks fixtures and collision hazards.
        - Evaluates air move promotion safety.
        - Updates IPW stock (carves swept material).
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

        swept_cutter = sweep_polygon(self.active_cutter, p1, p2)
        swept_holder = sweep_polygon(self.active_holder, p1, p2)

        move_collisions: List[CollisionRecord] = []

        # ---------------------------------------------------------------------
        # 1. Fixture (Chuck) Collision Check
        # ---------------------------------------------------------------------
        if not self.chuck_obstacle.is_empty:
            chuck_hit = unary_union([swept_cutter, swept_holder]).intersection(self.chuck_obstacle)
            if not chuck_hit.is_empty and chuck_hit.area > 1e-4:
                col = CollisionRecord(
                    segment_id=move.segment_id,
                    vector_key=v_key_str,
                    kind=CollisionKind.FIXTURE_COLLISION,
                    timestamp_sec=self.current_time_sec,
                    contact_point=Vector3D(p2[1], 0.0, p2[0]),
                    penetration_volume_mm3=chuck_hit.area * 10.0,
                    description=f"Tool collided with chuck fixture (overlap area: {chuck_hit.area:.3f} mm²)",
                )
                self.collisions.append(col)
                move_collisions.append(col)

        core_stock = self.ipw.buffer(-self.clearance_tol_mm)

        # ---------------------------------------------------------------------
        # 2. Rapid Move Verification (G0)
        # ---------------------------------------------------------------------
        if move.motion == MotionKind.RAPID:
            rapid_hit = swept_cutter.intersection(core_stock)
            if not rapid_hit.is_empty and rapid_hit.area > 1e-4:
                col = CollisionRecord(
                    segment_id=move.segment_id,
                    vector_key=v_key_str,
                    kind=CollisionKind.RAPID_INTO_STOCK,
                    timestamp_sec=self.current_time_sec,
                    contact_point=Vector3D(p2[1], 0.0, p2[0]),
                    penetration_volume_mm3=rapid_hit.area * 10.0,
                    description=f"Rapid move crashed into uncut stock (overlap area: {rapid_hit.area:.3f} mm²)",
                )
                self.collisions.append(col)
                move_collisions.append(col)

            holder_hit = swept_holder.intersection(core_stock)
            if not holder_hit.is_empty and holder_hit.area > 1e-4:
                col = CollisionRecord(
                    segment_id=move.segment_id,
                    vector_key=v_key_str,
                    kind=CollisionKind.HOLDER_STRIKE,
                    timestamp_sec=self.current_time_sec,
                    contact_point=Vector3D(p2[1], 0.0, p2[0]),
                    penetration_volume_mm3=holder_hit.area * 10.0,
                    description=f"Toolholder rapid strike into stock (overlap area: {holder_hit.area:.3f} mm²)",
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
        holder_hit = swept_holder.intersection(core_stock)
        if not holder_hit.is_empty and holder_hit.area > 1e-4:
            # Filter out trailing wake inside swept cutter area
            outside_wake = holder_hit.difference(swept_cutter.buffer(0.05))
            if not outside_wake.is_empty and outside_wake.area > 1e-4:
                col = CollisionRecord(
                    segment_id=move.segment_id,
                    vector_key=v_key_str,
                    kind=CollisionKind.HOLDER_STRIKE,
                    timestamp_sec=self.current_time_sec,
                    contact_point=Vector3D(p2[1], 0.0, p2[0]),
                    penetration_volume_mm3=outside_wake.area * 10.0,
                    description=f"Toolholder shank gouged raw stock (overlap area: {outside_wake.area:.3f} mm²)",
                )
                self.collisions.append(col)
                move_collisions.append(col)

        # ---------------------------------------------------------------------
        # 4. Air Move & Rapid Promotion Check
        # ---------------------------------------------------------------------
        seen = self.vector_occurrences.get(v_key_str, 0) + 1
        self.vector_occurrences[v_key_str] = seen

        if not move.is_arc and dist >= self.min_promote_len_mm:
            padded_ipw = self.ipw.buffer(self.promote_margin_mm)
            if (
                swept_cutter.intersection(padded_ipw).is_empty
                and swept_holder.intersection(padded_ipw).is_empty
            ):
                # Verified air move!
                if v_key_str not in self.promoted_vectors:
                    self.promoted_vectors[v_key_str] = PromotedVectorRule(min_occur=seen)

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
        # If this vector was previously thought to be air, pushing min_occur to next pass
        if v_key_str in self.promoted_vectors:
            self.promoted_vectors[v_key_str] = PromotedVectorRule(min_occur=seen + 1)

        mirrored = Polygon([(z, -r) for z, r in swept_cutter.exterior.coords])
        combined_cut = unary_union([swept_cutter, mirrored])

        removed = self.ipw.intersection(combined_cut)
        removed_upper = removed.intersection(self.upper_half)
        area_2d = removed_upper.area

        if area_2d > 1e-6:
            r_cent = max(abs(removed_upper.centroid.y), 0.01)
            vol_3d_mm3 = 2.0 * math.pi * r_cent * area_2d
        else:
            vol_3d_mm3 = 0.0

        # Carve IPW
        self.ipw = self.ipw.difference(combined_cut)
        self.total_volume_removed_mm3 += vol_3d_mm3

        mrr_rate = (vol_3d_mm3 / max(1e-4, move.duration_sec)) * 60.0

        sample = MRRSample(
            timestamp_sec=self.current_time_sec,
            duration_sec=move.duration_sec,
            segment_id=move.segment_id,
            tool_id=self.active_tool.tool_id if self.active_tool else 0,
            volume_removed_mm3=vol_3d_mm3,
            mrr_rate_mm3_per_min=mrr_rate,
            depth_of_cut_mm=math.sqrt(area_2d),
        )
        self.mrr_timeline.append(sample)

        return MoveEvaluation(
            segment_id=move.segment_id,
            vector_key=v_key_str,
            is_air_move=False,
            material_removed_mm3=vol_3d_mm3,
            instantaneous_mrr=mrr_rate,
            collisions=move_collisions,
        )

    def generate_report(self) -> VerificationReport:
        """Compile final report with collision records, promoted vectors, and MRR timeline."""
        # Find statistical MRR spikes (> 2.5x mean non-zero MRR)
        non_zero_mrrs = [s.mrr_rate_mm3_per_min for s in self.mrr_timeline if s.mrr_rate_mm3_per_min > 1.0]
        spikes = []
        if non_zero_mrrs:
            mean_mrr = sum(non_zero_mrrs) / len(non_zero_mrrs)
            threshold = mean_mrr * 2.5
            spikes = [s for s in self.mrr_timeline if s.mrr_rate_mm3_per_min > threshold]

        return VerificationReport(
            total_machining_time_sec=self.total_machining_time_sec,
            total_volume_removed_mm3=self.total_volume_removed_mm3,
            collisions=self.collisions,
            promoted_vectors=self.promoted_vectors,
            mrr_timeline=self.mrr_timeline,
            mrr_spikes=spikes,
        )
