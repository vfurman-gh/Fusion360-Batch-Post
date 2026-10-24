"""
Core Domain Models for Workpiece, Tool, and Kinematic Verification.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

try:
    from shapely.geometry import Polygon
except (ImportError, OSError):
    Polygon = Any  # Type alias fallback for Fusion 360 embedded Python



class MotionKind(Enum):
    RAPID = auto()          # G0
    LINEAR = auto()         # G1
    ARC_CW = auto()         # G2
    ARC_CCW = auto()        # G3
    PROBE = auto()          # G38.x
    DWELL = auto()          # G4


class CollisionKind(Enum):
    RAPID_INTO_STOCK = auto()
    SHANK_RUB = auto()
    HOLDER_STRIKE = auto()
    FIXTURE_COLLISION = auto()
    EXCESSIVE_ENGAGEMENT = auto()


class ToolCompensationQuadrant(Enum):
    """
    Lathe Tool Nose Radius Compensation (TNRC) Quadrants (P1 - P9).
    """
    P1 = 1  # X+ Z+ (Boring front)
    P2 = 2  # X+ Z- (Boring back)
    P3 = 3  # X- Z- (OD turning back - standard right-hand OD tool)
    P4 = 4  # X- Z+ (OD turning front)
    P5 = 5  # Top edge
    P6 = 6  # Right edge
    P7 = 7  # Bottom edge
    P8 = 8  # Left edge
    P9 = 9  # Center of tool nose arc (no directional bias)


@dataclass(frozen=True)
class Vector3D:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class VectorKey:
    """
    Spatial vector address matching universal_wrapper.cps format.
    Normalized to fixed integer precision (0.0001 resolution).
    """
    sec_id: int
    start_x: int
    start_y: int
    start_z: int
    end_x: int
    end_y: int
    end_z: int

    @classmethod
    def from_floats(cls, sec_id: int, start: Vector3D, end: Vector3D) -> "VectorKey":
        return cls(
            sec_id=sec_id,
            start_x=round(start.x * 10000),
            start_y=round(start.y * 10000),
            start_z=round(start.z * 10000),
            end_x=round(end.x * 10000),
            end_y=round(end.y * 10000),
            end_z=round(end.z * 10000),
        )

    def to_wrapper_str(self) -> str:
        return f"{self.sec_id}:{self.start_x},{self.start_y},{self.start_z}->{self.end_x},{self.end_y},{self.end_z}"


@dataclass(frozen=True)
class AxisymmetricStock2D:
    """Lathe stock profile revolved symmetrically around the Z-axis (R >= 0)."""
    z_min: float
    z_max: float
    outer_radius: float
    inner_radius: float = 0.0


@dataclass(frozen=True)
class BoxStock3D:
    """Prismatic bounding box for milling stock."""
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float


@dataclass(frozen=True)
class AxisymmetricChuckObstacle:
    """Lathe chuck body and jaws modeled as an axisymmetric swept keep-out body."""
    z_min: float
    z_max: float
    outer_radius: float
    clamping_radius: float


WorkpieceGeometry = Union[AxisymmetricStock2D, BoxStock3D]


@dataclass
class WorkpieceDefinition:
    stock: WorkpieceGeometry
    fixtures: List[AxisymmetricChuckObstacle] = field(default_factory=list)
    work_offset: Vector3D = field(default_factory=lambda: Vector3D(0, 0, 0))


@dataclass(frozen=True)
class LatheToolGeometry:
    """2D insert profile and holder geometry in the (Z, Radius) plane."""
    insert_shape: str                               # e.g., 'C', 'D', 'V', 'W', 'T', 'Groove', 'Drill'
    nose_radius: float
    hand: str                                       # 'R' or 'L'
    quadrant: ToolCompensationQuadrant
    cutter_polygon: Polygon                         # Valid Shapely Polygon at tip (0, 0)
    holder_polygon: Polygon                         # Valid Shapely Polygon for shank keep-out
    is_boring: bool = False


@dataclass(frozen=True)
class MillToolGeometry:
    """3D parametric milling tool and holder collar."""
    diameter: float
    corner_radius: float
    flute_length: float
    overall_length: float
    taper_angle_deg: float = 0.0
    holder_diameter: float = 40.0
    holder_length: float = 60.0


ToolGeometry = Union[LatheToolGeometry, MillToolGeometry]


@dataclass
class ToolAssembly:
    tool_id: int
    name: str
    geometry: ToolGeometry


@dataclass
class MotionSegment:
    """A continuous kinematic move in workpiece coordinates."""
    segment_id: int               # Monotonic sequential identifier within operation
    op_id: str                    # Owning CAM operation identifier
    sec_id: int                   # Section index in post processor
    motion: MotionKind
    start: Vector3D
    end: Vector3D
    feedrate_mmpm: float          # Standardized metric feedrate (mm/min)
    spindle_rpm: float
    duration_sec: float           # Explicit duration (handles G4 dwells where distance = 0)
    vector_key: VectorKey         # Deterministic spatial key for universal_wrapper
    is_arc: bool = False
    arc_center: Optional[Vector3D] = None
    arc_plane: str = "XY"         # 'XY' (G17), 'ZX' (G18), 'YZ' (G19)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CollisionRecord:
    segment_id: int
    vector_key: str
    kind: CollisionKind
    timestamp_sec: float
    contact_point: Vector3D
    penetration_volume_mm3: float
    description: str


@dataclass
class MoveEvaluation:
    """Evaluation output for an atomic step."""
    segment_id: int
    vector_key: str
    is_air_move: bool             # True if move removed 0 stock and cleared obstacles by >= margin
    material_removed_mm3: float   # Volume removed during this move
    instantaneous_mrr: float      # mm^3/min
    collisions: List[CollisionRecord] = field(default_factory=list)


@dataclass
class MRRSample:
    timestamp_sec: float
    duration_sec: float
    segment_id: int
    tool_id: int
    volume_removed_mm3: float
    mrr_rate_mm3_per_min: float
    depth_of_cut_mm: float


@dataclass
class PromotedVectorRule:
    """Occurrence-gated promotion rule ingested by universal_wrapper.cps."""
    min_occur: int = 1            # Threshold occurrence index (>= min_occur is authorized air)


@dataclass
class VerificationReport:
    total_machining_time_sec: float
    total_volume_removed_mm3: float
    collisions: List[CollisionRecord]
    promoted_vectors: Dict[str, PromotedVectorRule] # Map of vector keys to occurrence rules
    mrr_timeline: List[MRRSample]
    mrr_spikes: List[MRRSample]
