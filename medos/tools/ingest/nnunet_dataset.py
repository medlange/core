#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Build an nnU-Net raw dataset from a Slicer NRRD corpus, with a per-case supervision mask.

WHAT THIS IS, AND WHAT IT IS NOT
---------------------------------
It is a BYPASS. The platform's route to a fit is console -> cohort -> seal -> DatasetVersion
-> trainer, and every step of that exists so a model can be traced to the exact images and
decisions that produced it. This tool skips all of it and writes files straight onto disk.

So a model trained from this output has NO sealed DatasetVersion, NO provenance record and
NO ValidationReport, and nothing produced here may be served, published or compared with a
model that does. It answers one question -- does this data train -- which is worth knowing
BEFORE building the evidence chain around it, because if the answer is no the chain would
only have failed more slowly.

THE SUPERVISION RULE, WHICH IS THE ONLY INTERESTING DECISION HERE
------------------------------------------------------------------
This corpus is partially labelled in two different ways, and they need different answers.

  SEGMENT ABSENT.  15 of 100 cases have no `benign` segment at all. Nobody drew one and
  nobody recorded why. That is UNSUPERVISED: the honest reading is that nobody looked, and
  training it as "no benign nodule here" would teach the model that benign nodules do not
  occur in exactly the population where they do.

  SEGMENT PRESENT, ZERO VOXELS.  22 cases have a `neo` segment containing nothing, and 13
  have an empty `benign`. This is the harder one and the default is CONSERVATIVE --
  unsupervised -- for a measured reason: 82% of segments in this corpus carry
  `Segmentation.Status: inprogress` and exactly 2 say `completed`, so "the annotator
  created the segment and did not finish it" is at least as good a reading as "the
  annotator looked and found none". The two are indistinguishable in the file, and only one
  of them is safe to train on.

  Dropping them costs less than it looks. Most voxels in a supervised case are background,
  so the model still sees abundant negative evidence -- it just never sees a whole case
  asserted as finding-free on evidence nobody can confirm. `--empty-is-negative` takes the
  other reading for anyone who can confirm it, and the choice is recorded in the output.

A case that ends up supervising NO channel is excluded rather than staged, because a case
with an all-zero mask contributes nothing to any gradient and would only inflate the
epoch's step count.

THE LABEL VALUES ARE REMAPPED BY NAME AND NEVER BY POSITION. `Segment0` is `neo` in some
cases and `benign` in others; a builder that trusted the order would silently swap the
classes on an unknown subset. Every segment is resolved through the channel declaration
(`medos/medos/training/channelmap.py`), which refuses a raw name nobody declared.

Usage:
    python medos/tools/ingest/nnunet_dataset.py --root F:/... --corpus-id lung-nods-100 \\
        --out D:/MedOS-data/nnunet/raw --dataset-id 501 --dataset-name LungNods
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

try:
    import nibabel as nib
    import nrrd
except ImportError:  # pragma: no cover - import guard
    sys.exit("pynrrd and nibabel are required.\n  pip install pynrrd nibabel")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from medos.training.channelmap import load_channel_map  # noqa: E402
from tools.ingest.nrrd_to_dicom import _segments, raw_segment_names  # noqa: E402

TOOL = "medicalos-nnunet-dataset"


def _affine(header: dict[str, Any]) -> np.ndarray:
    """LPS (NRRD/DICOM) -> RAS (NIfTI). Flipping x and y is the whole conversion.

    Getting this wrong mirrors the patient, and a left/right mirror in a lung study does
    not announce itself -- the image still looks like a chest. The image and its label go
    through the SAME transform here, so a mirror would at least be consistent between
    them, which is precisely why it would survive training and only surface at deployment.
    """
    directions = np.asarray(header["space directions"], dtype=float)
    origin = np.asarray(header["space origin"], dtype=float)
    lps_to_ras = np.diag([-1.0, -1.0, 1.0])
    affine = np.eye(4)
    affine[:3, :3] = lps_to_ras @ directions.T
    affine[:3, 3] = lps_to_ras @ origin
    return affine


class Misaligned(ValueError):
    """The segmentation cannot be placed on the image grid from its header alone."""


