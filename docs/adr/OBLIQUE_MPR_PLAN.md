<!-- Synthesised from three independent designs, each adversarially attacked.
     Run wf_e9f1249d-ec9. Not yet implemented; this is the plan. -->

I read `mpr.js` (774 lines), `orientation.js`, `transform.js`, `measure.js`, `units.js`, `reference.js`, `volume.js`, the `plane` call sites in `app.js`/`annotations.js`/`measurements-panel.js`/`sync.js`, and the mpr gates. Findings that decided the design are marked **[verified]**.

---

# 1. RECOMMENDATION

**Spine: `restrict`** — cut only obliques that contain a lattice axis, sample nearest-neighbour, refuse the rest — but rebuilt on the two corrections its attack landed: the plane stays a **canonical interned string** (not an object), and the `screenEdges` runtime guard is deleted as dead code. **Grafted in:** from `restrict`'s attack, the interned-name fix and routing the toolbar's greying through `reconstructionRefusal`; from `interpolate`'s attack, `sourceMmAlong` — the one quantity that says what a resliced plane actually resolves — generalised here into a single `crossingsAlong` that yields *both* the pitch and the resolution; from `nearest`, the full-bounding-extent grid (constant frame size as the reader scrolls), the explicit tie-break in `directionLetters`, and the slab experiment; from `nearest`'s attack, the killing of the min-over-axes rule as a *plane* rule and the demand that the patient→index map be written down.

**The tradeoff accepted:** no double oblique and no in-plane rotation — no cardiac short axis on a 2 mm chest CT, and no arbitrarily-rotated axial — and no oblique at all on a tilted-gantry stack the coronal still accepts. In exchange every oblique frame is a strided nearest-neighbour copy with multiplicity exactly 1, so `regionStatistics`, `length`, `fitOf`, `scaleBarOf` and `referenceLine` are all correct with no second channel and no new caveat, and the one thing that is not what it appears — a 0.99 mm row grid carrying 2.83 mm of detail — is a derived number printed in words.

**Where the attacks landed, and what I am not carrying forward:**

- **`nearest` dies on its pitch rule.** `stepMm(u) = min_k spacing_k/|u_k|` is a 1-D rule applied to two axes independently; 2-D sample density is the *product*. On a pure in-plane rotation it gives 0.98995 on both axes, index offsets `(c−r, c+r)`, sum always even — half the voxels unreachable, drawn crisp, and both pitches equal so `zoomForOneToOne` **[verified: refuses only on `Math.abs(rowMm - colMm) > 1e-6`]** offers 1:1 on the one plane where half the data does not exist. Not carried. Its `plane`-as-object signature dies too.
- **`interpolate` dies on its premise.** §1 claims the existing planes "never faced the choice." They faced it and decided it: `geometryOf`'s docstring — *"Rounded rather than interpolated … Sub-pixel shear correction would be the same invention in a different axis"* — and `test_the_shear_flag_means_what_the_volume_layout_does` pins it with `assert mpr.count("Math.round(o)") == 1` **[verified]**. The module's rule is *displace rather than invent, keep the displacement under half the plane's own pixel, and say so*. That is a rule this design can satisfy; it is not a rule to reverse. Trilinear not carried.
- **`restrict`'s `screenEdges` guard is dead code.** Every reachable `m` comes from `rotatedRight`/`flippedHorizontally`/`flippedVertically` composed from `NO_TRANSFORM` **[verified: `transform.js:85,90,95`]**, all signed permutations. Not carried as runtime code — carried as an invariant and a gate on `transform.js`, which should be committed whether or not this ships.
- **`restrict`'s `resamplingNote` is deleted, not fixed.** Under the pitch rule below, multiplicity is exactly 1 on every plane this design permits, so the note would always return `''`. A note that can never fire is decoration.
- **All three missed the same thing**, and it is the single most important line in the module: **the patient→index map**, written in §2.

---

# 2. THE API

### `reslice()` after the change

```js
export function reslice(stack, plane, index, seg = null, slab = null)
```

**Unchanged. So are `planeDepth(stack, plane)`, `planeStepMm(stack, plane)` and `reconstructionRefusal(stack, plane)`.** That is the point, and it is forced: `plane` is not a parameter in this codebase, it is an **address**, compared with `===` and printed as text in six places outside `mpr.js` **[all verified]**:

