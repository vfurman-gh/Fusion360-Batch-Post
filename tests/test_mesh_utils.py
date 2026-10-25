#!/usr/bin/env python3
"""
Unit tests for geometry/mesh_utils.py (Greedy Meshing & CAM Palette).
"""

import unittest
import numpy as np

from geometry.mesh_utils import greedy_mesh_heightfield, compute_cam_palette_facecolors


class TestGreedyMeshHeightfield(unittest.TestCase):
    def test_empty_or_none(self):
        polys, normals, z_levels = greedy_mesh_heightfield(None, 0.0, 0.0, 1.0)
        self.assertEqual(len(polys), 0)
        self.assertEqual(len(normals), 0)

        polys, normals, z_levels = greedy_mesh_heightfield(np.array([]), 0.0, 0.0, 1.0)
        self.assertEqual(len(polys), 0)

    def test_single_uniform_plane(self):
        # 10x10 uniform grid at Z = 5.0
        Z = np.full((10, 10), 5.0, dtype=np.float32)
        polys, normals, z_levels = greedy_mesh_heightfield(Z, x_min=0.0, y_min=0.0, cell_size=1.0)
        
        # Should merge into exactly 1 horizontal quad and 0 vertical step walls
        self.assertEqual(len(polys), 1)
        self.assertEqual(len(normals), 1)
        np.testing.assert_allclose(normals[0], [0.0, 0.0, 1.0])
        self.assertEqual(polys[0], [[0.0, 0.0, 5.0], [10.0, 0.0, 5.0], [10.0, 10.0, 5.0], [0.0, 10.0, 5.0]])

    def test_step_grid_with_vertical_walls(self):
        # Left half at 0.0, right half at -5.0
        Z = np.zeros((10, 10), dtype=np.float32)
        Z[:, 5:] = -5.0

        polys, normals, z_levels = greedy_mesh_heightfield(
            Z, x_min=0.0, y_min=0.0, cell_size=1.0, z_floor=-10.0, omit_floor=False
        )
        
        # 2 horizontal quads + 1 vertical step wall in X
        self.assertEqual(len(polys), 3)
        # Check that there is a vertical wall normal:
        has_vertical = any(abs(n[0]) > 0.9 for n in normals)
        self.assertTrue(has_vertical)

    def test_omit_floor(self):
        # Left half at 0.0, right half cut through to z_floor=-10.0
        Z = np.zeros((10, 10), dtype=np.float32)
        Z[:, 5:] = -10.0

        # With omit_floor=True, right half horizontal quad should be skipped
        polys, normals, z_levels = greedy_mesh_heightfield(
            Z, x_min=0.0, y_min=0.0, cell_size=1.0, z_floor=-10.0, omit_floor=True
        )
        # 1 horizontal quad (left half) + 1 vertical step wall
        self.assertEqual(len(polys), 2)
        self.assertAlmostEqual(z_levels[0], 0.0)

    def test_palette_facecolors(self):
        normals = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0]], dtype=np.float32)
        z_levels = np.array([0.0, -8.0], dtype=np.float32)
        colors = compute_cam_palette_facecolors(normals, z_levels, z_stock_min=-12.0, z_stock_max=0.0)
        self.assertEqual(colors.shape, (2, 4))
        # Top raw stock should be blueish (B > R)
        self.assertGreater(colors[0, 2], colors[0, 0])
        # Machined wall should be greenish (G > B)
        self.assertGreater(colors[1, 1], colors[1, 2])


if __name__ == "__main__":
    unittest.main()
