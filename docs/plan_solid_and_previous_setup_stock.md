# Plan: "From Solid" and "From Preceding Setup" Stock Support (Revision 2)

Revision 2 checks the earlier plan against the Fusion API installed on this machine
(`Autodesk Fusion.app/Contents/Api/Python/packages/adsk/cam.py`) and against the current
verifier code. Sections marked **Correction** replace claims from Revision 1.

---

## 0. Verified facts (from the installed API)

| Fact | Source |
|---|---|
| `Setup.stockMode` is a typed enum `adsk.cam.SetupStockModes` with values `FixedBoxStock`, `RelativeBoxStock`, `FixedCylinderStock`, `RelativeCylinderStock`, `FixedTubeStock`, `RelativeTubeStock`, `SolidStock`, `PreviousSetupStock`. | `class SetupStockModes`, `Setup._get_stockMode` |
| `Setup.stockSolids` accepts **`BRepBody` and/or `MeshBody`**. | `Setup._get_stockSolids` docstring |
| `Setup.workCoordinateSystem` returns a 4x4 `Matrix3D`. | `Setup._get_workCoordinateSystem` |
| There is **no** `previousSetup` property. "Preceding" means the earlier setup in browser order. | `Setup` member list |
| There is **no** API that returns the simulated in-process stock (IPW) as a BRep, mesh or file. *Simulation → Save Stock* is UI-only. | No stock/IPW/simulation export members in `adsk.cam` |

**Correction (stock detection).** The detection added in `fd37d1c` (`_GetSetupStockMode`) guesses
parameter names (`job_stockMode`, `job_previousSetup`, …) and substring-matches values like
`"prev"`, `"rest"`, `"solid"`. It also treats a non-empty `stockSolids` as "From Solid". This is
fragile:
- It does not use the real API field (`setup.stockMode`).
- `stockSolids` can still hold a stale selection after the user switches to a box or cylinder,
  which would reject a valid setup.
- The `getParameter("job:stockMode")` probe in `universal_dumper.cps` is also an unverified name.

**Fix (do first):** use `setup.stockMode == adsk.cam.SetupStockModes.PreviousSetupStock` /
`SolidStock` as the only source. Pass the result to the dumper through the config, drop the
heuristic parameter scanning, and add a debug log of the enum value. Then retest in Fusion.

---

## 1. Can we get a BRep of the preceding setup's result?

**No, not through the public API** (see §0). Revision 1's reasons were partly speculation.
Specifically, the claim about which simulation kernel Fusion uses internally is unverified and
irrelevant. Two workarounds exist, both with real costs:

| Option | Pros | Cons |
|---|---|---|
| A. **Recompute the IPW ourselves** by verifying all preceding setups in order, then hand our IPW to the next setup. | Fully automatic. Reuses the existing engines. Deterministic. | Our IPW is an approximation (tool models, grid resolution). Errors compound across setups. Requires processing predecessors even if the user did not select them. |
| B. **User-saved stock**: the user does *Save Stock* in Fusion and selects it as "From Solid" in the next setup. | Uses Fusion's own simulation, which is the reference. Reduces to the "From Solid" case. | Manual step that goes stale if toolpaths change. Requires "From Solid" support first. |
| C. Build an exact BRep with `adsk.fusion` booleans of swept tool volumes. | Exact geometry. | Thousands of booleans per setup. Swept-volume BRep construction for arbitrary toolpaths isn't available. Impractical. **Rejected.** |

Recommendation: implement B (it falls out of "From Solid") and A as the automatic path.

---

## 2. Representation fidelity: is heightfield loss a problem?

**Correction.** Revision 1 described a "dual heightfield (z_top, z_bottom)" and "multi-dexel"
as if they were available. They are not. `HeightfieldGrid` stores **only a top surface per
cell above a constant `z_floor`**. This affects both stock modes.

The loss only matters in one direction. The verifier promotes a move to G0 only if it is in
air. So:
- **Overestimating material is safe.** It gives fewer promotions and possibly spurious collision
  warnings.
- **Underestimating material is unsafe.** A rapid can be promoted through real stock.

Every conversion must therefore be a **conservative superset** of the true stock:

| Conversion | Top-only heightfield behavior | Safe? |
|---|---|---|
| Solid/mesh stock → heightfield (take max Z of the solid per cell) | Overhangs and undercuts get filled down to the floor. | Yes (superset) |
| Mesh chordal error | The mesh can sit inside the true surface by the tolerance. | Only if padded by the tessellation tolerance |
| Preceding setup IPW, same orientation | Lossless. | Yes |
| Preceding setup IPW, **180° flip** | A top-only grid has no bottom surface. The flipped bottom must be assumed flat at the old `z_floor`, i.e. full material. | Yes, but loses all Op 1 bottom-side cuts (false collision warnings in Op 2) |
| Preceding setup IPW, 90° / arbitrary rotation | A top-only grid can't be reprojected faithfully. | Only if we use the bounding envelope of the IPW, which is very coarse |
| Lathe 2D polygon, flip along Z | Exact (rigid 2D transform). | Yes |
| Lathe from solid: R-Z envelope (max radius per Z, padded) | Non-round stock (hex bar, casting) becomes its circumscribed revolution. Internal voids are filled unless an inner profile is extracted. | Yes (superset) |

**Mitigation options (in order of cost):**
1. Accept the conservative superset. This is safe and the cost is only missed promotions and
   noisy warnings in flipped setups. Good enough for a first version.
2. Add a bottom surface (`z_bottom` per cell) to `HeightfieldGrid`. That makes 180° flips
   exact for 3-axis work. Moderate effort: carving, ray tests and plotting all need updating.
3. Multi-dexel (interval list per cell). Needed only for 4th/5th-axis indexing. High effort.
   Defer until required.

There is a second, independent source of error: **accumulated approximation in Option A**.
Our Op 1 IPW is computed with our tool models and grid, not Fusion's. Any spot where we remove
*more* material than reality makes Op 2 unsafe. To prevent that, the carving model must never
remove more than the true tool envelope. Pad tool radius *down* when carving, never up, and add
a test for this.

---

## 3. Phased plan

### Phase 0 — Correct detection (deferred by decision 1; still the first step before Phase 1)
- Replace `_GetSetupStockMode` with an enum check on `setup.stockMode`. Keep the explicit
  user-facing error.
- Remove `stockSolids`-count and parameter-name heuristics. Pass the enum name in the dumper
  config. Remove the `getParameter("job:stockMode")` probe from the dumper.
- Tests: mock `setup.stockMode` with each enum value. Check that stale `stockSolids` with a box
  mode is *not* rejected.
- Manual Fusion retest with a "From Preceding Setup" setup.

### Phase 1 — "From Solid"
- Read `setup.stockSolids` (BRep or Mesh). Tessellate BRep bodies with
  `meshManager.createMeshCalculator()`. Use mesh bodies directly.
- Transform vertices to setup WCS with `setup.workCoordinateSystem` (inverted). Verify the
  frame convention against a known box setup before trusting it.
- Milling: rasterize the max Z per cell into `HeightfieldGrid`. Pad by the tessellation
  tolerance. Set the floor to the solid's min Z.
- Turning: R-Z outer envelope per Z slice, padded. Optionally an inner profile for bores.
  Error out if the solid isn't reasonably centered on the spindle axis.
- This also enables Option B for preceding setups.

### Phase 2 — "From Preceding Setup" (Option A, stored IPW)
Predecessors are **never run implicitly** (decision 4). Every successful setup run stores its
final IPW (§4). A child setup loads its predecessor's stored IPW. If it can't, the child stops.

1. **Resolve the predecessor** by browser order (`cam.setups`): the nearest earlier,
   non-suppressed setup.
2. **Load and validate** the predecessor's stored IPW (§4.3). If it is missing, stale, failed, or
   unreadable, stop with **no output** and an error such as:
   > Setup 'Op2' uses stock from preceding setup 'Op1', but no up-to-date stock result for
   > 'Op1' was found. Post process these setups first, in this order: Op1. Then run 'Op2' again.

   If the predecessor itself uses "From Preceding Setup" and its stored IPW is stale, list the
   whole chain in order. The chain check comes for free from the hash chaining in §4.3.
3. **Transfer** the IPW using `T = WCS_child⁻¹ · WCS_parent`:
   - Translation only: exact.
   - 180° flip about X or Y: conservative (the new bottom is full material) until `z_bottom`
     exists.
   - Any other rotation (decision 2): use a **coarse conservative envelope** and continue with
     a **warning**. For milling, this is the axis-aligned bounding box of the transformed IPW
     in the child frame (a superset). The warning recommends Option B (decision 3):
     > Setup 'Op2' is rotated relative to 'Op1'. Stock was approximated by its bounding
     > envelope, so some rapid moves may not be restored and extra collision warnings may
     > appear. For exact results, use *Simulation → Save Stock* on 'Op1' in Fusion and set
     > 'Op2' stock to *From Solid* using the saved body.