| site | use |
|---|---|
| `render/annotations.js:103` | `m.plane === location.plane` — the key every measurement is drawn by |
| `app.js:878, 913, 1262` | `b.dataset.plane` — a DOM **string** map |
| `app.js:417` | HUD `` `${p.plane}  ${p.index + 1} / ${depth}` `` |
| `app.js:456` | reference-line `label: other.plane` |
| `ui/measurements-panel.js:84` | `escape(m.plane)` in a table cell |
| `image/sync.js:160` | `sourcePlane === PLANES.AXIAL` |

An object plane makes `annotations.js:103` reference equality: two panels on the same oblique never share a caliper, and a panel re-entering the same oblique after a layout change (`app.js:229`, `:252`) loses every measurement taken there — permanently, with the panel still listing it as `[object Object] 37`. `describeMeasurement` returns a **shallow** `Object.freeze` **[verified `measure.js:214`]**, so the record would also retain a live closure over the stack.

So `PLANES` stays a frozen three-member table of the *named* planes, and the oblique vocabulary is **open in its values, closed in its grammar, and generated in exactly one place**:

```
'oblique x+45.0°'     e1 = ex,  e2 = cosθ·ey + sinθ·down     θ=0 is axial, θ=90 is coronal
'oblique y-12.5°'     e1 = ey,  e2 = cosθ·down + sinθ·ex     θ=0 is sagittal
                      down = -n, so θ=90 on x reproduces reconstructedGeometry's [...ex, ...down] exactly
```

Canonical: one decimal, signed, `|θ|` strictly inside `(0, 90)`. **0 and 90 are excluded because they are planes that already have names**, and a second name for the axial splits `annotations.js`'s address in half.

### New module `viewer/src/image/oblique.js`

```js
/** Canonical name, or null when the request names a plane that already has one. */
export function obliqueName(axis, deg)        // 'x'|'y', number -> 'oblique x+45.0°' | null

/** Whether a plane name is an oblique at all. The only test anything makes. */
export function isOblique(plane)              // -> boolean

/**
 * How far apart, along `w`, the crossings of each voxel-layer family are.
 *
 *   crossing_k(w) = d_k / |w · e_k|      e = [ex, ey, n],  d = [colMm, rowMm, sliceSpacing]
 *
 * THE MINIMUM IS THE SAMPLE PITCH and THE MAXIMUM IS WHAT THAT AXIS RESOLVES, and they are
 * the same formula because they are the same question asked of a different family. On every
 * named plane they are equal, which is why no existing frame has anything to declare.
 * ONE decision point: pixelSpacing, resolutionMm, planeStepMm and slab thickness all ask it.
 */
export function crossingsAlong(stack, w)      // -> { pitchMm:number, coarsestMm:number }

/** Everything about the plane that does not depend on the index. Cached per stack per name. */
export function obliqueGrid(stack, plane)
// -> { e1:[3], e2:[3], n:[3], deg:number, axis:'x'|'y',
//      rows:number, columns:number, depth:number,
//      pixelSpacing:[rowMm, colMm], resolutionMm:[rowMm, colMm], stepMm:number,
//      originAt(index) -> [3] }        // patient position of pixel (0,0) at that index
// -> null when frames[0].orientation.length !== 6 (the refusal reports it; this just says no)

/** The pixels and the overlay for one index. Nearest voxel, out-of-box -> fillValue. */
export function obliqueSample(stack, grid, index, volume, overlayVolume, fillValue)
// -> { pixels:TypedArray, overlay:Uint8Array|null }
```

**The pitch rule, and the whole argument for it.** `crossingsAlong` is the same per-axis rule `nearest` proposed, but this design pays the price that makes it sound: **at most one of a plane's two axes may be oblique to the lattice, and the other must be a lattice axis.** Then the exact axis strides by 1 lattice cell per sample and the oblique axis lands on the *tightest* crossed family exactly once per sample — the generated index lattice has determinant 1 in the plane, so the map `(c, r) → voxel` is **injective, and surjective onto the voxels the plane substantially crosses**. No checkerboard, no duplicates, multiplicity exactly 1. `nearest`'s rule collapses precisely when *both* axes are oblique, which this design refuses (§5).

At `oblique x+45.0°` on the phantom, with `e1 = (1,0,0)`, `e2 = (0, .70711, −.70711)`, `n_ob = (0, .70711, .70711)`:

