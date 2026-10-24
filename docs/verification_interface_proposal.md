# [OBSOLETE] Generic Collision, IPW & Rapid-Promotion Engine: Interface Specification (v2)

> [!WARNING]
> **OBSOLETE DOCUMENT**: The 4-pass APT extraction + per-batch `universal_wrapper.cps` + G-code stitching pipeline in this document has been superseded by the **Context-Dump + Unified Verification + Single-Pass Replay** architecture (`universal_dumper.cps` + `context_parser.py` + `universal_replayer.cps`).
> - See [`docs/architecture_and_interfaces.md`](file:///Users/vadimf/Documents/Fusion360-Batch-Post-master/docs/architecture_and_interfaces.md) for the current system architecture and tool interfaces.
> - See [`docs/universal_replayer.md`](file:///Users/vadimf/Documents/Fusion360-Batch-Post-master/docs/universal_replayer.md) for the internal design and corner cases of `universal_replayer.cps`.

## 1. Architecture Overview (Historical)

The verification system strictly isolates intermediate toolpath interpretation from physical simulation and post-processing:

```
┌────────────────────────────────┐
│  Fusion 360 Intermediate CAM   │  Compact-NCI (.cnc) / apt.cps (.apt)
└───────────────┬────────────────┘
                │ Kinematic events & section headers
                ▼
┌────────────────────────────────┐
│      Toolpath Normalizer       │  Translates APT / CAM records into canonical metric moves
└───────────────┬────────────────┘
                │ Sequence[MotionSegment], ToolAssembly, WorkpieceDefinition
                ▼
┌────────────────────────────────┐
│      IVerificationEngine       │  Generic Abstract Simulation Contract:
│  ┌──────────────────────────┐  │  • Axisymmetric 2D & 3D Heightfield / Mesh IPW
│  │    Lathe2DIPWVerifier    │  │  • Collision detection (stock, shank, holder, chuck)
│  ├──────────────────────────┤  │  • Volumetric Material Removal Rate (MRR) tracking
│  │ Mill3DHeightfieldVerifier│  │  • Monotonic air-move & occurrence safety proof
│  └──────────────────────────┘  │
└───────────────┬────────────────┘
                │ VerificationReport (contains promoted_vectors map)
                ▼
┌────────────────────────────────┐
│     Downstream Consumers       │  • universal_wrapper.cps (BASE_POST + PROMOTED_MOVES_JSON)
│                                │  • Matplotlib IPW & MRR timeline visualizers
│                                │  • Fusion 360 add-in verification UI
└────────────────────────────────┘
```

The verification engine has **zero knowledge of G-code syntax, regexes, machine dialects, or formatting strings**. All calculations are performed in normalized metric units ($mm, mm/min, mm^3$).

---

## 2. Core Domain Data Models

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Sequence, Tuple, Union


# ==============================================================================
# Enumerations
# ==============================================================================

class MotionKind(Enum):
    RAPID = auto()          # G0
    LINEAR = auto()         # G1
    ARC_CW = auto()         # G2
    ARC_CCW = auto()        # G3
    PROBE = auto()          # G38.x
    DWELL = auto()          # G4


class CollisionKind(Enum):
    RAPID_INTO_STOCK = auto()      # Rapid motion penetrating uncut workpiece
    SHANK_RUB = auto()             # Non-fluted shank rubbing raw stock (depth > flute_length)
    HOLDER_STRIKE = auto()         # Toolholder colliding with stock or finished part
    FIXTURE_COLLISION = auto()     # Interference with chuck, vise, table, or clamps
    EXCESSIVE_ENGAGEMENT = auto()  # MRR spike exceeding allowable tool envelope


class ToolCompensationQuadrant(Enum):
    """
    Lathe Tool Nose Radius Compensation (TNRC) Quadrants (P1 - P9).
    Defines the spatial vector from the circular insert center to the theoretical tool tip.
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


# ==============================================================================
# Kinematic Motion Primitives
# ==============================================================================

@dataclass(frozen=True)
class Vector3D:
    x: float  # mm
    y: float  # mm
    z: float  # mm


@dataclass(frozen=True)
class VectorKey:
    """
    Spatial vector address matching universal_wrapper.cps format.
    Normalized to fixed integer precision (0.0001 mm resolution).
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
        """Emits wrapper key format: 'secId:x1,y1,z1->x2,y2,z2'."""
        return f"{self.sec_id}:{self.start_x},{self.start_y},{self.start_z}->{self.end_x},{self.end_y},{self.end_z}"


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


# ==============================================================================
# Tool Assembly Definitions
# ==============================================================================

@dataclass(frozen=True)
class LatheToolGeometry:
    """2D insert profile and holder geometry in the (Z, Radius) plane."""
    insert_shape: str                               # e.g., 'CNMG', 'WNMG', 'DNMG'
    nose_radius: float                              # mm (e.g. 0.4 mm or 0.8 mm)
    hand: str                                       # 'R' or 'L'
    quadrant: ToolCompensationQuadrant              # TNRC tracking direction
    insert_polygon_coords: Sequence[Tuple[float, float]] # (Z, R) relative to theoretical tip
    holder_polygon_coords: Sequence[Tuple[float, float]] # Keep-out shank boundary


@dataclass(frozen=True)
class MillToolGeometry:
    """3D parametric milling tool and holder collar."""
    diameter: float                                 # mm
    corner_radius: float                            # mm (0 = flat, dia/2 = ball, >0 = bullnose)
    flute_length: float                             # mm (cutting portion)
    overall_length: float                           # mm (tool stickout from holder face)
    taper_angle_deg: float = 0.0                    # Chamfer or tapered cutter
    holder_diameter: float = 40.0                   # mm (keep-out collar diameter)
    holder_length: float = 60.0                     # mm (keep-out collar length)


ToolGeometry = Union[LatheToolGeometry, MillToolGeometry]


@dataclass
class ToolAssembly:
    tool_id: int
    name: str
    geometry: ToolGeometry


# ==============================================================================
# Workpiece & Fixture Definitions
# ==============================================================================

@dataclass(frozen=True)
class AxisymmetricStock2D:
    """Lathe stock profile revolved symmetrically around the Z-axis (R >= 0)."""
    z_min: float                                    # mm
    z_max: float                                    # mm
    outer_radius: float                             # mm
    inner_radius: float = 0.0                       # mm (> 0 for hollow tube / pipe)


@dataclass(frozen=True)
class BoxStock3D:
    """Simple prismatic bounding box for milling stock."""
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float


@dataclass(frozen=True)
class ArbitraryMeshStock3D:
    """
    Arbitrary 3D stock geometry (in-process cast stock, complex pre-forms,
    or result of previous roughing setups).
    Represented as triangular boundary mesh vertices and facets.
    """
    vertices: Sequence[Vector3D]                    # 3D points
    triangles: Sequence[Tuple[int, int, int]]       # Vertex index triplets


@dataclass(frozen=True)
class AxisymmetricChuckObstacle:
    """
    Lathe chuck body and jaws. Because turning fixtures rotate symmetrically
    around the spindle axis, they are modeled as an axisymmetric swept keep-out body.
    """
    z_min: float                                    # mm
    z_max: float                                    # mm
    outer_radius: float                             # mm
    clamping_radius: float                          # mm


@dataclass(frozen=True)
class StaticFixtureMesh3D:
    """Static milling fixtures (vises, clamps, tombstones, tables)."""
    name: str
    bounding_box: BoxStock3D
    mesh_path: Optional[str] = None


WorkpieceGeometry = Union[AxisymmetricStock2D, BoxStock3D, ArbitraryMeshStock3D]
FixtureObstacle = Union[AxisymmetricChuckObstacle, StaticFixtureMesh3D]


@dataclass
class WorkpieceDefinition:
    stock: WorkpieceGeometry
    fixtures: List[FixtureObstacle]
    work_offset: Vector3D = field(default_factory=lambda: Vector3D(0, 0, 0))


# ==============================================================================
# Verification Output Models
# ==============================================================================

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
```

---

## 3. The Verifier Interface (`IVerificationEngine`)

The verification engine interface adopts a streamlined **atomic step pattern**, eliminating the artificial and hazardous two-phase `commit_move()` ceremony.

```python
class IVerificationEngine(ABC):
    """Abstract contract for collision verification, IPW tracking, and rapid promotion."""

    @abstractmethod
    def reset(self, workpiece: WorkpieceDefinition) -> None:
        """
        Initialize the In-Process Workpiece (IPW) and mount obstacle boundaries.
        Resets internal machining timers and MRR accumulators.
        """
        pass

    @abstractmethod
    def load_tool(self, tool: ToolAssembly) -> None:
        """Mount the active tool, fluted geometry, and keep-out holder assembly."""
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
        """
        Compile collision logs, MRR time-series, statistical spikes,
        and the promoted_vectors dictionary formatted for universal_wrapper.cps.
        """
        pass

    def verify_toolpath(
        self,
        moves: Sequence[MotionSegment],
        tools_by_op: Dict[str, ToolAssembly],
        workpiece: WorkpieceDefinition,
    ) -> VerificationReport:
        """
        Convenience orchestration loop executing a complete toolpath.
        """
        self.reset(workpiece)
        current_op = None

        for move in moves:
            if move.op_id != current_op:
                current_op = move.op_id
                self.load_tool(tools_by_op[current_op])

            self.step(move)

        return self.generate_report()
```

---

## 4. Architectural Rationales

### 1. Atomic `step()` vs. Two-Phase `commit_move()`
* **Elimination of Temporal Coupling**: Subtractive CAM simulation is strictly deterministic and linear. Splitting evaluation from commitment created state-desynchronization hazards and duplicate geometric boolean operations.
* **Performance**: Intersecting a cutter with stock and subtracting it are physically unified in 2D Shapely clipping and 2.5D/3D heightfield matrix updates. Atomic execution cuts simulation wall-clock time in half.

### 2. Spatial Vector Keys vs. Line Counters
* Integer line counters inevitably drift when machine posts expand canned cycles (`expandCyclePoint`), linearize arcs, or insert rotary unwinds.
* Operating on `VectorKey` (`sec_id:start->end`) guarantees absolute spatial idempotency across Python simulation and JavaScript post-processing.

### 3. Occurrence Gating for Monotonic Stock Removal
* Due to subtractive monotonicity, a fixed trajectory can only transition from cutting to air ($0 \to 1$), never the reverse.
* The verifier tracks occurrences for repeated spatial vectors (e.g. spring passes, roughing retracts) and outputs `min_occur: K`, completely eliminating temporal aliasing bugs.

### 4. Realistic Machine & Tool Physics
* **Lathe TNRC Direction**: Incorporating `ToolCompensationQuadrant` ($P1 \dots P9$) accounts for the offset between the theoretical tool tip and the circular corner radius tangent, eliminating geometric simulation errors up to $1.13\text{ mm}$.
* **Mill Flute Length**: Explicitly separating `flute_length` from `overall_length` enables the engine to distinguish valid cutting from non-fluted shank rubbing collisions.
* **Axisymmetric Fixtures**: Modeling lathe chucks as axisymmetric solids of revolution naturally absorbs spindle rotation without requiring an artificial `is_rotating` flag.
