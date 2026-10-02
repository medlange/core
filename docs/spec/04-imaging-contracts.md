<!-- MedicalOS Specification v0.4.0 — chapter 4 of 19. Normative.
     161 requirements. Do not edit without a requirement-ID review. -->

[← 3. Medical Data Plane: Gateway, De-identification and Triage](03-medical-data-plane.md) · [Index](../../MEDICALOS_SPEC.md) · [5. Execution: Jobs, Queue and Failure Handling →](05-execution.md)

---

## 4. Imaging Contracts: Geometry, Preprocessing and DICOM Output

### 4.1 Scope, ownership and vocabulary

This chapter is the normative contract for everything that happens between a set of DICOM
source instances and a set of DICOM objects written back to the PACS. It defines three
things that the previous version of this specification named but never specified: the
**volume geometry contract** (§4.2), the **PreprocessingSpec** artifact and its startup
self-test (§4.3), and the **DICOM output identity contract** (§4.4–§4.10). It closes with the
**interoperability test battery** (§4.11) that gates every ServiceVersion.

**MOS-IMG-001** — The boundary of this chapter is: *source series → canonical volume →
model space → inverse → `ResultBundle` → DICOM objects → PACS*. Series *selection* is out of
scope and belongs to `SeriesSelector` and study triage (Chapter 3, MOS-DATA). De-identification
and the UID mapping table are out of scope and belong to the DICOM Gateway (Chapter 3).
Job state and retry mechanics are out of scope and belong to Chapter 5 (MOS-EXEC).

**MOS-IMG-002** — Per the ownership boundary in Chapter 2 (MOS-SVC), the **service** builds the
canonical volume, executes the `PreprocessingSpec`, runs inference and produces a `ResultBundle`.
The **platform** independently rebuilds the canonical volume from the same source instances,
validates the `ResultBundle` against it, and is the **sole writer of every DICOM object**. A
service MUST NOT construct, sign or transmit a DICOM object.

**MOS-IMG-003** — The platform MUST publish two versioned Python packages that implement this
chapter exactly once: `medicalos-imaging` (canonical volume builder, §4.2) and
`medicalos-preprocessing` (PreprocessingSpec executor, §4.3). A second implementation of either
contract anywhere in the repository — including in a training pipeline, a notebook shipped as an
example, or a worker — is a defect. The corresponding engineering rule is in Chapter 15.

**MOS-IMG-004** — A `native`-mode ServiceVersion MUST import `medicalos-imaging` and
`medicalos-preprocessing` and MUST NOT reimplement resampling, orientation handling, slice
sorting or rescale. A `sealed`-mode ServiceVersion MAY implement the contract independently but
MUST pass the geometry conformance fixture corpus (§4.11.2) at registration; conformance is
verified by the platform, not asserted by the vendor.

#### 4.1.1 Coordinate frames

Three frames exist. They are never interchangeable and every requirement below names which one
it operates in.

| Frame | Definition | Who produces it | Lifetime |
|---|---|---|---|
| **Source geometry** | The grid of the selected source DICOM instances as acquired: `PixelSpacing`, the projected slice spacing Δs of §4.2.5, the direction cosines of `ImageOrientationPatient`, the origin `ImagePositionPatient` of the first sorted slice, in the patient LPS frame identified by the source `FrameOfReferenceUID`. | The scanner | Immutable |
| **Canonical volume** | A dense `float32` array plus an explicit affine, built from source geometry by §4.2. Identical to source geometry unless gantry-tilt correction or non-uniform-spacing resampling was applied, both of which MUST be declared and recorded. | `medicalos-imaging` | Per job |
| **Model space** | The tensor the model consumes, produced from the canonical volume by the `PreprocessingSpec`. Orientation-permuted, resampled, cropped, clipped, normalised, patched. | `medicalos-preprocessing` | Per inference |

**MOS-IMG-005** — All arrays in this chapter are C-ordered and indexed `[k, j, i]` where `k` is
the slice index, `j` the row index and `i` the column index. The affine maps the *column-major*
index vector `(i, j, k, 1)` to LPS millimetres. Implementations MUST NOT silently swap these
conventions; conversion between the two is explicit and covered by unit tests.

**MOS-IMG-006** — The patient coordinate system is **LPS** (+x toward patient Left, +y toward
patient Posterior, +z toward patient Superior), matching DICOM. NIfTI is RAS+; conversion in
either direction MUST be `A_LPS = diag(-1, -1, 1, 1) @ A_RAS`. No other flip is permitted, and
`nibabel` affine handling MUST go through the single helper in `medicalos-imaging`.

**MOS-IMG-007** — Supported source SOP Classes for volume construction are listed below. Any
other SOP Class in the selected series set MUST cause rejection with
`geometry_unsupported_sop_class`.

| SOP Class | UID | Handling |
|---|---|---|
| CT Image Storage | `1.2.840.10008.5.1.4.1.1.2` | One instance per slice |
| Enhanced CT Image Storage | `1.2.840.10008.5.1.4.1.1.2.1` | One instance, geometry per frame |
| MR Image Storage | `1.2.840.10008.5.1.4.1.1.4` | One instance per slice |
| Enhanced MR Image Storage | `1.2.840.10008.5.1.4.1.1.4.1` | One instance, geometry per frame |

**MOS-IMG-008** — For Enhanced (multi-frame) SOP Classes, per-frame geometry MUST be read from
`PerFrameFunctionalGroupsSequence (5200,9230)` → `PlanePositionSequence (0020,9113)`,
`PlaneOrientationSequence (0020,9116)` and `PixelMeasuresSequence (0028,9110)`, falling back to
`SharedFunctionalGroupsSequence (5200,9229)` where a group is shared. Every rule in §4.2 that
speaks of "a slice" applies to "a frame" unchanged.

---

### 4.2 The volume geometry contract

#### 4.2.1 Inputs and the rejection vocabulary

**MOS-IMG-009** — The volume builder takes an **ordered-by-nothing** set of source instances
already selected by `SeriesSelector` (Chapter 3) plus the tenant's `PreprocessingSpec`
(§4.3) for the target ModelVersion. It emits either a `CanonicalVolume` (§4.2.8) or a single
machine-readable rejection code. It MUST NOT emit a volume with a warning.

**MOS-IMG-010** — Every geometry failure MUST terminate the job in state `REJECTED`
(Chapter 5, MOS-EXEC-001) carrying exactly one code from the table below in
`rejection.reason_code`, plus a `rejection.detail` object whose fields are named in the table.
These codes are a closed enum; adding one is a minor version of this specification.

| `reason_code` | Condition | `detail` fields |
|---|---|---|
| `geometry_missing_position` | `ImagePositionPatient (0020,0032)` or `ImageOrientationPatient (0020,0037)` absent on any instance | `sop_instance_uid` |
| `geometry_unsupported_sop_class` | SOP Class not in MOS-IMG-007 | `sop_class_uid` |
| `geometry_inconsistent_grid` | `Rows`, `Columns` or `PixelSpacing` differ across instances | `rows`, `columns`, `pixel_spacing_values` |
| `geometry_inconsistent_orientation` | Max angle between any slice's row (or column) direction cosine and the first slice's exceeds 0.1° | `max_angle_deg`, `sop_instance_uid` |
| `geometry_duplicate_positions` | Two instances at the same projected position with different pixel digests | `projected_position_mm`, `sop_instance_uids` |
| `geometry_non_uniform_spacing` | Spacing jitter exceeds the `JITTERED` tolerance and the spec does not permit resampling | `median_spacing_mm`, `max_jitter_mm` |
| `geometry_gapped` | Any local Δs exceeds 2 × median Δs | `gap_mm`, `median_spacing_mm`, `after_sop_instance_uid` |
| `geometry_gantry_tilt` | Tilt detected and the spec does not permit correction | `tilt_deg`, `detection` ∈ `tag` \| `geometric` |
| `geometry_tilt_correction_out_of_range` | Tilt exceeds `gantry_tilt.max_deg` from the spec | `tilt_deg`, `max_deg` |
| `geometry_unsupported_rescale` | `RescaleType (0028,1054)` present and not `HU`/`US`, or an unsupported Modality LUT | `rescale_type`, `sop_instance_uid` |
| `geometry_insufficient_instances` | Fewer instances remain after de-duplication than `SeriesSelector.min_instances` | `instances_after_dedup`, `min_required` |

**MOS-IMG-011** — `REJECTED` is a clinical outcome, not a failure. Every code above MUST render
in every UI as a rejection, visually distinct from `FAILED`, with the human-readable message
carried alongside the code (Chapter 5, Chapter 10).

#### 4.2.2 Slice sorting

**MOS-IMG-012** — Let `IOP = (X_x, X_y, X_z, Y_x, Y_y, Y_z)` be `ImageOrientationPatient`, where
`X` is the unit direction of increasing **column** index and `Y` the unit direction of increasing
**row** index, both in LPS. The slice normal is `n = X × Y` (cross product, in that order). `n`
is a unit vector and MUST be recomputed per slice, never assumed to be `(0, 0, 1)`.

**MOS-IMG-013** — The scalar slice position of instance `m` is `d_m = IPP_m · n_0`, the dot
product of that instance's `ImagePositionPatient` with the slice normal **of the first instance
in file order**, `n_0`. Instances MUST be sorted by `d_m` ascending. `InstanceNumber (0020,0013)`,
`SliceLocation (0020,1041)` and file name MUST NOT be used for ordering; they are unreliable and
are the classic source of superior-inferior flips.

**MOS-IMG-014** — Ties in `d_m` are resolved as follows, in order: (a) if the two instances have
identical SHA-256 digests over their decoded, rescaled `float32` pixel arrays, keep the instance
whose `SOPInstanceUID` sorts lexicographically first, drop the other, and record it in
`dropped_duplicate_sop_instance_uids`; (b) otherwise reject with `geometry_duplicate_positions`.
Two instances are "at the same position" when `|d_a − d_b| ≤ 0.01 mm`.

**MOS-IMG-015** — Orientation consistency: for every instance `m`, the angle between `X_m` and
`X_0`, and between `Y_m` and `Y_0`, MUST be ≤ 0.1° (0.001745 rad). Otherwise reject with
`geometry_inconsistent_orientation`. `Rows (0028,0010)`, `Columns (0028,0011)` and
`PixelSpacing (0028,0030)` MUST be identical across all instances (`PixelSpacing` compared
element-wise to within 1e-6 mm), otherwise reject with `geometry_inconsistent_grid`.

#### 4.2.3 Slice spacing and non-uniformity

**MOS-IMG-016** — Slice spacing MUST be derived from positions, never from tags. Let
`Δs_k = d_{k+1} − d_k` for `k = 0 … K−2`, and `Δs_med = median(Δs)`. `SliceThickness (0018,0050)`
and `SpacingBetweenSlices (0018,0088)` MUST NOT be used to construct the affine; they MAY be
recorded for reporting and MAY be consumed by `SeriesSelector`.

**MOS-IMG-017** — Uniformity classification and the mandatory action:

| Class | Condition | Action |
|---|---|---|
| `UNIFORM` | `max_k \|Δs_k − Δs_med\| ≤ max(0.01 mm, 0.005 · Δs_med)` | Accept. Canonical spacing = `Δs_med`. Canonical grid ≡ source grid. |
| `JITTERED` | Above tolerance but `≤ max(0.05 mm, 0.02 · Δs_med)`, and no `Δs_k > 2·Δs_med` | Accept. Canonical spacing = `Δs_med`. Canonical grid ≡ source grid (slice indices unchanged, no resampling). Record `spacing_class: JITTERED` and `max_jitter_mm`. |
| `NON_UNIFORM` | Beyond `JITTERED` tolerance, no gap | If `PreprocessingSpec.canonical_geometry.non_uniform_spacing == "resample_to_uniform"`: resample along `k` to `Δs_med` with `image_interpolator`; set `resampled_from_source: true`. Otherwise reject `geometry_non_uniform_spacing`. |
| `GAPPED` | Any `Δs_k > 2 · Δs_med` | Reject `geometry_gapped`. Never interpolate across a gap. |

**MOS-IMG-018** — A series in which **all** `Δs_k` are equal is `UNIFORM` regardless of the ratio
between `Δs_med` and `SliceThickness`. A genuinely gapped acquisition (for example 10 mm slices
every 20 mm) is a valid uniform volume; it is the `SeriesSelector`'s job to exclude it if the
model cannot use it, not the volume builder's.

**MOS-IMG-019** — When `resampled_from_source` is true, the canonical grid is no longer the source
grid. The mask returned by the service MUST then be resampled back onto the source slice
positions with nearest-neighbour before any SEG is written (§4.2.7), and every measurement MUST
still be computed in source geometry (§4.2.5).

#### 4.2.4 Gantry tilt

**MOS-IMG-020** — Tilt MUST be detected by two independent tests, both mandatory:

1. **Tag test.** `GantryDetectorTilt (0018,1120)` present and `|value| > 0.1°`.
2. **Geometric test.** For the displacement `v_k = IPP_{k+1} − IPP_k`, the shear angle
   `θ_k = arccos((v_k · n_0) / ‖v_k‖)`. Tilt is present if `max_k θ_k > 0.1°`.

The geometric test is authoritative; the tag test exists to catch series where positions were
rewritten by an intermediary. Disagreement between the two MUST be recorded in
`geometry.tilt_detection_disagreement`.

**MOS-IMG-021** — Default behaviour on tilt is **reject** with `geometry_gantry_tilt`. A
`PreprocessingSpec` MAY declare `canonical_geometry.gantry_tilt: {mode: "correct", max_deg: 30.0}`.
In `correct` mode the builder MUST resample the volume onto the orthogonal grid spanned by
`X_0`, `Y_0`, `n_0` with origin `IPP_0` and spacing `(Δc, Δr, Δs_med)` using
`image_interpolator`, set `tilt_corrected: true` and `tilt_deg`, and set
`resampled_from_source: true`. If `max_k θ_k > max_deg`, reject with
`geometry_tilt_correction_out_of_range`.

**MOS-IMG-022** — Tilt MUST NOT be "corrected" by shearing the affine while leaving voxels in
place, and MUST NOT be ignored on the grounds that the volume looks acceptable. A sheared volume
handed to a model trained on orthogonal grids produces a confidently wrong mask that passes every
structural check.

#### 4.2.5 Pixel values, rescale, and the voxel volume identity

**MOS-IMG-023** — Stored pixel values MUST be decoded honouring `BitsAllocated (0028,0100)`,
`BitsStored (0028,0101)`, `HighBit (0028,0102)` and `PixelRepresentation (0028,0103)`, with
sign extension for `PixelRepresentation = 1`.