4. **Lathe**: rigid (Z, R) transform of the polygon. Chuck obstacles come from the child
   setup. A non-coaxial transfer (spindle axes not parallel) falls back to the envelope with
   the same warning.
5. Simulate the child's cuts with a slightly *undersized* tool so that the stored result never
   under-estimates material (§2).

### Phase 3 — Optional fidelity upgrade
- Add `z_bottom` to `HeightfieldGrid` for exact 3-axis flips.

---

## 4. Stored IPW: location and format

### 4.1 Location
Sidecar file next to the setup's NC output, resolved with the same
`_ResolveSetupFolderAndFilename()` used for the NC file:

```
<NC program output folder>/<setup subfolders>/<SetupName>.ipw.npz
```

Why here:
- It travels with the NC file, report and plot. The user can see it, delete it to force a
  re-run, or archive it.
- The child finds it deterministically from the predecessor's setup name and the current NC
  Program's output folder. No global cache to clean up and no cross-document collisions.

Rejected alternatives:
- **Fusion document attributes**: writing them modifies (dirties) the user's document, which
  we avoid everywhere else, and they have size limits.
- **Temp/app-data cache**: invisible to the user, collides across documents with equal setup
  names, and gets lost on cleanup.

If the output folder changes between runs, the child doesn't find the file and asks the user
to re-run the predecessor. That is the intended behavior.

### 4.2 Format
A single **NumPy `.npz`** (compressed). NumPy is already required by the milling verifier.
It is written atomically: write to `*.tmp`, then `os.replace`. It is written only when the
setup completes without error and isn't cancelled.

| Key | Content |
|---|---|
| `meta` | UTF-8 JSON string (see below) |
| `grid_z` | *(milling)* `float32[ny, nx]` top-surface heights, mm, in the producing setup's WCS |
| `poly_wkb` | *(turning)* `uint8[]` Shapely WKB of the (Z, R) IPW polygon, mm |

The `meta` JSON contains:
- `schema` (int, starts at 1), `kind` (`"mill_heightfield"` or `"lathe_polygon"`), `units: "mm"`.
- Grid geometry: `x0`, `y0`, `cell_mm`, `nx`, `ny`, `z_floor`, `stock_bounds`.
- `wcs`: the producing setup's `workCoordinateSystem` as 16 floats, row-major.
- Provenance: `setup_name`, `document_name`, `created_utc`, `addin_version`, verifier
  parameters (cell size, tool undersize).
- Hashes (§4.3): `input_stock_hash`, `toolpath_hash`, `ipw_hash`.

Size: a 300×300 mm stock at 0.5 mm cells is 360k floats (about 1.4 MB raw, much less
compressed). Lathe polygons are a few KB.

If numpy is unavailable and milling falls back to `MillClearanceVerifier`, there is no
heightfield. No IPW is stored, and children report that the predecessor's stock result is
unavailable.

### 4.3 Staleness detection
A stored IPW is valid only if it was produced from the predecessor's *current* toolpaths and
stock:
- `toolpath_hash`: SHA-256 of the predecessor's merged Pass-1 context (motion events, tools,
  WCS), canonicalized and with volatile fields removed.
- `input_stock_hash`: hash of the predecessor's own stock input:
  - box/cylinder/tube: parameter values;
  - From Solid: the tessellated mesh;
  - From Preceding Setup: the parent file's `ipw_hash`. This chains the validity through
    multi-setup sequences.
- `ipw_hash`: hash of the stored payload, for integrity.

To validate, the child re-runs **Pass 1 only** (dump, no verify, no output) for the
predecessor and compares `toolpath_hash`. It then compares `input_stock_hash` against the
predecessor's current stock definition. This costs one extra dump of the predecessor per
child run. In exchange, a toolpath edited after the last run can never be silently used.
Any mismatch triggers the stop-and-ask error from Phase 2, step 2.

---

## 5. Decisions (review of Revision 2)
1. **Stock-mode detection fix (Phase 0)**: not now. It remains the first step of the plan,
   to be done before Phase 1.
2. **Non-flip rotations**: coarse conservative estimate plus a warning.
3. **Option B** (save stock in Fusion, then use *From Solid*): accepted. It is the workflow
   recommended in the rotation warning, and in the unsupported-mode error once Phase 1 lands.
4. **Missing or stale predecessor results**: never run predecessors implicitly. Tell the user
   which setups to post, in order, and stop without output.