```
crossingsAlong(e1)   = { pitch 0.700,   coarsest 0.700  }   only ex has a component
crossingsAlong(e2)   = { pitch 0.98995, coarsest 2.8284 }   min/max of 0.7/.707 and 2.0/.707
crossingsAlong(n_ob) = { pitch 0.98995, coarsest 2.8284 }
```

### The patient→index map — the line all three proposals omitted

```
P(c, r, z) = P0 + c·colMm·ex + r·rowMm·ey + z·sliceSpacing·n            (forward)
c = (Q−P0)·ex / colMm      r = (Q−P0)·ey / rowMm      z = (Q−P0)·n / sliceSpacing   (inverse)
```

This is affine **exactly because the stack is unsheared**, which refusal C guarantees. On a row-sheared stack `volumeOf` packs at `g.rowStart[z]`, rounded **[verified `mpr.js:268`, `geometryOf:217`]**, so the volume's third lattice vector is the table direction with a per-slice whole-pixel relabel, and the inverse above is wrong by up to half a row per slice. That is the entire argument for refusing an oblique on a stack the coronal still accepts.

Sample `(c_out, r_out)` at index `i` sits at `originAt(i) + c_out·colMm_ob·e1 + r_out·rowMm_ob·e2`; run the inverse, `Math.round` each, and take `volume[z·frame + r·columns + c]` or `fillValue`.

**Extent** — the full bounding box at every index, never the chord, or the frame's size changes as the reader scrolls and the zoom jumps with it:

```
span(w) = |w·ex|·columns·colMm + |w·ey|·rows·rowMm + |w·n|·depth·sliceSpacing
columns_ob = ceil(span(e1)/colMm_ob)   rows_ob = ceil(span(e2)/rowMm_ob)   depth = ceil(span(n_ob)/stepMm)
```

At x+45°: **448 × 252, depth 252**. `index` runs `0 … 251` — unsigned, like every other plane — so `app.js:884`'s `Math.floor(planeDepth/2)` = 126 lands 0.7 mm from the volume centre, `app.js:1274`'s `(p.index + 1) % planeDepth` wraps, and `slabPlan`'s `Math.max(0, centre − half)` clipping means what it always meant. No `app.js` arithmetic changes.

### What the frame object gains

| field | on axial / coronal / sagittal | on an oblique |
|---|---|---|
| `resolutionMm: [rowMm, colMm]` | **new**, always `=== pixelSpacing` | `[2.828, 0.700]` at x+45° — **different**, and that inequality is what everything downstream keys on |
| `position`, `orientation`, `normal` | already there on coronal/sagittal | `[...e1, ...e2]`, `originAt(index)`, `cross(e1,e2)` — all three, re-derived **per index**, as `test_a_reconstructed_plane_knows_where_it_is` requires and `reference.js:55` consumes |
| `paddingValue`, `paddingRangeLimit` | inherited from the acquisition | `fillValueOf(stack)` **unconditionally** — the rectangle's corners are outside the box at every index |

`describeMeasurement` gains one field beside `projection`, for the same reason `projection` is there rather than looked up at render time:

```js
resolutionMm: frame.resolutionMm ? [...frame.resolutionMm] : null,
```

`units.js` gains one export, sibling to `projectionNote`, taking anything that carries both fields — the frame for the HUD, the record for the panel, so there is one sentence and not two:

```js
export function resolutionNote(source)   // '' when pitch and resolution agree on both axes
```

**The oblique branch is written after `const common = {` and spreads it** — `test_every_frame_consumer_reads_only_fields_reslice_sets` splits `reslice`'s body at that literal **[verified: `cut = body.index("if (plane === PLANES.AXIAL)")`, `recon = body.index("const common = {")`]**, and a branch above it is scanned as the axial section where `re.search(r"\.\.\.f\b", …)` matches and the check collapses to `valueUnit` alone.

---

# 3. ORIENTATION

**The letters work unchanged, on both functions, and that is the evidence the design is in the right place.**

`edgeLetters(frame)` reads `frame.orientation` and nothing else **[verified `orientation.js:110-112`]**. An oblique's `e1`/`e2` are orthonormal direction cosines in patient space by construction, so `orientation: [...e1, ...e2]` is a real (0020,0037) and `edgeLetters` consumes it with no edit.

