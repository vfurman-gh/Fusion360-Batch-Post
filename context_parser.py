"""
JSON Context Merger, Motion Segment Normalizer, and Promotion Injector.

Processes structured JSON context dumps emitted by `universal_dumper.cps`:
1. `merge_context_dumps`: Combines multiple single-tool batch JSON dumps into a
   single unified multi-section context dictionary, re-indexing section IDs
   monotonically (`0..N-1`) while preserving batch provenance.
2. `parse_context_dump`: Converts a single or merged JSON context dictionary into
   canonical metric `MotionSegment` sequences (`mm`, `mm/min`, `sec`) for
   `IVerificationEngine` simulation.
3. `apply_promotions_to_context`: Annotates authorized air moves directly in the
   combined JSON context (`ev["promote"] = True`) while respecting occurrence
   thresholds (`min_occur`) and canned-cycle / synchronized-threading guards.
"""

import copy
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from geometry.models import (
    MotionKind,
    MotionSegment,
    Vector3D,
    VectorKey,
    PromotedVectorRule,
)


def load_context_dump(source: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Load a JSON context dump from a file path or return a deep copy of a dict."""
    if isinstance(source, str):
        with open(source, "r", encoding="utf-8") as f:
            return json.load(f)
    if isinstance(source, dict):
        return copy.deepcopy(source)
    raise TypeError(f"Expected file path or dict, got {type(source)}")


def merge_context_dumps(sources: Sequence[Union[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Merge one or more single-tool `universal_dumper.cps` JSON dumps into a single
    unified multi-tool context dictionary ready for verification and replay.
    """
    if not sources:
        raise ValueError("merge_context_dumps requires at least one source dump")

    dumps = [load_context_dump(s) for s in sources]
    merged = copy.deepcopy(dumps[0])
    merged["sections"] = []

    if not merged.get("maximumSpindleSpeed"):
        for d in dumps:
            if d.get("maximumSpindleSpeed"):
                merged["maximumSpindleSpeed"] = d["maximumSpindleSpeed"]
                break

    global_sec_id = 0
    for batch_idx, d in enumerate(dumps):
        for k, v in d.get("globalParams", {}).items():
            if k not in merged["globalParams"]:
                merged["globalParams"][k] = v

        for sec in d.get("sections", []):
            sec_copy = copy.deepcopy(sec)
            sec_copy["origSecId"] = sec.get("id", 0)
            sec_copy["batchIdx"] = batch_idx
            sec_copy["id"] = global_sec_id
            merged["sections"].append(sec_copy)
            global_sec_id += 1

    return merged


def parse_context_dump(
    source: Union[str, Dict[str, Any]],
    default_rapid_feed_mmpm: float = 5000.0,
) -> List[MotionSegment]:
    """
    Translate a single or merged `universal_dumper.cps` JSON context dump into a
    normalized metric `List[MotionSegment]` for `IVerificationEngine`.
    """
    data = load_context_dump(source)
    # In CAM360/post: IN = 0, MM = 1
    unit = data.get("unit", 0)
    scale = 25.4 if unit == 0 else 1.0

    segments: List[MotionSegment] = []
    segment_id = 0

    # In CAM360/post: FEED_PER_REVOLUTION = 1 (or 2 in turning depending on post),
    # check section feedMode or parameter
    for sec_idx, sec in enumerate(data.get("sections", [])):
        sec_id = sec.get("id", sec_idx)
        batch_idx = sec.get("batchIdx", 0)
        orig_sec_id = sec.get("origSecId", sec_id)
        params = sec.get("params", {})
        op_name = params.get("operation-comment", f"Section_{sec_id}")
        tool_info = sec.get("tool", {})
        tool_id = int(tool_info.get("number", 0))
        spindle_rpm = float(sec.get("initSpindleSpeed", tool_info.get("spindleRPM", 1000.0)) or 1000.0)
        sec_type = sec.get("type", 0)  # 0 = TYPE_MILLING, 1 = TYPE_TURNING
        feed_mode = sec.get("feedMode", 0)

        is_turning = (sec_type == 1) or bool(tool_info.get("_isTurningTool", False))
        is_css = is_turning and (
            tool_info.get("_spindleMode") == 1
            or tool_info.get("spindleMode") == 1
            or bool(params.get("operation:tool_useConstantSurfaceSpeed"))
            or sec.get("spindleMode") == 1
        )
        raw_surface_speed = (
            params.get("operation:tool_surfaceSpeed")
            or tool_info.get("surfaceSpeed")
            or tool_info.get("_surfaceSpeed")
            or sec.get("surfaceSpeed")
            or 0.0
        )
        surface_speed_mmpm = float(raw_surface_speed) * scale

        # Resolve maximum spindle speed
        sec_max_rpm = None
        for cand in (
            params.get("operation:tool_maximumSpindleSpeed"),
            tool_info.get("maximumSpindleSpeed"),
            tool_info.get("_maximumSpindleSpeed"),
            sec.get("maximumSpindleSpeed"),
            data.get("maximumSpindleSpeed"),
            data.get("postProperties", {}).get("maximumSpindleSpeed") if isinstance(data.get("postProperties"), dict) else None,
        ):
            if cand is not None:
                try:
                    v = float(cand)
                    if v > 0:
                        sec_max_rpm = v
                        break
                except (TypeError, ValueError):
                    pass
        max_spindle_rpm = sec_max_rpm if sec_max_rpm is not None else 5000.0

        z_clearance_mm = None
        for k in ("operation:zClearance", "operation:clearanceHeight_value"):
            if k in params and params[k] is not None:
                try:
                    z_clearance_mm = float(params[k]) * scale
                    break
                except (TypeError, ValueError):
                    pass

        z_retract_mm = None
        for k in ("operation:zRetract", "operation:retractHeight_value", "clearance"):
            if k in params and params[k] is not None:
                try:
                    z_retract_mm = float(params[k]) * scale
                    break
                except (TypeError, ValueError):
                    pass

        def _calc_move_feed_and_rpm(
            raw_feed: float,
            s_pos: Vector3D,
            e_pos: Vector3D,
            ev_rpm: Optional[float] = None,
        ) -> Tuple[float, float]:
            feed_mm = float(raw_feed) * scale
            if is_css and surface_speed_mmpm > 0:
                r_eff = 0.5 * (abs(s_pos.x) + abs(e_pos.x))
                if r_eff > 1e-4:
                    calc_rpm = surface_speed_mmpm / (2.0 * math.pi * r_eff)
                    active_rpm = min(max_spindle_rpm, calc_rpm)
                else:
                    active_rpm = max_spindle_rpm
            else:
                active_rpm = float(ev_rpm or spindle_rpm) if ev_rpm is not None else spindle_rpm
                if active_rpm <= 0:
                    active_rpm = 1000.0

            is_feed_per_rev = (
                feed_mode in (1, 3)
                or bool(params.get("operation:tool_useFeedPerRevolution"))
                or (sec_type == 1 and feed_mode != 0 and float(raw_feed) < 2.0)
            )

            if is_feed_per_rev:
                feed_mmpm = feed_mm * active_rpm
            else:
                feed_mmpm = feed_mm

            return feed_mmpm, active_rpm

        def _to_mmpm(raw_feed: float, rpm: float, fm: int) -> float:
            feed_mm = float(raw_feed) * scale
            # In turning (sec_type == 1), feedMode == 1 or small per-rev values when fm != 0
            # Wait: in Fusion Post Kernel, FEED_PER_MINUTE = 0, FEED_PER_REVOLUTION = 1
            if fm == 1 or (sec_type == 1 and fm != 0 and float(raw_feed) < 2.0):
                eff_rpm = rpm if rpm > 0 else 1000.0
                return feed_mm * eff_rpm
            return feed_mm

        for ev_idx, ev in enumerate(sec.get("events", [])):
            ev_type = ev.get("type")
            if ev_type == "onSpindleSpeed":
                spindle_rpm = float(ev.get("s", spindle_rpm) or spindle_rpm)
                continue
            if ev_type == "onFeedMode":
                feed_mode = int(ev.get("fm", feed_mode))
                continue

            if ev_type == "onRapid":
                start_pos = Vector3D(
                    float(ev.get("sx", 0.0)) * scale,
                    float(ev.get("sy", 0.0)) * scale,
                    float(ev.get("sz", 0.0)) * scale,
                )
                end_pos = Vector3D(
                    float(ev.get("x", 0.0)) * scale,
                    float(ev.get("y", 0.0)) * scale,
                    float(ev.get("z", 0.0)) * scale,
                )
                dist = math.hypot(end_pos.x - start_pos.x, end_pos.y - start_pos.y, end_pos.z - start_pos.z)
                if dist < 1e-9:
                    continue
                duration = (dist / max(1.0, default_rapid_feed_mmpm)) * 60.0
                segment_id += 1
                segments.append(
                    MotionSegment(
                        segment_id=segment_id,
                        op_id=op_name,
                        sec_id=sec_id,
                        motion=MotionKind.RAPID,
                        start=start_pos,
                        end=end_pos,
                        feedrate_mmpm=default_rapid_feed_mmpm,
                        spindle_rpm=float(ev.get("rpm", spindle_rpm) or spindle_rpm),
                        duration_sec=duration,
                        vector_key=VectorKey.from_floats(sec_id, start_pos, end_pos),
                        is_arc=False,
                        arc_center=None,
                        arc_plane="ZX" if sec_type == 1 else "XY",
                        metadata={
                            "tool_id": tool_id,
                            "in_cycle": False,
                            "batch_idx": batch_idx,
                            "batch_sec_id": orig_sec_id,
                            "event_idx": ev_idx,
                            "z_clearance_mm": z_clearance_mm,
                            "z_retract_mm": z_retract_mm,
                            "sec_type": sec_type,
                        },
                    )
                )

            elif ev_type == "onLinear":
                start_pos = Vector3D(
                    float(ev.get("sx", 0.0)) * scale,
                    float(ev.get("sy", 0.0)) * scale,
                    float(ev.get("sz", 0.0)) * scale,
                )
                end_pos = Vector3D(
                    float(ev.get("x", 0.0)) * scale,
                    float(ev.get("y", 0.0)) * scale,
                    float(ev.get("z", 0.0)) * scale,
                )
                dist = math.hypot(end_pos.x - start_pos.x, end_pos.y - start_pos.y, end_pos.z - start_pos.z)
                if dist < 1e-9:
                    continue
                raw_ev_rpm = float(ev.get("rpm", 0.0)) if ev.get("rpm") else None
                feed_mmpm, rpm = _calc_move_feed_and_rpm(float(ev.get("feed", 0.0)), start_pos, end_pos, raw_ev_rpm)
                duration = (dist / max(1.0, feed_mmpm)) * 60.0 if feed_mmpm > 0 else 0.0
                segment_id += 1
                segments.append(
                    MotionSegment(
                        segment_id=segment_id,
                        op_id=op_name,
                        sec_id=sec_id,
                        motion=MotionKind.LINEAR,
                        start=start_pos,
                        end=end_pos,
                        feedrate_mmpm=feed_mmpm,
                        spindle_rpm=rpm,
                        duration_sec=duration,
                        vector_key=VectorKey.from_floats(sec_id, start_pos, end_pos),
                        is_arc=False,
                        arc_center=None,
                        arc_plane="ZX" if sec_type == 1 else "XY",
                        metadata={
                            "tool_id": tool_id,
                            "in_cycle": bool(ev.get("sync", False)),
                            "movement": ev.get("movement", 1),
                            "batch_idx": batch_idx,
                            "batch_sec_id": orig_sec_id,
                            "event_idx": ev_idx,
                            "z_clearance_mm": z_clearance_mm,
                            "z_retract_mm": z_retract_mm,
                            "sec_type": sec_type,
                        },
                    )
                )

            elif ev_type == "onCircular":
                start_pos = Vector3D(
                    float(ev.get("sx", 0.0)) * scale,
                    float(ev.get("sy", 0.0)) * scale,
                    float(ev.get("sz", 0.0)) * scale,
                )
                end_pos = Vector3D(
                    float(ev.get("x", 0.0)) * scale,
                    float(ev.get("y", 0.0)) * scale,
                    float(ev.get("z", 0.0)) * scale,
                )
                center_pos = Vector3D(
                    float(ev.get("cx", 0.0)) * scale,
                    float(ev.get("cy", 0.0)) * scale,
                    float(ev.get("cz", 0.0)) * scale,
                )
                raw_ev_rpm = float(ev.get("rpm", 0.0)) if ev.get("rpm") else None
                feed_mmpm, rpm = _calc_move_feed_and_rpm(float(ev.get("feed", 0.0)), start_pos, end_pos, raw_ev_rpm)
                radius_mm = float(ev.get("radius", 0.0)) * scale
                sweep_rad = abs(float(ev.get("sweep", 0.0)))
                chord = math.hypot(end_pos.x - start_pos.x, end_pos.y - start_pos.y, end_pos.z - start_pos.z)
                dist = max(chord, radius_mm * sweep_rad)
                duration = (dist / max(1.0, feed_mmpm)) * 60.0 if feed_mmpm > 0 else 0.0
                cw = bool(ev.get("cw", True))
                motion = MotionKind.ARC_CW if cw else MotionKind.ARC_CCW

                plane_idx = int(ev.get("plane", 0))
                plane_str = {0: "XY", 1: "ZX", 2: "YZ"}.get(plane_idx, "ZX" if sec_type == 1 else "XY")
                segment_id += 1
                segments.append(
                    MotionSegment(
                        segment_id=segment_id,
                        op_id=op_name,
                        sec_id=sec_id,
                        motion=motion,
                        start=start_pos,
                        end=end_pos,
                        feedrate_mmpm=feed_mmpm,
                        spindle_rpm=rpm,
                        duration_sec=duration,
                        vector_key=VectorKey.from_floats(sec_id, start_pos, end_pos),
                        is_arc=True,
                        arc_center=center_pos,
                        arc_plane=plane_str,
                        metadata={
                            "tool_id": tool_id,
                            "in_cycle": False,
                            "batch_idx": batch_idx,
                            "batch_sec_id": orig_sec_id,
                            "event_idx": ev_idx,
                        },
                    )
                )

            elif ev_type == "onCyclePoint":
                # Use pre-captured expandedMoves to simulate full physical hole/cycle trajectory in IPW
                for sub_mv in ev.get("expandedMoves", []):
                    sub_t = sub_mv.get("type")
                    if sub_t not in ("onRapid", "onLinear"):
                        continue
                    s_pos = Vector3D(
                        float(sub_mv.get("sx", 0.0)) * scale,
                        float(sub_mv.get("sy", 0.0)) * scale,
                        float(sub_mv.get("sz", 0.0)) * scale,
                    )
                    e_pos = Vector3D(
                        float(sub_mv.get("x", 0.0)) * scale,
                        float(sub_mv.get("y", 0.0)) * scale,
                        float(sub_mv.get("z", 0.0)) * scale,
                    )
                    is_rap = (sub_t == "onRapid")
                    if is_rap:
                        f_mmpm = default_rapid_feed_mmpm
                        rpm = float(sub_mv.get("rpm", spindle_rpm) or spindle_rpm)
                    else:
                        raw_ev_rpm = float(sub_mv.get("rpm", 0.0)) if sub_mv.get("rpm") else None
                        f_mmpm, rpm = _calc_move_feed_and_rpm(float(sub_mv.get("feed", 0.0)), s_pos, e_pos, raw_ev_rpm)
                    dist = math.hypot(e_pos.x - s_pos.x, e_pos.y - s_pos.y, e_pos.z - s_pos.z)
                    duration = (dist / max(1.0, f_mmpm)) * 60.0 if f_mmpm > 0 else 0.0
                    segment_id += 1
                    segments.append(
                        MotionSegment(
                            segment_id=segment_id,
                            op_id=op_name,
                            sec_id=sec_id,
                            motion=MotionKind.RAPID if is_rap else MotionKind.LINEAR,
                            start=s_pos,
                            end=e_pos,
                            feedrate_mmpm=f_mmpm,
                            spindle_rpm=rpm,
                            duration_sec=duration,
                            vector_key=VectorKey.from_floats(sec_id, s_pos, e_pos),
                            is_arc=False,
                            arc_center=None,
                            arc_plane="ZX" if sec_type == 1 else "XY",
                            metadata={
                                "tool_id": tool_id,
                                "in_cycle": True,
                                "batch_idx": batch_idx,
                                "batch_sec_id": orig_sec_id,
                                "event_idx": ev_idx,
                            },
                        )
                    )

            elif ev_type == "onDwell":
                dwell_sec = float(ev.get("sec", 0.0))
                if segments:
                    cur_p = segments[-1].end
                else:
                    init_p = sec.get("initialPosition", {"x": 0.0, "y": 0.0, "z": 0.0})
                    cur_p = Vector3D(float(init_p["x"]) * scale, float(init_p["y"]) * scale, float(init_p["z"]) * scale)
                segment_id += 1
                segments.append(
                    MotionSegment(
                        segment_id=segment_id,
                        op_id=op_name,
                        sec_id=sec_id,
                        motion=MotionKind.DWELL,
                        start=cur_p,
                        end=cur_p,
                        feedrate_mmpm=0.0,
                        spindle_rpm=spindle_rpm,
                        duration_sec=dwell_sec,
                        vector_key=VectorKey.from_floats(sec_id, cur_p, cur_p),
                        is_arc=False,
                        arc_center=None,
                        metadata={
                            "tool_id": tool_id,
                            "in_cycle": False,
                            "batch_idx": batch_idx,
                            "batch_sec_id": orig_sec_id,
                            "event_idx": ev_idx,
                        },
                    )
                )

    return segments


def apply_promotions_to_context(
    context_data: Dict[str, Any],
    promoted_vectors: Dict[str, Union[PromotedVectorRule, Dict[str, Any]]],
) -> int:
    """
    Annotate authorized `onLinear` moves directly inside `context_data` with
    `ev["promote"] = True`, respecting `min_occur` occurrence thresholds and
    skipping moves inside canned cycles or speed-feed synchronization.

    Returns the total number of individual `onLinear` move events promoted.
    """
    if not promoted_vectors:
        return 0

    unit = context_data.get("unit", 0)
    scale = 25.4 if unit == 0 else 1.0
    occur_counts: Dict[str, int] = {}
    promoted_count = 0

    for sec_idx, sec in enumerate(context_data.get("sections", [])):
        sec_id = sec.get("id", sec_idx)
        orig_sec_id = sec.get("origSecId", sec_id)
        in_cycle = False

        for ev in sec.get("events", []):
            ev_type = ev.get("type")
            if ev_type == "onCycle":
                in_cycle = True
                continue
            if ev_type == "onCycleEnd":
                in_cycle = False
                continue

            if ev_type == "onLinear" and not in_cycle and not ev.get("sync", False):
                s_pos = Vector3D(
                    float(ev.get("sx", 0.0)) * scale,
                    float(ev.get("sy", 0.0)) * scale,
                    float(ev.get("sz", 0.0)) * scale,
                )
                e_pos = Vector3D(
                    float(ev.get("x", 0.0)) * scale,
                    float(ev.get("y", 0.0)) * scale,
                    float(ev.get("z", 0.0)) * scale,
                )
                v_key = VectorKey.from_floats(sec_id, s_pos, e_pos).to_wrapper_str()
                rule = promoted_vectors.get(v_key)
                if rule is None and orig_sec_id != sec_id:
                    alt_key = VectorKey.from_floats(orig_sec_id, s_pos, e_pos).to_wrapper_str()
                    rule = promoted_vectors.get(alt_key)
                    if rule is not None:
                        v_key = alt_key

                if rule is not None:
                    occ = occur_counts.get(v_key, 0) + 1
                    occur_counts[v_key] = occ
                    if isinstance(rule, PromotedVectorRule):
                        min_occ = rule.min_occur
                    elif isinstance(rule, dict):
                        min_occ = int(rule.get("minOccur", rule.get("min_occur", 1)))
                    else:
                        min_occ = 1

                    if occ >= min_occ:
                        ev["promote"] = True
                        promoted_count += 1

    return promoted_count


def extract_workpiece_from_context(
    context_data: Dict[str, Any],
    fallback_workpiece: Any = None,
    is_turning: Optional[bool] = None,
) -> Any:
    """
    Construct a metric `WorkpieceDefinition` (`mm`) from `context_data["workpiece"]`
    (and chuck obstacles / stock dimensions from globalParams if turning).
    Supports:
    - Turning: AxisymmetricStock2D with AxisymmetricChuckObstacle fixtures.
    - Milling: BoxStock3D (prismatic stock bounding box).
    """
    from geometry.models import (
        AxisymmetricChuckObstacle,
        AxisymmetricStock2D,
        BoxStock3D,
        WorkpieceDefinition,
    )

    def _dbg_ctx(msg: str) -> None:
        try:
            from PostProcessAll import _dbg_global
            _dbg_global(msg)
        except Exception:
            pass

    stock_mode = str(
        context_data.get("stockMode")
        or context_data.get("globalParams", {}).get("job:stockMode")
        or context_data.get("globalParams", {}).get("stockMode")
        or ""
    ).lower()
    _dbg_ctx(f"extract_workpiece_from_context: stock_mode='{stock_mode}', fallback={fallback_workpiece}")
    if any(k in stock_mode for k in ("previous", "preceding", "prev", "rest")):
        return None
    if any(k in stock_mode for k in ("solid", "body")):
        return fallback_workpiece

    wp = context_data.get("workpiece")
    if not wp or not isinstance(wp, dict):
        stock_data = context_data.get("stock")
        if isinstance(stock_data, dict):
            if "lower" in stock_data and "upper" in stock_data:
                wp = stock_data
            elif "x_min" in stock_data:
                wp = {
                    "lower": {
                        "x": stock_data.get("x_min", 0.0),
                        "y": stock_data.get("y_min", 0.0),
                        "z": stock_data.get("z_min", 0.0),
                    },
                    "upper": {
                        "x": stock_data.get("x_max", 0.0),
                        "y": stock_data.get("y_max", 0.0),
                        "z": stock_data.get("z_max", 0.0),
                    },
                }
    if not wp or not isinstance(wp, dict):
        return fallback_workpiece

    lower = wp.get("lower") or {}
    upper = wp.get("upper") or {}
    unit = context_data.get("unit", 0)
    scale = 25.4 if unit == 0 else 1.0

    if is_turning is None:
        # Auto-detect turning if any section is turning type (1) or has turning tool
        is_turning = any(
            sec.get("type") == 1 or bool(sec.get("tool", {}).get("_isTurningTool", False))
            for sec in context_data.get("sections", [])
        )

    if not is_turning:
        x_min = float(lower.get("x", 0.0)) * scale
        x_max = float(upper.get("x", 0.0)) * scale
        y_min = float(lower.get("y", 0.0)) * scale
        y_max = float(upper.get("y", 0.0)) * scale
        z_min = float(lower.get("z", 0.0)) * scale
        z_max = float(upper.get("z", 0.0)) * scale
        if x_max <= x_min + 1e-6 or y_max <= y_min + 1e-6 or z_max <= z_min + 1e-6:
            return fallback_workpiece
        return WorkpieceDefinition(
            stock=BoxStock3D(
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
                z_min=z_min,
                z_max=z_max,
            ),
            fixtures=[],
        )

    z_min = float(lower.get("z", 0.0)) * scale
    z_max = float(upper.get("z", 0.0)) * scale
    r_outer = max(
        abs(float(upper.get("x", 0.0))),
        abs(float(lower.get("x", 0.0))),
        abs(float(upper.get("y", 0.0))),
        abs(float(lower.get("y", 0.0))),
    ) * scale

    if r_outer <= 1e-6 or z_max <= z_min + 1e-6:
        return fallback_workpiece

    # Inner radius from globalParams or fallback_workpiece
    r_inner = 0.0
    g_params = context_data.get("globalParams") or {}
    for k in ("stock-diameter-inner", "job:stockDiameterInner"):
        if k in g_params and g_params[k]:
            try:
                r_inner = float(g_params[k]) * 0.5 * scale
                break
            except (TypeError, ValueError):
                pass

    fb_stock = getattr(fallback_workpiece, "stock", None) if fallback_workpiece else None
    if r_inner <= 0.0 and fb_stock is not None and hasattr(fb_stock, "inner_radius"):
        fb_r_in = float(fb_stock.inner_radius)
        if 0.0 < fb_r_in < r_outer:
            r_inner = fb_r_in

    fixtures = []
    fb_fixtures = getattr(fallback_workpiece, "fixtures", None) if fallback_workpiece else None
    if fb_fixtures:
        dz = (z_min - float(fb_stock.z_min)) if (fb_stock is not None and hasattr(fb_stock, "z_min")) else 0.0
        for f in fb_fixtures:
            if hasattr(f, "z_min") and hasattr(f, "z_max"):
                fixtures.append(
                    AxisymmetricChuckObstacle(
                        z_min=float(f.z_min) + dz,
                        z_max=float(f.z_max) + dz,
                        outer_radius=max(float(getattr(f, "outer_radius", r_outer + 30.0)), r_outer + 10.0),
                        clamping_radius=r_outer,
                    )
                )
            else:
                fixtures.append(f)

    return WorkpieceDefinition(
        stock=AxisymmetricStock2D(
            z_min=z_min,
            z_max=z_max,
            outer_radius=r_outer,
            inner_radius=r_inner,
        ),
        fixtures=fixtures,
    )


def extract_tools_from_context(
    context_data: Dict[str, Any],
    fallback_tools: Any = None,
    is_turning: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Construct exact metric `ToolAssembly` objects (`mm`) from the kernel-provided
    profiles in `context_data["sections"]`.
    - Turning operations: LatheToolGeometry using 2D SVG profiles (_cutterSVG / _holderSVG).
    - Milling operations: MillToolGeometry using parametric tool dimensions.
    """
    from geometry.models import (
        LatheToolGeometry,
        MillToolGeometry,
        ToolAssembly,
        ToolCompensationQuadrant,
    )
    from geometry.legacy_adapter import transform_tool_svg

    unit = context_data.get("unit", 0)
    scale = 25.4 if unit == 0 else 1.0
    tools_by_op: Dict[str, Any] = dict(fallback_tools) if fallback_tools else {}

    for sec_idx, sec in enumerate(context_data.get("sections", [])):
        sec_id = sec.get("id", sec_idx)
        sec_type = sec.get("type", 0)
        params = sec.get("params") or {}
        op_name = params.get("operation-comment", f"Section_{sec_id}")
        t = sec.get("tool") or {}
        cutter_svg = t.get("_cutterSVG", "")
        holder_svg = t.get("_holderSVG", "")

        is_turning_tool = bool(t.get("_isTurningTool", False)) or (sec_type == 1)
        if is_turning is False:
            is_turning_tool = False
        elif is_turning is True:
            is_turning_tool = True

        if not is_turning_tool or not cutter_svg:
            # Construct MillToolGeometry for milling tools
            dia = float(t.get("diameter", 0.0) or 0.0) * scale
            cr = float(t.get("cornerRadius", t.get("corner_radius", t.get("noseRadius", 0.0))) or 0.0) * scale
            flute_l = float(t.get("fluteLength", t.get("flute_length", 0.0)) or 0.0) * scale
            overall_l = float(t.get("overallLength", t.get("overall_length", 0.0)) or 0.0) * scale
            raw_taper = t.get("_taperAngle", t.get("taperAngle", t.get("taper_angle", 0.0))) or 0.0
            taper_val = float(raw_taper)
            if 0.0 < taper_val <= math.pi:
                taper_a = math.degrees(taper_val)
            else:
                taper_a = taper_val
            holder_d = float(t.get("holderDiameter", t.get("holder_diameter", 40.0)) or 40.0) * scale
            holder_l = float(t.get("holderLength", t.get("holder_length", 60.0)) or 60.0) * scale
            mill_geom = MillToolGeometry(
                diameter=dia,
                corner_radius=cr,
                flute_length=flute_l,
                overall_length=overall_l,
                taper_angle_deg=taper_a,
                holder_diameter=holder_d,
                holder_length=holder_l,
            )
            tools_by_op[op_name] = ToolAssembly(
                tool_id=int(t.get("number", 0) or 0),
                name=op_name,
                geometry=mill_geom,
            )
            continue

        try:
            from shapely.affinity import scale as shapely_scale
        except (ImportError, OSError):
            continue

        comp_d = t.get("_compDisplacement") or {"x": 0.0, "y": 0.0}
        comp = (float(comp_d.get("x", 0.0)), float(comp_d.get("y", 0.0)))
        if "operation:toolOrientation" in params:
            orient_deg = float(params["operation:toolOrientation"])
        else:
            orient_deg = float(sec.get("toolOrientation", 0.0) or 0.0) * 180.0 / math.pi

        ttype = str(t.get("_toolTypeName", "") or "")
        desc = str(t.get("_description", "") or t.get("description", "") or "")
        is_boring = (
            "boring" in ttype.lower()
            or "bore" in ttype.lower()
            or "boring" in desc.lower()
            or "bore" in op_name.lower()
        )
        if not ttype and is_boring:
            ttype = "boring turning"

        c_poly = transform_tool_svg(cutter_svg, orient_deg, comp, "REAR", tool_type=ttype)
        if c_poly.is_empty:
            continue
        h_poly = transform_tool_svg(holder_svg, orient_deg, comp, "REAR", tool_type=ttype) if holder_svg else c_poly.difference(c_poly)

        c_poly_mm = shapely_scale(c_poly, xfact=scale, yfact=scale, origin=(0, 0))
        h_poly_mm = shapely_scale(h_poly, xfact=scale, yfact=scale, origin=(0, 0)) if not h_poly.is_empty else h_poly
        h_clean_mm = h_poly_mm.difference(c_poly_mm.buffer(0.0015 * scale)) if not h_poly_mm.is_empty else h_poly_mm

        geom = LatheToolGeometry(
            insert_shape=ttype or "rhombic",
            nose_radius=float(t.get("noseRadius", t.get("cornerRadius", 0.0)) or 0.0) * scale,
            hand=str(t.get("hand", "R") or "R"),
            quadrant=ToolCompensationQuadrant.P3,
            cutter_polygon=c_poly_mm,
            holder_polygon=h_clean_mm,
            is_boring=is_boring,
        )
        tools_by_op[op_name] = ToolAssembly(
            tool_id=int(t.get("number", 0) or 0),
            name=op_name,
            geometry=geom,
        )

    return tools_by_op
