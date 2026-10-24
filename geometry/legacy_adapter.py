"""
Legacy Adapter: Parsing and Validation of Autodesk CAM Post Metadata.

Provides backward-compatible ingestion of legacy ;META_STOCK and ;META_OP comments,
and SVG path parsing to serve as ground-truth fixtures for parity testing.
"""

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from shapely.geometry import Polygon
except (ImportError, OSError):
    Polygon = Any


from .models import (
    AxisymmetricStock2D,
    LatheToolGeometry,
    ToolCompensationQuadrant,
    WorkpieceDefinition,
)


def parse_svg_polygon(svg_path: str, arc_steps: int = 16) -> Polygon:
    """Convert an SVG path ('M x y L x y A rx ry ... Z') into a Shapely Polygon."""
    tokens = re.findall(r"[MLAZ]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", svg_path)
    pts: List[Tuple[float, float]] = []
    i = 0
    while i < len(tokens):
        cmd = tokens[i]
        if cmd in ("M", "L"):
            pts.append((float(tokens[i + 1]), float(tokens[i + 2])))
            i += 3
        elif cmd == "A":
            rx = float(tokens[i + 1])
            sweep = int(float(tokens[i + 5]))
            x2, y2 = float(tokens[i + 6]), float(tokens[i + 7])
            x1, y1 = pts[-1]
            pts.extend(_sample_svg_arc(x1, y1, x2, y2, rx, sweep, arc_steps))
            i += 8
        else:
            i += 1
    if not pts:
        return Polygon()
    return Polygon(pts).buffer(0)


def _sample_svg_arc(
    x1: float, y1: float, x2: float, y2: float, r: float, sweep: int, steps: int
) -> List[Tuple[float, float]]:
    """Interpolate a circular SVG arc between (x1, y1) and (x2, y2)."""
    dx, dy = x2 - x1, y2 - y1
    d = math.hypot(dx, dy)
    if d < 1e-9 or r < 1e-9:
        return [(x2, y2)]
    h = math.sqrt(max(0.0, r * r - (d * 0.5) ** 2))
    mx, my = (x1 + x2) * 0.5, (y1 + y2) * 0.5
    sign = 1.0 if sweep == 1 else -1.0
    cx = mx - sign * h * (dy / d)
    cy = my + sign * h * (dx / d)

    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    da = a2 - a1
    if sweep == 1 and da < 0:
        da += 2 * math.pi
    elif sweep == 0 and da > 0:
        da -= 2 * math.pi

    return [
        (cx + r * math.cos(a1 + da * k / steps), cy + r * math.sin(a1 + da * k / steps))
        for k in range(1, steps + 1)
    ]


def transform_tool_svg(
    svg_path: str,
    orient_deg: float,
    comp: Tuple[float, float],
    post: str,
    tool_type: str = "",
) -> Polygon:
    """Transform a Fusion tool SVG path into the lathe (Z, R) frame at tip (0, 0)."""
    raw_poly = parse_svg_polygon(svg_path)
    if raw_poly.is_empty or raw_poly.area < 1e-9:
        return Polygon()
    theta = math.radians(orient_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    comp_x, comp_y = comp
    tt_lower = tool_type.lower()
    is_boring = "boring" in tt_lower or "bore" in tt_lower
    is_drill = "drill" in tt_lower
    r_sign = -1.0 if post == "REAR" else 1.0

    def map_pt(pt: Tuple[float, float]) -> Tuple[float, float]:
        x, y = pt
        if is_drill and abs(orient_deg) < 1e-4:
            return (y - comp_y, x - comp_x)
        if is_boring and abs(orient_deg) < 1e-4:
            return (-y + comp_y, -x + comp_x)
        x_rot = x * cos_t - y * sin_t
        y_rot = x * sin_t + y * cos_t
        return (x_rot - comp_x, r_sign * (y_rot - comp_y))

    return Polygon([map_pt(p) for p in raw_poly.exterior.coords]).buffer(0)


def parse_legacy_stock(meta_stock_line: str) -> WorkpieceDefinition:
    """Parse ;META_STOCK: JSON payload into WorkpieceDefinition."""
    payload = json.loads(meta_stock_line.split(":", 1)[1])
    stock = AxisymmetricStock2D(
        z_min=payload["zLower"],
        z_max=payload["zUpper"],
        outer_radius=payload["rOuter"],
        inner_radius=payload.get("rInner", 0.0),
    )
    return WorkpieceDefinition(stock=stock)


def parse_legacy_tool_op(meta_op_line: str) -> LatheToolGeometry:
    """Parse ;META_OP: JSON payload into LatheToolGeometry."""
    payload = json.loads(meta_op_line.split(":", 1)[1])
    comp = (payload["comp"][0], payload["comp"][1])
    ttype = payload.get("toolType", "")
    cutter = transform_tool_svg(
        payload["cutterSVG"], payload["orientDeg"], comp, payload["post"], tool_type=ttype
    )
    holder = transform_tool_svg(
        payload["holderSVG"], payload["orientDeg"], comp, payload["post"], tool_type=ttype
    )
    holder_clean = holder.difference(cutter.buffer(0.0015))

    return LatheToolGeometry(
        insert_shape=ttype,
        nose_radius=payload.get("noseR", 0.0),
        hand=payload.get("hand", "R"),
        quadrant=ToolCompensationQuadrant.P3,
        cutter_polygon=cutter,
        holder_polygon=holder_clean,
    )
