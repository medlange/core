# SPDX-License-Identifier: Apache-2.0
"""`chain-equivalence` -- §15.1.2's 0.3.0 gate row via chapter 17's `MOS-TRAIN-193`.

    "the generated transform chain is byte-identical between training and serving"

`MOS-TRAIN-133` fixes the comparison and forbids the obvious weakening of it:

    "For every registered `PreprocessingSpec` version, CI MUST assert that the generated
     MONAI chain and `medicalos-preprocessing` produce the **byte-identical** model-space
     tensor on the golden fixture, compared by the `sha256` of `MOS-IMG-054` step 4 and
     **not** by a tolerance."

So every comparison below is `==` on two digests. The failure this exists to catch is a
resampler that differs in the fourth decimal place, and that is invisible to every
tolerance anybody would pick -- while being exactly large enough to move a boundary voxel,
which moves a volume, which moves a measurement a clinician reads.

WHY "TRAINING" AND "SERVING" ARE TWO THINGS HERE AND NOT ONE
--------------------------------------------------------------
Training loads `configs/inference.json` -- the document `medos.sdk.chain` RENDERS,
whose transform names and keyword arguments are MONAI's. Serving executes
`medos.sdk.preprocess`, the platform's own implementation. They are two code paths
over one spec, and the whole point of `MOS-TRAIN-034` (`build_chain` is the ONLY
constructor) is that they cannot drift. This module drives both from the same spec and
compares the tensors, which is the only form of the claim that a drift can fail.

THE THREE BUCKETS, AND WHY A REFUSAL IS A PASS
------------------------------------------------
`test_..._every_shipped_spec_is_accounted_for` partitions every `PreprocessingSpec` the
release ships:

  EXECUTES   the chain runs here; byte equality is asserted, and against the PINNED golden
             hash as well as against itself. This is the bucket the check lives in, and
             `test_..._at_least_one_spec_actually_executed` refuses a run where it is
             empty.
  REFUSES    the spec is valid and names an interpolator `medos.sdk.preprocess`
             does not implement (`bspline3`). `MOS-TRAIN-132` -- "MUST NOT substitute the
             nearest available option" -- makes the refusal the CORRECT behaviour, and a
             silent substitution of `linear` would be the defect. So the bucket asserts
             the refusal names the field, rather than treating it as a gap.
  INVALID    the document does not parse. Asserted to be refused by requirement id, so a
             spec that stops parsing because the PARSER broke is not mistaken for a
             deliberate negative fixture.

Needs nothing: no database, no deployment, no corpus, no docker. Pure arithmetic over a
shipped phantom.

Spec: MOS-TRAIN-034, MOS-TRAIN-036, MOS-TRAIN-131, MOS-TRAIN-132, MOS-TRAIN-133,
MOS-TRAIN-191, MOS-TRAIN-193, MOS-IMG-054, MOS-REL-004; chapter 17 acceptance criterion 6.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from medos.sdk import chain as ch
from medos.sdk import fixtures as fx
from medos.sdk import preprocess as pre
from medos.sdk import spec as sp
from medos.sdk.errors import ChainRefused

pytestmark = pytest.mark.gate_0_3_0


def _shipped_documents() -> dict[str, dict[str, Any]]:
    """Every `PreprocessingSpec` document this release ships, by name.

    Enumerated from `medos.sdk.fixtures` rather than listed here, so that a spec
    added to the release is gated the day it is added -- `MOS-TRAIN-133` says "for every
    registered `PreprocessingSpec` version", and a hand-written list is a version that
    stops being every.
    """
    out: dict[str, dict[str, Any]] = {"selftest": fx.selftest_spec_document()}
    for name in sorted(getattr(fx, "__all__", ())):
        value = getattr(fx, name, None)
        if isinstance(value, dict) and "target_spacing_mm" in value:
            out[name] = copy.deepcopy(value)
    return out


def _classify(document: dict[str, Any]) -> tuple[str, Any]:
    """`(bucket, payload)` -- see the module docstring's three buckets."""
    try:
        spec = sp.parse_spec(document)
    except ChainRefused as exc:
        return "invalid", exc
    try:
        served = pre.tensor_digest(
            pre.first_patch(spec, pre.model_space_tensor(spec, fx.phantom_input()))
        )
    except ChainRefused as exc:
        return "refuses", exc
    return "executes", (spec, served)


# =====================================================================================
# THE CHECK
# =====================================================================================
def test_chain_equivalence_the_generated_chain_and_the_serving_path_agree_byte_for_byte(
) -> None:
    """`MOS-TRAIN-133`, over every spec that executes here. `==`, never a tolerance."""
    executed = 0
    for name, document in sorted(_shipped_documents().items()):
        bucket, payload = _classify(document)
        if bucket != "executes":
            continue
        spec, served = payload
        generated = ch.build_chain_from_document(ch.serialize_chain(spec))
        from_chain = pre.tensor_digest(
            pre.first_patch(
                spec,
                pre.model_space_tensor(spec, fx.phantom_input(), chain=generated),
            )
        )
        assert served == from_chain, (
            f"{name} ({spec.id} v{spec.version}): the generated chain and the serving "
            f"implementation disagree.\n  generated {from_chain}\n  serving   {served}\n"
            f"MOS-TRAIN-133 requires byte equality, not agreement within a tolerance: the "
            f"difference this catches is a resampler off in the fourth decimal place, "
            f"which no tolerance anyone would choose can see."
        )
        executed += 1
    assert executed, (
        "no shipped PreprocessingSpec executed, so nothing was compared. See "
        "`test_chain_equivalence_at_least_one_spec_actually_executed`."
    )