On the head-first supine phantom at `oblique x+45.0°`: `e1 = ex = (1,0,0)` → right **`L`**, left **`R`**. `e2 = (0, .70711, −.70711)` → the y component is +P and the z component is −F, both at .70711, both above `SECOND_LETTER_ABOVE = sin(15°) = 0.2588` **[verified]** → bottom **`PF`**, top **`AH`**. That interpolates the recorded phantom readings exactly: axial bottom `P`, coronal bottom `F`, 45° bottom `PF`. At `oblique x+10.0°` the second component is `sin10° = 0.174 < 0.2588` and the bottom reads plain **`P`** — the threshold doing precisely the job its docstring claims, with no new rule.

**One wart, named rather than hidden.** At exactly 45° the two components tie, `.sort((a, b) => b.size - a.size)` is stable, and array order (x, y, z) decides — so `PF` becomes `FP` one tenth of a degree later with nothing on screen changing. Add an explicit tie-break in `directionLetters` with the comment *"a tie is not a ranking; x before y before z is a stated convention, not a measurement."* Both letters are still shown, so only the ranking was ever arbitrary, not the information.

`PF` reads the same at 20° and at 70°, so the angle has to be on screen — and it already is, because **the plane's name is the angle**: `app.js:417` prints `${p.plane}`, which is `oblique x+45.0°`. Zero new HUD code for the angle.

**`screenEdges()` is unchanged, with no guard, and the boundary it rests on becomes a stated invariant.** `restrict`'s proposed `Math.abs(m[0]*m[1]) > 1e-9` guard is dead code — every reachable `m` is a signed permutation **[verified]** — and in the one scenario it names it would replace correct letters with "orientation not recorded". But the boundary is real and load-bearing, and it is the one thing all three attacks agreed on: **obliquity is a property of the plane and never of the view.** If an angle ever entered `m`, four things break silently at once **[all four verified]**:

- `screenEdges`'s `fromImage(0,1)` at `m = [.707,−.707,.707,.707]` gives `(x,y) = (.707,.707)`, hits `y > 0.5`, returns `letters.top`; `fromImage(1,0)` gives `(.707,−.707)` and returns `letters.bottom` — the screen's top and right resolved by threshold order, no error (`orientation.js:159`);
- `fitOf`'s `shownW = |m[0]|·storedW + |m[1]|·storedH` stops being exact at the same moment, and its own comment says why: *"Each row of these matrices has exactly one non-zero entry"* (`transform.js:162-166`);
- `millimetresPerScreenPixel` reads `fit.shownW` (`:260`) → the scale bar is wrong;
- `zoomForOneToOne`'s `across` (`:386`) → wrong.

So a "rotate the plane" gesture re-derives `e1`/`e2` and re-slices. It never multiplies `m`. **This becomes a gate on `transform.js` — the view matrix is a signed permutation, always — and it should be committed whether or not oblique MPR ships.**

---

# 4. MEASUREMENT

**The caliper is exact and needs no code change.** `e1 ⊥ e2` exactly (`e1` is a lattice axis and `e2` is built orthogonal to it), and `pixelSpacing = [0.990, 0.700]` is the true millimetre step along each, so `length()`'s `hypot(dx·colMm, dy·rowMm)` **[verified `measure.js:88-94`]** is a patient distance. The orthogonality assumption is now load-bearing and is guaranteed by construction, not by hope: there is no path by which a caller supplies a skewed pair, because the vocabulary has no grammar for one.

**The ROI is exact and needs no code change, and this is earned rather than asserted.** Multiplicity is exactly 1 (§2), so `count` is a count of *distinct acquired voxels*, `areaMm2 = count · 0.990 · 0.700` is right, and the population SD is over a set with no repeats. There is nothing to declare about resampling multiplicity — which is why `resamplingNote` is deleted rather than written.

**`zoomForOneToOne` refuses every oblique, correctly and with no edit** — `Math.abs(0.98995 − 0.700) > 1e-6` **[verified `transform.js:377`]**. A 1:1 on a plane whose grid is finer than its content would be exactly the *"label the picture cannot contradict"* its own docstring warns against.

**What they must say about themselves.** One fact, one number, one sentence, from `resolutionNote`, which returns `''` whenever pitch and resolution agree — so it is absent on every named plane of its own accord and its presence therefore means something:

```
HUD        oblique x+45.0°   116 / 252   ·   0.99 mm   ·   rows resolved at 2.83 mm, drawn at 0.99 mm   ·   100%

caliper    7.9 mm · rows resolved at 2.83 mm, drawn at 0.99 mm

ROI        30 ± 0 HU over 78 px · 54.1 mm² · rows resolved at 2.83 mm, drawn at 0.99 mm

slab ROI   30 ± 0 HU · maximum over 8.9 mm · rows resolved at 2.83 mm, drawn at 0.99 mm
```

(78 px is the reader's own ellipse; 2.83, 0.99 and 8.9 are derived and exact.) The clause compounds with `projectionNote` exactly as `projectionNote` compounds today, and it is recorded on the measurement rather than looked up at render time so a reader who straightens the plane afterwards cannot relabel an old row.

**Why that sentence and not another.** On this phantom the same nodule reads **7.0 mm across the columns and 7.9 mm down the rows** (§6). Both numbers are correct arithmetic on the data. The 0.9 mm disagreement *is* the 2.83 mm row resolution, showing up as a staircase that over-reports the craniocaudal extent — so the sentence is not a disclaimer bolted onto the picture, it is the explanation of a discrepancy the reader can measure for themselves.

**What the HUD does not need:** `app.js` must not read `frame.pixelSpacing[0]` to build this — `test_slab_thickness_is_measured_along_the_axis_it_projects` asserts `re.findall(r"\w*\.(?:sliceSpacing|pixelSpacing\[\d\])", app)` is empty **[verified]**. It calls `resolutionNote(frame)`.

---

# 5. REFUSALS

Three new entries, added **inside `reconstructionRefusal`**, after the four existing ones so a cine loop still hears the more fundamental sentence first. All four existing refusals inherit automatically, because the function returns `null` only for `PLANES.AXIAL` **[verified `mpr.js:512`]**, and an oblique is not that.

**A. `oblique_needs_a_stated_orientation`** — when `stack.frames[0].orientation.length !== 6`.

> this series does not state (0020,0037), so its slices have no stated direction in the patient. A coronal is still the volume's own row axis and can be built without one — it selects the right voxels and merely cannot say which edge is the patient's left. An oblique is an angle, and an angle needs an axis to be an angle from; there is nothing here to measure it against.

This is the refusal `interpolate` omitted while writing its more exotic twin. A stack with positions and no (0020,0037) passes all four existing refusals today **[verified: `volume.js:263` leaves `orientation` as `[]`; `spatial` at `:378` needs only positions and distinct depths].**

**B. `oblique_needs_stated_pixel_spacing`** — when `stack.frames[0].hasPixelSpacing === false`.

> this series does not state (0028,0030), so the distance from one pixel to the next is unknown and `volume.js` has substituted one. An oblique is cut at an angle to those pixels, and the angle only means something once the distances do: a 45 degree plane through pixels that might be 0.5 mm or 2.0 mm apart is not 45 degrees to the patient, and the voxels this plane would select are not the ones it crosses. The three named planes stay available, because they select along the lattice rather than across it.

**C. `oblique_needs_an_unsheared_stack`** — when `geometryOf(stack).sheared === true` and the plane is oblique.

> these slices are displaced within their own plane by up to __ pixels, and this module rectifies that by translating each slice a whole number of rows. The named planes index the rectified volume directly, so the rounding is a relabelling they can carry. An oblique is a direction in the patient that has to be converted into volume indices, and after a rounded rectification the volume's third axis is the table's direction rather than the slice normal — so the conversion would be wrong by up to half a row per slice, and every sample would be taken from a voxel next to the one the plane crosses.

**Not written, and the reason matters:** there is no `oblique_needs_a_lattice_axis`. The grammar in §2 cannot name a plane without one, so a runtime branch guarding that condition would be a guard on nothing — which is the shape `restrict`'s attack correctly convicted. The restriction is enforced by the vocabulary, and `obliqueGrid` returns `null` for any string outside it (a stale session-restore value), which `draw`'s existing `try` already renders as a sentence **[verified `app.js:339-347`]**.

**The toolbar asks the same function.** `buildPlaneButtons` probes `reconstructionRefusal(stack, obliqueName(axis, 45))` for the Oblique control's offered state and tooltip — and one probe is honest for the whole family **because every one of the seven refusals is angle-independent**: four are about the series, and A/B/C are about orientation, spacing and shear. This is the `restrict` attack's point (ii), and stating the angle-independence is what makes the single probe sound rather than lucky.

---

# 6. THE DECISIVE EXPERIMENT

`oblique x+45.0°` on the phantom, at the index whose plane passes nearest the nodule centre (**115**, 0.10 mm from the centre voxel).

**Stated first, because two of the three proposals got this wrong:** the volume does not contain a sphere. It contains five 2.0 mm slabs at Δz = −4, −2, 0, +2, +4 mm of in-plane radius ≈0, 3.464, 4.000, 3.464, ≈0. The reslice must reproduce *that*. Any prediction of 8.0 mm craniocaudal is a prediction about the sphere, and a correct implementation would fail it.

| # | measured | **right** | wrong, and how |
|---|---|---|---|
| **a** | `frame.pixelSpacing` | **`[0.990, 0.700]`** | **`[0.700, 0.700]`** — kept the source in-plane spacing; isotropic, so `zoomForOneToOne` offers a 1:1 on a plane that has none. **`[0.990, 0.990]`** — applied the row rule to both axes. **`[2.000, 0.700]`** — quoted `sliceSpacing`, i.e. drew a coronal |
| **b** | **samples reading 30 HU along the row axis through the nodule centre** | **exactly 9** (k ∈ [−4, +4]), extent **8 × 0.98995 = 7.92 mm → "7.9 mm"** | **exactly 5** — the rotation is in **index space**. This is the invisible bug, and it is the only test that sees it |
| **c** | samples reading 30 HU along the column axis, same frame | **exactly 11**, extent **10 × 0.700 = 7.00 mm → "7.0 mm"** | any other number means `e1 ≠ ex`, i.e. the plane is not the one it is named |
| **d** | `frame.resolutionMm` | **`[2.828, 0.700]`**, and the HUD carries `rows resolved at 2.83 mm, drawn at 0.99 mm` | **`[0.990, 0.700]`** — set resolution = pitch; the note vanishes and a 0.99 mm grid carrying 2.83 mm of detail ships with nothing saying so |
| **e** | 10 mm maximum slab, label | **`maximum over 8.9 mm`** — `slabHalf(10, 0.98995) = 4` → 9 × 0.98995 = 8.91 | **`over 10.0 mm`** — fell through `planeStepMm` to `return stack.sliceSpacing`: `slabHalf(10, 2.0) = 2` → 5 × 2.0. **`over 9.1 mm`** — quoted in-plane 0.7: 13 × 0.7. The wrong answer is the suspiciously round one |
| **f** | `frame.paddingValue` | **−1000**, declared at every index | **absent** — the zero-initialised corners read **0 HU (water)**, undeclared, and an ROI near the edge averages them in |
| **g** | rows carrying data at this index | **183 of 252**; the remaining **69 (27%)** declared padding | frame dimensions that change with `index` — the grid was cut to the chord, and the zoom jumps as the reader scrolls |

**The arithmetic for (b), because it is the row that matters.** Patient space: the row step is 0.98995 mm along `e2 = (0, .70711, −.70711)`, so `Δrow = +1.000` and `Δslice = −0.350` per sample — the row index advances exactly one voxel, the slice index stairsteps with a tread of 2.857 rows.

```
k =  0 → (row 150, slice 28)  in-plane 0.0 mm, disc radius 4.000  →  30
k = ±2 → (152/148, 27/29)     in-plane 1.4 mm, disc radius 3.464  →  30
k = ±4 → (154/146, 27/29)     in-plane 2.8 mm, disc radius 3.464  →  30      ← the deciding sample
k = ±5 → (155/145, 26/30)     round(26.25) = 26, disc radius ≈0   → −820
```

Nine samples, 7.92 mm. Index space — rotating as though the voxels were cubic — gives `Δrow = Δslice = 0.7071`, so `k = ±3` already lands on slices 26/30 and the run is **five samples, 3.96 mm, a nodule that reads half its size**. Both look like a round blob. Only the count separates them, and **(a) cannot see it**: an index-space implementation can derive the same `[0.990, 0.700]`.

**And the disclosure and the experiment are the same fact.** 7.9 down the rows against 7.0 across the columns, on a solid that is symmetric in-plane, is the 2.83 mm row resolution over-reporting the craniocaudal extent by 0.9 mm. That is exactly what (d)'s sentence exists to say, and a reader can reproduce the discrepancy with two calipers.

---

# 7. THREE GATES

### Gate 1 — the two axes are asked separately, and pitch and resolution are the same question

```python
def test_an_oblique_states_a_pitch_and_a_resolution_for_each_of_its_own_axes() -> None:
    """A 0.99 mm row carrying 2.83 mm of detail is a value that is not what it appears to
    be, and the only thing that can say so is a second number derived from the same
    formula on the same axis. Asked with the axes crossed, the note names the wrong one."""
    code = _code(_js("image", "oblique.js"))
    body = code[code.index("export function obliqueGrid("):]
    assert re.search(r"pixelSpacing: \[\s*crossingsAlong\(stack, e2\)\.pitchMm,\s*"
                     r"crossingsAlong\(stack, e1\)\.pitchMm", body)
    assert re.search(r"resolutionMm: \[\s*crossingsAlong\(stack, e2\)\.coarsestMm,\s*"
                     r"crossingsAlong\(stack, e1\)\.coarsestMm", body)
    # ONE formula. Two functions for "how far apart" is how the HUD and the slab came to
    # disagree once already, one function apart.
    assert len(re.findall(r"^export function crossings", code, re.M)) == 1
    assert "pixelSpacing[0]" not in body and "sliceSpacing" not in body, (
        "the oblique picks a named plane's spacing instead of deriving its own"
    )
```

**One-line break that must turn it red** — in `oblique.js`, change

```js
  resolutionMm: [crossingsAlong(stack, e2).coarsestMm, crossingsAlong(stack, e1).coarsestMm],
```
to
```js
  resolutionMm: [crossingsAlong(stack, e1).coarsestMm, crossingsAlong(stack, e2).coarsestMm],
```

The second regex fails. And the defect is real: `resolutionMm` becomes `[0.700, 2.828]`, so `resolutionNote` reports the *columns* as resolving at 2.83 mm — the one axis that resolves exactly — while the rows, which stairstep at 2.83 mm, are reported as honest at 0.70 mm. The sentence is still there and now points at the wrong edge.

### Gate 2 — every oblique plane has exactly one name, and one place makes it

```python
def test_every_oblique_plane_has_exactly_one_name() -> None:
    """`plane` is the key `annotations.js` matches measurements on with `===`, the string
    app.js writes into dataset.plane, and the cell measurements-panel.js escapes. Two
    spellings of one plane are two addresses, and a caliper recorded under the second is
    listed forever and drawn never."""
    oblique = _code(_js("image", "oblique.js"))
    built = [p for p in _viewer_js_files() for _ in re.findall(r"['\"`]oblique ", _code(p))]
    assert len(built) == 1 and built[0].name == "oblique.js", (
        f"the oblique plane name is constructed in {len(built)} places: {built}"
    )
    body = oblique[oblique.index("export function obliqueName("):]
    body = body[: body.index("\n}")]
    assert "toFixed(1)" in body, "45 and 45.0 are two names for one plane"
    assert re.search(r"if \(!\(abs > 0\) \|\| !\(abs < 90\)\) return null;", body), (
        "0 and 90 degrees are the axial, coronal and sagittal under a second name, and a "
        "plane with two names has its measurements split between them"
    )