**MOS-IMG-024** — The Modality LUT MUST be applied per slice, in this precedence: if
`ModalityLUTSequence (0028,3000)` is present it takes precedence and MUST be applied as a lookup;
otherwise `output = stored · RescaleSlope (0028,1053) + RescaleIntercept (0028,1052)`.
`RescaleSlope` and `RescaleIntercept` MAY differ per slice and MUST be read per slice. Output
dtype is `float32`.

**MOS-IMG-025** — The VOI LUT (`WindowCenter (0028,1050)`, `WindowWidth (0028,1051)`,
`VOILUTSequence (0028,3010)`) MUST NOT be applied. Any presentation-state transform MUST NOT be
applied. The canonical volume carries modality values (HU for CT), not display values.

**MOS-IMG-026** — For `Modality = CT`, `RescaleType (0028,1054)` MUST be absent or equal to `HU`;
`US` (unspecified) is accepted and treated as HU with `rescale_type_assumed: true` recorded. Any
other value rejects with `geometry_unsupported_rescale`.

**MOS-IMG-027** — If `PixelPaddingValue (0028,0120)` is present, stored values equal to it — or
within the range implied by `PixelPaddingRangeLimit (0028,0121)` when present — MUST be replaced,
**after** rescale, with `PreprocessingSpec.canonical_geometry.padding_output_value` (default
`-1024.0` for CT). Leaving padding at its stored magnitude corrupts every normalisation statistic
and every HU threshold measurement.

**MOS-IMG-028 (voxel volume identity, normative).** The physical volume of one voxel of the
source grid is

```
V_voxel = |det([ X·Δc , Y·Δr , v ])| = Δc · Δr · (n · v) = Δc · Δr · Δs
```

where `Δr`, `Δc` are the row and column elements of `PixelSpacing (0028,0030)` (`PixelSpacing[0]`
is the spacing **between rows**, i.e. along `Y`; `PixelSpacing[1]` is between columns, along `X`)
and `Δs` is the **projected** spacing of MOS-IMG-016, not `‖v‖`. This identity is why slices are
sorted and spaced by projection onto `n`: under gantry tilt, `‖v‖ > Δs` and using `‖v‖` inflates
every reported volume by `1/cos θ`. Implementations MUST use `Δs`.

#### 4.2.6 The affine

**MOS-IMG-029** — The canonical affine is constructed exactly as:

```
A = [[ X_x·Δc,  Y_x·Δr,  n_x·Δs,  P_x ],
     [ X_y·Δc,  Y_y·Δr,  n_y·Δs,  P_y ],
     [ X_z·Δc,  Y_z·Δr,  n_z·Δs,  P_z ],
     [ 0,       0,       0,       1   ]]
```

with `P = IPP` of the first sorted slice, mapping `(i, j, k, 1)ᵀ → (L, P, S, 1)ᵀ` in millimetres.

**MOS-IMG-030** — Because `n = X × Y` and `Δr, Δc, Δs > 0`, `det(A) > 0` always. The canonical
volume is therefore always right-handed, and a builder that produces `det(A) ≤ 0` has a defect.
CI MUST assert `det(A) > 0` on every fixture.

#### 4.2.7 Canonical → model space, and the inverse

**MOS-IMG-031** — The forward transform is exactly the ordered steps declared by the
`PreprocessingSpec` (§4.3.2) and MUST be applied in this order: (1) orientation permutation and
axis flips to `orientation_target`; (2) resample to `target_spacing_mm`; (3) foreground crop;
(4) intensity clip; (5) normalisation; (6) sliding-window patch extraction. No step may be
reordered, skipped, or fused for performance in a way that changes the numerical result.

**MOS-IMG-032** — The inverse transform MUST be the exact inverse of steps (6) → (1):
(6′) sliding-window aggregation with the declared `blend`; (5′) none (normalisation is not
inverted for label or probability outputs); (4′) none; (3′) undo crop by padding with the
declared background label or probability 0.0; (2′) resample to canonical spacing using
`label_interpolator` for discrete label maps and `probability_interpolator` for continuous maps;
(1′) undo the orientation permutation and flips.

