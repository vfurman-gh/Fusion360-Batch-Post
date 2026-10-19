#!/usr/bin/env python3
"""
2D Lathe G-Code Collision Verifier, IPW Rapid Promoter & MRR Analyzer.

1. Parses ;META_STOCK and ;META_OP comments, spindle/feed states, and motions.
2. Simulates 2D material removal (IPW), checks Insert/Holder/Stock/Chuck collisions,
   and computes exact 3D Material Removal Rate (MRR in in^3/min) via Pappus's theorem.
3. Optionally promotes collision-free G1 air moves to G0 and re-verifies in Pass 2.
4. Exports the MRR time-series to CSV, plots both 2D geometry and MRR vs. time,
   and flags G-code lines that cause large MRR spikes.
"""

import csv
from dataclasses import dataclass
import json
import math
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGES_DIR = os.path.join(_SCRIPT_DIR, "packages")
if sys.version_info >= (3, 14) and os.path.isdir(_PACKAGES_DIR) and _PACKAGES_DIR not in sys.path:
    sys.path.insert(0, _PACKAGES_DIR)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as MplPolygon
    from shapely.affinity import translate
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
    _HAS_DEPS = True
except Exception:
    Polygon = Any  # type: ignore
    _HAS_DEPS = False

_VENV_PYTHON = (
    os.path.join(_SCRIPT_DIR, ".venv", "bin", "python")
    if os.path.isfile(os.path.join(_SCRIPT_DIR, ".venv", "bin", "python"))
    else os.path.expanduser("~/Documents/.lathe_venv/bin/python")
)


# ---------------------------------------------------------------------------
# 1. Data Models
# ---------------------------------------------------------------------------

@dataclass
class StockConfig:
    z_lower: float
    z_upper: float
    r_outer: float
    r_inner: float = 0.0
    part_z_lower: Optional[float] = None
    part_z_upper: Optional[float] = None
    part_r_outer: Optional[float] = None


@dataclass
class OperationMeta:
    id: int
    name: str
    tool: int
    post: str
    orient_deg: float
    insert_orient: str
    hand: str
    nose_r: float
    comp: Tuple[float, float]
    cutter_poly: Polygon
    holder_poly: Polygon
    tool_type: str = ""


@dataclass
class Move:
    line_no: int
    op_name: str
    motion: str                 # 'G0' or 'G1'
    start: Tuple[float, float]  # (z1, r1)
    end: Tuple[float, float]    # (z2, r2)
    feed: float = 0.003         # IPR (G99) or IPM (G98)
    feed_mode: int = 99         # 99 = IPR, 98 = IPM
    spindle_mode: int = 96      # 96 = CSS (SFM), 97 = RPM
    spindle_val: float = 250.0  # S value (SFM or RPM)
    max_rpm: float = 2000.0     # G50 S clamp
    is_arc: bool = False
    rapid_tag: str = ""         # '', 'RESTORED', or 'PROMOTED'
    raw_line: str = ""

    def rpm_at(self, r: float) -> float:
        if self.spindle_mode == 97:
            return min(self.max_rpm, max(1.0, self.spindle_val))
        r_eff = max(abs(r), 0.005)
        rpm = (12.0 * self.spindle_val) / (2.0 * math.pi * r_eff)
        return min(self.max_rpm, max(1.0, rpm))

    def ipm_at(self, r: float, rapid_ipm: float = 200.0) -> float:
        if self.motion == "G0":
            return rapid_ipm
        if self.feed_mode == 98:
            return max(0.01, self.feed)
        return max(0.01, self.feed * self.rpm_at(r))


@dataclass
class CollisionEvent:
    line_no: int
    op_name: str
    kind: str                   # 'RAPID_STOCK', 'HOLDER_STOCK', 'CHUCK'
    overlap_area: float
    max_depth: float
    location: Tuple[float, float]
    raw_line: str
    t_sec: float = 0.0          # machining time at the start of the offending move


# Plot styles for real collisions: kind -> (color, marker, legend description)
COLLISION_STYLES: Dict[str, Tuple[str, str, str]] = {
    "RAPID_STOCK": ("#d50000", "x", "Rapid (G0) insert into stock"),
    "HOLDER_STOCK": ("#8e24aa", "X", "Toolholder into stock"),
}


@dataclass
class MRRSample:
    t_start_sec: float
    t_end_sec: float
    dt_sec: float
    line_no: int
    op_name: str
    tool: int
    motion: str
    z: float
    r: float
    dist_in: float
    rpm: float
    ipm: float
    area_2d_sq_in: float        # 2D cross-section removed (in^2)
    eff_doc_in: float           # Effective radial engagement = area_2d / dist (in)
    vol_cu_in: float            # 3D volume removed = 2 * pi * R_centroid * area_2d (in^3)
    mrr_cu_in_min: float        # Instantaneous 3D MRR = vol / dt_min (in^3/min)
    raw_line: str


