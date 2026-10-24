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

try:
    from .lathe_engine import Lathe2DIPWVerifier
except (ImportError, OSError):
    Lathe2DIPWVerifier = None

try:
    from .mill_engine import MillClearanceVerifier
except (ImportError, OSError):
    MillClearanceVerifier = None

try:
    from .tool_generator import (
        generate_rhombic_insert,
        generate_grooving_insert,
        generate_drill_cutter,
        generate_turning_holder,
    )
except (ImportError, OSError):
    generate_rhombic_insert = None
    generate_grooving_insert = None
    generate_drill_cutter = None
    generate_turning_holder = None

try:
    from .stock_generator import (
        generate_axisymmetric_stock_polygon,
        generate_chuck_obstacle_polygon,
    )
except (ImportError, OSError):
    generate_axisymmetric_stock_polygon = None
    generate_chuck_obstacle_polygon = None

try:
    from .legacy_adapter import (
        parse_legacy_stock,
        parse_legacy_tool_op,
        parse_svg_polygon,
        transform_tool_svg,
    )
except (ImportError, OSError):
    parse_legacy_stock = None
    parse_legacy_tool_op = None
    parse_svg_polygon = None
    transform_tool_svg = None

try:
    from .plotter import generate_verification_plot
except (ImportError, OSError):
    generate_verification_plot = None


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
    "Lathe2DIPWVerifier",
    "MillClearanceVerifier",
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
    "generate_verification_plot",
]
