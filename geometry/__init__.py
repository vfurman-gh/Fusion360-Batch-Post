"""
CAM Geometry & Verification Domain Models and Analytical Generators.

Provides:
- Strongly-typed domain models for Workpieces, Tools, and Kinematic moves.
- Pure-Python analytical generators for standard turning inserts (C, D, V, W, T, Grooving, Drilling).
- Legacy adapters for extracting and validating geometries from Autodesk CAM post outputs.
"""

import os
import sys

# Ensure packages directory is discoverable for Fusion 360's Python 3.14 environment
if sys.version_info >= (3, 14):
    _addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _packages_dir = os.path.join(_addon_dir, "packages")
    if os.path.isdir(_packages_dir) and _packages_dir not in sys.path:
        sys.path.insert(0, _packages_dir)

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

try:
    from .engine_interface import IVerificationEngine, BaseVerificationEngine
except (ImportError, AttributeError):
    import importlib
    if "geometry.engine_interface" in sys.modules:
        try:
            importlib.reload(sys.modules["geometry.engine_interface"])
        except Exception:
            pass
    from .engine_interface import IVerificationEngine, BaseVerificationEngine

try:
    from .lathe_engine import Lathe2DIPWVerifier
except (ImportError, OSError):
    Lathe2DIPWVerifier = None

try:
    from .mill_engine import MillClearanceVerifier
except (ImportError, OSError):
    MillClearanceVerifier = None

try:
    from .heightfield_engine import MillHeightfieldVerifier, HeightfieldGrid
except (ImportError, AttributeError):
    import importlib
    if "geometry.heightfield_engine" in sys.modules:
        try:
            importlib.reload(sys.modules["geometry.heightfield_engine"])
        except Exception:
            pass
    from .heightfield_engine import MillHeightfieldVerifier, HeightfieldGrid
    from .mesh_utils import greedy_mesh_heightfield, compute_cam_palette_facecolors
except Exception:
    pass

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
        parse_svg_polygon,
        transform_tool_svg,
    )
except (ImportError, OSError):
    parse_svg_polygon = None
    transform_tool_svg = None

try:
    from .plotter import generate_verification_plot, generate_mill_verification_plot
except (ImportError, OSError):
    generate_verification_plot = None
    generate_mill_verification_plot = None


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
    "BaseVerificationEngine",
    "Lathe2DIPWVerifier",
    "MillClearanceVerifier",
    "MillHeightfieldVerifier",
    "HeightfieldGrid",
    "greedy_mesh_heightfield",
    "compute_cam_palette_facecolors",
    "generate_rhombic_insert",
    "generate_grooving_insert",
    "generate_drill_cutter",
    "generate_turning_holder",
    "generate_axisymmetric_stock_polygon",
    "generate_chuck_obstacle_polygon",
    "parse_svg_polygon",
    "transform_tool_svg",
    "generate_verification_plot",
    "generate_mill_verification_plot",
]