# ---------------------------------------------------------------------------
# 2. SVG Path & Tool Geometry Builder
# ---------------------------------------------------------------------------

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
    svg_path: str, orient_deg: float, comp: Tuple[float, float], post: str, tool_type: str = ""
) -> Polygon:
    """Transform a Fusion tool SVG path into the lathe (Z, R) frame at tip (0, 0)."""
    raw_poly = parse_svg_polygon(svg_path)
    theta = math.radians(orient_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    comp_x, comp_y = comp
    is_boring = "boring" in tool_type.lower()
    r_sign = -1.0 if post == "REAR" else 1.0

    def map_pt(pt: Tuple[float, float]) -> Tuple[float, float]:
        x, y = pt
        if is_boring and abs(orient_deg) < 1e-4:
            # Fusion exports lathe boring bars along -Y for shank length and +X for diameter.
            # In lathe frame, shank extends along +Z (out of bore) and towards -R (towards centerline).
            return (-y + comp_y, -x - comp_x)
        x_rot = x * cos_t - y * sin_t
        y_rot = x * sin_t + y * cos_t
        return (x_rot - comp_x, r_sign * (y_rot - comp_y))

    return Polygon([map_pt(p) for p in raw_poly.exterior.coords]).buffer(0)


# ---------------------------------------------------------------------------
# 3. G-Code & Metadata Parser
# ---------------------------------------------------------------------------

def parse_cnc_file(filepath: str) -> Tuple[StockConfig, List[OperationMeta], List[Move]]:
    """Parse stock metadata, operation tool metadata, and G-code moves."""
    stock_cfg: Optional[StockConfig] = None
    ops: List[OperationMeta] = []
    moves: List[Move] = []

    current_op_name = "INIT"
    motion = "G0"
    feed = 0.003
    feed_mode = 99
    spindle_mode = 97
    spindle_val = 600.0
    max_rpm = 2000.0
    cur_z, cur_r = 3.0, 2.0
    has_z, has_r = False, False
    tool_comments: Dict[int, str] = {}

    with open(filepath, "r") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue

            # Parse tool table comments like: ;T0606 NR=0.008 - ZMIN=-3.3984 - BORING TURNING
            m_tcomm = re.match(r"^;\s*T(\d+)\s+.*-\s*([^;\n]+)$", line, re.IGNORECASE)
            if m_tcomm:
                t_num = int(m_tcomm.group(1)) // 100
                tool_comments[t_num] = m_tcomm.group(2).strip().lower()

            if line.startswith(";META_STOCK:"):
                data = json.loads(line.split(":", 1)[1])
                stock_cfg = StockConfig(
                    z_lower=data["zLower"],
                    z_upper=data["zUpper"],
                    r_outer=data["rOuter"],
                    r_inner=data.get("rInner", 0.0),
                    part_z_lower=data.get("partZLower"),
                    part_z_upper=data.get("partZUpper"),
                    part_r_outer=data.get("partROuter"),
                )
                continue

            if line.startswith(";META_OP:"):
                data = json.loads(line.split(":", 1)[1])
                comp = (data["comp"][0], data["comp"][1])
                ttype = data.get("toolType", "")
                tnum = data.get("tool", 0)
                # Fallback: if toolType missing from header, infer from tool comments or op name
                tinfo = (ttype + " " + tool_comments.get(tnum, "") + " " + data.get("name", "")).lower()
                if "boring" in tinfo or "bore" in tinfo:
                    ttype = "boring"
                cutter = transform_tool_svg(
                    data["cutterSVG"], data["orientDeg"], comp, data["post"], tool_type=ttype
                )
                holder = transform_tool_svg(
                    data["holderSVG"], data["orientDeg"], comp, data["post"], tool_type=ttype
                )
                holder_clean = holder.difference(cutter.buffer(0.0015))
                op = OperationMeta(
                    id=data["id"],
                    name=data["name"],
                    tool=data["tool"],
                    post=data["post"],
                    orient_deg=data["orientDeg"],
                    insert_orient=data["insertOrient"],
                    hand=data["hand"],
                    nose_r=data["noseR"],
                    comp=comp,
                    cutter_poly=cutter,
                    holder_poly=holder_clean,
                    tool_type=ttype,
                )
                ops.append(op)
                current_op_name = op.name
                continue

            if line.startswith(";"):
                continue

            rapid_tag = "PROMOTED" if "PROMOTED" in line else ""
            code_part = line.split(";", 1)[0].strip()
            if not code_part:
                continue

            words = dict(re.findall(r"([A-Z])([-+]?\d*\.?\d+)", code_part))
            g_codes = [int(float(g)) for g in re.findall(r"G(\d+(?:\.\d+)?)", code_part)]

            if 17 in g_codes or 19 in g_codes or ("Y" in words and abs(float(words["Y"])) > 1e-6):
                raise ValueError(
                    f"Line {line_no}: Milling G-code detected (G17/G19 or Y-axis); lathe verifier only supports turning setups."
                )

            for g_val in g_codes:
                if g_val in (0, 1, 2, 3):
                    motion = f"G{g_val}"
                elif g_val in (98, 99):
                    feed_mode = g_val
                elif g_val in (96, 97):
                    spindle_mode = g_val
                elif g_val == 28:
                    if "U" in words:
                        cur_r, has_r = 2.0, False
                    if "W" in words:
                        cur_z, has_z = 2.0, False

            if 50 in g_codes and "S" in words:
                max_rpm = float(words["S"])
            elif "S" in words:
                spindle_val = float(words["S"])

            if "F" in words and 4 not in g_codes:
                feed = float(words["F"])

            if 28 in g_codes or 4 in g_codes:
                continue

            next_r = float(words["X"]) * 0.5 if "X" in words else cur_r
            next_z = float(words["Z"]) if "Z" in words else cur_z
            if "X" in words:
                has_r = True
            if "Z" in words:
                has_z = True

            if not (has_r and has_z):
                cur_r, cur_z = next_r, next_z
                continue

            if math.hypot(next_z - cur_z, next_r - cur_r) < 1e-6:
                continue

            if motion in ("G0", "G1"):
                moves.append(
                    Move(
                        line_no=line_no,
                        op_name=current_op_name,
                        motion=motion,
                        start=(cur_z, cur_r),
                        end=(next_z, next_r),
                        feed=feed,
                        feed_mode=feed_mode,
                        spindle_mode=spindle_mode,
                        spindle_val=spindle_val,
                        max_rpm=max_rpm,
                        is_arc=False,
                        rapid_tag=rapid_tag,
                        raw_line=line,
                    )
                )
            elif motion in ("G2", "G3"):
                i_off = float(words.get("I", 0.0))
                k_off = float(words.get("K", 0.0))
                for p1, p2 in _linearize_gcode_arc(cur_z, cur_r, next_z, next_r, k_off, i_off):
                    moves.append(
                        Move(
                            line_no=line_no,
                            op_name=current_op_name,
                            motion="G1",
                            start=p1,
                            end=p2,
                            feed=feed,
                            feed_mode=feed_mode,
                            spindle_mode=spindle_mode,
                            spindle_val=spindle_val,
                            max_rpm=max_rpm,
                            is_arc=True,
                            raw_line=line,
                        )
                    )

            cur_z, cur_r = next_z, next_r

    if stock_cfg is None:
        raise ValueError("Missing ;META_STOCK comment in CNC file.")
    return stock_cfg, ops, moves


def _linearize_gcode_arc(
    z1: float, r1: float, z2: float, r2: float, k: float, i: float, steps: int = 12
) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Break a G2/G3 lathe arc into short linear segments in (Z, R)."""
    cz, cr = z1 + k, r1 + i
    rad = math.hypot(z1 - cz, r1 - cr)
    a1 = math.atan2(r1 - cr, z1 - cz)
    a2 = math.atan2(r2 - cr, z2 - cz)
    da = a2 - a1
    if abs(da) > math.pi:
        da -= math.copysign(2 * math.pi, da)
    pts = (
        [(z1, r1)]
        + [
            (cz + rad * math.cos(a1 + da * s / steps), cr + rad * math.sin(a1 + da * s / steps))
            for s in range(1, steps)
        ]
        + [(z2, r2)]
    )
    return list(zip(pts[:-1], pts[1:]))


# ---------------------------------------------------------------------------
# 4. 2D Swept-Polygon Collision, IPW & MRR Simulator
# ---------------------------------------------------------------------------

def sweep_polygon(poly: Polygon, p1: Tuple[float, float], p2: Tuple[float, float]) -> Polygon:
    """Compute the 2D area swept by translating `poly` linearly from p1 to p2."""
    poly1 = translate(poly, xoff=p1[0], yoff=p1[1])
    poly2 = translate(poly, xoff=p2[0], yoff=p2[1])
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    if math.hypot(dx, dy) < 1e-6:
        return poly1

    coords = list(poly1.exterior.coords)
    quads = [
        Polygon([
            coords[i],
            coords[i + 1],
            (coords[i + 1][0] + dx, coords[i + 1][1] + dy),
            (coords[i][0] + dx, coords[i][1] + dy),
        ])
        for i in range(len(coords) - 1)
    ]
    return unary_union([poly1, poly2] + quads).buffer(0)


class LatheSimulator:
    """Simulates material removal, collisions, G0 promotion candidates, and MRR."""

    def __init__(self, stock_cfg: StockConfig, ops: List[OperationMeta]):
        self.stock_cfg = stock_cfg
        self.ops_by_name: Dict[str, OperationMeta] = {op.name: op for op in ops}

        if stock_cfg.r_inner > 1e-4:
            upper_stock = box(stock_cfg.z_lower, stock_cfg.r_inner, stock_cfg.z_upper, stock_cfg.r_outer)
            lower_stock = box(stock_cfg.z_lower, -stock_cfg.r_outer, stock_cfg.z_upper, -stock_cfg.r_inner)
            self.initial_stock = unary_union([upper_stock, lower_stock])
        else:
            self.initial_stock = Polygon([
                (stock_cfg.z_lower, -stock_cfg.r_outer),
                (stock_cfg.z_upper, -stock_cfg.r_outer),
                (stock_cfg.z_upper, stock_cfg.r_outer),
                (stock_cfg.z_lower, stock_cfg.r_outer),
            ])
        self.ipw = self.initial_stock
        self.upper_half = box(stock_cfg.z_lower - 5.0, 0.0, stock_cfg.z_upper + 5.0, stock_cfg.r_outer + 5.0)

        self.chuck = Polygon([
            (stock_cfg.z_lower - 1.0, -stock_cfg.r_outer - 1.0),
            (stock_cfg.z_lower, -stock_cfg.r_outer - 1.0),
            (stock_cfg.z_lower, stock_cfg.r_outer + 1.0),
            (stock_cfg.z_lower - 1.0, stock_cfg.r_outer + 1.0),
        ])
        self.collisions: List[CollisionEvent] = []
        self.promotable_lines: Set[int] = set()
        self.promoted_distance: float = 0.0
        self.mrr_series: List[MRRSample] = []
        self.elapsed_sec: float = 0.0

    def run(
        self,
        moves: List[Move],
        clearance_tol: float = 0.0015,
        promote_margin: float = 0.005,
        min_promote_len: float = 0.015,
        mrr_max_step: float = 0.04,
    ) -> List[CollisionEvent]:
        """Run full simulation, recording collisions, promotable moves, and MRR samples."""
        for mv in moves:
            op = self.ops_by_name.get(mv.op_name)
            if op is None:
                continue

            swept_cutter = sweep_polygon(op.cutter_poly, mv.start, mv.end)
            swept_holder = sweep_polygon(op.holder_poly, mv.start, mv.end)
            dist = math.hypot(mv.end[0] - mv.start[0], mv.end[1] - mv.start[1])

            # 1. Chuck check
            chuck_hit = unary_union([swept_cutter, swept_holder]).intersection(self.chuck)
            if not chuck_hit.is_empty and chuck_hit.area > 1e-6:
                self._record_collision(mv, "CHUCK", chuck_hit)

            # 2. Holder vs. IPW check
            core_stock = self.ipw.buffer(-clearance_tol)
            holder_hit = swept_holder.intersection(core_stock)
            if not holder_hit.is_empty and holder_hit.area > 1e-6:
                self._record_collision(mv, "HOLDER_STOCK", holder_hit)

            # 3. Handle G0 rapid
            if mv.motion == "G0":
                rapid_hit = swept_cutter.intersection(core_stock)
                if not rapid_hit.is_empty and rapid_hit.area > 1e-6:
                    self._record_collision(mv, "RAPID_STOCK", rapid_hit)
                self._record_mrr_step(mv, op.tool, mv.start, mv.end, 0.0, 0.0)
                continue

            # 4. Check if linear G1 move is collision-free in air (>= promote_margin)
            if not mv.is_arc and dist >= min_promote_len:
                padded_ipw = self.ipw.buffer(promote_margin)
                if (
                    swept_cutter.intersection(padded_ipw).is_empty
                    and swept_holder.intersection(padded_ipw).is_empty
                ):
                    self.promotable_lines.add(mv.line_no)
                    self.promoted_distance += dist
                    self._record_mrr_step(mv, op.tool, mv.start, mv.end, 0.0, 0.0)
                    continue

            # 5. Sub-step cutting move for accurate time-resolved MRR & IPW update
            n_sub = max(1, int(math.ceil(dist / mrr_max_step)))
            z1, r1 = mv.start
            z2, r2 = mv.end
            for k in range(n_sub):
                p_a = (z1 + (z2 - z1) * k / n_sub, r1 + (r2 - r1) * k / n_sub)
                p_b = (z1 + (z2 - z1) * (k + 1) / n_sub, r1 + (r2 - r1) * (k + 1) / n_sub)
                sub_cutter = sweep_polygon(op.cutter_poly, p_a, p_b)
                mirrored = Polygon([(z, -r) for z, r in sub_cutter.exterior.coords])
                combined_cut = unary_union([sub_cutter, mirrored])

                removed = self.ipw.intersection(combined_cut)
                removed_upper = removed.intersection(self.upper_half)
                area_2d = removed_upper.area
                if area_2d > 1e-9:
                    r_cent = max(abs(removed_upper.centroid.y), 0.001)
                    vol_3d = 2.0 * math.pi * r_cent * area_2d
                else:
                    vol_3d = 0.0

                self.ipw = self.ipw.difference(combined_cut)
                self._record_mrr_step(mv, op.tool, p_a, p_b, area_2d, vol_3d)

        return self.collisions

    def _record_mrr_step(
        self,
        mv: Move,
        tool: int,
        p1: Tuple[float, float],
        p2: Tuple[float, float],
        area_2d: float,
        vol_3d: float,
    ) -> None:
        dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        r_mid = 0.5 * (p1[1] + p2[1])
        z_mid = 0.5 * (p1[0] + p2[0])
        rpm = mv.rpm_at(r_mid)
        ipm = mv.ipm_at(r_mid)
        dt_min = dist / ipm if ipm > 0 else 0.0
        dt_sec = dt_min * 60.0
        t_start = self.elapsed_sec
        self.elapsed_sec += dt_sec
        mrr = vol_3d / dt_min if dt_min > 1e-9 else 0.0
        eff_doc = area_2d / dist if dist > 1e-6 else 0.0

        self.mrr_series.append(
            MRRSample(
                t_start_sec=t_start,
                t_end_sec=self.elapsed_sec,
                dt_sec=dt_sec,
                line_no=mv.line_no,
                op_name=mv.op_name,
                tool=tool,
                motion=mv.motion,
                z=z_mid,
                r=r_mid,
                dist_in=dist,
                rpm=rpm,
                ipm=ipm,
                area_2d_sq_in=area_2d,
                eff_doc_in=eff_doc,
                vol_cu_in=vol_3d,
                mrr_cu_in_min=mrr,
                raw_line=mv.raw_line,
            )
        )

    def _record_collision(self, mv: Move, kind: str, hit_geom) -> None:
        c = hit_geom.centroid
        b = hit_geom.bounds
        self.collisions.append(
            CollisionEvent(
                line_no=mv.line_no,
                op_name=mv.op_name,
                kind=kind,
                overlap_area=hit_geom.area,
                max_depth=max(b[2] - b[0], b[3] - b[1]),
                location=(c.x, c.y),
                raw_line=mv.raw_line,
                t_sec=self.elapsed_sec,
            )
        )


# ---------------------------------------------------------------------------
# 5. MRR Spike Detection & CSV Export
# ---------------------------------------------------------------------------

def detect_mrr_spikes(
    samples: List[MRRSample], spike_Multiplier: float = 1.6
) -> Tuple[float, float, float, List[MRRSample]]:
    """
    Compute cutting MRR statistics (mean, std, threshold) and return one peak
    MRRSample per G-code line that exceeds the spike threshold.
    """
    cutting = [s for s in samples if s.vol_cu_in > 1e-7 and s.dt_sec > 1e-5]
    if not cutting:
        return 0.0, 0.0, 0.0, []

    total_t = sum(s.dt_sec for s in cutting)
    mean_mrr = sum(s.mrr_cu_in_min * s.dt_sec for s in cutting) / total_t
    var_mrr = sum(((s.mrr_cu_in_min - mean_mrr) ** 2) * s.dt_sec for s in cutting) / total_t
    std_mrr = math.sqrt(var_mrr)
    threshold = max(mean_mrr * spike_Multiplier, mean_mrr + 1.5 * std_mrr)

    # Group spikes by G-code line number (keep max MRR sub-step per line)
    by_line: Dict[int, MRRSample] = {}
    for s in cutting:
        if s.mrr_cu_in_min >= threshold:
            if s.line_no not in by_line or s.mrr_cu_in_min > by_line[s.line_no].mrr_cu_in_min:
                by_line[s.line_no] = s

    spikes = sorted(by_line.values(), key=lambda x: x.mrr_cu_in_min, reverse=True)
    return mean_mrr, std_mrr, threshold, spikes


def export_mrr_csv(samples: List[MRRSample], csv_path: str) -> None:
    """Export the complete MRR time-series to a CSV file."""
    fieldnames = [
        "t_start_sec", "t_end_sec", "dt_sec", "line_no", "op_name", "tool",
        "motion", "z", "r", "dist_in", "rpm", "ipm", "area_2d_sq_in",
        "eff_doc_in", "vol_cu_in", "mrr_cu_in_min", "raw_line",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        for s in samples:
            writer.writerow([
                f"{s.t_start_sec:.4f}", f"{s.t_end_sec:.4f}", f"{s.dt_sec:.4f}",
                s.line_no, s.op_name, s.tool, s.motion,
                f"{s.z:.4f}", f"{s.r:.4f}", f"{s.dist_in:.4f}",
                f"{s.rpm:.1f}", f"{s.ipm:.3f}",
                f"{s.area_2d_sq_in:.6f}", f"{s.eff_doc_in:.4f}",
                f"{s.vol_cu_in:.6f}", f"{s.mrr_cu_in_min:.5f}", s.raw_line,
            ])


# ---------------------------------------------------------------------------
# 6. G-Code Rewriter for Promoted G0 Moves
# ---------------------------------------------------------------------------

def rewrite_gcode_with_promoted_rapids(
    in_path: str, out_path: str, promotable_lines: Set[int], margin: float
) -> int:
    """Promote safe G1 lines to G0 and restore G1 + F on the next cutting move."""
    with open(in_path, "r") as f:
        lines = f.read().splitlines()

    out_lines: List[str] = []
    orig_modal_g = "G0"
    out_modal_g = "G0"
    active_feed = ""
    feed_needs_restore = False
    promoted_count = 0

    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(";"):
            out_lines.append(raw)
            continue

        code_part, *comment_parts = raw.split(";", 1)
        code = code_part.strip()

        n_match = re.match(r"^(N\d+\s+)", code)
        n_prefix = n_match.group(1) if n_match else ""
        body_code = code[len(n_prefix):].strip()

        f_match = re.search(r"(F[-+]?\d*\.?\d+)", body_code)
        if f_match:
            active_feed = f_match.group(1)

        for g_str in re.findall(r"G(\d+(?:\.\d+)?)", body_code):
            g_val = float(g_str)
            if g_val in (0, 1, 2, 3):
                orig_modal_g = f"G{int(g_val)}"
                if line_no not in promotable_lines:
                    out_modal_g = orig_modal_g

        has_xz = bool(re.search(r"[XZ][-+]?\d*\.?\d+", body_code)) and "G28" not in body_code

        if line_no in promotable_lines and has_xz:
            clean_code = re.sub(r"\bG0?1\b\s*", "", body_code)
            clean_code = re.sub(r"\s*\bF[-+]?\d*\.?\d+\b", "", clean_code).strip()
            g_prefix = "G0 " if out_modal_g != "G0" or body_code.startswith("G") else ""
            out_lines.append(f"{n_prefix}{g_prefix}{clean_code} ;PROMOTED CLEARANCE>={margin}")
            out_modal_g = "G0"
            if f_match or active_feed:
                feed_needs_restore = True
            promoted_count += 1
        else:
            if has_xz and orig_modal_g == "G1":
                if out_modal_g == "G0" and not re.search(r"\bG0?1\b", body_code):
                    body_code = f"G1 {body_code}"
                    out_modal_g = "G1"
                if feed_needs_restore and active_feed and not f_match:
                    body_code = f"{body_code} {active_feed}"
                    feed_needs_restore = False
                elif f_match:
                    feed_needs_restore = False

            suffix = f" ;{comment_parts[0].strip()}" if comment_parts else ""
            out_lines.append(f"{n_prefix}{body_code}{suffix}")

    with open(out_path, "w") as f:
        f.write("\n".join(out_lines) + "\n")
    return promoted_count


# ---------------------------------------------------------------------------
# 7. Visualization (2D Geometry + MRR Time-Series with Operation Span Labels)
# ---------------------------------------------------------------------------

def plot_verification(
    sim: LatheSimulator,
    ops: List[OperationMeta],
    moves: List[Move],
    mean_mrr: float,
    threshold: float,
    spikes: List[MRRSample],
    out_png: str,
) -> None:
    """Save a 2-panel plot: (Top) 2D Lathe Geometry & Spikes, (Bottom) MRR Time-Series."""
    fig, (ax_geo, ax_mrr) = plt.subplots(
        2, 1, figsize=(13, 10.5), dpi=150, gridspec_kw={"height_ratios": [1.3, 1.0]}
    )

    def draw_geom(geom, **kwargs):
        if geom.is_empty:
            return
        polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms)
        for idx, p in enumerate(polys):
            kw = dict(kwargs)
            if idx > 0:
                kw.pop("label", None)
            ax_geo.add_patch(MplPolygon(list(p.exterior.coords), closed=True, **kw))

    # --- Top Panel: 2D Geometry ---
    draw_geom(sim.initial_stock, fill=False, edgecolor="#888888", linestyle="--", linewidth=1.2, label="Initial Stock")
    draw_geom(sim.ipw, facecolor="#cfe2f3", edgecolor="#2b579a", alpha=0.8, label="Remaining Stock (IPW)")
    draw_geom(sim.chuck, facecolor="#555555", edgecolor="#222222", alpha=0.85, label="Chuck / Clamped Zone")

    seen_labels: Set[str] = set()
    for mv in moves:
        z1, r1 = mv.start
        z2, r2 = mv.end
        if mv.motion == "G1":
            lbl = "G1 Cutting" if "G1 Cutting" not in seen_labels else None
            seen_labels.add("G1 Cutting")
            ax_geo.plot([z1, z2], [r1, r2], color="#1f77b4", linewidth=0.6, alpha=0.6, label=lbl)
        elif mv.rapid_tag == "PROMOTED":
            lbl = "G0 Promoted by Python IPW" if "G0 Promoted by Python IPW" not in seen_labels else None
            seen_labels.add("G0 Promoted by Python IPW")
            ax_geo.plot([z1, z2], [r1, r2], color="#d62728", linestyle="--", linewidth=1.0, alpha=0.85, label=lbl)
        else:
            lbl = "G0 Initial Positioning" if "G0 Initial Positioning" not in seen_labels else None
            seen_labels.add("G0 Initial Positioning")
            ax_geo.plot([z1, z2], [r1, r2], color="#2ca02c", linestyle=":", linewidth=0.9, alpha=0.75, label=lbl)

    sample_poses = {
        8: (-1.45, 0.02, "T8 (orient=25°) at Back Apex"),
        1: (-0.40, 0.17, "T1 (orient=0°) on Front Profile"),
    }
    seen_tools: Set[int] = set()
    for op in ops:
        if op.tool in seen_tools or op.tool not in sample_poses:
            continue
        seen_tools.add(op.tool)
        pz, pr, label = sample_poses[op.tool]
        draw_geom(translate(op.holder_poly, pz, pr), facecolor="#7f7f7f", edgecolor="black", alpha=0.65, label=f"{label} Holder")
        draw_geom(translate(op.cutter_poly, pz, pr), facecolor="#f1c232", edgecolor="black", alpha=0.95, label=f"{label} Insert")

    for idx, sp in enumerate(spikes[:8]):
        lbl = "MRR Spike Location" if idx == 0 else None
        ax_geo.plot(sp.z, sp.r, "o", color="#e65100", markersize=5.5, markeredgecolor="black", label=lbl)

    stock_cols = [c for c in sim.collisions if c.kind in COLLISION_STYLES]
    labeled_kinds: Set[str] = set()
    for col in stock_cols:
        color, marker, desc = COLLISION_STYLES[col.kind]
        lbl = None
        if col.kind not in labeled_kinds:
            labeled_kinds.add(col.kind)
            n = sum(1 for c in stock_cols if c.kind == col.kind)
            lbl = f"COLLISION: {desc} ({n})"
        ax_geo.plot(col.location[0], col.location[1], marker, color=color,
                    markersize=10, markeredgewidth=2.5, linestyle="none", label=lbl, zorder=8)

    ax_geo.axhline(0, color="black", linestyle="-.", linewidth=0.8, label="Spindle Centerline (X=0)")
    ax_geo.set_xlim(sim.stock_cfg.z_lower - 0.5, sim.stock_cfg.z_upper + 0.6)
    ax_geo.set_ylim(-0.2, sim.stock_cfg.r_outer + 1.1)
    ax_geo.set_aspect("equal", adjustable="box")
    ax_geo.set_xlabel("Z Axis (inches in G54 WCS)")
    ax_geo.set_ylabel("Radius R = X/2 (inches)")
    ax_geo.set_title(
        f"Lathe G-Code Verification — Stock/Holder Collisions: {len(stock_cols)} | "
        f"Chuck Warnings: {len(sim.collisions) - len(stock_cols)} | Total Cycle Time: {sim.elapsed_sec/60:.2f} min"
    )
    ax_geo.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
    ax_geo.grid(True, linestyle=":", alpha=0.5)

    # --- Bottom Panel: MRR Time-Series & Operation Span Labels ---
    t_vals: List[float] = []
    mrr_vals: List[float] = []
    op_spans: List[Tuple[str, int, float, float]] = []
    for s in sim.mrr_series:
        t_vals.extend([s.t_start_sec, s.t_end_sec])
        mrr_vals.extend([s.mrr_cu_in_min, s.mrr_cu_in_min])
        if not op_spans or op_spans[-1][0] != s.op_name:
            op_spans.append((s.op_name, s.tool, s.t_start_sec, s.t_end_sec))
        else:
            name, tool, t0, _ = op_spans[-1]
            op_spans[-1] = (name, tool, t0, s.t_end_sec)

    y_max = max(mrr_vals or [0.01]) * 1.22

    # Shade alternating operation spans and draw vertical boundary lines
    span_colors = ["#e8f1f8", "#f7f9fc"]
    for idx, (op_name, tool, t0, t1) in enumerate(op_spans):
        ax_mrr.axvspan(t0, t1, facecolor=span_colors[idx % 2], alpha=0.7, zorder=0)
        if idx > 0:
            ax_mrr.axvline(t0, color="#666666", linestyle="--", linewidth=0.85, alpha=0.75, zorder=2)

    ax_mrr.fill_between(t_vals, mrr_vals, color="#1f77b4", alpha=0.30, zorder=3)
    ax_mrr.plot(t_vals, mrr_vals, color="#1f77b4", linewidth=0.8, label="Instantaneous MRR (in³/min)", zorder=4)
    if mean_mrr > 0:
        ax_mrr.axhline(mean_mrr, color="#2ca02c", linestyle="--", linewidth=1.0, label=f"Mean Cutting MRR ({mean_mrr:.4f} in³/min)", zorder=5)
        ax_mrr.axhline(threshold, color="#d62728", linestyle=":", linewidth=1.1, label=f"Spike Threshold ({threshold:.4f} in³/min)", zorder=5)

    for sp in spikes[:6]:
        t_mid = 0.5 * (sp.t_start_sec + sp.t_end_sec)
        ax_mrr.plot(t_mid, sp.mrr_cu_in_min, "o", color="#e65100", markersize=5, markeredgecolor="black", zorder=6)
        ax_mrr.annotate(
            f"L{sp.line_no} ({sp.mrr_cu_in_min:.3f})",
            xy=(t_mid, sp.mrr_cu_in_min),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            fontsize=7,
            color="#b71c1c",
            weight="bold",
            zorder=7,
        )

    # Mark collisions on the timeline: vertical line + marker along the top edge
    labeled_kinds = set()
    annotated_lines: List[int] = []
    for col in sorted(stock_cols, key=lambda c: c.t_sec):
        color, marker, desc = COLLISION_STYLES[col.kind]
        lbl = None
        if col.kind not in labeled_kinds:
            labeled_kinds.add(col.kind)
            lbl = f"COLLISION: {desc}"
        ax_mrr.axvline(col.t_sec, color=color, linestyle="-", linewidth=1.0, alpha=0.6, zorder=6)
        ax_mrr.plot(col.t_sec, y_max * 0.96, marker, color=color, markersize=9,
                    markeredgewidth=2.2, linestyle="none", label=lbl, zorder=8)
        if col.line_no not in annotated_lines and len(annotated_lines) < 12:
            k = len(annotated_lines)
            annotated_lines.append(col.line_no)
            ax_mrr.annotate(f"L{col.line_no}", xy=(col.t_sec, y_max * 0.96), xytext=(6, -12 - 10 * (k % 4)),
                            textcoords="offset points", fontsize=6.5, color=color, weight="bold", zorder=8)

    total_time = max(1.0, sim.elapsed_sec)
    ax_mrr.set_xlim(0, total_time)
    ax_mrr.set_ylim(0, y_max)
    ax_mrr.set_xlabel("Machining Time (seconds)", labelpad=6)
    ax_mrr.set_ylabel("MRR (in³ / min)")
    ax_mrr.set_title(
        f"Material Removal Rate (MRR) Time-Series — Flagged Spike Lines: {len(spikes)} | Collisions: {len(stock_cols)}",
        pad=10,
    )
    ax_mrr.legend(loc="upper right", fontsize=8, framealpha=0.9)
    ax_mrr.grid(True, linestyle=":", alpha=0.5, zorder=1)

    # Add Operation Span axis along the bottom time axis
    ax_spans = ax_mrr.secondary_xaxis(-0.14)
    span_mids = [0.5 * (t0 + t1) for _, _, t0, t1 in op_spans]
    span_labels = [
        f"{op_name} (T{tool})\n[{t0:.0f}–{t1:.0f}s]"
        for op_name, tool, t0, t1 in op_spans
    ]
    ax_spans.set_xticks(span_mids)
    ax_spans.set_xticklabels(span_labels, rotation=28, ha="right", fontsize=7.5, fontweight="medium")
    ax_spans.tick_params(axis="x", length=0)
    ax_spans.spines["bottom"].set_visible(False)

    # Also mark exact boundary ticks on the secondary axis spine
    boundary_ticks = sorted({round(t0, 1) for _, _, t0, _ in op_spans} | {round(total_time, 1)})
    ax_mrr.set_xticks(boundary_ticks, minor=True)
    ax_mrr.tick_params(axis="x", which="minor", length=4, color="#444444")

    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 8. High-Level Workflow & CLI Entrypoint
# ---------------------------------------------------------------------------

def verify_and_promote(
    cnc_path: str,
    out_png: Optional[str] = None,
    out_csv: Optional[str] = None,
    promote: bool = True,
    margin: float = 0.005,
    min_len: float = 0.015,
) -> Tuple[List[CollisionEvent], List[int], List[MRRSample]]:
    """
    Run Pass 1 simulation, optionally promote collision-free G1 moves to G0,
    re-verify in Pass 2, export MRR CSV, and report MRR spikes.
    """
    if not _HAS_DEPS:
        cmd = [_VENV_PYTHON, "-E", os.path.abspath(__file__), cnc_path]
        if out_png is not None:
            cmd.append(out_png)
        if not promote:
            cmd.append("--no-promote")
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("PYTHON") and k not in ("VIRTUAL_ENV", "__PYVENV_LAUNCHER__")
        }
        clean_env["MPLBACKEND"] = "Agg"
        proc = subprocess.run(cmd, env=clean_env, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            details = (proc.stdout + "\n" + proc.stderr).strip()
            raise RuntimeError(f"Lathe verifier failed (exit {proc.returncode}):\n{details}")
        return [], [], []

    base = os.path.splitext(cnc_path)[0]
    if out_png is None:
        out_png = base + "_verification.png"
    if out_csv is None:
        out_csv = base + "_mrr.csv"

    stock_cfg, ops, moves = parse_cnc_file(cnc_path)
    print(f"Pass 1 — Analyzing {cnc_path} ({len(ops)} ops, {len(moves)} moves)...")
    sim1 = LatheSimulator(stock_cfg, ops)
    sim1.run(moves, promote_margin=margin, min_promote_len=min_len)

    promoted_lines = sorted(sim1.promotable_lines)
    if promote and promoted_lines:
        count = rewrite_gcode_with_promoted_rapids(cnc_path, cnc_path, sim1.promotable_lines, margin)
        print(
            f"Promoted {count} collision-free G1 moves ({sim1.promoted_distance:.2f}\" travel, "
            f"clearance >= {margin}\") to G0 in {cnc_path}."
        )
        print("Pass 2 — Re-verifying modified G-code from scratch...")
        stock_cfg2, ops2, moves2 = parse_cnc_file(cnc_path)
        sim2 = LatheSimulator(stock_cfg2, ops2)
        sim2.run(moves2, promote_margin=margin, min_promote_len=min_len)

        ipw_diff = sim1.ipw.symmetric_difference(sim2.ipw).area
        print(f"  IPW consistency check: area delta = {ipw_diff:.8f} sq.in (must be 0)")
        if ipw_diff > 1e-5:
            raise RuntimeError(f"IPW mismatch after G0 promotion: {ipw_diff}")
        sim_final, ops_final, moves_final = sim2, ops2, moves2
    else:
        sim_final, ops_final, moves_final = sim1, ops, moves

    export_mrr_csv(sim_final.mrr_series, out_csv)
    mean_mrr, std_mrr, threshold, spikes = detect_mrr_spikes(sim_final.mrr_series)
    plot_verification(sim_final, ops_final, moves_final, mean_mrr, threshold, spikes, out_png)

    total_vol = sum(s.vol_cu_in for s in sim_final.mrr_series)
    stock_cols = [c for c in sim_final.collisions if c.kind in ("RAPID_STOCK", "HOLDER_STOCK")]
    chuck_cols = [c for c in sim_final.collisions if c.kind == "CHUCK"]

    print(f"\nSaved MRR time-series CSV to: {out_csv} ({len(sim_final.mrr_series)} samples)")
    print(f"Saved verification & MRR plot to: {out_png}")
    print(
        f"\nFINAL VERIFICATION & MRR SUMMARY:\n"
        f"  - Total Simulated Cycle Time:                  {sim_final.elapsed_sec:.1f} sec ({sim_final.elapsed_sec/60:.2f} min)\n"
        f"  - Total Material Volume Removed:               {total_vol:.4f} in³\n"
        f"  - Mean Cutting MRR / Std Dev:                  {mean_mrr:.4f} / {std_mrr:.4f} in³/min\n"
        f"  - MRR Spike Threshold:                         {threshold:.4f} in³/min ({len(spikes)} lines flagged)\n"
        f"  - Insert/Stock Rapid Collisions (RAPID_STOCK): {sum(1 for c in stock_cols if c.kind == 'RAPID_STOCK')}\n"
        f"  - Toolholder/Stock Collisions (HOLDER_STOCK):  {sum(1 for c in stock_cols if c.kind == 'HOLDER_STOCK')}\n"
        f"  - Chuck Plane Warnings (Z < {stock_cfg.z_lower:.4f}):          {len(chuck_cols)}"
    )

    if spikes:
        print("\nFLAGGED HIGH-MRR SPIKE MOVES (Top 15):")
        for sp in spikes[:15]:
            print(
                f"  [Line {sp.line_no:4d}] MRR={sp.mrr_cu_in_min:.4f} in³/min ({sp.mrr_cu_in_min/mean_mrr:.1f}x mean) | "
                f"eff_DOC={sp.eff_doc_in:.4f}\", RPM={sp.rpm:.0f}, F={sp.ipm:.2f} IPM at Z={sp.z:.4f}, R={sp.r:.4f} "
                f"({sp.op_name}) | {sp.raw_line}"
            )

    return sim_final.collisions, promoted_lines, spikes


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    promote = "--no-promote" not in sys.argv[1:]
    cnc_path = args[0] if len(args) > 0 else "/Users/vadimf/Documents/NCs/Spinner.cnc"
    out_png = args[1] if len(args) > 1 else None
    collisions, _, _ = verify_and_promote(cnc_path, out_png, promote=promote)
    stock_cols = [c for c in collisions if c.kind in ("RAPID_STOCK", "HOLDER_STOCK")]
    return 1 if stock_cols else 0


if __name__ == "__main__":
    sys.exit(main())
