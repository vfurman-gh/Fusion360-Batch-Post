#!/usr/bin/env python3
"""
Solid and Mesh Stock Processing for Milling (3D Heightfield) and Turning (2D IPW).

Extracts, transforms, and converts solid B-Rep and MeshBody stock geometry
from Autodesk Fusion 360 CAM Setups into simulation-ready verification structures.
"""

import math
import sys
from typing import Any, List, Optional, Sequence, Tuple

try:
    import numpy as np
except (ImportError, OSError):
    np = None

try:
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
except (ImportError, OSError):
    Polygon = Any
    box = None
    unary_union = None

from geometry.models import (
    AxisymmetricChuckObstacle,
    AxisymmetricStock2D,
    SolidMeshStock3D,
    WorkpieceDefinition,
)


def _dbg(msg: str) -> None:
    """Internal debug logging helper that forwards to PostProcessAll._dbg_global if available."""
    try:
        from PostProcessAll import _dbg_global
        _dbg_global(msg)
    except Exception:
        pass


def transform_design_points_to_setup_wcs(
    points_cm: np.ndarray,
    wcs: Any,
) -> np.ndarray:
    """
    Transform point coordinates from Fusion 360 design space (cm) to Setup WCS (mm).

    Args:
        points_cm: (N, 3) numpy array of coordinates in design space (cm)
        wcs: adsk.core.Matrix3D representing setup.workCoordinateSystem

    Returns:
        (N, 3) numpy array of coordinates in setup WCS (mm)
    """
    if points_cm is None or len(points_cm) == 0:
        return np.empty((0, 3), dtype=np.float32)

    pts = np.asarray(points_cm, dtype=np.float64)
    if wcs is None:
        return (pts * 10.0).astype(np.float32)

    pts_setup_cm = None

    # Strategy 1: Invert Matrix3D directly (exact Autodesk Matrix3D transformation)
    # setup.workCoordinateSystem transforms from Setup WCS to Design space.
    # Its inverse transforms from Design space to Setup WCS.
    if hasattr(wcs, "copy") and hasattr(wcs, "invert"):
        try:
            inv_m = wcs.copy()
            if inv_m.invert():
                if hasattr(inv_m, "getCell"):
                    # M[r, c] is row r, column c of 4x4 matrix
                    # x' = M00*x + M01*y + M02*z + M03, etc.
                    M = np.array([[inv_m.getCell(r, c) for c in range(4)] for r in range(4)], dtype=np.float64)
                    pts_setup_cm = pts @ M[:3, :3].T + M[:3, 3]
                    _dbg(f"transform_design_points_to_setup_wcs: inverted via Matrix3D.getCell. Origin shift: {M[:3, 3]}")
                elif hasattr(inv_m, "asArray"):
                    arr = np.array(inv_m.asArray(), dtype=np.float64)
                    if arr.size == 16:
                        # Row-major matrix has bottom row [0, 0, 0, 1]
                        if abs(arr[15] - 1.0) < 1e-4 and abs(arr[12]) < 1e-4 and abs(arr[13]) < 1e-4 and abs(arr[14]) < 1e-4:
                            M = arr.reshape((4, 4))
                        else:
                            M = arr.reshape((4, 4)).T
                        pts_setup_cm = pts @ M[:3, :3].T + M[:3, 3]
                        _dbg("transform_design_points_to_setup_wcs: inverted via Matrix3D.asArray.")
        except Exception as inv_err:
            _dbg(f"transform_design_points_to_setup_wcs: Matrix3D.invert error: {inv_err}")

    # Strategy 2: getAsCoordinateSystem (handles 4-tuple and 5-tuple SWIG returns)
    if pts_setup_cm is None and hasattr(wcs, "getAsCoordinateSystem"):
        try:
            res = wcs.getAsCoordinateSystem()
            if isinstance(res, (tuple, list)):
                if len(res) == 5:
                    _, origin_pt, x_vec, y_vec, z_vec = res
                elif len(res) == 4:
                    origin_pt, x_vec, y_vec, z_vec = res
                else:
                    raise ValueError(f"Unexpected getAsCoordinateSystem tuple length: {len(res)}")
            else:
                raise TypeError(f"getAsCoordinateSystem returned unexpected type: {type(res)}")

            origin = np.array([origin_pt.x, origin_pt.y, origin_pt.z], dtype=np.float64)
            R = np.array(
                [
                    [x_vec.x, y_vec.x, z_vec.x],
                    [x_vec.y, y_vec.y, z_vec.y],
                    [x_vec.z, y_vec.z, z_vec.z],
                ],
                dtype=np.float64,
            )
            # P_setup_cm = (P_design_cm - origin) @ R
            pts_setup_cm = (pts - origin) @ R
            _dbg(f"transform_design_points_to_setup_wcs: transformed via getAsCoordinateSystem. Origin: {origin}")
        except Exception as cs_err:
            _dbg(f"transform_design_points_to_setup_wcs: getAsCoordinateSystem error: {cs_err}")

    if pts_setup_cm is None:
        _dbg("WARNING: transform_design_points_to_setup_wcs: Failed to apply WCS transform, falling back to untransformed points!")
        pts_setup_cm = pts

    # Convert cm to mm (Fusion internal distance unit is cm)
    return (pts_setup_cm * 10.0).astype(np.float32)


