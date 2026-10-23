"""
Verification Engine Interface Contract.
"""

from abc import ABC, abstractmethod
from typing import Dict, Sequence

from .models import (
    MotionSegment,
    MoveEvaluation,
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
        moves: Sequence[MotionSegment],
        tools_by_op: Dict[str, ToolAssembly],
        workpiece: WorkpieceDefinition,
    ) -> VerificationReport:
        """Convenience orchestration loop executing a complete toolpath evaluation."""
        self.reset(workpiece)
        current_op = None

        for move in moves:
            if move.op_id != current_op:
                current_op = move.op_id
                if current_op in tools_by_op:
                    self.load_tool(tools_by_op[current_op])

            self.step(move)

        return self.generate_report()
