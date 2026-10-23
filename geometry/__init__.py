"""
CAM Geometry & Verification Domain Models and Analytical Generators.

Provides:
- Strongly-typed domain models for Workpieces, Tools, and Kinematic moves.
- Pure-Python analytical generators for standard turning inserts (C, D, V, W, T, Grooving, Drilling).
- Legacy adapters for extracting and validating geometries from Autodesk CAM post outputs.
"""

from .models import (
    MotionKind,
    CollisionKind,
    ToolCompensationQuadrant,
    Vector3D,
    VectorKey,
    AxisymmetricStock2D,
    BoxStock3D,
    AxisymmetricChuckObstacle,
    WorkpieceDefinition,
    LatheToolGeometry,
    MillToolGeometry,
    ToolAssembly,
    MotionSegment,
    CollisionRecord,
    MoveEvaluation,
    MRRSample,
    PromotedVectorRule,
    VerificationReport,
)
from .engine_interface import IVerificationEngine
from .tool_generator import (
    generate_rhombic_insert,
    generate_grooving_insert,
    generate_drill_cutter,
    generate_turning_holder,
)
from .stock_generator import (
    generate_axisymmetric_stock_polygon,
    generate_chuck_obstacle_polygon,
)
from .legacy_adapter import (
    parse_legacy_stock,
    parse_legacy_tool_op,
    parse_svg_polygon,
    transform_tool_svg,
)

__all__ = [
    "MotionKind",
    "CollisionKind",
    "ToolCompensationQuadrant",
    "Vector3D",
    "VectorKey",
    "AxisymmetricStock2D",
    "BoxStock3D",
    "AxisymmetricChuckObstacle",
    "WorkpieceDefinition",
    "LatheToolGeometry",
    "MillToolGeometry",
    "ToolAssembly",
    "MotionSegment",
    "CollisionRecord",
    "MoveEvaluation",
    "MRRSample",
    "PromotedVectorRule",
    "VerificationReport",
    "IVerificationEngine",
    "generate_rhombic_insert",
    "generate_grooving_insert",
    "generate_drill_cutter",
    "generate_turning_holder",
    "generate_axisymmetric_stock_polygon",
    "generate_chuck_obstacle_polygon",
    "parse_legacy_stock",
    "parse_legacy_tool_op",
    "parse_svg_polygon",
    "transform_tool_svg",
]