def extract_mesh_from_fusion_setup(setup: Any) -> Tuple[np.ndarray, np.ndarray]:
    """
    Extract triangular mesh from setup.stockSolids in setup WCS (mm).

    Supports both adsk.fusion.BRepBody (via TriangleMeshCalculator) and
    adsk.fusion.MeshBody (via displayMesh).

    Returns:
        vertices_mm: (N, 3) float32 numpy array in setup WCS (mm)
        indices: (M, 3) int32 numpy array of triangle indices
    """
    if np is None:
        raise ImportError("numpy is required for solid stock extraction")

    if setup is None:
        raise ValueError("Cannot extract stock from None setup")

    setup_name = getattr(setup, "name", "unnamed")
    solids = getattr(setup, "stockSolids", None)
    cnt = getattr(solids, "count", 0) if solids is not None else 0
    _dbg(f"extract_mesh_from_fusion_setup: setup='{setup_name}', stockSolids count={cnt}")

    if solids is None:
        raise ValueError(f"Setup '{setup_name}' has no stockSolids property")

    # Safely collect solid bodies from ObjectCollection, list, or iterable
    bodies = []
    cnt_val = getattr(solids, "count", None)
    if isinstance(cnt_val, int):
        for i in range(cnt_val):
            b = solids.item(i)
            if b is not None:
                bodies.append(b)
    elif hasattr(solids, "__iter__"):
        bodies = [b for b in solids if b is not None]

    if not bodies:
        raise ValueError(
            f"Setup '{setup_name}' uses 'From Solid' stock, "
            "but no stock bodies are selected in the setup."
        )

    all_verts_cm = []
    all_indices = []
    vert_offset = 0

    adsk_mod = sys.modules.get("adsk")
    quality_opt = None
    if adsk_mod and hasattr(adsk_mod, "fusion") and hasattr(adsk_mod.fusion, "TriangleMeshQualityOptions"):
        quality_opt = getattr(adsk_mod.fusion.TriangleMeshQualityOptions, "NormalQualityTriangleMesh", None)

    for idx_b, body in enumerate(bodies):
        b_name = getattr(body, "name", f"Body_{idx_b}")
        b_type = type(body).__name__
        _dbg(f"extract_mesh_from_fusion_setup: reading body[{idx_b}] '{b_name}' ({b_type})")
        mesh = None

        # 1. BRepBody tessellation via meshManager
        mesh_mgr = getattr(body, "meshManager", None)
        if mesh_mgr is not None and hasattr(mesh_mgr, "createMeshCalculator"):
            try:
                calc = mesh_mgr.createMeshCalculator()
                if calc is not None:
                    if quality_opt is not None and hasattr(calc, "setQuality"):
                        try:
                            calc.setQuality(quality_opt)
                        except Exception:
                            pass
                    mesh = calc.calculate()
            except Exception as mm_err:
                _dbg(f"extract_mesh_from_fusion_setup: createMeshCalculator error for '{b_name}': {mm_err}")
                mesh = None

            if mesh is None and hasattr(mesh_mgr, "displayMeshes"):
                try:
                    d_meshes = mesh_mgr.displayMeshes
                    if d_meshes and getattr(d_meshes, "count", 0) > 0:
                        mesh = d_meshes.item(0)
                except Exception:
                    mesh = None

        # 2. MeshBody via displayMesh or mesh property
        if mesh is None:
            for attr in ("displayMesh", "mesh"):
                m = getattr(body, attr, None)
                if m is not None and hasattr(m, "nodeCoordinatesAsDouble"):
                    mesh = m
                    break

        if mesh is None:
            _dbg(f"extract_mesh_from_fusion_setup: could not obtain mesh for body '{b_name}'")
            continue

        try:
            coords = mesh.nodeCoordinatesAsDouble
            node_indices = mesh.nodeIndices
            if coords and node_indices:
                v = np.array(coords, dtype=np.float64).reshape(-1, 3)
                idx = np.array(node_indices, dtype=np.int32).reshape(-1, 3)
                all_verts_cm.append(v)
                all_indices.append(idx + vert_offset)
                vert_offset += len(v)
                _dbg(f"extract_mesh_from_fusion_setup: extracted {len(v)} vertices, {len(idx)} triangles from '{b_name}'")
        except Exception as extract_err:
            _dbg(f"extract_mesh_from_fusion_setup: node extraction error for '{b_name}': {extract_err}")

    if not all_verts_cm:
        raise ValueError(
            f"Failed to extract 3D mesh triangles from stock solids of setup '{setup_name}'."
        )

    merged_verts_cm = np.vstack(all_verts_cm)
    merged_indices = np.vstack(all_indices)
    _dbg(f"extract_mesh_from_fusion_setup: raw merged design coords (cm): min={merged_verts_cm.min(axis=0)}, max={merged_verts_cm.max(axis=0)}")

    # Transform coordinates from Fusion design space (cm) to setup WCS (mm)
    wcs = getattr(setup, "workCoordinateSystem", None)
    vertices_mm = transform_design_points_to_setup_wcs(merged_verts_cm, wcs)
    _dbg(f"extract_mesh_from_fusion_setup: setup WCS coords (mm): min={vertices_mm.min(axis=0)}, max={vertices_mm.max(axis=0)}")

    return vertices_mm, merged_indices


