# SPDX-License-Identifier: Apache-2.0
"""Write `splits_final.json` so every channel is measurable in every fold.

WHY nnU-Net's OWN SPLIT IS NOT ENOUGH HERE
-------------------------------------------
`nnunetv2.utilities.crossval_split.generate_crossval_split` is
`sklearn.model_selection.KFold(shuffle=True, random_state=12345)`. Plain KFold. It
knows nothing about labels, which is correct for a single-label dataset where every
case carries every class, and wrong for a PARTIALLY LABELLED one where each case
carries the channels of the corpus it came from and no others.

MEASURED, on the 90-case ten-channel build this tool was written for:

    channel                   total  train   val
    coronary_calcification       15     14     1     <- one validation case
    vertebral_body               15     13     2
    lung_neoplasm                 4      2     2

One validation case. The per-channel Dice for `coronary_calcification` was computed
from a single patient, and in most epochs the 100 validation patches drew nothing from
it at all -- so the channel read `nan`, the checkpoint metric could not see it, and a
number that looked like a result was an artefact of who landed in the fold.

WHAT THIS DOES INSTEAD
----------------------
Stratifies on the SUPERVISION SIGNATURE: the exact frozen set of channels a case is
annotated for. Cases are grouped by signature and dealt round-robin into the folds, so
each fold receives its proportional share of every signature. That is stronger than
stratifying by corpus, because one corpus does not imply one signature -- in the
lung-nodule corpus some cases carry `benign_nodule`, some `lung_neoplasm`, some both,
and treating them as interchangeable is how `lung_neoplasm` ended up with two.

IT REFUSES RATHER THAN SILENTLY UNDER-DELIVERING. `--min-val-cases` is a floor, and a
channel that cannot meet it in every fold is named with its count and the arithmetic
that makes it impossible. A split that quietly leaves a channel unmeasurable is the
defect this tool exists to remove, and producing one silently would reintroduce it.

WHY IT IS A TOOL AND NOT PART OF THE TRAINER
---------------------------------------------
The same reason `nnunet_dataset.py` is: this decides what a model is measured against,
which is a claim about data. `MOS-EVID-034`'s leakage checks and `MOS-TRAIN-141`'s
staged layout treat the split as an input to a run, not something a run invents.

Usage
-----
    python medos/tools/ingest/stratified_split.py \\
        --raw  D:/MedOS-data/nnunet/raw/Dataset512_MedOS10Big \\
        --preprocessed D:/MedOS-data/nnunet/preprocessed/Dataset512_MedOS10Big \\
        --folds 5 --min-val-cases 3
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

SUPERVISION_FILE = "supervision.json"
SPLITS_FILE = "splits_final.json"

#: Deterministic, and NOT nnU-Net's 12345. A different seed from the one the unstratified
#: split used makes it obvious at a glance that a `splits_final.json` came from here.
SEED = 20260921


def load_supervision(raw_dataset: Path) -> tuple[list[str], dict[str, list[str]]]:
    path = raw_dataset / SUPERVISION_FILE
    if not path.is_file():
        raise SystemExit(
            f"no {SUPERVISION_FILE} at {path}. This tool stratifies on which channels "
            f"each case is annotated for, and without that file it has nothing to "
            f"stratify on. Build the dataset with medos/tools/ingest/nnunet_dataset.py."
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    return list(document["channels"]), {k: list(v) for k, v in document["cases"].items()}


def stratify(
    cases: dict[str, list[str]], folds: int, seed: int = SEED
) -> list[dict[str, list[str]]]:
    """Deal cases into folds so that every CHANNEL is evenly represented.

    Greedy on per-channel counts, not round-robin on signatures. The difference is not
    cosmetic: a channel carried by two different signatures -- `benign_nodule` appears
    both alone and alongside `lung_neoplasm` -- comes out [2, 2, 3, 3, 4] under
    independent round-robins, because neither knows about the other. Choosing the fold
    that currently holds the fewest cases of THIS case's channels balances the thing
    the caller actually reads.

    Deterministic: the signature order is fixed, ties break on fold size and then index,
    and the shuffle inside each group is seeded. The shuffle is what stops the result
    depending on filename order, which would make the split a property of the
    filesystem rather than of the data.
    """
    by_signature: dict[frozenset[str], list[str]] = defaultdict(list)
    for case, channels in cases.items():
        by_signature[frozenset(channels)].append(case)

    rng = random.Random(seed)
    buckets: list[list[str]] = [[] for _ in range(folds)]
    # Per-fold, per-channel counts. BALANCING CHANNELS AND NOT SIGNATURES is the
    # correction that matters: round-robin inside each signature group independently
    # balances the groups, and a channel carried by two groups can still come out
    # [2, 2, 3, 3, 4] because the two round-robins do not know about each other.
    # `benign_nodule` is exactly that case -- eleven cases alone, three alongside
    # `lung_neoplasm` -- and it is the channel the caller cares about, not the
    # signature.
    #
    # So each case goes to the fold that currently holds the FEWEST cases of the
    # channels that case supervises, ties broken by fold size and then by index. Greedy
    # and deterministic; with equal-sized groups it degenerates to round-robin, and
    # where it does not, it is because the folds genuinely disagree about a channel.
    every_channel = {c for channels in cases.values() for c in channels}
    counts: list[dict[str, int]] = [dict.fromkeys(every_channel, 0) for _ in range(folds)]

    # Largest signature first: the big groups set the shape and the small ones fill the
    # gaps, rather than the reverse.
    for signature in sorted(by_signature, key=lambda s: (-len(by_signature[s]), sorted(s))):
        members = sorted(by_signature[signature])
        rng.shuffle(members)
        for case in members:
            chosen = min(
                range(folds),
                key=lambda f: (
                    sum(counts[f][c] for c in signature),
                    len(buckets[f]),
                    f,
                ),
            )
            buckets[chosen].append(case)
            for channel in signature:
                counts[chosen][channel] += 1

    splits: list[dict[str, list[str]]] = []
    for fold in range(folds):
        val = sorted(buckets[fold])
        train = sorted(c for f in range(folds) if f != fold for c in buckets[f])
        splits.append({"train": train, "val": val})
    return splits


def per_channel_counts(
    split: dict[str, list[str]], channels: list[str], cases: dict[str, list[str]]
) -> dict[str, tuple[int, int]]:
    train, val = set(split["train"]), set(split["val"])
    out: dict[str, tuple[int, int]] = {}
    for channel in channels:
        have = {c for c, v in cases.items() if channel in v}
        out[channel] = (len(have & train), len(have & val))
    return out


def check(
    splits: list[dict[str, list[str]]],
    channels: list[str],
    cases: dict[str, list[str]],
    minimum: int,
) -> list[str]:
    """Every channel must have at least `minimum` validation cases in EVERY fold."""
    problems: list[str] = []
    for channel in channels:
        total = sum(1 for v in cases.values() if channel in v)
        worst = min(
            per_channel_counts(split, channels, cases)[channel][1] for split in splits
        )
        if worst < minimum:
            reachable = total // len(splits)
            problems.append(
                f"{channel}: {total} case(s) in the dataset, worst fold has {worst} in "
                f"validation, {minimum} required. With {len(splits)} folds the most any "
                f"split can guarantee is {reachable}."
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--raw", required=True, type=Path, help="the nnUNet_raw DATASET dir")
    p.add_argument(
        "--preprocessed", type=Path, default=None,
        help="the nnUNet_preprocessed DATASET dir; splits_final.json is written here. "
             "Omitted, nothing is written and the report is printed.",
    )
    p.add_argument("--folds", type=int, default=5)
    p.add_argument(
        "--min-val-cases", type=int, default=3,
        help="the floor, per channel, in EVERY fold. A channel below it is named and "
             "the tool refuses rather than writing a split that cannot measure it.",
    )
    p.add_argument(
        "--allow-unmeasurable", action="store_true",
        help="write the split anyway. Takes a channel that cannot be measured and "
             "records it as such rather than pretending; use only when the dataset "
             "genuinely has too few cases and you want the other nine channels.",
    )
    p.add_argument("--seed", type=int, default=SEED)
    args = p.parse_args(argv)

    channels, cases = load_supervision(args.raw)
    splits = stratify(cases, args.folds, args.seed)

    print(f"{len(cases)} cases, {len(channels)} channels, {args.folds} folds, seed {args.seed}")
    print()
    columns = "".join(f"  f{i}tr/f{i}va" for i in range(args.folds))
    print("channel".ljust(26) + "total" + columns)
    for channel in channels:
        total = sum(1 for v in cases.values() if channel in v)
        cells = "".join(
            f"  {train:3}/{val:<4}"
            for train, val in (
                per_channel_counts(s, channels, cases)[channel] for s in splits
            )
        )
        print(f"  {channel:24}{total:5}{cells}")

    problems = check(splits, channels, cases, args.min_val_cases)
    print()
    if problems:
        print("CHANNELS THAT CANNOT MEET THE FLOOR:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        if not args.allow_unmeasurable:
            print(
                "\nRefusing to write. A split that leaves a channel with fewer "
                "validation cases than it needs produces a per-channel number computed "
                "from one or two patients, which reads like a result and is not one. "
                "Add cases, lower --folds, lower --min-val-cases, or pass "
                "--allow-unmeasurable to record the limitation and proceed.",
                file=sys.stderr,
            )
            return 2
        print("  (--allow-unmeasurable: writing anyway)", file=sys.stderr)
    else:
        print(f"every channel has at least {args.min_val_cases} validation cases in every fold")

    if args.preprocessed is None:
        print("\nno --preprocessed given; nothing written")
        return 0
    args.preprocessed.mkdir(parents=True, exist_ok=True)
    target = args.preprocessed / SPLITS_FILE
    target.write_text(json.dumps(splits, indent=2), encoding="utf-8")
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