```

**One-line break** — in `app.js`, replace

```js
  setPlane(obliqueName(axis, deg));
```
with
```js
  setPlane(`oblique ${axis}${deg >= 0 ? '+' : ''}${deg}°`);
```

`built` becomes length 2 and the gate names both files. The defect is exactly the one the gate describes: the bypass skips `toFixed(1)`, so a reader who reaches 45° by the slider gets `oblique x+45°` and one who reaches it by the keyboard gets `oblique x+45.0°` — two addresses, and half their measurements disappear from the image while staying in the panel.

### Gate 3 — an oblique declares its out-of-volume region as padding, whether or not the stack is sheared

```python
def test_an_oblique_declares_its_out_of_volume_region_as_padding() -> None:
    """`stack._fill` is set only when a shear correction needs it. An oblique's rectangle
    necessarily leaves the acquired box at every index — 69 of 252 rows at 45 degrees on
    this phantom — so on an unsheared stack those pixels were zero-initialised: 0 HU,
    water, undeclared, and averaged into every ROI that reached the edge. That is verbatim
    the defect test_the_shear_fill_declares_itself_as_padding was written for."""
    code = _code(_js("image", "mpr.js"))
    body = code[code.index("const common = {"):]
    assert re.search(r"const fill = g\.sheared \|\| isOblique\(plane\)", body), (
        "the reconstruction's fill is still gated on the shear alone, so an oblique of an "
        "unsheared stack has no declared value outside the acquired box"
    )
    assert re.search(r"if \(g\.sheared \|\| isOblique\(plane\)\) \{\s*stack\._fill",
                     code[code.index("function volumeOf("):]), (
        "fillValueOf is never reached for an unsheared oblique, so stack._fill is undefined "
        "and the declaration above declares nothing"
    )