def rasterize_triangles_to_grid(
    grid: Any,
    vertices: np.ndarray,
    indices: np.ndarray,
    tol_pad_z_mm: float = 0.05,
) -> None:
    """
    Rasterize 3D triangular mesh facets onto a 2.5D HeightfieldGrid.

    For each grid cell (row, col) overlapping a triangle, sets:
        grid.H[row, col] = max(grid.H[row, col], interpolated_z + tol_pad_z_mm)

    Args:
        grid: HeightfieldGrid instance
        vertices: (N, 3) float32 array in setup WCS (mm)
        indices: (M, 3) int32 triangle vertex indices
        tol_pad_z_mm: Upward padding (mm) to guarantee chordal conservatism
    """
    if vertices.size == 0 or indices.size == 0:
        return

    cell_size = grid.cell_size_mm
    gx_min = grid.grid_x_min
    gy_min = grid.grid_y_min
    nx = grid.nx
    ny = grid.ny

    # Extract triangle vertices: (M, 3, 3)
    t_verts = vertices[indices]  # M triangles, 3 vertices each, (x, y, z)

    # 1. Update vertex cells directly to ensure small features and vertices are always captured
    v_cols = np.clip(np.floor((vertices[:, 0] - gx_min) / cell_size).astype(int), 0, nx - 1)
    v_rows = np.clip(np.floor((vertices[:, 1] - gy_min) / cell_size).astype(int), 0, ny - 1)
    v_z = vertices[:, 2] + tol_pad_z_mm
    for r, c, z in zip(v_rows, v_cols, v_z):
        if z > grid.H[r, c]:
            grid.H[r, c] = z

    # 2. Rasterize each triangle
    for v0, v1, v2 in t_verts:
        x0, y0, z0 = v0
        x1, y1, z1 = v1
        x2, y2, z2 = v2

        # 2D cross product normal in XY
        nx_val = (y1 - y0) * (z2 - z0) - (z1 - z0) * (y2 - y0)
        ny_val = (z1 - z0) * (x2 - x0) - (x1 - x0) * (z2 - z0)
        nz_val = (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)

        t_min_x = min(x0, x1, x2)
        t_max_x = max(x0, x1, x2)
        t_min_y = min(y0, y1, y2)
        t_max_y = max(y0, y1, y2)

        c_min = max(0, int(math.floor((t_min_x - gx_min) / cell_size)))
        c_max = min(nx - 1, int(math.ceil((t_max_x - gx_min) / cell_size)))
        r_min = max(0, int(math.floor((t_min_y - gy_min) / cell_size)))
        r_max = min(ny - 1, int(math.ceil((t_max_y - gy_min) / cell_size)))

        if c_max < c_min or r_max < r_min:
            continue

        # Fast path: triangle fits inside a single cell
        if c_min == c_max and r_min == r_max:
            top_z = max(z0, z1, z2) + tol_pad_z_mm
            if top_z > grid.H[r_min, c_min]:
                grid.H[r_min, c_min] = top_z
            continue

        # Near-vertical triangles: interpolate edge segments
        if abs(nz_val) < 1e-7:
            max_edge_z = max(z0, z1, z2) + tol_pad_z_mm
            for pa, pb in ((v0, v1), (v1, v2), (v2, v0)):
                dx = pb[0] - pa[0]
                dy = pb[1] - pa[1]
                dz = pb[2] - pa[2]
                length = math.hypot(dx, dy)
                steps = max(1, int(math.ceil(length / (cell_size * 0.5))))
                for s in range(steps + 1):
                    frac = s / steps
                    px = pa[0] + frac * dx
                    py = pa[1] + frac * dy
                    pz = pa[2] + frac * dz + tol_pad_z_mm
                    c_idx = int(math.floor((px - gx_min) / cell_size))
                    r_idx = int(math.floor((py - gy_min) / cell_size))
                    if 0 <= c_idx < nx and 0 <= r_idx < ny:
                        if pz > grid.H[r_idx, c_idx]:
                            grid.H[r_idx, c_idx] = pz
            continue

        # General non-vertical triangle: compute cell centers in bounding box
        cols = np.arange(c_min, c_max + 1)
        rows = np.arange(r_min, r_max + 1)
        grid_x = gx_min + (cols + 0.5) * cell_size
        grid_y = gy_min + (rows + 0.5) * cell_size

        X, Y = np.meshgrid(grid_x, grid_y)

        # Edge function test for point-in-triangle (2D cross product: V_edge x V_point)
        e01 = (x1 - x0) * (Y - y0) - (y1 - y0) * (X - x0)
        e12 = (x2 - x1) * (Y - y1) - (y2 - y1) * (X - x1)
        e20 = (x0 - x2) * (Y - y2) - (y0 - y2) * (X - x2)

        eps = 1e-4
        inside = ((e01 >= -eps) & (e12 >= -eps) & (e20 >= -eps)) | ((e01 <= eps) & (e12 <= eps) & (e20 <= eps))

        if not np.any(inside):
            continue

        # Plane equation: N . (P - V0) = 0 => Z = z0 - (Nx*(X - x0) + Ny*(Y - y0)) / Nz
        Z = z0 - (nx_val * (X - x0) + ny_val * (Y - y0)) / nz_val + tol_pad_z_mm

        sub_h = grid.H[r_min : r_max + 1, c_min : c_max + 1]
        mask = inside & (Z > sub_h)
        sub_h[mask] = Z[mask]