**MOS-IMG-033** — The inverse MUST target the **recorded canonical grid**, not a recomputed one.
After inversion, the implementation MUST assert that the output array shape equals
`CanonicalVolume.shape` exactly and that the reconstructed affine matches `CanonicalVolume.affine`
element-wise within 1e-4. A mismatch is a `FAILED` job with `geometry_mismatch` (the code is
Chapter 2's, MOS-SVC-097; this chapter adds the check, not a new code), never a silently-resized
array.

**MOS-IMG-034** — Discrete label maps MUST be resampled with nearest-neighbour
(`label_interpolator: nearest`) or, when declared, `onehot_linear_argmax`. Linear or spline
interpolation applied directly to integer label values is forbidden — it invents labels that do
not exist in the label set.

**MOS-IMG-035** — When `resampled_from_source` is true (MOS-IMG-019 or MOS-IMG-021), a further
resample from the canonical grid onto the source slice positions MUST be applied to the label map
with nearest-neighbour before SEG writing and before any measurement. The platform MUST record
`source_grid_resample_applied: true` in provenance.

#### 4.2.8 The `CanonicalVolume` descriptor

**MOS-IMG-036** — `medicalos-imaging` MUST emit the descriptor below alongside the array. It is
serialised as JSON into the job input handed to the service, and is the object the platform
re-derives independently to validate the returned `ResultBundle`.

```python
from dataclasses import dataclass
import numpy as np

@dataclass(frozen=True)
class CanonicalVolume:
    array: np.ndarray                     # float32, C-order, shape (K, J, I) = [k, j, i]
    affine: np.ndarray                    # float64, (4, 4), maps (i, j, k, 1) -> LPS mm
    shape: tuple[int, int, int]           # (K, J, I)
    spacing_mm: tuple[float, float, float]  # (delta_s, delta_r, delta_c) along (k, j, i)
    origin_lps_mm: tuple[float, float, float]
    direction_lps: tuple[float, ...]      # 9 floats, column-major (X, Y, n)
    anatomical_code: str                  # 3 chars, e.g. "SPL": direction of increasing (k, j, i)
    frame_of_reference_uid: str
    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uids: tuple[str, ...]    # ordered, index k -> source instance
    uid_space: str                        # "source" | "deid"
    modality: str                         # "CT" | "MR"
    value_units: str                      # "HU" | "arbitrary"
    spacing_class: str                    # "UNIFORM" | "JITTERED" | "NON_UNIFORM"
    max_jitter_mm: float
    tilt_deg: float
    tilt_corrected: bool
    resampled_from_source: bool
    dropped_duplicate_sop_instance_uids: tuple[str, ...]
    rescale_type_assumed: bool
    pixel_digest: str                     # "sha256:<64 hex>", see MOS-IMG-037
    builder_version: str                  # medicalos-imaging package version
```

**MOS-IMG-037** — `pixel_digest` MUST be
`"sha256:" + hashlib.sha256(array.astype("<f4", copy=False).tobytes(order="C")).hexdigest()`,
computed on the canonical array after rescale and padding substitution and before any
preprocessing. This value is the `input_pixel_digest` of the provenance record (Chapter 9).

**MOS-IMG-038** — `anatomical_code` is a three-character string in `{L,R,A,P,S,I}` giving, for
array axes `(k, j, i)`, the anatomical direction of increasing index. For a head-first-supine
axial CT with `IOP = (1,0,0,0,1,0)` it is `"SPL"`. It is computed from the affine, never assumed,
and it is the value the `PreprocessingSpec.orientation_target` is reconciled against.

#### 4.2.9 The measurement rule

**MOS-IMG-039 (the measurement rule, normative).** Every measurement that appears in a
`ResultBundle`, in a DICOM SR, in the API, or in any UI MUST be computed on the **source grid**
using source `PixelSpacing` and the projected slice spacing `Δs` of MOS-IMG-016. Measurements
MUST NOT be computed in model space, and MUST NOT be computed on a canonical grid that was
resampled under MOS-IMG-019 or MOS-IMG-021. A measurement computed anywhere else is a defect,
not an approximation.

**MOS-IMG-040** — Volume:
`V_ml = N_voxels · Δr · Δc · Δs / 1000.0`, with `N_voxels` counted on the source-grid label map
and `Δr, Δc, Δs` in millimetres. Rounding for display is a presentation concern; the stored value
MUST be the unrounded `float64`.

**MOS-IMG-041** — Intensity-threshold measurements (for example `emphysema_laa`) MUST be computed
on **source HU values after the Modality LUT and padding substitution, without any resampling**:

```
LAA_percent = 100.0 * |{ v ∈ mask : HU(v) < threshold_hu }| / |mask|
```

with `threshold_hu` a declared, versioned constant on the measurement definition (Chapter 7), the
mask taken on the source grid, and the result reported with the threshold and the source
`ConvolutionKernel (0018,1210)` and `Δs` carried alongside it. Computing %LAA on a resampled grid
changes the answer by several percent and is forbidden.

**MOS-IMG-042** — Linear measurements (diameter, long axis, short axis) MUST be computed in
millimetres in the source patient frame by transforming voxel indices through `A`, never by
multiplying an index distance by a single spacing scalar.

**MOS-IMG-043** — A ModelVersion MAY declare `derived_geometry: true` to report in a grid other
than source. In that case **all** of the following MUST hold, or the job FAILS with
`derived_geometry_incomplete`: the SEG is written on the declared derived grid with its own
minted `FrameOfReferenceUID` (§4.4.2); a DICOM Spatial Registration object
(`1.2.840.10008.5.1.4.1.1.66.1`) linking the derived frame to the source frame is written in the
same commit; the SR carries the derived `PixelMeasures` and a `Measurement Method` coded concept
identifying the derived grid. `derived_geometry` is not used by any capability in releases 0.1
through 0.3.

#### 4.2.10 Tolerances — the single table

**MOS-IMG-044** — These are the only numeric tolerances in the imaging path. No other epsilon may
be introduced in code without adding a row here.

| Name | Symbol | Value | Used by |
|---|---|---|---|
| Same-position threshold | `ε_pos` | 0.01 mm | MOS-IMG-014 |
| Orientation consistency | `ε_ang` | 0.1° | MOS-IMG-015 |
| Tilt detection | `ε_tilt` | 0.1° | MOS-IMG-020 |
| Uniform spacing | `ε_unif` | `max(0.01 mm, 0.005·Δs_med)` | MOS-IMG-017 |
| Jittered spacing | `ε_jit` | `max(0.05 mm, 0.02·Δs_med)` | MOS-IMG-017 |
| Gap factor | `f_gap` | 2.0 × `Δs_med` | MOS-IMG-017 |
| PixelSpacing equality | `ε_ps` | 1e-6 mm | MOS-IMG-015 |
| Affine round-trip | `ε_aff` | 1e-4 (element-wise) | MOS-IMG-033, MOS-IMG-141, MOS-IMG-149 |
| Measurement round-trip | `ε_meas` | 1e-6 relative | MOS-IMG-144, MOS-IMG-154 |

---

### 4.3 `PreprocessingSpec`

#### 4.3.1 Identity, co-location and signature

**MOS-IMG-045** — `PreprocessingSpec` is a first-class versioned artifact (spine §3), stored in
the `Artifact` table with `kind = "preprocessing_spec"` and validated against a published
JSON Schema. It MUST be co-located with the model weights in the artifact store, MUST be covered
by the same detached signature as the weights (Chapter 6, Chapter 8), and MUST be pinned by
content digest in the provenance record of every job that used it (Chapter 9).

**MOS-IMG-046** — A `ModelVersion` MUST reference exactly one `PreprocessingSpec` version. A
change to any field of a `PreprocessingSpec` — including a resample backend version bump —
produces a new `PreprocessingSpec` version, which produces a new `ModelVersion`, which requires
its own `EvaluationRun` (Chapter 7). There is no "compatible edit".

**MOS-IMG-047** — Preprocessing MUST NOT be a stage of a shared pipeline. It is a property of the
model. A workflow with three models has three preprocessing executions, each with its own spec.
A single shared `preprocess` step is forbidden.

**MOS-IMG-048** — `medicalos-preprocessing` MUST be deterministic on CPU: single code path for
training and serving, no GPU kernels, no thread-count-dependent reductions, and an explicit
version pin of its resample backend recorded in the spec. Determinism is what makes MOS-IMG-069
meaningful.

#### 4.3.2 Field list

**MOS-IMG-049** — The schema below is complete. Every field is required unless marked optional;
an absent required field is a registration error, not a default.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema_version` | string | yes | Schema of this document. `"1.0"` for this specification. |
| `id` | string | yes | Stable artifact id, e.g. `prep.pulmo.effusion` |
| `version` | semver | yes | Version of this spec |
| `model_id` / `model_version` | string | yes | The ModelVersion this spec binds to |
| `canonical_geometry.gantry_tilt.mode` | enum `reject` \| `correct` | yes | §4.2.4 |
| `canonical_geometry.gantry_tilt.max_deg` | float | if `correct` | Upper bound for correction |
| `canonical_geometry.non_uniform_spacing` | enum `reject` \| `resample_to_uniform` | yes | §4.2.3 |
| `canonical_geometry.padding_output_value` | float | yes | Post-rescale substitute for padded voxels |
| `orientation_target` | 3-char code | yes | Anatomical directions of increasing model-space axes `(0,1,2)` |
| `axis_order` | list[enum `k`,`j`,`i`] | yes | Permutation applied to the canonical array before flips |
| `target_spacing_mm` | [float, float, float] | yes | Model-space spacing along axes `(0,1,2)` after permutation |
| `image_interpolator` | enum `nearest` \| `linear` \| `bspline3` | yes | Used for the intensity volume and for tilt/non-uniform correction |
| `label_interpolator` | enum `nearest` \| `onehot_linear_argmax` | yes | Used for discrete label maps in the inverse |
| `probability_interpolator` | enum `nearest` \| `linear` | yes | Used for continuous maps in the inverse |
| `clip.min_hu` / `clip.max_hu` | float | yes (CT) | Intensity clip window, applied before normalisation |
| `normalisation.scheme` | enum `zscore_dataset` \| `zscore_case` \| `zscore_foreground` \| `minmax` \| `clip_scale` | yes | |
| `normalisation.statistics_source` | enum `spec` \| `case` \| `foreground` | yes | Where mean/std come from. `spec` means the literal values below. |
| `normalisation.mean` / `normalisation.std` | float | if `statistics_source == spec` | Dataset-level statistics recorded at training time |
| `foreground_crop.mode` | enum `none` \| `threshold_bbox` \| `mask` | yes | |
| `foreground_crop.threshold_hu` | float | if `threshold_bbox` | |
| `foreground_crop.margin_mm` | [float×3] | if not `none` | |
| `foreground_crop.min_size_voxels` | [int×3] | if not `none` | Crop is expanded, never shrunk, to reach this |
| `foreground_crop.mask_artifact_role` | string | if `mask` | Role name of the upstream label map this crop uses |
| `patch.size_voxels` | [int×3] | yes | Model input patch, axes `(0,1,2)` |
| `patch.sliding_window_overlap` | float in [0,1) | yes | Fractional overlap between adjacent patches |
| `patch.blend` | enum `uniform` \| `gaussian` | yes | Patch aggregation weighting |
| `patch.gaussian_sigma_scale` | float | if `gaussian` | σ as a fraction of patch size |
| `patch.batch_size` | int ≥ 1 | yes | |
| `tta.axes` | list[list[int]] | yes | Mirror axes; empty list means no TTA |
| `tta.reduction` | enum `mean` \| `max` | if `tta.axes` non-empty | |
| `io.input_dtype` | enum `float32` | yes | |
| `io.input_layout` | string | yes | e.g. `"NCDHW"` |
| `io.channels` | int ≥ 1 | yes | |
| `io.output_kind` | enum `label` \| `probability` \| `logit` | yes | Determines which inverse interpolator applies |
| `io.label_set` | list[{`value`:int, `name`:string}] | if `output_kind == label` | Closed set; a value outside it is a FAILED job |
| `inverse.aggregate` | enum `gaussian_weighted_mean` \| `uniform_mean` \| `max` | yes | Step (6′) |
| `inverse.undo_crop` | enum `pad_background` | yes | Step (3′) |
| `inverse.target_grid` | const `canonical` | yes | MOS-IMG-033 |
| `inverse.assert_shape_equal` | const `true` | yes | MOS-IMG-033 |
| `inverse.affine_tolerance` | float | yes | Must equal `ε_aff` = 1e-4 |
| `backend.resampler` | string | yes | Package and exact version, e.g. `"SimpleITK==2.3.1"` |
| `backend.numpy` | string | yes | Exact numpy version the golden hash was recorded under |
| `golden_fixture.path` | string | yes | Path inside the artifact bundle |
| `golden_fixture.sha256` | sha256 digest | yes | Digest of the fixture file itself |
| `golden_fixture.output_tensor_sha256` | sha256 digest | yes | Digest of the model-space tensor, §4.3.4 |
| `golden_fixture.output_shape` | [int×5] | yes | `NCDHW` shape of that tensor |
| `golden_fixture.recorded_at` | RFC 3339 UTC | yes | |
| `golden_fixture.recorded_by_commit` | 40-hex | yes | Training-repository commit that produced the hash |

Every field typed `sha256 digest` above is encoded as `"sha256:"` followed by 64 lower-case hex
characters — the `sha256_digest` form of Chapter 12 `MOS-STORE-211`, and the encoding
`CanonicalVolume.pixel_digest` already uses (MOS-IMG-037). A bare hex string is invalid.
`recorded_by_commit` is a git object id rather than a digest of content and carries no prefix.
`backend.resampler` admits any package-and-version string; Chapter 17 `MOS-TRAIN-055` fixes the
value convention a MONAI-serialized chain writes into it and adds no field to this schema.

**MOS-IMG-050** — `normalisation.scheme = zscore_case` (per-case statistics) MUST NOT be used
unless it was the scheme used at training time. The spec is the record of what training did; it
is not a place to make a new choice.

**MOS-IMG-051** — `patch.sliding_window_overlap` and `patch.blend` MUST be the training-time
values. An overlap of 0.25 at serving against 0.5 at training, or `uniform` blending against
`gaussian`, produces seam artefacts exactly at patch boundaries and no exception anywhere.

#### 4.3.3 Complete example

**MOS-IMG-052** — The following is a valid, complete `PreprocessingSpec` for the 0.1.0 pleural
effusion capability. It is the CI fixture for schema validation.

```yaml
schema_version: "1.0"
id: prep.pulmo.effusion
version: 1.4.0
model_id: pulmo.pleural-effusion
model_version: 1.4.0

canonical_geometry:
  gantry_tilt:
    mode: reject
    max_deg: 0.0
  non_uniform_spacing: reject
  padding_output_value: -1024.0

orientation_target: "SPL"
axis_order: [k, j, i]
target_spacing_mm: [1.5, 0.8, 0.8]
image_interpolator: bspline3
label_interpolator: nearest
probability_interpolator: linear

clip:
  min_hu: -1024.0
  max_hu: 600.0

normalisation:
  scheme: zscore_dataset
  statistics_source: spec
  mean: -412.73
  std: 431.06

foreground_crop:
  mode: threshold_bbox
  threshold_hu: -300.0
  margin_mm: [10.0, 10.0, 10.0]
  min_size_voxels: [32, 128, 128]

patch:
  size_voxels: [64, 192, 192]
  sliding_window_overlap: 0.5
  blend: gaussian
  gaussian_sigma_scale: 0.125
  batch_size: 2

tta:
  axes: [[2]]
  reduction: mean

io:
  input_dtype: float32
  input_layout: NCDHW
  channels: 1
  output_kind: probability
  label_set:
    - {value: 0, name: background}
    - {value: 1, name: pleural_effusion_right}
    - {value: 2, name: pleural_effusion_left}

inverse:
  aggregate: gaussian_weighted_mean
  undo_crop: pad_background
  target_grid: canonical
  assert_shape_equal: true
  affine_tolerance: 1.0e-4

backend:
  resampler: "SimpleITK==2.3.1"
  numpy: "numpy==1.26.4"

golden_fixture:
  path: fixtures/golden_input.nii.gz
  sha256: "sha256:4c1f9a2d7e3b085614af92c0d6e8371b5a4029fd8c6e1b3740a95d2ef8137c6b"
  output_tensor_sha256: "sha256:9b2e7d410c5a8f36e1743b92ad60c8f572e14b093d8a6ce751f0b2498c3da7e6"
  output_shape: [1, 1, 64, 192, 192]
  recorded_at: "2026-03-11T09:14:22Z"
  recorded_by_commit: "a91f2c7d5b4e6038fa1c9d2e7b3045861f0ac7d9"
```

#### 4.3.4 The golden-fixture startup self-test

**MOS-IMG-053** — Every `model_version` artifact whose `weights_availability` is
`platform_managed` (Chapter 6, MOS-REG-036) MUST contain, co-located with the weights and the
`PreprocessingSpec` under one signature, the golden fixture named by `golden_fixture.path`: a
NIfTI or NRRD volume of at most 8 MiB carrying complete geometry, containing no PHI, and shipped
under the same signature as the weights (MOS-IMG-045).

**MOS-IMG-053.1** — A `sealed`-mode ServiceVersion has no platform-executed preprocessing and
therefore ships no golden fixture (Chapter 6, MOS-REG-039). Its equivalent gate is the geometry
conformance fixture corpus of §4.11.2 at registration (MOS-IMG-004) and the `POST /v1/selftest`
bundle-digest check of Chapter 2 MOS-SVC-026 at pod readiness. The two self-tests are distinct and
neither substitutes for the other.

**MOS-IMG-054** — At worker startup, and again whenever the resolved `PreprocessingSpec` version
changes, the worker MUST execute:

1. Verify `"sha256:" + sha256(fixture_bytes).hexdigest() == golden_fixture.sha256`.
2. Load the fixture into a `CanonicalVolume` through `medicalos-imaging`.
3. Apply the full forward transform of MOS-IMG-031 through `medicalos-preprocessing`, producing
   the model-space tensor `T` immediately before it would be handed to the inference backend,
   with TTA disabled and `patch.batch_size` forced to 1 so `T` is the first patch of the
   deterministic sliding-window order.
4. Compute `h = "sha256:" + sha256(numpy.ascontiguousarray(T, dtype="<f4").tobytes(order="C")).hexdigest()`.
5. Compare `h` to `golden_fixture.output_tensor_sha256` for **byte equality**.

**MOS-IMG-055** — On mismatch the worker MUST NOT become ready, MUST NOT claim any job, MUST log
a structured event `preprocessing_selftest_failed` carrying the expected digest, the observed
digest, `T.shape`, `T.dtype`, the declared and installed `backend.resampler` and `backend.numpy`
versions, and `max(|T − T_expected|)` when the expected tensor is also shipped. The readiness
probe MUST report not-ready. Chapter 13 defines how this surfaces as a deployment failure.

**MOS-IMG-056** — On mismatch the worker MUST NOT fall back to "serve anyway with a warning",
MUST NOT recompute the golden hash from the running code, and MUST NOT compare with a tolerance.
A tolerance-based comparison hides exactly the drift the test exists to catch.

**MOS-IMG-057** — The self-test result (pass/fail, observed digest, package versions, timestamp)
MUST be recorded in the provenance of every job that worker subsequently executes.

**MOS-IMG-058** — The recording side of this test is a CI obligation on the training path: the
training pipeline MUST emit `golden_fixture.output_tensor_sha256` using the identical function of
MOS-IMG-054 step 4, and a `PreprocessingSpec` whose golden hash was produced by any other code
path MUST be rejected at registration.

---

### 4.4 DICOM output: the identity contract

#### 4.4.1 Object inventory

**MOS-IMG-059** — The platform writes exactly three IOD families. Nothing else is written to the
PACS by MedicalOS in releases 0.1 through 0.4, except the Spatial Registration object required by
MOS-IMG-043.

| Kind | SOP Class | UID | Produced from |
|---|---|---|---|
| `seg` | Segmentation | `1.2.840.10008.5.1.4.1.1.66.4` | `ResultBundle.label_maps[]` |
| `sr` | Comprehensive 3D SR | `1.2.840.10008.5.1.4.1.1.88.34` | `ResultBundle.findings[]` + `ResultBundle.measurements[]` |
| `sc` | Secondary Capture Image | `1.2.840.10008.5.1.4.1.1.7` | Platform-rendered key images |

**MOS-IMG-060** — The SR SOP Class is **always** Comprehensive 3D SR, never Comprehensive SR
(`…88.33`) and never Basic Text SR, so that `SCOORD3D` evidence can be added later without a SOP
Class migration in deployed sites.

**MOS-IMG-061** — Transfer syntax MUST be Explicit VR Little Endian (`1.2.840.10008.1.2.1`) for
SEG and SR. SC MAY additionally use JPEG-LS Lossless (`1.2.840.10008.1.2.4.80`). Lossy
compression of any generated object is forbidden, and `LossyImageCompression (0028,2110)` MUST be
`"00"`.

#### 4.4.2 Deterministic UID derivation

**MOS-IMG-062** — Every UID the platform mints MUST be derived by the function below. Random UID
generation (`pydicom.uid.generate_uid()` with no entropy source pinned, `uuid4`, timestamp-based
schemes) is forbidden anywhere in the DICOM writing path. The argument tuple below is the complete
and binding input set; any chapter that names a shorter tuple is abbreviating, and the function
signature here governs.

The `idempotency_key` argument is the **platform-derived** `jobs.idempotency_key` defined in
Chapter 5 (MOS-EXEC-053). It is never client-supplied: the caller's HTTP `Idempotency-Key` header
is a request-deduplication token stored as `jobs.request_idempotency_key` (Chapter 10) and MUST
NOT be passed to `derive_uid` (Chapter 5, MOS-EXEC-054). This chapter neither defines nor derives
the key; it consumes it.

```python
import hashlib
import uuid

# Fixed for the life of the product. Changing it re-identifies every object ever written.
MEDICALOS_UID_NAMESPACE = uuid.UUID("b4c1e0a6-8f2d-5d3a-9c17-0f6a2e8b41d5")

UID_KINDS = frozenset({
    "seg.series", "seg.instance",
    "sr.series", "sr.instance",
    "sc.series", "sc.instance",
    "reg.series", "reg.instance",
    "frame_of_reference", "device_observer", "tracking",
})


def derive_uid(
    *,
    org_root: str | None,
    tenant_id: str,
    idempotency_key: str,
    service_id: str,
    service_version: str,
    model_id: str,
    model_version: str,
    uid_space: str,        # "source" | "deid"
    uid_kind: str,         # one of UID_KINDS
    output_index: int,     # >= 0, see MOS-IMG-065
) -> str:
    if uid_kind not in UID_KINDS:
        raise ValueError(uid_kind)
    if uid_space not in ("source", "deid"):
        raise ValueError(uid_space)
    name = "|".join((
        "MOS-IMG-UID-v1",
        tenant_id,
        idempotency_key,
        service_id,
        service_version,
        model_id,
        model_version,
        uid_space,
        uid_kind,
        str(output_index),
    ))
    if org_root is None:
        return "2.25." + str(uuid.uuid5(MEDICALOS_UID_NAMESPACE, name).int)
    digest12 = hashlib.sha256(name.encode("utf-8")).digest()[:12]
    return f"{org_root}.{int.from_bytes(digest12, 'big')}"
```

**MOS-IMG-063** — Length is bounded by construction. The `2.25.` form yields at most 44
characters (`2.25.` plus ≤ 39 decimal digits of a 128-bit integer). The org-root form yields at
most `len(org_root) + 1 + 29` characters, so deployments configuring `org_root` MUST enforce
`len(org_root) ≤ 34`; registration of a longer root is an error. Both forms satisfy the DICOM UI
VR limit of 64 and contain no leading-zero components.

**MOS-IMG-064** — `org_root` is a per-deployment configuration value. A deployment that has a
registered OID arc SHOULD set it; a deployment that does not MUST leave it unset and get the
`2.25.` form. Switching `org_root` after any object has been written changes every derived UID
and is therefore a breaking configuration change requiring a documented migration; the platform
MUST refuse to start if `org_root` differs from the value recorded in the deployment's first
written object unless an explicit `--allow-uid-root-change` flag is set.

**MOS-IMG-065** — `output_index` allocation is deterministic and total:

| `uid_kind` | `output_index` |
|---|---|
| `<kind>.series` | The 0-based **series index** of that kind within the job's output manifest (§4.4.5) |
| `seg.instance` | `series_index * 1000 + 0` (a SEG series holds exactly one multi-frame instance) |
| `sr.instance` | `series_index * 1000 + review_round`, where `review_round` is `0` for the SR written by the job and the 1-based `ResultReview` round ordinal for a verified SR revision issued under Chapter 9 MOS-SAFE-065. `review_round` MUST be `< 1000`; a job MUST NOT be reviewed more than 999 times. A revision reuses the `series_index` of the SR it supersedes and therefore lands as a new instance in that SR's existing series — no new `SeriesInstanceUID` is minted. |
| `sc.instance` | `series_index * 1000 + frame_ordinal`, `frame_ordinal` 0-based |
| `frame_of_reference` | `0` (only minted under `derived_geometry`) |
| `device_observer` | `0` |
| `tracking` | `segment_global_index`, the 0-based index of the segment across the job's full ordered segment list |

**MOS-IMG-066** — The ordering that produces `series_index` and `segment_global_index` MUST be
derived from the `ResultBundle` by a total, stable rule: label maps sorted by
`label_maps[].artifact_id` ascending as byte strings — that member is unique within one bundle,
because it is the key `label_map_ref` addresses a label map by (Chapter 2 MOS-SVC-092), and
`label_maps[]` carries no `role` member (MOS-SVC-084, MOS-SVC-085); within a label map, segments
sorted by label value ascending; findings sorted by `(finding.concept.scheme,
finding.concept.code, finding.finding_id)` ascending — Chapter 2's member names (MOS-SVC-080,
§2.8.5), not a second spelling of them. Iteration order of a hash map is not a rule.

**MOS-IMG-067** — `uid_space` MUST be `"source"` when the destination is the origin PACS and
`"deid"` when the destination is a de-identified research store. The discriminator is mandatory:
without it, the same job writing to both destinations would reuse one UID for two different pixel
contents, which DICOM forbids.

**MOS-IMG-068** — `FrameOfReferenceUID (0020,0052)` on a SEG MUST be **copied** from the source
series, never derived, unless `derived_geometry` is declared (MOS-IMG-043), in which case it is
derived with `uid_kind = "frame_of_reference"`.

**MOS-IMG-069** — `StudyInstanceUID (0020,000D)` MUST NEVER be minted. Minting one creates an
orphan study in the worklist and is the single most common AI-integration defect. CI MUST assert
that the string `derive_uid` never appears in the same expression as `StudyInstanceUID`.

**MOS-IMG-070** — Before the first write of a derived `SeriesInstanceUID`, the platform MUST
issue a QIDO-RS series query **across studies** for that UID. If the UID exists under a
`StudyInstanceUID` other than the job's, the job FAILS with `dicom_uid_collision` and no object
is written. This is a 96-bit / 122-bit collision guard, expected never to fire, and cheap.

#### 4.4.3 `SeriesNumber` allocation

**MOS-IMG-071** — `SeriesNumber (0020,0011)` for generated objects MUST come from the reserved
range ≥ 9000. Numbers below 9000 are the modality's and MUST NOT be used.

**MOS-IMG-072** — Each `Deployment` is allocated a `series_number_band` at creation: a value in
`{9000, 9010, 9020, …, 9890}` (90 bands of 10), assigned as the lowest free band for that tenant.
Chapter 6 owns the field and its uniqueness constraint.

**MOS-IMG-073** — Within a band `B`, the number is `B + offset + series_index`:

| Kind | `offset` | Max `series_index` |
|---|---|---|
| `seg` | 0 | 3 |
| `sr` | 4 | 2 |
| `sc` | 7 | 2 |

A ServiceVersion whose output manifest needs more series of a kind than the band allows MUST be
rejected at registration with `series_band_exhausted`. Band `9900`–`9999` is reserved for
platform-generated objects (Spatial Registration, RUO banner SC) and MUST NOT be allocated to a
Deployment.

**MOS-IMG-074** — `SeriesNumber` is a display-ordering attribute, not an identifier. Collisions
with another vendor's objects in the same study are tolerated and MUST NOT cause a write to be
retried, renumbered, or aborted. Identity is the UID.

#### 4.4.4 Equipment identity — the Type 1 tags

**MOS-IMG-075** — `Manufacturer (0008,0070)`, `ManufacturerModelName (0008,1090)`,
`DeviceSerialNumber (0018,1000)` and `SoftwareVersions (0018,1020)` are Type 1 in the Enhanced
General Equipment module that Segmentation requires. Their values MUST come from declared
identity, never from a constant in the writer and never invented at write time.

| DICOM attribute | Tag | VR | Value source |
|---|---|---|---|
| `Manufacturer` | (0008,0070) | LO | `ServiceVersion.legal_manufacturer.name` |
| `ManufacturerModelName` | (0008,1090) | LO | `ServiceVersion.display_name` |
| `DeviceSerialNumber` | (0018,1000) | LO | `f"{service_id}@{service_version}"` |
| `SoftwareVersions` | (0018,1020) | LO (VM 1-n) | `[service_version, medicalos_version, f"prep:{preprocessing_spec_version}"]` |
| `InstitutionName` | (0008,0080) | LO | `Deployment.institution_name` |
| `StationName` | (0008,1010) | SH | `Deployment.id` |

**MOS-IMG-076** — `legal_manufacturer` (name, registered address, contact, jurisdiction) is a
**required** field on `ServiceVersion` (Chapter 2, Chapter 9). A ServiceVersion without it MUST
be rejected at registration. A job whose resolved ServiceVersion has an incomplete
`legal_manufacturer` MUST FAIL with `dicom_equipment_identity_missing` before any object is
built — the platform MUST NOT write a DICOM object with a placeholder manufacturer.

**MOS-IMG-077** — Every Type 1 value above MUST be non-empty after stripping whitespace, MUST be
≤ 64 characters, and MUST NOT contain a backslash (the DICOM value delimiter). Values violating
this MUST be rejected at ServiceVersion registration, not truncated at write time.

**MOS-IMG-078** — `StationName` MUST NOT be a hostname, container id or IP address; those leak
infrastructure topology into patient records and change on every restart, breaking object
comparability.

#### 4.4.5 The output manifest and the skip-if-present retry rule

**MOS-IMG-079** — At the moment the first DICOM object of a job is successfully built, the
platform MUST persist a `dicom_output_manifest` on the job row (Chapter 5, Chapter 12): the full
ordered list of planned objects, each with `kind`, `series_index`, `series_instance_uid`,
`expected_sop_instance_uids[]`, `segment_count` and `frame_count`. The manifest is written in the
same transaction as the job's `RUNNING → phase=writing` step row.

**MOS-IMG-080** — On any retry of a job that already has a `dicom_output_manifest`, the platform
MUST NOT re-run inference. It MUST execute the reconciliation algorithm of MOS-IMG-081 against
the manifest. Inference re-runs only when no manifest exists.

**MOS-IMG-081** — Reconciliation, executed against the DICOM Gateway (never against the PACS
directly):

```python
def reconcile_and_store(manifest, study_uid, gateway, build_object):
    for obj in manifest.objects:                       # deterministic order, MOS-IMG-066
        present = gateway.qido_instances(
            study_instance_uid=study_uid,
            series_instance_uid=obj.series_instance_uid,
            includefield=["00080018"],                 # SOPInstanceUID
        )
        present_uids = {i["00080018"]["Value"][0] for i in present}
        expected_uids = set(obj.expected_sop_instance_uids)

        if present_uids == expected_uids:
            continue                                   # complete: skip, do not re-send
        unexpected = present_uids - expected_uids
        if unexpected:
            raise DicomIdentityConflict(
                series_instance_uid=obj.series_instance_uid,
                unexpected_sop_instance_uids=sorted(unexpected),
            )
        missing = sorted(expected_uids - present_uids)
        datasets = [build_object(obj, sop_instance_uid=u) for u in missing]
        response = gateway.stow(study_instance_uid=study_uid, datasets=datasets)
        if response.failed_sop_sequence:               # (0008,1198)
            raise DicomStowFailed(response.failed_sop_sequence)
```

**MOS-IMG-082** — Completeness MUST be determined by comparing the **set of SOPInstanceUIDs**,
not by `NumberOfSeriesRelatedInstances (0020,1209)` alone. A count comparison passes on a series
containing the right number of wrong instances.

**MOS-IMG-083** — `unexpected` instances in a derived series MUST FAIL the job with
`dicom_identity_conflict`. The platform MUST NOT delete them, MUST NOT overwrite them, and MUST
NOT proceed. Deletion of patient data is not an operation MedicalOS performs.

**MOS-IMG-084** — STOW-RS MUST be treated as successful only when the HTTP status is 200 and
`FailedSOPSequence (0008,1198)` is absent or empty, and `ReferencedSOPSequence (0008,1199)`
contains every submitted `SOPInstanceUID`. A 200 with a populated `FailedSOPSequence` is a
failure and MUST raise `dicom_stow_failed`.

**MOS-IMG-085** — An object already present with the derived `SOPInstanceUID` MUST NOT be
re-sent. The invariant that makes this safe is stated here explicitly: **a derived UID is a pure
function of the complete MOS-IMG-062 argument tuple — `tenant_id`, `idempotency_key`,
`service_id`, `service_version`, `model_id`, `model_version`, `uid_space`, `uid_kind`,
`output_index` — so the same UID implies the same declared inputs. Any change to pixels, segments
or measurements requires a version change, which produces a different UID.** Re-running the same
version to get "fresher" pixels is not a supported operation.

**MOS-IMG-086** — The reconciliation loop MUST be safe to execute concurrently for the same job:
two workers running it MUST converge, because both compute the same UIDs and STOW is idempotent
under MOS-IMG-085. Chapter 5 owns the lease that normally prevents concurrency.

#### 4.4.6 UID space and the reverse mapping

**MOS-IMG-087** — Services receive de-identified pixel data and de-identified `SOPInstanceUID`s;
`ResultBundle` references therefore live in the de-identified UID space. Before building any
object destined for the origin PACS, the platform MUST translate every referenced source UID —
`StudyInstanceUID`, `SeriesInstanceUID`, `SOPInstanceUID`, `FrameOfReferenceUID` — back through
the tenant's UID mapping table (Chapter 3, MOS-DATA).

**MOS-IMG-088** — A referenced UID with no reverse mapping MUST FAIL the job with
`dicom_uid_unmapped`, naming the unmapped UID. The platform MUST NOT write an object whose
`SourceImageSequence` points at a UID that does not exist in the destination.

**MOS-IMG-089** — `PatientIdentityRemoved (0012,0062)` MUST be set to `"NO"` when
`uid_space == "source"` and `"YES"` when `uid_space == "deid"`, and when `"YES"`,
`DeidentificationMethod (0012,0063)` MUST carry the tenant's de-identification profile identifier
as supplied by Chapter 3.

#### 4.4.7 Attribute inheritance: copy vs mint

**MOS-IMG-090** — The table below is exhaustive for the attributes MedicalOS controls. `copy` =
taken verbatim from the source instances; `copy-or-empty` = copied when present, written as a
zero-length Type 2 attribute when absent; `mint` = produced by MedicalOS; `n/a` = not part of
that IOD and MUST NOT be written. Attributes not listed and required by the IOD are supplied by
highdicom's constructors and MUST NOT be overridden.

| Attribute | Tag | SEG | SR | SC | Value / source |
|---|---|---|---|---|---|
| SpecificCharacterSet | (0008,0005) | mint | mint | mint | `ISO_IR 192` |
| PatientName | (0010,0010) | copy | copy | copy | source study |
| PatientID | (0010,0020) | copy | copy | copy | source study |
| IssuerOfPatientID | (0010,0021) | copy | copy | copy | omitted if absent |
| PatientBirthDate | (0010,0030) | copy | copy | copy | |
| PatientSex | (0010,0040) | copy-or-empty | copy-or-empty | copy-or-empty | |
| StudyInstanceUID | (0020,000D) | copy | copy | copy | **never minted** |
| StudyID | (0020,0010) | copy-or-empty | copy-or-empty | copy-or-empty | |
| StudyDate / StudyTime | (0008,0020)/(0008,0030) | copy | copy | copy | |
| AccessionNumber | (0008,0050) | copy-or-empty | copy-or-empty | copy-or-empty | |
| ReferringPhysicianName | (0008,0090) | copy-or-empty | copy-or-empty | copy-or-empty | |
| SeriesInstanceUID | (0020,000E) | mint | mint | mint | `derive_uid(uid_kind="<kind>.series")` |
| SeriesNumber | (0020,0011) | mint | mint | mint | MOS-IMG-073 |
| SeriesDate / SeriesTime | (0008,0021)/(0008,0031) | mint | mint | mint | object build time, UTC |
| SeriesDescription | (0008,103E) | mint | mint | mint | §4.9 marking rules |
| Modality | (0008,0060) | mint `SEG` | mint `SR` | mint `OT` | fixed per IOD |
| BodyPartExamined | (0018,0015) | copy | n/a | copy | omitted if absent |
| Laterality | (0020,0060) | copy-or-empty | n/a | copy-or-empty | |
| PatientPosition | (0018,5100) | copy | n/a | n/a | required when source is CT/MR |
| FrameOfReferenceUID | (0020,0052) | copy | n/a | n/a | mint only under `derived_geometry` |
| PositionReferenceIndicator | (0020,1040) | copy-or-empty | n/a | n/a | |
| Manufacturer | (0008,0070) | mint | mint | mint | MOS-IMG-075 |
| ManufacturerModelName | (0008,1090) | mint | mint | mint | MOS-IMG-075 |
| DeviceSerialNumber | (0018,1000) | mint | mint | mint | MOS-IMG-075 |
| SoftwareVersions | (0018,1020) | mint | mint | mint | MOS-IMG-075 |
| InstitutionName | (0008,0080) | mint | mint | mint | `Deployment.institution_name` |
| StationName | (0008,1010) | mint | mint | mint | `Deployment.id` |
| ContributingEquipmentSequence | (0018,A001) | mint | mint | mint | §4.9 |
| SOPClassUID | (0008,0016) | mint | mint | mint | MOS-IMG-059 |
| SOPInstanceUID | (0008,0018) | mint | mint | mint | `derive_uid(uid_kind="<kind>.instance")` |
| InstanceNumber | (0020,0013) | mint `1` | mint `1` | mint ordinal+1 | |
| InstanceCreationDate / Time | (0008,0012)/(0008,0013) | mint | mint | mint | |
| TimezoneOffsetFromUTC | (0008,0201) | mint `+0000` | mint `+0000` | mint `+0000` | objects are written in UTC |
| ContentDate / ContentTime | (0008,0023)/(0008,0033) | mint | mint | mint | |
| ImageType | (0008,0008) | mint `DERIVED\PRIMARY` | n/a | mint `DERIVED\SECONDARY` | |
| PhotometricInterpretation | (0028,0004) | mint `MONOCHROME2` | n/a | mint `RGB` | |
| LossyImageCompression | (0028,2110) | mint `00` | n/a | mint `00` | |
| BurnedInAnnotation | (0028,0301) | mint `NO` | n/a | mint `YES` | |
| PatientIdentityRemoved | (0012,0062) | mint | mint | mint | MOS-IMG-089 |
| DeidentificationMethod | (0012,0063) | mint | mint | mint | when `PatientIdentityRemoved = YES` |
| SegmentationType | (0062,0001) | mint | n/a | n/a | `BINARY` or `FRACTIONAL` |
| SegmentSequence | (0062,0002) | mint | n/a | n/a | §4.6 |
| ContentLabel | (0070,0080) | mint `MEDICALOS_AI` | n/a | n/a | |
| ContentDescription | (0070,0081) | mint | n/a | n/a | ≤ 64 chars |
| ContentCreatorName | (0070,0084) | mint | n/a | n/a | `legal_manufacturer.name` in PN form |
| ReferencedSeriesSequence | (0008,1115) | mint-from-source | n/a | n/a | Common Instance Reference |
| DerivationImageSequence (per frame) | (0008,9124) | mint-from-source | n/a | n/a | §4.6 |
| CompletionFlag | (0040,A491) | n/a | mint `COMPLETE` | n/a | |
| VerificationFlag | (0040,A493) | n/a | mint `UNVERIFIED` | n/a | MOS-IMG-091 |
| ConceptNameCodeSequence (root) | (0040,A043) | n/a | mint DCM 126000 | n/a | |
| CurrentRequestedProcedureEvidenceSequence | (0040,A375) | n/a | mint-from-source | n/a | §4.7.4 |
| ConversionType | (0008,0064) | n/a | n/a | mint `WSD` | |
| SecondaryCaptureDeviceManufacturer | (0018,1016) | n/a | n/a | mint | = `Manufacturer` |
| SecondaryCaptureDeviceManufacturerModelName | (0018,1018) | n/a | n/a | mint | = `ManufacturerModelName` |
| SecondaryCaptureDeviceSoftwareVersions | (0018,1019) | n/a | n/a | mint | = `SoftwareVersions` |
| ReferencedImageSequence | (0008,1140) | n/a | n/a | mint-from-source | key image provenance |

**MOS-IMG-091** — `VerificationFlag` MUST be `UNVERIFIED` on every SR a **job** writes. MedicalOS
has no human observer at job write time; marking an AI report `VERIFIED` misrepresents the
observation context. A `ResultReview` (Chapter 9) never mutates an already-written object: a
reviewed result is recorded in MedicalOS, and the single case in which a `VERIFIED` SR may be
produced is the **new SR instance** issued under Chapter 9 MOS-SAFE-065, whose conditions,
observer attributes and permission gate are Chapter 9's to define. That instance takes the
`output_index` MOS-IMG-065 allocates for its review round, is a new instance inside the superseded
SR's existing series, and MUST NOT delete or overwrite the SR it supersedes.

**MOS-IMG-092** — `ContentCreatorName (0070,0084)` is VR PN. The legal manufacturer's name MUST
be normalised to a single PN component group, with `^`, `=` and `\` removed and the result
truncated at 64 characters, and the normalisation MUST be a pure function tested in CI.

---

### 4.5 Library mandate

**MOS-IMG-093** — All SEG, SR and SC writing MUST go through **highdicom**, pinned in
`pyproject.toml` to an exact version (`highdicom==0.24.0`, `pydicom==3.0.1` for release 0.2.0).
Its constructors require the metadata this chapter mandates, which is the point: the library is
part of the enforcement, not a convenience.

**MOS-IMG-094** — Constructing a generated SEG, SR or SC by assembling a `pydicom.Dataset`
element by element, by invoking `dcmtk` command-line tools, or through any vendor SDK is
forbidden. CI MUST fail the build if `pydicom.Dataset(` or `FileDataset(` appears in the
`dicom_writer` package outside a whitelisted set of helper modules that only post-process a
highdicom-produced object.

**MOS-IMG-095** — Permitted post-processing of a highdicom-produced object is limited to: adding
the private block of §4.9, adding `ContributingEquipmentSequence`, and adding the
`copy`/`copy-or-empty` attributes of MOS-IMG-090 that highdicom does not carry over. Any other
mutation is forbidden.

**MOS-IMG-096** — Reading source DICOM MAY use `pydicom` directly. The read path is not
constrained by this section; only writing is.

**MOS-IMG-097** — The highdicom version pin is part of the imaging contract: a major or minor
bump MUST re-run the full interoperability battery (§4.11) before it may be merged, and the
battery result MUST be attached to the pull request.

---

### 4.6 DICOM SEG contract

**MOS-IMG-098** — One SEG **series** is written per `ResultBundle` label map, containing exactly
one multi-frame SEG instance, with one segment per label value present in that map. Empty
segments (zero foreground voxels on the source grid) MUST be omitted from `SegmentSequence` and
recorded in the `Result` as `empty_segments[]`; an empty segment MUST NOT be written as a segment
with no frames.

**MOS-IMG-099** — If `segment_count × frame_count > 20000`, the platform MUST split the output
into consecutive SEG series by contiguous segment groups, in the ordering of MOS-IMG-066, each
with its own `series_index`. The split rule MUST be deterministic so that retries reproduce the
identical split.

**MOS-IMG-100** — `SegmentationType (0062,0001)` MUST be `BINARY` when
`PreprocessingSpec.io.output_kind == "label"`, and MAY be `FRACTIONAL` with
`SegmentationFractionalType (0062,0010) = PROBABILITY` and
`MaximumFractionalValue (0062,000E) = 255` when the ModelVersion explicitly declares a
probability output intended for display. A thresholded probability written as `BINARY` MUST carry
the operating threshold in `SegmentAlgorithmIdentificationSequence` parameters (MOS-IMG-104).

**MOS-IMG-101** — `SegmentAlgorithmType (0062,0008)` MUST be `AUTOMATIC` for every segment
MedicalOS writes. `MANUAL` and `SEMIAUTOMATIC` are never correct for a platform-written AI
segmentation.

**MOS-IMG-102** — Every segment MUST carry both `SegmentedPropertyCategoryCodeSequence (0062,0003)`
and `SegmentedPropertyTypeCodeSequence (0062,000F)` from the code dictionary of §4.7.2. A segment
whose type code is absent renders as "Segment 1" in every viewer and is a failure of the
interoperability battery, not a cosmetic issue.

| Segment nature | Category code |
|---|---|
| Normal anatomy | SCT `123037004` "Anatomical Structure" |
| Lesion / abnormal finding | SCT `49755003` "Morphologically Abnormal Structure" |
| Fluid collection | SCT `49755003` "Morphologically Abnormal Structure" |

**MOS-IMG-103** — Every segment MUST carry `TrackingID (0062,0020)` and `TrackingUID (0062,0021)`.
`TrackingUID` MUST be `derive_uid(uid_kind="tracking", output_index=segment_global_index)`, so
that the SR measurement group (§4.7.3) and the SEG segment are joinable by a value both objects
derive independently.

**MOS-IMG-104** — `SegmentAlgorithmIdentificationSequence` MUST carry `AlgorithmName` =
`service_id`, `AlgorithmVersion` = `service_version`, `AlgorithmFamily` = DCM `123105`
"Artificial Intelligence" (CID 7162), `AlgorithmSource` = the ServiceVersion reference string, and
`AlgorithmParameters` containing at minimum `preprocessing_spec_digest` and, where applicable,
`operating_threshold`.

**MOS-IMG-105** — Per-frame `DerivationImageSequence (0008,9124)` → `SourceImageSequence (0008,2112)`
MUST reference the source instance whose slice the frame corresponds to, by
`ReferencedSOPClassUID` and `ReferencedSOPInstanceUID`, in the destination UID space
(MOS-IMG-087). Frames MUST be written in the canonical slice order of MOS-IMG-013. Every
referenced UID MUST be a member of `CanonicalVolume.sop_instance_uids`.

**MOS-IMG-106** — `PixelMeasuresSequence (0028,9110)` MUST carry the **source** `PixelSpacing` and
a `SliceThickness` equal to the projected `Δs` of MOS-IMG-016, and `PlanePositionSequence (0020,9113)`
MUST carry the **source** `ImagePositionPatient` per frame, unless `derived_geometry` is declared.
Writing model-space spacing here is the defect that makes a SEG misalign in every viewer.

**MOS-IMG-107** — `omit_empty_frames` MUST be enabled. A SEG that encodes 400 all-zero frames for
a 12-slice effusion is 30× larger for no information, and the omitted frames are reconstructible
from `PerFrameFunctionalGroupsSequence`.

**MOS-IMG-108** — Reference implementation. This is the shape the writer MUST take; the pinned
highdicom version is what CI validates against.

```python
import highdicom as hd
import numpy as np
from pydicom.sr.codedict import codes

algorithm_identification = hd.AlgorithmIdentificationSequence(
    name="pulmo.pleural-effusion",
    version="1.4.0",
    family=codes.cid7162.ArtificialIntelligence,
    source="medicalos://service/svc_pulmo_effusion@1.4.0",
    parameters={
        "preprocessing_spec_digest": "sha256:e73a04b86d2f915c08b4e7a3c95d16024f8ae37b21c609d5b7e34a80f6512cd9",
        "operating_threshold": "0.45",
        "medicalos_version": "0.2.0",
    },
)

segments = [
    hd.seg.SegmentDescription(
        segment_number=1,
        segment_label="Pleural effusion, right",
        segmented_property_category=hd.sr.CodedConcept(
            "49755003", "SCT", "Morphologically Abnormal Structure"
        ),
        segmented_property_type=hd.sr.CodedConcept("60046008", "SCT", "Pleural effusion"),
        algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
        algorithm_identification=algorithm_identification,
        tracking_id="medicalos:job_01J9F3QK7V:seg:0",
        tracking_uid="2.25.212338746194402350915827749412083315522",
        anatomic_regions=[hd.sr.CodedConcept("3341006", "SCT", "Right lung")],
    ),
]

seg = hd.seg.Segmentation(
    source_images=source_datasets,             # ordered per MOS-IMG-013
    pixel_array=mask.astype(np.uint8),         # (frames, rows, cols), source grid
    segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
    segment_descriptions=segments,
    series_instance_uid="2.25.106477911395738621007412208317744086119",
    series_number=9004,
    sop_instance_uid="2.25.331520049972186331480096174552108639225",
    instance_number=1,
    manufacturer="PulmoAI B.V.",
    manufacturer_model_name="PulmoAI Pleural Effusion",
    software_versions=["1.4.0", "medicalos 0.2.0", "prep:1.4.0"],
    device_serial_number="svc_pulmo_effusion@1.4.0",
    series_description="[AI] Pleural effusion (MedicalOS)",
    content_label="MEDICALOS_AI",
    content_description="AI-derived pleural effusion segmentation",
    content_creator_name="PulmoAI B.V.",
    transfer_syntax_uid="1.2.840.10008.1.2.1",
    omit_empty_frames=True,
)
```

---

### 4.7 DICOM SR contract (TID 1500)

#### 4.7.1 Structure

**MOS-IMG-109** — Every SR MedicalOS writes MUST be a TID 1500 *Measurement Report* with root
`ConceptNameCodeSequence` = DCM `126000` "Imaging Measurement Report", containing, in this order:

| Template | Content | Required |
|---|---|---|
| TID 1204 | Language of Content Item and Descendants: DCM `121049`, value RFC5646 `eng` "English" | MUST |
| TID 1001 / 1002 | Observation Context, device observer (§4.7.5) | MUST |
| — | Procedure reported: DCM `121058`, coded (e.g. SCT `169069000` "CT of chest") | MUST |
| TID 1600 | Image Library: every source instance actually consumed | MUST |
| TID 1411 | Volumetric ROI Measurements and Qualitative Evaluations, one group per segment | MUST for segment-derived measurements |
| TID 1501 | Measurement Group, one per non-ROI measurement | MUST for study-level measurements |

**MOS-IMG-110** — Free-text content items MUST NOT carry any clinically actionable value. Every
number a clinician could act on MUST be a `NUM` content item with a coded concept name and a UCUM
unit. A finding expressed only as a `TEXT` item is a failure of the battery.

#### 4.7.2 Coded concepts and the code dictionary

**MOS-IMG-111** — Every clinical coded concept the writer emits — finding, segmented property
category and type, anatomic region, laterality, and the concept naming a measurement together with
its UCUM unit — MUST be read at write time from `capability_concepts`, the platform's single code
dictionary (Chapter 12 `MOS-STORE-256`), which is the normalised form of the codes carried on the
Capability row (Chapter 6 `MOS-REG-042`). Each row pins `coding_scheme`, `code_value`,
`code_meaning`, `code_scheme_version`, and for a measurement `measurement_id`, `ucum_unit` and
`computation_geometry`, plus an optional `loinc_code` reserved for the later FHIR
`Observation.code` projection. This chapter defines no `code_dictionary` artifact and MedicalOS
stores none: a second code table anywhere in the platform is forbidden (`MOS-REG-042`).

**MOS-IMG-112** — A concept with no `capability_concepts` row MUST NOT be written. The writer MUST
raise rather than invent a code. Validation of every entry against the pinned release of its code
system is Chapter 6 `MOS-REG-044`'s CI obligation, not a second one.

**MOS-IMG-113** — The concepts this chapter's writer emits in releases 0.1–0.2, with the symbolic
`concept_key` this document refers to each by. **This list is documentation, not a dictionary**: it
MUST NOT be materialised in code as a table, a constant map or an enum, because
`capability_concepts` is the platform's only code table (MOS-IMG-111). Rows in the `DCM` scheme,
together with `finding_site`, `measurement.method`, `category.anatomy` and `category.abnormal`, are
**structural** — concept names and category values fixed by TID 1500, TID 1411 and CID 7150 and
supplied as constants by highdicom and `pydicom.sr.codedict`; they are never looked up and none of
them is a clinical code a Capability owns. `procedure.ct_chest` is the coded procedure of the
**source study**, not of a capability. Every remaining row names a clinical concept and MUST
resolve at write time to a `capability_concepts` row for the capability being written; Chapter 12
§12.9.1 carries the seed rows for the 0.1 capabilities, and its values govern wherever this list
and that table disagree.

| `concept_key` | Scheme | Code | Meaning |
|---|---|---|---|
| `report.title` | DCM | 126000 | Imaging Measurement Report |
| `group.measurement` | DCM | 125007 | Measurement Group |
| `tracking.identifier` | DCM | 112039 | Tracking Identifier |
| `tracking.uid` | DCM | 112040 | Tracking Unique Identifier |
| `context.language` | DCM | 121049 | Language of Content Item and Descendants |
| `context.observer_type` | DCM | 121005 | Observer Type |
| `context.observer_device` | DCM | 121007 | Device |
| `context.device_observer_uid` | DCM | 121012 | Device Observer UID |
| `context.device_observer_name` | DCM | 121013 | Device Observer Name |
| `context.device_observer_manufacturer` | DCM | 121014 | Device Observer Manufacturer |
| `context.device_observer_model` | DCM | 121015 | Device Observer Model Name |
| `context.device_observer_serial` | DCM | 121016 | Device Observer Serial Number |
| `context.procedure_reported` | DCM | 121058 | Procedure reported |
| `context.image_library` | DCM | 111028 | Image Library |
| `algorithm.name` | DCM | 111001 | Algorithm Name |
| `algorithm.parameters` | DCM | 111002 | Algorithm Parameters |
| `algorithm.version` | DCM | 111003 | Algorithm Version |
| `algorithm.family_ai` | DCM | 123105 | Artificial Intelligence |
| `finding` | DCM | 121071 | Finding |
| `finding_site` | SCT | 363698007 | Finding Site |
| `measurement.method` | SCT | 370129005 | Measurement Method |
| `quantity.volume` | SCT | 118565006 | Volume |
| `quantity.length` | SCT | 410668003 | Length |
| `quantity.long_axis` | SCT | 103339001 | Long Axis |
| `quantity.short_axis` | SCT | 103340004 | Short Axis |
| `quantity.diameter` | SCT | 81827009 | Diameter |
| `quantity.area` | SCT | 42798000 | Area |
| `anatomy.lung` | SCT | 39607008 | Lung structure |
| `anatomy.lung_left` | SCT | 44029006 | Left lung |
| `anatomy.lung_right` | SCT | 3341006 | Right lung |
| `finding.pleural_effusion` | SCT | 60046008 | Pleural effusion |
| `finding.emphysema` | SCT | 87433001 | Pulmonary emphysema |
| `procedure.ct_chest` | SCT | 169069000 | CT of chest |
| `category.anatomy` | SCT | 123037004 | Anatomical Structure |
| `category.abnormal` | SCT | 49755003 | Morphologically Abnormal Structure |
| `quantity.laa_percent` | 99MEDOS | `LAA950` | Percent lung volume below −950 HU |
| `quantity.model_probability` | 99MEDOS | `MODEL_PROBABILITY` | Model output probability |
| `param.operating_threshold` | 99MEDOS | `OPERATING_THRESHOLD` | Operating threshold |

**MOS-IMG-114** — Where a standard code exists, it MUST be used. A private code is permitted only
under the `99MEDOS` scheme designator (Chapter 12 `MOS-STORE-257` fixes the spelling, and it is the
only private value the `capability_concepts.coding_scheme` CHECK admits), only with a
`capability_concepts` row carrying a written rationale,
and only where the maintainers have recorded a search of SNOMED-CT, RadLex, DCM and LOINC that
found no applicable term. Private codes MUST be reviewed at each minor release for promotion to a
standard code.

**MOS-IMG-115** — Units MUST be UCUM, written as a `CodedConcept` with
`scheme_designator = "UCUM"`:

| Quantity | UCUM code | Code meaning |
|---|---|---|
| Volume | `ml` | mL |
| Length | `mm` | mm |
| Area | `mm2` | mm² |
| Attenuation | `[hnsf'U]` | HU |
| Percentage | `%` | % |
| Dimensionless ratio | `1` | (unity) |
| Count | `{count}` | count |

#### 4.7.3 Measurements

**MOS-IMG-116** — Every measurement content item MUST carry: a coded `ConceptNameCodeSequence`
from §4.7.2, a numeric value, a UCUM unit, and, when it derives from a segment, a
`ReferencedSegment` pointing at the SEG `SOPInstanceUID` and `ReferencedSegmentNumber (0062,000B)`.

**MOS-IMG-117** — Every measurement group derived from a SEG segment MUST carry the same
`TrackingUID` as that segment (MOS-IMG-103), and MUST carry a `finding_type` coded concept and at
least one `finding_site` coded concept.

**MOS-IMG-118** — Model probabilities MAY be written, and when written MUST use
`quantity.model_probability` with UCUM unit `1`, and MUST be accompanied in the same measurement
group by a `param.operating_threshold` NUM item with UCUM unit `1` carrying the threshold at
which the accompanying binary finding was produced. A probability written without its threshold
is uninterpretable and MUST NOT be emitted.

**MOS-IMG-119** — `%LAA` measurements MUST be written with `quantity.laa_percent`, unit `%`, and a
`measurement.method` qualifier whose text names the HU threshold, and MUST additionally carry the
source `ConvolutionKernel` and the source `Δs` as NUM/TEXT qualifiers, because %LAA is not
comparable across kernels or slice thicknesses.

**MOS-IMG-120** — Every numeric value in the SR MUST be byte-identical to the corresponding value
in the `Result` row and in the API response. The SR is a rendering of the `Result`, never a second
computation. CI asserts this (acceptance criterion 21, and MOS-IMG-154 for segment-linked volumes).

#### 4.7.4 Evidence

**MOS-IMG-121** — `CurrentRequestedProcedureEvidenceSequence (0040,A375)` MUST reference the
source study, every source series consumed, and every source `SOPInstanceUID` in
`CanonicalVolume.sop_instance_uids`, plus every SEG instance written by the same job. It MUST NOT
reference instances that were selected but dropped as duplicates (MOS-IMG-014); those appear only
in provenance.

**MOS-IMG-122** — The TID 1600 Image Library MUST contain one entry per referenced source
instance. An SR whose evidence sequence is empty or references instances absent from the
destination is a battery failure.

**MOS-IMG-123** — All evidence UIDs MUST be in the destination UID space (MOS-IMG-087).

#### 4.7.5 Device observer

**MOS-IMG-124** — The observation context MUST contain a **device** observer and MUST NOT contain
a person observer. Fields:

| Content item | Value |
|---|---|
| `context.observer_type` | `context.observer_device` (DCM 121007 "Device") |
| `context.device_observer_uid` | `derive_uid(uid_kind="device_observer", output_index=0)` |
| `context.device_observer_name` | `ServiceVersion.display_name` |
| `context.device_observer_manufacturer` | `legal_manufacturer.name` |
| `context.device_observer_model` | `ServiceVersion.display_name` |
| `context.device_observer_serial` | `f"{service_id}@{service_version}"` |

**MOS-IMG-125** — `DeviceObserverUID` MUST be derived, not random, so that two SRs produced by the
same ServiceVersion for different studies carry the same observer identity and a site can query
"everything this model observer produced".

**MOS-IMG-126** — Reference implementation:

```python
import highdicom as hd
from pydicom.sr.codedict import codes

observer_context = hd.sr.ObservationContext(
    observer_device_context=hd.sr.ObserverContext(
        observer_type=codes.DCM.Device,
        observer_identifying_attributes=hd.sr.DeviceObserverIdentifyingAttributes(
            uid="2.25.148803925169075318445032960287744118206",
            name="PulmoAI Pleural Effusion",
            manufacturer_name="PulmoAI B.V.",
            model_name="PulmoAI Pleural Effusion",
            serial_number="svc_pulmo_effusion@1.4.0",
        ),
    ),
)

volume_measurement = hd.sr.Measurement(
    name=hd.sr.CodedConcept("118565006", "SCT", "Volume"),
    value=842.7314,
    unit=hd.sr.CodedConcept("ml", "UCUM", "mL"),
)

roi_group = hd.sr.VolumetricROIMeasurementsAndQualitativeEvaluations(
    tracking_identifier=hd.sr.TrackingIdentifier(
        uid="2.25.212338746194402350915827749412083315522",
        identifier="medicalos:job_01J9F3QK7V:seg:0",
    ),
    referenced_segment=hd.sr.ReferencedSegment(
        sop_class_uid="1.2.840.10008.5.1.4.1.1.66.4",
        sop_instance_uid="2.25.331520049972186331480096174552108639225",
        segment_number=1,
        source_images=source_image_references,
    ),
    finding_type=hd.sr.CodedConcept("60046008", "SCT", "Pleural effusion"),
    finding_sites=[hd.sr.FindingSite(
        anatomic_location=hd.sr.CodedConcept("3341006", "SCT", "Right lung"),
    )],
    measurements=[volume_measurement],
)

report = hd.sr.MeasurementReport(
    observation_context=observer_context,
    procedure_reported=hd.sr.CodedConcept("169069000", "SCT", "CT of chest"),
    imaging_measurements=[roi_group],
    title=codes.DCM.ImagingMeasurementReport,
)

sr = hd.sr.Comprehensive3DSR(
    evidence=evidence_datasets,                 # source instances + the SEG, MOS-IMG-121
    content=report[0],
    series_instance_uid="2.25.284917403615092284470311158824990417362",
    series_number=9104,
    sop_instance_uid="2.25.190264431580927744118032849716253907441",
    instance_number=1,
    manufacturer="PulmoAI B.V.",
    manufacturer_model_name="PulmoAI Pleural Effusion",
    software_versions=["1.4.0", "medicalos 0.2.0", "prep:1.4.0"],
    device_serial_number="svc_pulmo_effusion@1.4.0",
    institution_name="Universitair Ziekenhuis Voorbeeld",
    series_description="[AI] Pleural effusion measurements (MedicalOS)",
    is_complete=True,
    is_verified=False,
    transfer_syntax_uid="1.2.840.10008.1.2.1",
)
```

---

### 4.8 DICOM SC contract

**MOS-IMG-127** — Secondary Capture is a **display artefact only**. An SC MUST NOT be the sole
carrier of any measurement, finding or probability; every value rendered into an SC MUST also
exist in the SR of the same job.

**MOS-IMG-128** — SC MUST be written with `ConversionType (0008,0064) = "WSD"`,
`ImageType = DERIVED\SECONDARY`, `PhotometricInterpretation = RGB`, `SamplesPerPixel = 3`,
`PlanarConfiguration = 0`, `BitsAllocated = 8`, `BitsStored = 8`, `HighBit = 7`,
`PixelRepresentation = 0`.

**MOS-IMG-129** — SC MUST NOT carry `ImagePositionPatient`, `ImageOrientationPatient` or
`FrameOfReferenceUID`. An SC with partial geometry invites viewers to attempt a spatial overlay
that is not registered. Spatial correspondence is expressed by
`ReferencedImageSequence (0008,1140)` naming the source instance the SC was rendered from.

**MOS-IMG-130** — Every SC frame MUST carry a burned-in banner. `BurnedInAnnotation (0028,0301)`
MUST be `"YES"`. The banner occupies the top of the frame, at least 24 pixels or 3% of the image
height (whichever is greater), white text on a solid black band, and reads exactly:

| `clinical_use_mode` | Banner text |
|---|---|
| `clinical` | `AI RESULT - NOT FOR PRIMARY DIAGNOSIS` |
| `research_only` | `RESEARCH USE ONLY - NOT FOR DIAGNOSTIC USE` |

**MOS-IMG-131** — The banner is the only place the AI warning can travel for SC, because SC has no
`SegmentAlgorithmType` and no device observer. It MUST NOT be made configurable off.

**MOS-IMG-132** — SC instances of one job form one series; `InstanceNumber` is the 1-based frame
ordinal; `output_index` for the UID is the 0-based ordinal (MOS-IMG-065).

---

### 4.9 Marking objects as AI-derived

**MOS-IMG-133** — Every DICOM object MedicalOS writes MUST be identifiable as AI-derived by at
least three **standard, non-private** mechanisms, so that stripping private tags downstream
cannot erase the marking.

| Mark | SEG | SR | SC |
|---|---|---|---|
| `SeriesDescription (0008,103E)` begins with `[AI] ` (or `[AI][RUO] `) | MUST | MUST | MUST |
| `ImageType` value 1 = `DERIVED` | MUST | n/a | MUST |
| `SegmentAlgorithmType = AUTOMATIC` + `AlgorithmFamily = Artificial Intelligence` | MUST | n/a | n/a |
| Device observer with AI service identity (§4.7.5) | n/a | MUST | n/a |
| `ContributingEquipmentSequence (0018,A001)` entry for MedicalOS | MUST | MUST | MUST |
| `Manufacturer` = the AI legal manufacturer, not the scanner vendor | MUST | MUST | MUST |
| Burned-in banner | n/a | n/a | MUST |

**MOS-IMG-134** — The `ContributingEquipmentSequence` entry MUST carry
`PurposeOfReferenceCodeSequence (0040,A170)` = DCM `109102` "Processing Equipment",
`Manufacturer` = `"MedicalOS"`, `ManufacturerModelName` = `"MedicalOS Control Plane"`,
`SoftwareVersions` = the MedicalOS release, `DeviceSerialNumber` = `Deployment.id`, and
`ContributionDescription` = `f"AI result generated by {service_id}@{service_version}"`.

**MOS-IMG-135** — `SeriesDescription` is limited to 64 characters (VR LO). The prefix consumes 5
(or 10 in research mode) characters; the remainder MUST be truncated on a character boundary
without ever truncating the prefix.

**MOS-IMG-136** — When `Deployment.clinical_use_mode == "research_only"` (Chapter 9), the
following MUST additionally hold, and the writer MUST refuse to emit an object that violates any
of them:

| Object | Additional research marking |
|---|---|
| SEG | `SeriesDescription` prefix `[AI][RUO] `; `ContentDescription` begins `RESEARCH USE ONLY - `; private `ClinicalUseMode = RESEARCH_ONLY` |
| SR | `SeriesDescription` prefix `[AI][RUO] `; a TEXT content item under the root with concept `finding` and value `RESEARCH USE ONLY - NOT FOR DIAGNOSTIC USE`; private `ClinicalUseMode = RESEARCH_ONLY` |
| SC | `SeriesDescription` prefix `[AI][RUO] `; banner text per MOS-IMG-130; private `ClinicalUseMode = RESEARCH_ONLY` |

Whether such an object may be routed to a clinical destination at all is decided in Chapter 3
(gateway destination policy) and Chapter 9 (clinical safety); this section only fixes the marks.

**MOS-IMG-137** — A supplementary private block MUST be written. It is supplementary: nothing may
depend on it, because de-identification downstream will remove it.

| Tag | VR | Name | Value |
|---|---|---|---|
| (0099,0010) | LO | PrivateCreator | `MEDICALOS_AI_1.0` |
| (0099,1001) | CS | AIDerived | `YES` |
| (0099,1002) | LO | ClinicalUseMode | `CLINICAL` \| `RESEARCH_ONLY` |
| (0099,1003) | LO | ServiceId | e.g. `svc_pulmo_effusion` |
| (0099,1004) | LO | ServiceVersion | e.g. `1.4.0` |
| (0099,1005) | LO | JobId | e.g. `job_01J9F3QK7V` |
| (0099,1006) | UI | ProvenanceRecordUID | `derive_uid(uid_kind="tracking", output_index=0)` |
| (0099,1007) | LO | PreprocessingSpecDigest | `sha256:e73a04b8…` |
| (0099,1008) | LO | MedicalOSVersion | e.g. `0.2.0` |

**MOS-IMG-138** — Group `0099` is odd and therefore a valid private group. The private creator
string MUST NOT change within a major version; a change makes previously written objects
unreadable by tag name.

**MOS-IMG-139** — MedicalOS MUST NOT write any object that claims a human author. `ReferringPhysicianName`
is copied from the source and is not an authorship claim; no other person-name attribute may be
populated with a human identity.

---

### 4.10 `ResultBundle` → DICOM conversion

**MOS-IMG-160** — The writer MUST refuse to convert a `Result` whose `visibility` is `shadow`
(Chapter 6 `MOS-REG-082`; Chapter 12 `MOS-STORE-275a`, `MOS-STORE-291a`). A shadow result is what a
`role = SHADOW` deployment produces: it is persisted and is the substrate for continuous site
monitoring, and it has not been through the acceptance gate that licenses clinical exposure. No
SEG, SR, SC or Spatial Registration object may be built from it, none may be handed to the DICOM
Gateway, and no `dicom_output_manifest` (MOS-IMG-079) may be written for its job; the Gateway
refuses the STOW independently (Chapter 3). The refusal is not a failure and not a `REJECTED`
outcome: the job completes normally having written zero DICOM objects. A `visibility = shadow`
result carrying a `result_dicom_objects` row is a defect (`MOS-STORE-275a`). This check MUST precede
every other rule in this section.

**MOS-IMG-140** — Label maps in a `ResultBundle` MUST conform to Chapter 2 MOS-SVC-084 —
gzip-compressed NIfTI-1 (`nifti-gzip`, `.nii.gz`) or gzip-encoded NRRD (`nrrd-gzip`), dtype
`uint8` or `uint16` — MUST carry a complete affine, and MUST be expressed **in the canonical
volume geometry** — the geometry of the `CanonicalVolume` the platform handed to the service, not
model space and not a re-derived grid. Chapter 2 owns what a service may put on the wire; this
chapter owns what the writer does with a valid bundle.

**MOS-IMG-141** — On receipt, the platform MUST independently rebuild the `CanonicalVolume` from
the source instances named in the job (deterministically reproducible from
`CanonicalVolume.sop_instance_uids` and `pixel_digest`) and MUST validate every label map against
it:

| Check | Tolerance | Failure code |
|---|---|---|
| Array shape equals `CanonicalVolume.shape` | exact | `geometry_mismatch` |
| Affine (converted to LPS) equals `CanonicalVolume.affine` | `ε_aff` = 1e-4, element-wise | `geometry_mismatch` |
| dtype ∈ {`uint8`, `uint16`} | exact | `result_dtype_invalid` |
| Distinct values ⊆ `PreprocessingSpec.io.label_set` values | exact | `result_label_out_of_range` |
| Rebuilt `pixel_digest` equals the one issued to the service | exact | `result_input_drift` |

Probability and logit maps are model-internal (`PreprocessingSpec.io.output_kind`, §4.3.2) and
MUST NOT appear in a `ResultBundle`; Chapter 2 MOS-SVC-019 closes the bundle's output kinds to
`{finding, measurement, label_map, key_image}`. A service returning a `float32` map MUST be failed
as `schema_invalid`. `geometry_mismatch` and `schema_invalid` are Chapter 2's codes (MOS-SVC-082,
MOS-SVC-097); `result_dtype_invalid`, `result_label_out_of_range`, `result_input_drift` and
`result_measurement_inconsistent` (MOS-IMG-144) are checks this chapter adds to that list, which
is closed at the document level.

**MOS-IMG-142** — A validation failure above terminates the job as `FAILED`, not `REJECTED`: it is
a service defect, not a clinical outcome. The failing label map MUST be retained in the job's
artifact store for diagnosis, and no DICOM object may be written.

**MOS-IMG-143** — The `ResultBundle` mapping to DICOM is total and fixed. The left-hand column
uses Chapter 2's member names verbatim (Chapter 2 owns the `ResultBundle` schema); this chapter
owns only the DICOM destinations.

| `ResultBundle` element | Destination |
|---|---|
| `label_maps[]` | one SEG series each (MOS-IMG-098) |
| `label_maps[].segments[].type` | `SegmentedPropertyTypeCodeSequence` |
| `label_maps[].segments[].category` | `SegmentedPropertyCategoryCodeSequence` |
| `label_maps[].segments[].anatomic_region` | `AnatomicRegionSequence (0008,2218)` |
| `label_maps[].segments[].anatomic_region_modifier` | `AnatomicRegionModifierSequence (0008,2220)` |
| `findings[]` | SR measurement groups (TID 1411 when segment-linked, TID 1501 otherwise) |
| `findings[].concept` | group `finding_type` |
| `findings[].finding_sites[]` (Chapter 2 §2.8.5) | group `finding_sites` — required by MOS-IMG-117 for segment-linked groups |
| `findings[].laterality` | `finding_site` modifier, `LateralityCodeSequence` |
| `findings[].score` | `quantity.model_probability` NUM, unit `1` |
| `findings[].operating_point.score_threshold` | `param.operating_threshold` NUM, unit `1` (MOS-IMG-118) |
| `findings[].source_sop_instance_uids[]` | `CurrentRequestedProcedureEvidenceSequence` and per-frame `SourceImageSequence` |
| `measurements[]` | SR NUM items with coded name + UCUM unit |
| `measurements[].value` / `.unit.code` / `.concept` | NUM value / UCUM unit / `ConceptNameCodeSequence` |
| `measurements[].qualifiers[]` | NUM/TEXT qualifier items on the same measurement group |
| `key_images[]` | SC instances |

**MOS-IMG-144** — Measurements arriving in the `ResultBundle` MUST be re-verified by the platform
against the returned label map on the source grid before being written to the SR: for every
volume measurement linked to a segment, the platform recomputes `V_ml` per MOS-IMG-040 and MUST
FAIL with `result_measurement_inconsistent` if the relative difference exceeds `ε_meas`. A service
may not report a volume that disagrees with the mask it shipped.

**MOS-IMG-145** — DICOM writing MUST happen in exactly one place in the codebase, the
`dicom_writer` package of the control plane's result service. It MUST NOT live in a modality
worker, in a service container, or in a tool implementation.

**MOS-IMG-146** — The write is the job's commit point. After the first successful STOW, recovery
is forward-only: a superseded result is marked `superseded_by` in MedicalOS and the DICOM object
stays where it is. No compensating deletion exists (Chapter 5).

---

### 4.11 Interoperability test battery

**MOS-IMG-147** — The battery below is normative and runnable in CI. It gates: (a) every merge
touching `medicalos-imaging`, `medicalos-preprocessing` or `dicom_writer`; (b) every
ServiceVersion registration (Chapter 6); (c) every ModelVersion promotion (Chapter 7). A
ServiceVersion that has not passed the battery MUST NOT be deployable.

#### 4.11.1 The seven checks

**MOS-IMG-148 (B-1, conformance).** `dciodvfy` from the pinned DICOM toolkit MUST return zero
errors on every generated SEG, SR and SC. Warnings MUST be captured, counted and asserted against
an explicit allowlist committed to the repository; an unlisted warning fails the build.

**MOS-IMG-149 (B-2, round trip).** The SEG MUST be read back with highdicom, the mask
reconstructed to the source grid, and asserted: Dice **exactly 1.0** against the in-memory mask
that was written (not "≥ 0.99"); reconstructed origin, spacing and direction matching the source
within `ε_aff` = 1e-4; reconstructed frame-to-instance mapping identical to
`CanonicalVolume.sop_instance_uids`.

**MOS-IMG-150 (B-3, SEG references).** `FrameOfReferenceUID` MUST equal the source series'; every
per-frame `DerivationImageSequence`→`SourceImageSequence` `ReferencedSOPInstanceUID` MUST be a
member of the source series; the encoded frame count MUST equal the number of non-empty frames
across segments; `PixelMeasuresSequence` MUST carry source spacing and projected `Δs`.

**MOS-IMG-151 (B-4, SR structure).** The SR MUST parse as a TID 1500 report with
`hd.sr.srread`; every measurement MUST have a coded `ConceptNameCodeSequence` and a UCUM unit;
the evidence sequence MUST resolve — every referenced study/series/instance UID MUST be
retrievable via QIDO-RS from the destination; the device observer fields MUST equal the job's
ServiceVersion identity exactly; no `TEXT` item may carry a numeric value.

**MOS-IMG-152 (B-5, transport).** STOW-RS MUST return 200 with an empty `FailedSOPSequence`, and
a follow-up QIDO-RS on the new `SeriesInstanceUID` MUST return exactly the expected set of
`SOPInstanceUID`s — set equality, not count equality.

**MOS-IMG-153 (B-6, identity idempotency).** Re-running the same job — which re-derives the same
`idempotency_key` under Chapter 5 MOS-EXEC-053, the key being platform-derived and not
settable — MUST produce zero new DICOM instances, MUST produce byte-identical derived UIDs,
and MUST NOT re-run inference (asserted by a counter on the inference backend). The variant where
the process is killed between the STOW and the `COMPLETED` transition MUST converge to exactly one
instance per expected UID on restart.

**MOS-IMG-154 (B-7, measurement agreement).** Every volume measurement in the written SR MUST
equal, within `ε_meas` = 1e-6 relative, the volume recomputed from the SEG read back off the PACS
on the source grid. This is the check that catches a measurement computed in model space.

#### 4.11.2 The geometry fixture corpus

**MOS-IMG-155** — The repository MUST contain a named, versioned fixture corpus of de-identified
or synthetic studies. Each fixture declares its expected outcome; the corpus is the definition of
"invalid DICOM" for this system. Chapter 14 owns how it runs in CI; this section owns its
contents.

| Fixture id | Content | Expected outcome |
|---|---|---|
| `geo-001-uniform-axial` | 320 slices, 1.0 mm, HFS, kernel B30f | `UNIFORM`, volume built |
| `geo-002-thick-uniform` | 60 slices, 5.0 mm, uniform | `UNIFORM`, volume built |
| `geo-003-jitter` | 300 slices, Δs ∈ [0.998, 1.002] | `JITTERED`, volume built, `max_jitter_mm ≈ 0.002` |
| `geo-004-nonuniform` | Δs alternating 1.0 / 1.4 | `geometry_non_uniform_spacing` (spec = reject) |
| `geo-005-gap` | one Δs of 5.0 among 1.0 | `geometry_gapped` |
| `geo-006-gantry-tilt` | `GantryDetectorTilt = 12.5°`, sheared positions | `geometry_gantry_tilt` |
| `geo-007-tilt-corrigible` | tilt 8.0°, spec `mode: correct, max_deg: 30` | volume built, `tilt_corrected: true` |
| `geo-008-duplicate-identical` | 2 instances, same position, identical pixels | duplicate dropped, volume built |
| `geo-009-duplicate-divergent` | 2 instances, same position, different pixels | `geometry_duplicate_positions` |
| `geo-010-ffp` | feet-first-prone acquisition | volume built, `anatomical_code` ≠ `"SPL"`, orientation step flips |
| `geo-011-localizer` | 2-instance localizer only | `geometry_insufficient_instances` |
| `geo-012-dose-report` | X-Ray Radiation Dose SR (`…88.67`) only | `geometry_unsupported_sop_class` |
| `geo-013-coronal-reformat` | reformatted coronal series | volume built; `SeriesSelector` exclusion asserted separately (Chapter 3) |
| `geo-014-mixed-iop` | one slice rotated 0.5° | `geometry_inconsistent_orientation` |
| `geo-015-mixed-grid` | one slice 512², rest 768² | `geometry_inconsistent_grid` |
| `geo-016-per-slice-rescale` | `RescaleSlope` differs on 3 slices | volume built; HU correct on those slices |
| `geo-017-padding` | `PixelPaddingValue` present | padded voxels equal `padding_output_value` |
| `geo-018-signed-pixels` | `PixelRepresentation = 1`, `BitsStored = 12` | volume built; HU range plausible |
| `geo-019-enhanced-ct` | single multi-frame Enhanced CT instance | volume built from per-frame groups |
| `geo-020-single-instance` | 1 instance | `geometry_insufficient_instances` |
| `geo-021-no-ipp` | `ImagePositionPatient` absent | `geometry_missing_position` |
| `geo-022-rescale-type-od` | `RescaleType = "OD"` on a CT | `geometry_unsupported_rescale` |

**MOS-IMG-156** — Every fixture MUST be ≤ 8 MiB, MUST contain no PHI, and MUST be generated by a
committed script from a public or synthetic source so it can be regenerated and audited. A fixture
committed as opaque binary with no generator is not acceptable.

#### 4.11.3 Viewer verification

**MOS-IMG-157** — **AMENDED at specification 0.4.0.** ~~A pinned-version OHIF rendering check MUST be automated for release 0.1.0, where
it is registered as acceptance test AT-11 and is release-blocking (Chapter 14, MOS-TEST-062 and
MOS-TEST-068 through MOS-TEST-071): a
headless browser loads the study, and the test asserts that the SEG overlay is present at the
expected slice indices and that the SR measurement panel hydrates with the expected number of
measurements carrying non-empty coded names. "A human looked at it and it displayed" is not a
check.~~

The text is struck and the obligation is not. This requirement's subject was never OHIF. It is
the only automated statement anywhere in this specification that the objects the platform
WRITES are correct in the hands of a renderer the platform did not write, and OHIF was the
pinned instance of "an independent viewer" — chosen because it was already deployed, not
because anything here depends on it. Release 0.4.0 withdrew that deployment:
`medos/deploy/compose/docker-compose.yml` no longer runs `ohif/app:v3.9.2`, and the service that
carried the name is a plain nginx serving the first-party viewer and the
extension package (and, until the specification-0.4.0 withdrawal of the engineering
surface, the training console). Withdrawing a deployment withdraws a container. `ohif/app:v3.9.2` is a
pinned tag, it stays in that file's history, and it remains raisable for exactly this check.
What changes is the pin, and only the pin: from an image digest asserted against a service the
stack is running, to a named viewer and version that the check raises for itself.

That availability is NOT the condition `docs/adr/BUILD_VS_ADOPT.md` attached to retiring the
incumbent, and the comment at `medos/deploy/compose/docker-compose.yml` which says it is should not
be read into this requirement. The ADR's condition is about evidence, not about a tag: "the
first-party viewer has no rendering-correctness evidence yet, and retiring the incumbent before
it does would be worse than the tension with `MOS-UI-205`." That evidence does not exist —
register entry 106 in `docs/spec/99-known-inconsistencies.md` restates it as still owed at
0.4.0 — so the incumbent was retired with the ADR's condition unmet. A pinned tag preserves the
ability to discharge THIS requirement. It is not the evidence the ADR asked for, and this
chapter records the difference rather than letting a container comment settle it.

The check MUST NOT be re-pointed at the first-party viewer at `viewer/`, served at
`/mos-viewer/`, and that is why this requirement is amended rather than re-worded onto the
surface the platform now ships. A check whose renderer under test is also its oracle proves
nothing: where the writer and the viewer share an assumption — about per-frame ordering in a
SEG, about resolving `ReferencedSOPInstanceUID` back to a source instance, about which way
`ImageOrientationPatient` runs — the two agree, the picture is wrong twice in the same
direction, and the test is green beside it. `MOS-CORE-038`'s row in Chapter 1 names what is
relied on instead in as many words: a site MAY still read results in OHIF, 3D Slicer, Weasis or
its incumbent viewer, and `MOS-IMG-158` requires that at least one of them be used to verify the
objects. That sentence is why reversing `MOS-CORE-038` was safe, and it is true only while this
check exists. The converse obligation — that the first-party viewer renders FOREIGN input
correctly — is recorded as unverified in `docs/adr/BUILD_VS_ADOPT.md`, belongs to Chapter 19,
and is not discharged here.

**MOS-IMG-157a** — A pinned-version rendering check against an **independent** viewer MUST be
automated for release 0.1.0, where it is registered as acceptance test AT-11 and is
release-blocking (Chapter 14, `MOS-TEST-062`; its steps are `MOS-TEST-068` through
`MOS-TEST-071`). The check drives the viewer with no human in it — a headless browser for a web
viewer, the viewer's own scripting interface for a desktop one — loads the study, and asserts
that the SEG overlay is present at the expected slice indices, under the alignment measurement
of `MOS-TEST-069` rather than by observing that something appeared, and that the SR measurements
hydrate with the expected number of items carrying non-empty coded names. "A human looked at it
and it displayed" is not a check. Four further clauses bind, and each is checkable rather than
asserted:

- **Independent means not ours.** The viewer under test MUST NOT be `viewer/`, MUST NOT be
  built from this repository, and MUST share no source with it. The acceptable set is the Viewer
  row of the `MOS-REL-027` register (Chapter 15 §15.3.1, `docs/adr/BUILD_VS_ADOPT.md`): OHIF v3,
  3D Slicer, Weasis, dwv, or a site's incumbent viewer. `ohif/app:v3.9.2` is the pin already
  recorded there and needs no new decision to use.
- **Pinned by name, version and digest.** The pin MUST identify exactly one build — viewer name,
  version, and the digest or checksum of the artifact the check raises — and MUST live in a data
  file, so that a viewer upgrade changes the pin and not the test (`MOS-TEST-068`). It MUST NOT
  be expressed as the digest of a running service: since 0.4.0 no viewer is part of the deployed
  stack, and a pin asserted against a container nobody starts passes by being absent. That
  second sentence contradicts `MOS-TEST-068` as it currently stands, and the contradiction is
  named here rather than left to be discovered by whoever writes the job: `MOS-TEST-068` makes
  CI job `viewer-pin` assert "the running container's `RepoDigest`" against
  `medos/deploy/compose/ohif.lock`, and that mechanism died with the deployment while the obligation
  did not. Chapter 14 owns AT-11's steps (`MOS-IMG-159`) and therefore owns the repair; this
  clause MUST NOT be read as having made it, and until it lands the two requirements are in open
  conflict, held by register entry 107.
- **The viewer is named in the release record.** The Release Decision Record MUST state which
  viewer and which version the check ran against. "A pinned viewer" with no name is not a pin,
  and a reader of a conformance report otherwise cannot tell which renderer the evidence came
  from.
- **Two renderers, not one run twice.** Until this amendment the two checks named disjoint
  viewers — this one OHIF, `MOS-IMG-158` 3D Slicer or Weasis — so the pair exercised MedicalOS
  output in two foreign renderers, and the named set of this requirement is now wide enough to
  swallow that property by accident. It is not traded away here: the viewer under test MUST NOT
  be the same product as the one used for `MOS-IMG-158`. The reason is the self-oracle argument
  above, one level out — two runs of Weasis agree with each other about a SEG that Weasis
  happens to tolerate, and a writer bug Weasis forgives is then green twice and unfound. A
  single execution MUST NOT be recorded against both in any case: `MOS-IMG-159` makes them
  additive, they gate different releases, and one is automated where the other is a documented
  manual procedure.

One part of AT-11 cannot move with the rest, and it is recorded here rather than left to be
found in a release review. `MOS-TEST-071` asserts the fifteen fields of the MedicalOS provenance
panel, and an independent viewer does not have one — since 0.4.0 that panel is the platform's
own surface, the first-party viewer and the extension package served at `/medicalos/`. It is an
assertion about what MedicalOS displays, not about whether a foreign renderer reads what
MedicalOS wrote, and it MUST NOT be counted as the independent-viewer evidence this requirement
exists to produce. AT-11 therefore runs across two surfaces; Chapter 14 owns how it is composed,
and until that is written a Release Decision Record claiming AT-11 passed MUST state which
surface each step ran against. The standing record is that the check has never been run at all,
by test or by hand — Chapter 19 `MOS-UI-013b` says so in those words — and entry 107 of
`docs/spec/99-known-inconsistencies.md` is opened by this amendment to hold that open, because
re-pointing a check is not running it and entry 106 was closed at 0.4.0 without it.

**MOS-IMG-158** — Verification against at least one independent third-party viewer (3D Slicer or
Weasis) MUST be part of release 0.4.0 acceptance, executed at minimum as a documented manual
procedure with screenshots attached to the release record.

**MOS-IMG-159** — Checks MOS-IMG-148 through MOS-IMG-154 are this chapter's own blocking set.
~~MOS-IMG-157~~ MOS-IMG-157a and MOS-IMG-158 are additive to them and MUST NOT be used as substitutes for them;
their release gating is owned by Chapter 14 — AT-11 at 0.1.0 and AT-22 at 0.4.0 — and this chapter
MUST NOT be read as relaxing it. The citation was re-pointed at specification 0.4.0 when
`MOS-IMG-157` was amended; nothing else in this requirement changed, and neither did the gating
it names.

---

### Acceptance criteria

These are executable. Each names the artifact it inspects and the condition that fails it.

1. **Geometry corpus.** Running `medicalos-imaging` over the 22 fixtures of MOS-IMG-155 produces,
   for each, exactly the declared outcome — a `CanonicalVolume` with the declared
   `spacing_class`/`tilt_corrected`/`anatomical_code`, or exactly the declared `reason_code`. Any
   fixture producing a different outcome, or producing a volume where a rejection is declared,
   fails.
2. **Right-handedness.** For every fixture that yields a volume, `det(affine) > 0` and
   `spacing_mm` is element-wise positive.
3. **Sorting independence.** Shuffling the input instance order of `geo-001-uniform-axial` 100
   times yields 100 byte-identical `pixel_digest` values.
4. **Tilt arithmetic.** For `geo-007-tilt-corrigible`, the volume of a synthetic 100 mL sphere
   computed per MOS-IMG-040 is within 1.0% of 100 mL, and the same computation using `‖v‖`
   instead of `Δs` is demonstrably outside that bound — proving the test discriminates.
5. **Padding and rescale.** For `geo-017-padding`, no voxel retains the stored padding magnitude;
   for `geo-016-per-slice-rescale`, HU values on the affected slices match a hand-computed
   reference within 1e-3.
6. **Spec schema.** The YAML of MOS-IMG-052 validates against the published
   `PreprocessingSpec` JSON Schema; removing any field marked required in MOS-IMG-049 makes
   validation fail.
7. **Golden fixture, positive.** A worker started with a matching `platform_managed` model
   artifact (MOS-IMG-053) reports ready within its startup budget and its readiness payload
   carries `preprocessing_selftest: {status: "pass", digest: "sha256:<64 hex>"}`. A `sealed`-mode
   ServiceVersion is not subject to this check (MOS-IMG-053.1); it reaches readiness through
   Chapter 2 MOS-SVC-026 instead.
8. **Golden fixture, negative.** Perturbing one voxel of the shipped golden fixture, or pinning a
   different `backend.resampler` version, causes the worker to report not-ready, to emit
   `preprocessing_selftest_failed`, and to claim zero jobs over a 60-second observation window.
9. **Single implementation.** A repository-wide search finds resampling, orientation-reordering
   and slice-sorting code only inside `medicalos-imaging` and `medicalos-preprocessing`; a
   deliberately added duplicate in a training script fails the lint rule.
10. **UID determinism.** `derive_uid` invoked 1000 times with identical arguments across three
    processes returns one distinct value; changing any single argument, including `uid_space`,
    changes it.
11. **UID length.** For 10⁵ random argument tuples, both the `2.25.` form and an `org_root` form
    with `len(org_root) = 34` produce UIDs of ≤ 64 characters matching
    `^[0-9]+(\.[0-9]+)*$` with no component having a leading zero.
12. **StudyInstanceUID.** A static check proves no code path assigns a derived or generated value
    to `StudyInstanceUID`; a mutation test that makes the writer mint one causes the SEG and SR
    round-trip tests to fail.
13. **Idempotency, clean.** Submitting the same job content twice — so that Chapter 5
    MOS-EXEC-053 derives the same `idempotency_key`, which no client can set — yields exactly one
    `results` row per kind, exactly one `SeriesInstanceUID` per kind in the PACS, and an
    inference-invocation counter of 1.
14. **Idempotency, crash.** Killing the worker between STOW and the `COMPLETED` transition and
    restarting yields the same three counts as criterion 13, with the QIDO reconciliation logged
    as `skipped_complete` for every already-written object.
15. **Identity conflict.** Injecting a foreign instance into a derived series causes the job to
    FAIL with `dicom_identity_conflict`, and the foreign instance still exists in the PACS
    afterwards.
16. **Equipment Type 1.** Every generated SEG has non-empty `Manufacturer`,
    `ManufacturerModelName`, `DeviceSerialNumber` and `SoftwareVersions`, each ≤ 64 characters
    with no backslash; a ServiceVersion registered without `legal_manufacturer` is rejected, and a
    job forced past registration fails with `dicom_equipment_identity_missing` before any STOW.
17. **SeriesNumber.** Every generated series has `SeriesNumber ≥ 9000` and inside the
    Deployment's allocated band; a ServiceVersion declaring five SEG series is rejected with
    `series_band_exhausted`.
18. **dciodvfy.** `dciodvfy` returns zero errors on every object produced from every corpus
    fixture; warnings are all present in the committed allowlist.
19. **SEG round trip.** Dice between the written mask and the mask read back off the PACS is
    exactly 1.0; origin, spacing and direction match source within 1e-4; `FrameOfReferenceUID`
    equals source.
20. **SEG frame references.** Every per-frame `SourceImageSequence` UID is a member of
    `CanonicalVolume.sop_instance_uids`, and no dropped-duplicate UID appears anywhere in the
    object.
21. **SR structure.** The SR parses as TID 1500; the count of NUM items with a coded name and a
    UCUM unit equals the count of measurements in the `Result` row; every NUM value is
    byte-identical to the corresponding `Result` row value and to the API response value
    (MOS-IMG-120); the count of `TEXT` items containing a decimal number is zero.
22. **SR evidence.** Every UID in `CurrentRequestedProcedureEvidenceSequence` resolves via
    QIDO-RS against the destination; the SEG written by the same job is among them.
23. **Device observer.** `DeviceObserverUID` equals `derive_uid(uid_kind="device_observer",
    output_index=0)` for the job's ServiceVersion, and the four device observer text fields equal
    the ServiceVersion identity strings byte for byte.
24. **Measurement agreement.** For every segment-linked volume in the SR, recomputing from the
    SEG read back off the PACS agrees within 1e-6 relative; deliberately switching the writer to
    model-space spacing makes this criterion fail.
25. **Measurement rule.** A test model whose `target_spacing_mm` is `[1.0, 1.0, 1.0]` against a
    5 mm source produces a reported volume that matches the source-grid computation, not the
    model-grid computation, and the two differ by more than `ε_meas` in the fixture — proving the
    check discriminates.
26. **Code dictionary.** Every clinical `CodedConcept` emitted by the writer resolves to a
    `capability_concepts` row, and every other one to a structural concept of MOS-IMG-113;
    introducing an unlisted code raises at write time rather than producing an object. A
    repository-wide search finds no second table, constant map or enum of SNOMED, RadLex or
    private codes (Chapter 6 `MOS-REG-042`).
27. **AI marking.** Every generated object satisfies all rows of MOS-IMG-133 applicable to its
    kind; stripping the private group `0099` leaves at least three standard marks intact.
28. **RUO marking.** With `clinical_use_mode = research_only`, every object carries the
    `[AI][RUO] ` prefix and its kind-specific research marking, and the writer refuses (raises,
    writes nothing) when the marking is programmatically removed.
29. **SC banner.** Every SC frame has `BurnedInAnnotation = YES` and a banner band of at least
    `max(24, 0.03 × rows)` pixels whose OCR'd text equals the exact string for the active
    `clinical_use_mode`.
30. **ResultBundle validation.** A label map submitted with a one-voxel shape difference, with an
    affine off by 1e-3, with a label value outside `io.label_set`, or with `float64` dtype each
    fails the job with the corresponding code from MOS-IMG-141, and no DICOM object is written in
    any of the four cases.
31. **Writer locality.** A repository-wide search finds SEG/SR/SC construction only in the
    `dicom_writer` package; adding a `pydicom.Dataset(` construction inside it outside the
    whitelist fails the build.
32. **Library pin.** `highdicom` and `pydicom` are pinned to exact versions in the lockfile, and
    a CI job asserts the battery was re-run on the commit that last changed either pin.
33. **Shadow results are never converted.** A job dispatched to a `role = SHADOW` deployment
    stores its `Result` with `visibility = shadow`, writes zero DICOM objects, creates no
    `dicom_output_manifest`, issues zero STOW-RS requests, and still reaches `COMPLETED`; a
    mutation that forces the writer past the check makes this criterion fail. (MOS-IMG-160)

---

[← 3. Medical Data Plane: Gateway, De-identification and Triage](03-medical-data-plane.md) · [Index](../../MEDICALOS_SPEC.md) · [5. Execution: Jobs, Queue and Failure Handling →](05-execution.md)