def test_chain_equivalence_at_least_one_spec_actually_executed() -> None:
    """The check above passes trivially over an empty set. This refuses that run.

    `MOS-REL-012` -- an unexecuted acceptance criterion means the requirement is not
    satisfied -- covers a criterion that executed against nothing just as squarely as one
    that did not run.
    """
    buckets = [_classify(d)[0] for d in _shipped_documents().values()]
    assert buckets.count("executes") >= 1, (
        f"no shipped PreprocessingSpec reached execution (buckets: {sorted(buckets)}). "
        f"`chain-equivalence` then compares nothing and would be green forever."
    )


def test_chain_equivalence_the_agreed_tensor_is_the_pinned_golden_hash() -> None:
    """Agreement between two implementations that both drifted is not equivalence.

    `MOS-TRAIN-065` forbids a "re-record the hash" operation for exactly this reason: two
    paths built from one generator agree by construction, and the pinned value is what
    says they agree with what was evaluated. If this fails, read the diff and establish
    what changed -- do not re-pin.
    """
    spec = sp.parse_spec(fx.selftest_spec_document())
    served = pre.tensor_digest(
        pre.first_patch(spec, pre.model_space_tensor(spec, fx.phantom_input()))
    )
    assert served == fx.GOLDEN_TENSOR_SHA256, (
        f"the golden hash no longer reproduces: {served} != {fx.GOLDEN_TENSOR_SHA256}. "
        f"MOS-TRAIN-065 forbids re-recording it."
    )


def test_chain_equivalence_a_perturbed_interpolation_mode_breaks_the_equality() -> None:
    """Criterion 6's second half, and the reason the check above is worth running.

    "perturb the generated chain's `Spacingd` mode from `bilinear` to `nearest` and assert
    the check fails." A check that cannot fail is not a check.
    """
    spec = sp.parse_spec(fx.selftest_spec_document())
    document = copy.deepcopy(ch.serialize_chain(spec))
    touched = 0
    for transform in document["preprocessing"]["transforms"]:
        if transform["_target_"].endswith("Spacingd"):
            transform["mode"] = [pre.INTERP_ORDER["nearest"]]
            touched += 1
    assert touched == 1, (
        "the generated chain has no Spacingd to perturb, so the negative control cannot "
        "be performed and the byte-equality assertion is unguarded"
    )
    perturbed = ch.build_chain_from_document(document)
    served = pre.tensor_digest(
        pre.first_patch(spec, pre.model_space_tensor(spec, fx.phantom_input()))
    )
    broken = pre.tensor_digest(
        pre.first_patch(
            spec, pre.model_space_tensor(spec, fx.phantom_input(), chain=perturbed)
        )
    )
    assert served != broken, (
        "changing the resampler's interpolation order did not change the output tensor. "
        "The comparison is therefore not sensitive to the class of defect MOS-TRAIN-133 "
        "names, and its passes mean nothing."
    )


def test_chain_equivalence_the_chain_is_byte_reproducible_from_the_spec_alone() -> None:
    """`MOS-TRAIN-131`: reproducible, not merely deterministic within one process.

    Two independent parses of the same document give the same bytes, and a change to one
    spec field changes them. That is what lets a `TrainingRun` and a `ValidationReport`
    cite `chain_digest` and mean the same object.
    """
    document = fx.selftest_spec_document()
    first = sp.parse_spec(document)
    second = sp.parse_spec(fx.selftest_spec_document())
    assert ch.chain_bytes(first) == ch.chain_bytes(second)
    assert ch.chain_digest(first).startswith("sha256:")

    moved = fx.selftest_spec_document()
    moved["target_spacing_mm"] = [1.5, 0.9, 0.9]
    assert ch.chain_digest(sp.parse_spec(moved)) != ch.chain_digest(first), (
        "two different specs produce the same chain digest; the digest is not a function "
        "of the spec's content and cannot be cited as one"
    )


def test_chain_equivalence_every_shipped_spec_is_accounted_for() -> None:
    """No spec is silently skipped. See the module docstring's three buckets.

    The `refuses` bucket is the interesting one: `MOS-TRAIN-132` makes refusing an
    unimplemented interpolator correct and substituting the nearest available one the
    defect, so the assertion is that the refusal NAMES what it refused rather than that it
    did not happen.
    """
    documents = _shipped_documents()
    assert documents, "the release ships no PreprocessingSpec document to check"
    for name, document in sorted(documents.items()):
        bucket, payload = _classify(document)
        if bucket == "executes":
            continue
        message = str(payload)
        assert "MOS-" in message, (
            f"{name} was refused without naming a requirement: {message[:200]}. A refusal "
            f"that cannot be traced to a rule is indistinguishable from a broken parser."
        )
        if bucket == "refuses":
            assert "interpolator" in message or "not implemented" in message, (
                f"{name} parses but does not execute, for a reason that is not an "
                f"unimplemented interpolator: {message[:200]}. MOS-TRAIN-132's refusal is "
                f"the only execution refusal this check treats as correct."
            )


def test_chain_equivalence_one_generator_serves_both_paths() -> None:
    """`MOS-TRAIN-034`: `build_chain` is the ONLY constructor of a transform.

    The byte-equality above is a property of two paths built from one generator. If a
    second place in the platform could instantiate a transform, the two tensors could
    agree today and diverge the moment that second place was used -- and the comparison
    would never notice, because it drives the generator.
    """
    from pathlib import Path

    root = Path(pre.__file__).parent
    permitted = {"preprocess.py", "chain.py"}
    offenders = [
        f"{path.name}:{i}"
        for path in sorted(root.glob("*.py"))
        if path.name not in permitted
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "Transform(" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"a transform is constructed outside build_chain: {offenders}. MOS-TRAIN-034 "
        f"makes build_chain the only constructor precisely so that training and serving "
        f"cannot be two implementations."
    )
