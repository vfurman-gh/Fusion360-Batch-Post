"""
Simple Milling Verification Engine (`MillClearanceVerifier`).

Implements `IVerificationEngine` for Milling / Router setups in the unified
2-pass Context-Dump & Single-Pass Replay pipeline. Conservatively authorizes
rapid (`G0`) promotion strictly for pure `Z+` upward movements (`dx == 0, dy == 0, dz > 0`)
that retract to or above the operation's clearance height (`zClearance`).
"""

import math
from typing import Dict, List, Optional, Sequence

from .engine_interface import IVerificationEngine
from .models import (
    CollisionRecord,
    MotionKind,
    MotionSegment,
    MoveEvaluation,
    MRRSample,
    PromotedVectorRule,
    ToolAssembly,
    VerificationReport,
    WorkpieceDefinition,
)


class MillClearanceVerifier(IVerificationEngine):
    """
    Conservative Milling verifier that only promotes pure `Z+` vertical retract
    moves (`dx == 0, dy == 0, end.z > start.z`) reaching at or above the
    operation's clearance height (`zClearance`).
    """

    def __init__(
        self,
        xy_tol_mm: float = 1e-3,
        z_tol_mm: float = 1e-3,
    ) -> None:
        self.xy_tol_mm = float(xy_tol_mm)
        self.z_tol_mm = float(z_tol_mm)
        self.workpiece: Optional[WorkpieceDefinition] = None
        self._sec_clearance_mm: Dict[int, float] = {}

    def reset_workpiece(self, workpiece: WorkpieceDefinition) -> None:
        """Initialize or reset the workpiece state and per-section clearance cache."""
        self.workpiece = workpiece
        self._sec_clearance_mm.clear()

    def _resolve_clearance_z_mm(self, segment: MotionSegment) -> Optional[float]:
        """Resolve the target clearance Z height (in mm) for `segment.sec_id`."""
        meta = segment.metadata or {}
        z_clear = meta.get("z_clearance_mm")
        if z_clear is not None:
            return float(z_clear)

        z_retract = meta.get("z_retract_mm")
        if z_retract is not None:
            return float(z_retract)

        if segment.sec_id in self._sec_clearance_mm:
            return self._sec_clearance_mm[segment.sec_id]

        if self.workpiece is not None and getattr(self.workpiece, "stock", None) is not None:
            stock = self.workpiece.stock
            if hasattr(stock, "z_max"):
                return float(stock.z_max)

        return None

    def step_segment(
        self,
        segment: MotionSegment,
        tool: Optional[ToolAssembly] = None,
    ) -> MoveEvaluation:
        """
        Evaluate a single kinematic move.
        Only pure `Z+` linear moves outside canned cycles that reach `>= clearance_z`
        are marked as `is_air_move = True` for `G0` promotion.
        """
        vkey_str = segment.vector_key.to_wrapper_str()

        if segment.motion == MotionKind.RAPID:
            top_z = max(segment.start.z, segment.end.z)
            prev_z = self._sec_clearance_mm.get(segment.sec_id)
            if prev_z is None or top_z > prev_z:
                self._sec_clearance_mm[segment.sec_id] = top_z
            return MoveEvaluation(
                segment_id=segment.segment_id,
                vector_key=vkey_str,
                is_air_move=True,
                material_removed_mm3=0.0,
                instantaneous_mrr=0.0,
                collisions=[],
            )

        meta = segment.metadata or {}
        in_cycle = bool(meta.get("in_cycle", False))

        is_air_move = False
        if segment.motion == MotionKind.LINEAR and not in_cycle:
            dx = segment.end.x - segment.start.x
            dy = segment.end.y - segment.start.y
            dz = segment.end.z - segment.start.z
            is_pure_z_plus = (math.hypot(dx, dy) <= self.xy_tol_mm) and (dz > self.z_tol_mm)
            if is_pure_z_plus:
                clearance_z = self._resolve_clearance_z_mm(segment)
                if clearance_z is not None and segment.end.z >= (clearance_z - self.z_tol_mm):
                    is_air_move = True

        return MoveEvaluation(
            segment_id=segment.segment_id,
            vector_key=vkey_str,
            is_air_move=is_air_move,
            material_removed_mm3=0.0,
            instantaneous_mrr=0.0,
            collisions=[],
        )

    def verify_toolpath(
        self,
        segments: Sequence[MotionSegment],
        tools_by_op: Dict[str, ToolAssembly],
        workpiece: WorkpieceDefinition,
    ) -> VerificationReport:
        """
        Run the complete Milling verification pass across all segments and return
        a `VerificationReport` with `promoted_vectors` for `Z+` clearance retracts.
        """
        self.reset_workpiece(workpiece)

        total_time_sec = 0.0
        all_collisions: List[CollisionRecord] = []
        mrr_timeline: List[MRRSample] = []
        mrr_spikes: List[MRRSample] = []

        vector_total_occurrences: Dict[str, int] = {}
        vector_last_non_air_occurrence: Dict[str, int] = {}
        vector_has_air_occurrence: Dict[str, bool] = {}

        for seg in segments:
            tool = tools_by_op.get(seg.op_id) if tools_by_op else None
            eval_res = self.step_segment(seg, tool)
            total_time_sec += max(0.0, float(seg.duration_sec))

            if eval_res.collisions:
                all_collisions.extend(eval_res.collisions)

            meta = seg.metadata or {}
            if seg.motion == MotionKind.LINEAR and not bool(meta.get("in_cycle", False)):
                vkey = eval_res.vector_key
                occ = vector_total_occurrences.get(vkey, 0) + 1
                vector_total_occurrences[vkey] = occ
                if eval_res.is_air_move:
                    vector_has_air_occurrence[vkey] = True
                else:
                    vector_last_non_air_occurrence[vkey] = occ

        promoted_vectors: Dict[str, PromotedVectorRule] = {}
        for vkey, total_occ in vector_total_occurrences.items():
            if not vector_has_air_occurrence.get(vkey, False):
                continue
            last_non_air = vector_last_non_air_occurrence.get(vkey, 0)
            if last_non_air < total_occ:
                promoted_vectors[vkey] = PromotedVectorRule(min_occur=last_non_air + 1)

        return VerificationReport(
            total_machining_time_sec=total_time_sec,
            total_volume_removed_mm3=0.0,
            collisions=all_collisions,
            promoted_vectors=promoted_vectors,
            mrr_timeline=mrr_timeline,
            mrr_spikes=mrr_spikes,
        )
