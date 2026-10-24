"""
Analytical Tool Geometry Generators.

Constructs exact 2D Shapely Polygons for standard turning inserts and tools:
- Diamond/Rhombic Inserts (CNMG 80°, DNMG 55°, VNMG 35°)
- Grooving Inserts (Flat cutting edge with corner radii and side relief)
- Twist Drills (Conical drill point with 118° point angle + fluted body)
- Toolholders (Rectangular OD shanks and boring bars)
"""

import math
from typing import List, Optional, Tuple
from shapely.geometry import Polygon


def _sample_arc(cx: float, cy: float, r: float, a_start: float, a_end: float, steps: int = 16) -> List[Tuple[float, float]]:
    """Sample points along a circular arc between two radian angles."""
    if steps < 2:
        steps = 2
    da = a_end - a_start
    return [(cx + r * math.cos(a_start + da * (i / steps)), cy + r * math.sin(a_start + da * (i / steps))) for i in range(steps + 1)]


def generate_rhombic_insert(
    edge_length: float,
    nose_radius: float,
    lead_angle_deg: float = 5.0,
    steps: int = 16,
) -> Polygon:
    """
    Generate an analytical 2D diamond/rhombic insert polygon (e.g. CNMG 80° or DNMG 55°).
    Primary nose radius arc is centered at (0, 0) matching Fusion's internal tool frame.
    All 4 corners are rounded with radius nose_radius.
    """
    lead = math.radians(lead_angle_deg)
    L = edge_length
    r = nose_radius

    v1 = (L * math.sin(lead), -L * math.cos(lead))
    v3 = (L * math.cos(lead), -L * math.sin(lead))
    v2 = (v1[0] + v3[0], v1[1] + v3[1])

    c0 = (0.0, 0.0)
    c1 = v1
    c2 = v2
    c3 = v3

    pts: List[Tuple[float, float]] = []

    # Arc 0 (nose): from flank 3-0 to flank 0-1
    pts.extend(_sample_arc(c0[0], c0[1], r, math.pi * 0.5 + lead, math.pi - lead, steps))
    # Arc 1: from flank 0-1 to flank 1-2
    pts.extend(_sample_arc(c1[0], c1[1], r, math.pi - lead, math.pi * 1.5 + lead, steps))
    # Arc 2 (rear): from flank 1-2 to flank 2-3
    pts.extend(_sample_arc(c2[0], c2[1], r, math.pi * 1.5 + lead, 2.0 * math.pi - lead, steps))
    # Arc 3: from flank 2-3 to flank 3-0
    pts.extend(_sample_arc(c3[0], c3[1], r, -lead, math.pi * 0.5 + lead, steps))

    return Polygon(pts).buffer(0)


def generate_grooving_insert(
    blade_width: float,
    blade_depth: float,
    corner_radius: float = 0.0,
    shank_width: Optional[float] = None,
    shank_depth: float = 0.5,
    steps: int = 16,
) -> Polygon:
    """
    Generate an analytical 2D grooving insert polygon with corner radii and relief angle.
    Cutting edge sits along Y=0 (from -blade_width/2 to +blade_width/2).
    """
    if shank_width is None:
        shank_width = blade_width * 0.7875  # Standard grooving blade neck ratio

    hw_tip = blade_width * 0.5
    hw_base = shank_width * 0.5
    hw_shank = shank_width * 0.5
    r = corner_radius

    pts: List[Tuple[float, float]] = []

    if r > 1e-6 and r < hw_tip:
        # Left corner arc: from (-hw_tip, -r) to (-hw_tip + r, 0)
        pts.extend(_sample_arc(-hw_tip + r, -r, r, math.pi, math.pi * 0.5, steps))
        # Top cutting edge up to right corner arc
        # Right corner arc: from (hw_tip - r, 0) to (hw_tip, -r)
        pts.extend(_sample_arc(hw_tip - r, -r, r, math.pi * 0.5, 0.0, steps))
        # Right relief flank down to blade_depth
        pts.append((hw_base, -blade_depth))
        # Shank right down to shank_depth
        pts.append((hw_shank, -shank_depth))
        # Shank bottom
        pts.append((-hw_shank, -shank_depth))
        # Shank left up to blade_depth
        pts.append((-hw_base, -blade_depth))
    else:
        pts = [
            (-hw_tip, 0.0),
            (hw_tip, 0.0),
            (hw_base, -blade_depth),
            (hw_shank, -shank_depth),
            (-hw_shank, -shank_depth),
            (-hw_base, -blade_depth),
        ]

    return Polygon(pts).buffer(0)


def generate_drill_cutter(
    diameter: float,
    point_angle_deg: float = 118.0,
    flute_length: float = 4.0,
    overall_length: float = 4.5,
) -> Polygon:
    """
    Generate an analytical 2D drill cutter polygon in Fusion tool frame.
    X corresponds to radius (-diameter/2 to +diameter/2).
    Y corresponds to axial length (0 to overall_length).
    """
    radius = diameter * 0.5
    half_angle = math.radians(point_angle_deg * 0.5)
    tip_len = radius / math.tan(half_angle)

    pts = [
        (0.0, 0.0),                               # Drill tip point
        (radius, tip_len),                        # Cutting lip outer corner
        (radius, flute_length),                   # End of fluted length
        (radius, overall_length),                 # Shank end
        (0.0, overall_length),                    # Shank centerline
        (-radius, overall_length),                # Shank left end
        (-radius, flute_length),                  # Flute left
        (-radius, tip_len),                       # Lip left
    ]
    return Polygon(pts).buffer(0)


def generate_turning_holder(
    shank_width: float,
    shank_length: float,
    clearance_x: float = 0.008,
    clearance_z: float = 0.008,
    is_boring: bool = False,
) -> Polygon:
    """
    Generate standard rectangular OD turning or ID boring toolholder keep-out polygon.
    For OD turning, the shank extends radially in R <= 0.
    For boring bars, the shank extends axially along Z >= clearance_z.
    """
    if is_boring:
        pts = [
            (clearance_z, -clearance_x),
            (clearance_z + shank_length, -clearance_x),
            (clearance_z + shank_length, -clearance_x - shank_width),
            (clearance_z, -clearance_x - shank_width),
        ]
    else:
        pts = [
            (clearance_z, -clearance_x),
            (clearance_z + shank_width, -clearance_x),
            (clearance_z + shank_width, -shank_length),
            (clearance_z, -shank_length),
        ]
    return Polygon(pts).buffer(0)