def revolve_mesh_to_lathe_stock(
    vertices: np.ndarray,
    indices: np.ndarray,
    z_slice_step_mm: float = 0.5,
    tol_pad_r_mm: float = 0.05,
    max_radial_offset_mm: float = 1.0,
    fixtures: Optional[List[AxisymmetricChuckObstacle]] = None,
) -> WorkpieceDefinition:
    """
    Compute axisymmetric revolution profile in (Z, R) plane for turning setups.

    Validates that the solid is centered on the spindle axis (Z axis, R = sqrt(X^2 + Y^2)).
    Generates a conservative superset envelope polygon in the half-plane R >= 0.

    Returns:
        WorkpieceDefinition with AxisymmetricStock2D stock.
    """
    if np is None:
        raise ImportError("numpy is required for revolving lathe stock")

    if vertices.size == 0 or indices.size == 0:
        raise ValueError("Cannot revolve empty mesh to lathe stock")

    v = np.asarray(vertices, dtype=np.float32)

    # 1. Spindle axis centering verification
    x_min, y_min, z_min = v.min(axis=0)
    x_max, y_max, z_max = v.max(axis=0)

    center_x = (x_min + x_max) * 0.5
    center_y = (y_min + y_max) * 0.5
    offset_from_spindle = math.hypot(center_x, center_y)

    if offset_from_spindle > max_radial_offset_mm:
        raise ValueError(
            f"Solid stock is not centered on the turning spindle axis (Z-axis offset is {offset_from_spindle:.2f} mm, "
            f"exceeding maximum tolerance of {max_radial_offset_mm:.2f} mm). "
            "Ensure the stock body axis is aligned with the setup Z-axis."
        )

    # 2. Compute radius for all vertices: R = sqrt(X^2 + Y^2)
    radii = np.hypot(v[:, 0], v[:, 1]) + tol_pad_r_mm
    z_coords = v[:, 2]

    # Include edge midpoint samples to ensure convex curves between vertices are enveloped
    t_verts = v[indices]
    edge_mids = (t_verts[:, [0, 1, 2], :] + t_verts[:, [1, 2, 0], :]) * 0.5
    mids_flat = edge_mids.reshape(-1, 3)
    mids_r = np.hypot(mids_flat[:, 0], mids_flat[:, 1]) + tol_pad_r_mm
    mids_z = mids_flat[:, 2]

    all_z = np.concatenate([z_coords, mids_z])
    all_r = np.concatenate([radii, mids_r])

    # 3. Discretize into Z slices to construct the outer boundary profile
    total_z_len = max(0.1, z_max - z_min)
    num_slices = max(2, int(math.ceil(total_z_len / z_slice_step_mm)))
    slice_edges = np.linspace(z_min, z_max, num_slices + 1)

    profile_pts = []
    # Start at bottom centerline: (z_min, 0)
    profile_pts.append((float(z_min), 0.0))

    global_max_r = 0.0
    for i in range(num_slices):
        z_lo = slice_edges[i]
        z_hi = slice_edges[i + 1]
        in_slice = (all_z >= z_lo) & (all_z <= z_hi)
        if np.any(in_slice):
            max_r = float(np.max(all_r[in_slice]))
        else:
            max_r = global_max_r

        if max_r > global_max_r:
            global_max_r = max_r

        profile_pts.append((float(z_lo), max_r))
        profile_pts.append((float(z_hi), max_r))

    # Close the profile along the Z axis
    profile_pts.append((float(z_max), 0.0))
    profile_pts.append((float(z_min), 0.0))

    if Polygon is not None:
        poly = Polygon(profile_pts).buffer(0)
    else:
        poly = None

    stock = AxisymmetricStock2D(
        z_min=float(z_min),
        z_max=float(z_max),
        outer_radius=float(global_max_r),
        inner_radius=0.0,
        polygon=poly,
    )

    return WorkpieceDefinition(
        stock=stock,
        fixtures=fixtures or [],
    )


