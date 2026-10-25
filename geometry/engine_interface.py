"""
Verification Engine Interface Contract and Base Implementation.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Sequence

from .models import (
    CollisionRecord,
    MRRSample,
    MotionSegment,
    MoveEvaluation,
    PromotedVectorRule,
    ToolAssembly,
    VerificationReport,
    WorkpieceDefinition,
)


class IVerificationEngine(ABC):
    """Abstract contract for collision verification, IPW tracking, and rapid promotion."""

    @abstractmethod
    def reset(self, workpiece: WorkpieceDefinition) -> None:
        """Initialize or reset the In-Process Workpiece (IPW) and obstacle state."""
        pass

    @abstractmethod
    def load_tool(self, tool: ToolAssembly) -> None:
        """Mount the active tool and holder assembly."""
        pass

    @abstractmethod
    def step(self, move: MotionSegment) -> MoveEvaluation:
        """
        Atomically evaluate the move against the current IPW and fixtures,
        mutate the IPW (carve swept volume), advance the clock, and return metrics.
        """
        pass

    @abstractmethod
    def generate_report(self) -> VerificationReport:
        """Compile collisions, MRR timeline, statistical spikes, and promotion list."""
        pass

    def verify_toolpath(
        self,
        moves: Optional[Sequence[MotionSegment]] = None,
        tools_by_op: Optional[Dict[str, ToolAssembly]] = None,
        workpiece: Optional[WorkpieceDefinition] = None,
        segments: Optional[Sequence[MotionSegment]] = None,
    ) -> VerificationReport:
        """Convenience orchestration loop executing a complete toolpath evaluation."""
        moves_seq = moves if moves is not None else (segments or [])
        if workpiece is not None:
            self.reset(workpiece)
        current_op = None

        for move in moves_seq:
            if move.op_id != current_op:
                current_op = move.op_id
                if tools_by_op and current_op in tools_by_op:
                    self.load_tool(tools_by_op[current_op])

            self.step(move)

        return self.generate_report()


class BaseVerificationEngine(IVerificationEngine):
    """
    Base implementation providing shared state tracking, timing,
    vector occurrence gating, MRR spike detection, and report compilation.
    """

    def __init__(self) -> None:
        self.workpiece: Optional[WorkpieceDefinition] = None
        self.active_tool: Optional[ToolAssembly] = None
        self.current_time_sec: float = 0.0
        self.total_machining_time_sec: float = 0.0
        self.total_volume_removed_mm3: float = 0.0
        self.collisions: List[CollisionRecord] = []
        self.mrr_timeline: List[MRRSample] = []
        self.vector_occurrences: Dict[str, int] = {}
        self.vector_last_cut_occurrence: Dict[str, int] = {}
        self.vector_has_air_occurrence: Dict[str, bool] = {}
        self.promoted_vectors: Dict[str, PromotedVectorRule] = {}

    def _reset_base_state(self, workpiece: WorkpieceDefinition) -> None:
        """Reset the shared timing, collision, MRR, and vector occurrence collections."""
        self.workpiece = workpiece
        self.active_tool = None
        self.current_time_sec = 0.0
        self.total_machining_time_sec = 0.0
        self.total_volume_removed_mm3 = 0.0
        self.collisions.clear()
        self.mrr_timeline.clear()
        self.vector_occurrences.clear()
        self.vector_last_cut_occurrence.clear()
        self.vector_has_air_occurrence.clear()
        self.promoted_vectors.clear()

    def record_vector_occurrence(self, v_key_str: str, is_air_move: bool) -> int:
        """
        Record occurrence of a vector key and update tentative promotion rules.
        Returns the 1-based occurrence index for this move.
        """
        seen = self.vector_occurrences.get(v_key_str, 0) + 1
        self.vector_occurrences[v_key_str] = seen

        if is_air_move:
            self.vector_has_air_occurrence[v_key_str] = True
            if v_key_str not in self.promoted_vectors:
                self.promoted_vectors[v_key_str] = PromotedVectorRule(min_occur=seen)
        else:
            self.vector_last_cut_occurrence[v_key_str] = seen
            if v_key_str in self.promoted_vectors:
                self.promoted_vectors[v_key_str] = PromotedVectorRule(min_occur=seen + 1)

        return seen

    def record_mrr_sample(
        self,
        segment_id: int,
        duration_sec: float,
        volume_removed_mm3: float,
        depth_of_cut_mm: float = 0.0,
    ) -> MRRSample:
        """Record volumetric removal rate sample in timeline."""
        dur = max(1e-4, float(duration_sec))
        mrr_rate = (volume_removed_mm3 / dur) * 60.0
        sample = MRRSample(
            timestamp_sec=self.current_time_sec,
            duration_sec=dur,
            segment_id=segment_id,
            tool_id=self.active_tool.tool_id if self.active_tool else 0,
            volume_removed_mm3=volume_removed_mm3,
            mrr_rate_mm3_per_min=mrr_rate,
            depth_of_cut_mm=depth_of_cut_mm,
        )
        self.mrr_timeline.append(sample)
        self.total_volume_removed_mm3 += volume_removed_mm3
        return sample

    def generate_report(self) -> VerificationReport:
        """
        Compile VerificationReport with strictly validated monotonic promotion rules,
        collating collisions, total volume removed, and statistical MRR spikes.
        """
        final_promoted: Dict[str, PromotedVectorRule] = {}
        for vkey, total_occ in self.vector_occurrences.items():
            if not self.vector_has_air_occurrence.get(vkey, False):
                continue
            last_cut = self.vector_last_cut_occurrence.get(vkey, 0)
            if last_cut < total_occ:
                final_promoted[vkey] = PromotedVectorRule(min_occur=last_cut + 1)

        non_zero_mrrs = [s.mrr_rate_mm3_per_min for s in self.mrr_timeline if s.mrr_rate_mm3_per_min > 1.0]
        spikes: List[MRRSample] = []
        if non_zero_mrrs:
            mean_mrr = sum(non_zero_mrrs) / len(non_zero_mrrs)
            threshold = mean_mrr * 2.5
            spikes = [s for s in self.mrr_timeline if s.mrr_rate_mm3_per_min > threshold]

        return VerificationReport(
            total_machining_time_sec=self.total_machining_time_sec,
            total_volume_removed_mm3=self.total_volume_removed_mm3,
            collisions=list(self.collisions),
            promoted_vectors=final_promoted,
            mrr_timeline=list(self.mrr_timeline),
            mrr_spikes=spikes,
        )