def align_to_image(
    labels: "np.ndarray",
    image_shape: tuple[int, ...],
    image_header: dict[str, Any],
    seg_header: dict[str, Any],
    *,
    tolerance: float = 0.05,
) -> "np.ndarray":
    """Return `labels` resampled-by-translation onto the image's voxel grid.

    Translation only. No interpolation, no resampling, no nearest-neighbour: a label map
    is categorical, and the two grids here differ by a whole number of voxels, so the
    correct operation is to copy the overlapping block. Anything fancier would invent
    label values at boundaries.
    """
    idirs = np.asarray(image_header["space directions"], dtype=float)
    sdirs = np.asarray(seg_header["space directions"], dtype=float)
    if not np.allclose(idirs, sdirs, rtol=1e-4, atol=1e-6):
        raise Misaligned(
            f"the segmentation's voxel axes differ from the image's "
            f"({sdirs.tolist()} vs {idirs.tolist()}). That is a resampling, not a shift, "
            f"and translating it would move every label through the body."
        )

    delta_world = np.asarray(seg_header["space origin"], float) - np.asarray(
        image_header["space origin"], float
    )
    # idirs rows are the world vector of one step along each ARRAY axis, so the transpose
    # maps voxel offsets to world offsets and its inverse does the reverse.
    shift = np.linalg.solve(idirs.T, delta_world)
    rounded = np.rint(shift)
    if np.any(np.abs(shift - rounded) > tolerance):
        raise Misaligned(
            f"the segmentation is offset by {shift.round(3).tolist()} voxels, which is not "
            f"a whole number within {tolerance}. A sub-voxel offset means the two grids do "
            f"not line up and the labels would have to be interpolated."
        )
    rounded = rounded.astype(int)

    declared = str(seg_header.get("Segmentation_ReferenceImageExtentOffset", "")).split()
    if len(declared) == 3:
        try:
            said = np.array([int(v) for v in declared])
        except ValueError:
            said = None
        if said is not None and not np.array_equal(said, rounded):
            # Corroboration only. The geometry decides; a disagreement is reported because
            # it means one of the two is wrong and the case deserves a human.
            raise Misaligned(
                f"the origins imply a shift of {rounded.tolist()} voxels but Slicer's "
                f"Segmentation_ReferenceImageExtentOffset says {said.tolist()}. Those "
                f"disagree, so the correct placement is not established."
            )

    out = np.zeros(image_shape, dtype=labels.dtype)
    src_slices, dst_slices = [], []
    for axis, size in enumerate(image_shape):
        offset = int(rounded[axis])
        start_dst = max(0, offset)
        start_src = max(0, -offset)
        length = min(size - start_dst, labels.shape[axis] - start_src)
        if length <= 0:
            raise Misaligned(
                f"the segmentation does not overlap the image on axis {axis} "
                f"(offset {offset}, image {size}, segmentation {labels.shape[axis]})."
            )
        dst_slices.append(slice(start_dst, start_dst + length))
        src_slices.append(slice(start_src, start_src + length))
    out[tuple(dst_slices)] = labels[tuple(src_slices)]
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog=TOOL, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", nargs=2, action="append", required=True,
                   metavar=("ROOT", "CORPUS_ID"),
                   help="a corpus root and its id; repeat for a multi-corpus dataset. "
                        "Two arguments rather than ROOT:ID because a Windows path already "
                        "contains a colon.")
    p.add_argument("--out", required=True, help="nnUNet_raw directory")
    p.add_argument("--dataset-id", type=int, required=True)
    p.add_argument("--dataset-name", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--uncompressed", action="store_true",
                   help="write .nii instead of .nii.gz. Roughly 3x faster to write and to "
                        "read back during preprocessing, at roughly 3x the disk. For a "
                        "600-case corpus of 512x512x900 volumes the gzip time dominates "
                        "everything else, and nnU-Net reads the file ending from "
                        "dataset.json so nothing downstream needs to know.")
    p.add_argument("--empty-is-negative", action="store_true",
                   help="treat a declared-but-empty segment as a confirmed negative "
                        "rather than as unknown. See the module docstring: only take this "
                        "if you can confirm the annotator finished.")
    args = p.parse_args(argv)

    channel_map = load_channel_map()
    channels = list(channel_map.channels())
    if not channels:
        return _die("the channel declaration assigns no channel")
    # Label ids are 1..C in the declared channel order. Deterministic and written into
    # dataset.json, so the trainer and every reader agree without inferring anything.
    label_of = {name: index + 1 for index, name in enumerate(channels)}

    ext = ".nii" if args.uncompressed else ".nii.gz"
    out = Path(args.out) / f"Dataset{args.dataset_id:03d}_{args.dataset_name}"
    (out / "imagesTr").mkdir(parents=True, exist_ok=True)
    (out / "labelsTr").mkdir(parents=True, exist_ok=True)

    # (corpus_id, case, image, seg). The case key is prefixed with the corpus so that
    # `1` from six different corpora are six different cases -- they are six different
    # scans, verified by digest, and collapsing them would silently drop five.
    cases: list[tuple[str, str, Path, Path]] = []
    for raw_root, corpus_id in args.corpus:
        root = Path(raw_root)
        if not root.is_dir():
            return _die(f"--corpus root {root} is not a directory")
        found = 0
        ambiguous: list[tuple[str, str, list[str]]] = []
        for directory in sorted(
            (d for d in root.rglob("*") if d.is_dir()),
            key=lambda d: (len(d.parts), int(d.name) if d.name.isdigit() else 1 << 30),
        ):
            # `*-label.nrrd` is Slicer's own name for a label map exported beside a
            # segmentation -- it is a DERIVED copy of the .seg.nrrd, not an image, and
            # the pneumonia corpus ships one per case. Counting it as a second image made
            # every case in that corpus ambiguous and silently dropped all 99.
            images = sorted(
                f for f in directory.glob("*.nrrd")
                if not f.name.endswith(".seg.nrrd") and not f.stem.endswith("-label")
            )
            segs = sorted(directory.glob("*.seg.nrrd"))
            if len(images) > 1:
                # THE CANONICAL IMAGE IS THE ONE NAMED AFTER ITS CASE. Every corpus here
                # is laid out as `<case>/<case>.nrrd`, and the extra files are
                # derivatives: measured in ct_coronary-ca_seg, four cases carry a
                # `.crop` (which has no space metadata at all), a `_flip`, or a `_pred`.
                #
                # `_pred` is why this is a named convention and not a heuristic like
                # "largest file": that one is a MODEL'S OWN OUTPUT sitting in the data
                # directory, and any rule that picked it would train a model on a
                # previous model's guesses while reporting the corpus as ground truth.
                named = [f for f in images if f.stem == directory.name]
                if len(named) == 1:
                    images = named
            if len(images) > 1 and len(segs) == 1:
                ambiguous.append((corpus_id, directory.name, [f.name for f in images]))
            if len(images) == 1 and len(segs) == 1:
                label_maps = sorted(f for f in directory.glob("*.nrrd") if f.stem.endswith("-label"))
                cases.append((corpus_id, directory.name, images[0], segs[0],
                              label_maps[0] if len(label_maps) == 1 else None))
                found += 1
                if args.limit and found >= args.limit:
                    break
        if ambiguous:
            # Refused, not guessed. Picking "the biggest file" would work here and would
            # be a rule that silently picks the wrong volume the first time a corpus
            # ships two series per case.
            lines = "\n".join(f"    {c}/{d}: {names}" for c, d, names in ambiguous[:5])
            return _die(
                f"{len(ambiguous)} case(s) under {root} have more than one candidate "
                f"image and this tool will not choose between them:\n{lines}\n"
                f"Name the series, or split the corpus so each case has one image."
            )
        if not found:
            return _die(f"no case pairs under {root} (corpus {corpus_id})")
    if not cases:
        return _die("no case pairs in any corpus")

    print("-" * 88)
    print(f"{TOOL}   {len(args.corpus)} corpus/corpora -> {out.name}")
    for raw_root, corpus_id in args.corpus:
        print(f"    {corpus_id:20s} {raw_root}")
    print(f"  channels: {', '.join(f'{c}={label_of[c]}' for c in channels)}")
    print(f"  declared-but-empty segment is: "
          f"{'a CONFIRMED NEGATIVE' if args.empty_is_negative else 'UNKNOWN (conservative)'}")
    print("-" * 88)

    supervision: dict[str, list[str]] = {}
    staged = skipped = 0
    per_channel = {c: 0 for c in channels}
    per_corpus: dict[str, int] = {}
    for index, (corpus_id, case, image_path, seg_path, label_path) in enumerate(cases, start=1):
        key = f"{corpus_id.replace('-', '_')}_{case}"
        volume, header = nrrd.read(str(image_path))
        labels, seg_header = nrrd.read(str(seg_path))
        aligned = ""

        # NAMES FROM THE .seg.nrrd, VOXELS FROM THE -label.nrrd, when that is the only
        # pair that is coherent.
        #
        # Measured in NRRD_DATASET_PNEUMONIA: the .seg.nrrd sits on an isotropic ~0.42mm
        # grid with flipped x/y axes while the image is 0.70 x 0.70 x 1.25 -- a genuine
        # resampling that `align_to_image` rightly refuses. The `-label.nrrd` beside it is
        # byte-for-byte on the IMAGE grid (same sizes, same origin, same space) and its
        # values are exactly the LabelValues the .seg.nrrd names. So the two files each
        # hold half of what is needed, and taking a half from each is not a workaround --
        # it is the only reading under which both files are true.
        #
        # Conditional, and narrowly: only when the .seg does NOT match the image and the
        # -label does. Preferring the label map generally would discard the segmentation's
        # own geometry in the 900+ cases where it is correct.
        if label_path is not None and volume.shape != labels.shape:
            label_volume, label_header = nrrd.read(str(label_path))
            if label_volume.shape == volume.shape:
                labels = label_volume
                aligned = f"[voxels from {label_path.name}] "
                # The names still come from seg_header; only the voxels moved.

        if volume.shape != labels.shape:
            # Not a defect on its own: Slicer often writes the segmentation over a
            # slightly different extent and records where. Place it, or refuse.
            try:
                original = labels.shape
                labels = align_to_image(labels, volume.shape, header, seg_header)
                aligned = f"[aligned {original}->{labels.shape}] "
            except Misaligned as exc:
                print(f"  [{index:4d}] {corpus_id:16s} {case:>4s}  SKIPPED  {exc}")
                skipped += 1
                continue

        segments = _segments(seg_header, raw_segment_names(seg_path))
        target = np.zeros(volume.shape, dtype=np.uint8)
        supervised: list[str] = []
        detail = []
        for segment in segments:
            row = channel_map.resolve(corpus_id, segment["source_name_raw"])
            if row.channel is None:
                continue
            mask = labels == segment["label_value"]
            count = int(mask.sum())
            if count == 0 and not args.empty_is_negative:
                detail.append(f"{row.channel}=empty/unknown")
                continue
            target[mask] = label_of[row.channel]
            supervised.append(row.channel)
            per_channel[row.channel] += 1
            detail.append(f"{row.channel}={count}")
        for channel in channels:
            if channel not in supervised:
                detail.append(f"{channel}=UNSUPERVISED") if channel not in [
                    d.split("=")[0] for d in detail] else None

        if not supervised:
            print(f"  [{index:4d}] {corpus_id:16s} {case:>4s}  EXCLUDED supervises no channel")
            skipped += 1
            continue

        affine = _affine(header)
        nib.save(nib.Nifti1Image(volume.astype(np.int16), affine),
                 str(out / "imagesTr" / f"{key}_0000{ext}"))
        nib.save(nib.Nifti1Image(target, affine), str(out / "labelsTr" / f"{key}{ext}"))
        supervision[key] = supervised
        staged += 1
        per_corpus[corpus_id] = per_corpus.get(corpus_id, 0) + 1
        print(f"  [{index:4d}] {corpus_id:16s} {case:>4s}  {volume.shape}  {aligned}{' '.join(detail)}")

    dataset = {
        "channel_names": {"0": "CT"},
        # Region-style label values. medos_trainer forces region mode for singletons --
        # nnU-Net gives them a softmax head otherwise, see register entry 95.
        "labels": {"background": 0, **{c: [label_of[c]] for c in channels}},
        "regions_class_order": [label_of[c] for c in channels],
        "numTraining": staged,
        "file_ending": ext,
        "overwrite_image_reader_writer": "NibabelIOWithReorient",
    }
    (out / "dataset.json").write_text(json.dumps(dataset, indent=2) + "\n", encoding="utf-8")

    # The mask, keyed on the same case key nnU-Net puts in batch['keys'].
    (out / "supervision.json").write_text(
        json.dumps(
            {
                "_note": ("Which channels each case SUPERVISES. A channel absent from a "
                          "case's list is UNKNOWN for that case and must contribute zero "
                          "to the loss and zero to every gradient -- not a negative."),
                "_known_limitation_cross_corpus_patients": (
                    "These corpora came from one clinic and carry NO patient identifier, "
                    "so a person scanned for two different studies appears here as two "
                    "unrelated cases. Case `1` in each corpus was verified by digest to be "
                    "a DIFFERENT SCAN, so no case is duplicated -- but two scans of one "
                    "person cannot be detected, and MOS-EVID-034's L1 patient-overlap "
                    "check is blind to it. A train/test split over this dataset may "
                    "therefore leak, and the leakage is undetectable from the data."),
                "empty_segment_is_negative": bool(args.empty_is_negative),
                "channels": channels,
                "label_of": label_of,
                "cases": supervision,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("-" * 88)
    print(f"  staged   {staged}")
    print(f"  skipped  {skipped}")
    for corpus_id, n in sorted(per_corpus.items()):
        print(f"    from {corpus_id:20s} {n:5d}")
    for channel in channels:
        print(f"  supervised on {channel:16s} {per_channel[channel]:4d} / {staged}"
              f"   ({staged - per_channel[channel]} unsupervised)")
    print(f"  -> {out}")
    print("-" * 88)
    return 0 if staged else 1


def _die(message: str) -> int:
    print(f"{TOOL}: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