```

**One-line break** — in `mpr.js`, change

```js
  const fill = g.sheared || isOblique(plane)
```
back to
```js
  const fill = g.sheared
```

The first assertion fails. The defect returns in full: on this phantom's unsheared stack, `fill` is `{}`, `paddingTest` returns `() => false`, and the 69 padding rows read 0 HU — water — at full contrast, inside the reconstruction, entering every ROI mean and every min-IP with nothing on screen distinguishing them from tissue.

---

# 8. WHAT THIS DOES NOT DO

1. **No double oblique.** Only planes containing `ex` or `ey`. No cardiac short axis, no plane normal to a vessel that bends in two axes, no MPR down a curved fracture. RadiAnt gives these. On an anisotropic volume both output axes oblique means the sample density falls below the voxel density — at 45° in-plane, by exactly half, in a checkerboard, drawn crisp — and this project would rather not offer the plane than offer that.
2. **No in-plane rotation.** A rotated axial is unreachable: the vocabulary cannot name one and the view matrix does 90° multiples and flips only. A reader who wants the aortic arch upright cannot have it.
3. **No oblique on a tilted-gantry stack** — refusal C — even one whose coronal and sagittal are offered. The rectification is a whole-pixel relabel the named planes carry and the patient→index map cannot.
4. **No oblique without (0020,0037) or (0028,0030)** — refusals A and B — on series whose coronal still works.
5. **The resolution note is per-axis worst case, not per-line.** A caliper drawn entirely along the columns, which resolve at 0.700 mm exactly, is still told the rows resolve at 2.83 mm. The note names the axis so a reader can see it does not apply to their line, but the number is conservative rather than exact for that line. `crossingsAlong` would answer the per-line question with no new formula; it is not wired up.
6. **The staircase is stated, not removed.** At 45° a slice-boundary edge — the superior pole of a nodule, the thing a reader calipers — is quantised at 2.83 mm along the row axis and drawn as a hard edge, which reads as a measured one. The picture is also displaced as a whole by up to half a source voxel diagonal, 1.1 mm here, and nothing on screen marks where. The sentence in §4 is a mitigation; the craniocaudal measurement still belongs on the axial, and §6(b)'s 7.9-against-7.0 is that fact showing.
7. **An ROI over air reads a different count on an oblique than on the axial.** The fill is the stack minimum (−1000 here) declared as padding, so genuine air is over-excluded — the trade `fillValueOf`'s own docstring already argues and `paddingNote`'s `· N padding px excluded` already reports, now reaching a plane that declares no padding of its own.
8. **No 1:1 on any oblique.** `zoomForOneToOne` refuses them as anisotropic, correctly. A reader cannot get a native-resolution zoom on these planes, and should not want one.
9. **Two gates must be amended, and both amendments are narrowings.** `test_slab_thickness_is_measured_along_the_axis_it_projects` becomes four returns, four distinct, with `returns[2]` the oblique's `stepMm` and `returns[3]` still `sliceSpacing`, and its `slabPlan` caller list becomes `["PLANES.AXIAL", "PLANES.CORONAL", "PLANES.SAGITTAL", "plane"]`. `_STACK_DERIVED` gains `resolutionMm` so both of `reslice`'s branches must name it. The point of each gate — one decision point for the step; each slab along its own normal; no field silently undefined on one plane out of three — is preserved verbatim, and the amendments must be made in the same commit as the feature or the gates pass for the wrong reason.
10. **An oblique is never cut from another oblique.** `reslice` takes a `stack` and there is no path by which its output becomes one; that is a property of the signature, not a check.