def extract_solid_workpiece_from_fusion_setup(
    setup: Any,
    is_turning: bool = False,
    cell_size_mm: float = 0.5,
    pad_mm: float = 10.0,
) -> WorkpieceDefinition:
    """
    Extract complete WorkpieceDefinition from a native Fusion 360 Setup configured with Solid stock.

    Args:
        setup: adsk.cam.Setup instance
        is_turning: True if Turning setup, False for Milling
        cell_size_mm: Cell size for milling heightfield (mm)
        pad_mm: Padding around stock bounding box (mm)

    Returns:
        WorkpieceDefinition ready for verifier simulation
    """
    vertices_mm, indices = extract_mesh_from_fusion_setup(setup)

    if is_turning:
        # Extract chuck fixtures if defined in setup parameters
        fixtures = []
        if hasattr(setup, "parameters"):
            try:
                clamp_p = setup.parameters.itemByName("job_stockClampingLength")
                if clamp_p:
                    clamp_val = getattr(clamp_p, "value", None)
                    v_sub = getattr(clamp_val, "value", clamp_val)
                    if isinstance(v_sub, (int, float)) and v_sub > 0.01:
                        clamp_len_mm = float(v_sub) * 10.0  # cm to mm
                        z_min_mm = float(vertices_mm[:, 2].min())
                        r_max_mm = float(np.hypot(vertices_mm[:, 0], vertices_mm[:, 1]).max())
                        fixtures.append(
                            AxisymmetricChuckObstacle(
                                z_min=z_min_mm - 50.0,
                                z_max=z_min_mm + clamp_len_mm,
                                outer_radius=r_max_mm + 30.0,
                                clamping_radius=r_max_mm,
                            )
                        )
            except Exception:
                pass

        return revolve_mesh_to_lathe_stock(vertices_mm, indices, fixtures=fixtures)
    else:
        stock = SolidMeshStock3D.from_mesh(vertices_mm, indices)
        _dbg(
            f"extract_solid_workpiece_from_fusion_setup: created SolidMeshStock3D for '{getattr(setup, 'name', 'unnamed')}' "
            f"X=[{stock.x_min:.2f}, {stock.x_max:.2f}], Y=[{stock.y_min:.2f}, {stock.y_max:.2f}], Z=[{stock.z_min:.2f}, {stock.z_max:.2f}]"
        )
        return WorkpieceDefinition(stock=stock)
