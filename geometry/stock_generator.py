"""
Analytical Workpiece and Fixture Geometry Generators.
"""

from typing import Any

try:
    from shapely.geometry import Polygon, box
except (ImportError, OSError):
    Polygon = Any
    box = None

from .models import AxisymmetricStock2D, AxisymmetricChuckObstacle


def generate_axisymmetric_stock_polygon(stock: AxisymmetricStock2D) -> Polygon:
    """
    Generate the 2D cross-section polygon of axisymmetric lathe stock in the (Z, R) plane.
    Z corresponds to horizontal axis, R corresponds to vertical axis (R >= 0).
    """
    if stock.z_min >= stock.z_max:
        raise ValueError(f"Invalid Z extents: z_min ({stock.z_min}) must be < z_max ({stock.z_max})")
    if stock.outer_radius <= 0.0:
        raise ValueError(f"outer_radius must be positive, got {stock.outer_radius}")
    if stock.inner_radius < 0.0 or stock.inner_radius >= stock.outer_radius:
        raise ValueError(f"Invalid inner_radius ({stock.inner_radius}) relative to outer ({stock.outer_radius})")

    # Outer solid box in (Z, R)
    outer_poly = box(stock.z_min, 0.0, stock.z_max, stock.outer_radius)

    # If hollow / tube, subtract the core
    if stock.inner_radius > 1e-6:
        inner_poly = box(stock.z_min - 0.001, -0.001, stock.z_max + 0.001, stock.inner_radius)
        return outer_poly.difference(inner_poly)

    return outer_poly


def generate_chuck_obstacle_polygon(chuck: AxisymmetricChuckObstacle) -> Polygon:
    """
    Generate the 2D swept keep-out polygon of rotating lathe chuck jaws and body.
    """
    if chuck.z_min >= chuck.z_max:
        raise ValueError(f"Invalid chuck Z extents: z_min ({chuck.z_min}) must be < z_max ({chuck.z_max})")
    if chuck.outer_radius <= chuck.clamping_radius:
        raise ValueError(f"outer_radius ({chuck.outer_radius}) must be > clamping_radius ({chuck.clamping_radius})")

    # Clamping jaws extend from clamping_radius out to outer_radius
    return box(chuck.z_min, chuck.clamping_radius, chuck.z_max, chuck.outer_radius)
