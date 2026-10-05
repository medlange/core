# SPDX-License-Identifier: Apache-2.0
"""`MEDOS_TRAINING_ENVIRONMENT` and the build stamp: register entry 82, as assertions.

WHAT ENTRY 82 SAYS, AND WHAT THIS MODULE HOLDS
------------------------------------------------
    "`code_commit` and `image_digest` ... change with every build, so a value
    written into `docker-compose.yml` is correct exactly until the next
    `docker compose build` and silently false afterwards -- and it is silently
    false in the provenance record of every training run, which is the one
    place the record exists to make trustworthy."

The fix is that the trainer image computes the two facts about ITSELF at
build time. What can be held here, without a container, is the half that is
arithmetic: the stamp refuses a placeholder commit, the content digest is a
pure function of the image's content, and the nine keys the trainer emits
are the nine keys the router demands. The other half -- that a REBUILD
changes the value with nobody editing a file -- needs two builds and is in
`tests/integration/test_trainer_image.py::test_a_rebuild_restamps_without_anybody_editing_a_file`.

THE TWO KEY SETS ARE COMPARED AND NOT RESTATED. `medos/medos/api/routes_training.py`
and `medos/medos/training/runs.py` own the vocabulary; `medos_trainer.environment`
emits it. Each is spelled in its own module because neither image imports
the other at run time, and this module is the only thing making them one
vocabulary.

WHAT CHANGED UNDER THIS GATE
-----------------------------
The trainer's nnU-Net backend was replaced by a vanilla-PyTorch stack
written from scratch, and the platform handshake went with it: the trainer
is standalone and the declaration's `backend_versions` and `preprocessing`
blocks are recorded as EMPTY rather than filled from a backend registry or
a bindings file -- both of which the rewrite deleted. The gates below
assert the new reality; the nine-key shape, the seeds, the determinism
settings and the stamp are unchanged.

Spec: MOS-TRAIN-124, MOS-TRAIN-125, MOS-TRAIN-126, MOS-REL-037,
docs/spec/99-known-inconsistencies.md entry 82.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINER_ROOT = REPO_ROOT / "trainer"
if not (TRAINER_ROOT / "medos_trainer").is_dir():
    pytest.skip(
        "the trainer tree is not present in this checkout (core split) -- "
        "the same gates run in medlange/trainer's own CI",
        allow_module_level=True,
    )


if str(TRAINER_ROOT) not in sys.path:
    sys.path.insert(0, str(TRAINER_ROOT))

from medos.training import runs as tr  # noqa: E402
from medos_trainer import environment as env  # noqa: E402
from medos_trainer import stamp as st  # noqa: E402

from medos.api import routes_training as rt  # noqa: E402


# =====================================================================================
# 1. One vocabulary, spelled in two images
# =====================================================================================
def test_the_trainer_emits_exactly_the_nine_keys_the_router_refuses_without() -> None:
    assert env.ENVIRONMENT_KEYS == rt._ENVIRONMENT_KEYS, (
        "the trainer's declaration and medos/medos/api/routes_training.py's requirement have "
        "diverged. Every submit would answer 503 TRAINING_ENVIRONMENT_NOT_RECORDED on a "
        "deployment whose trainer believes it declared everything"
    )
    assert len(env.ENVIRONMENT_KEYS) == 9


def test_the_seed_block_carries_every_key_mos_train_124_binds() -> None:
    assert tuple(sorted(env.SEEDS)) == tuple(sorted(tr._SEED_KEYS))


def test_the_determinism_block_carries_every_key_mos_train_124_binds() -> None:
    assert tuple(sorted(env.DETERMINISM)) == tuple(sorted(tr._DETERMINISM_KEYS))


def test_the_framework_block_names_only_what_the_vanilla_stack_installs() -> None:
    """torch and numpy, and nothing else.

    The pre-vanilla image recorded `monai` as `absent` -- the platform then
    generated MONAI Bundle configs as data and never imported MONAI. The
    rewrite removed the reason: the trainer writes its own bundle and
    installs neither MONAI nor SimpleITK, so declaring either would be a
    claim about a package `pip freeze` cannot find. Every key the trainer
    declares must still be a key the router's vocabulary knows, and the two
    absent-by-design names stay asserted by their absence.
    """
    assert set(env._FRAMEWORK_DISTRIBUTIONS) <= set(tr._FRAMEWORK_KEYS)
    assert set(env._FRAMEWORK_DISTRIBUTIONS) == {"torch", "numpy"}
    assert "monai" not in env._FRAMEWORK_DISTRIBUTIONS
    assert "simpleitk" not in env._FRAMEWORK_DISTRIBUTIONS


def test_the_declaration_records_no_backend_and_no_bindings() -> None:
    """THE NEW REALITY, as a document: `backend_versions` and `preprocessing`
    are present and EMPTY.

    The vanilla stack is its own backend -- there is no pluggable registry
    and no nnU-Net distribution to pin -- and a standalone run binds no
    deployment-level preprocessing spec, so the honest values are empty
    objects, not omitted keys and not claims about packages that are not
    installed. The keys stay because the nine-key shape is the vocabulary
    this trainer has always emitted.
    """
    assert not hasattr(env, "_BACKEND_DISTRIBUTIONS"), (
        "the backend registry is back. The vanilla stack declares no backend "
        "distributions; a registry here is the nnU-Net shape returning under "
        "another name"
    )


def test_the_package_declares_no_backend_kind() -> None:
    """`BACKEND_KIND` named the one backend the image shipped (`nnunet`).

    A one-backend image has no backend to name: the framework is the backend,
    and its version is the build stamp's `code_commit`. The constant going
    away is the assertion -- a backend kind reappearing on the package is a
    second-backend decision that has to be made out loud, in code and in the
    environment document together.
    """
    import medos_trainer

    assert not hasattr(medos_trainer, "BACKEND_KIND"), (
        "medos_trainer.BACKEND_KIND is back. The vanilla stack is not one of "
        "several selectable backends; reintroducing a kind is a product "
        "decision, not a constant"
    )


def test_a_declaration_carries_the_empty_blocks_and_the_nine_keys(
    tmp_path: Path,
) -> None:
    """`declare()` end to end against a written stamp: the document shape
    holds, the empty blocks are the recorded facts, and the observed keys
    are present. `allow_cpu` because this unit test declares no GPU."""
    stamp_file = tmp_path / "build-stamp.json"
    assert st.main([
        "--code-commit", "e" * 40, "--code-dirty", "false", "--out", str(stamp_file)
    ]) == 0
    document = env.declare(stamp_path=stamp_file, allow_cpu=True)

    assert tuple(sorted(document)) == tuple(sorted(env.ENVIRONMENT_KEYS))
    assert document["backend_versions"] == {}
    assert document["preprocessing"] == {}
    assert document["code_commit"] == "e" * 40
    assert document["framework_versions"]["torch"]
    assert document["hardware"]["accelerator"] == "cpu"
    assert document["hardware"]["cpu_run_explicitly_permitted"] is True


# =====================================================================================
# 2. The build stamp
# =====================================================================================
def test_the_stamp_refuses_a_placeholder_commit() -> None:
    """A commit the platform would then refuse at submit, refused at BUILD instead.

    `medos.training.runs._COMMIT_RE` is what a submit is checked against.
    Catching it here means an unbuildable image rather than an image that
    builds and then cannot train.
    """
    for bad in ("", "HEAD", "unknown", "0", "deadbeef", "Z" * 40):
        with pytest.raises(ValueError, match="code_commit"):
            st.build_stamp(code_commit=bad, code_dirty=False)


def test_the_stamp_accepts_the_commit_form_the_platform_binds() -> None:
    document = st.build_stamp(code_commit="a" * 40, code_dirty=True)
    assert document["code_commit"] == "a" * 40
    assert document["code_dirty"] is True
    assert document["image_digest"].startswith("sha256:")
    assert len(document["image_digest"]) == len("sha256:") + 64


def test_a_dirty_tree_is_stamped_and_not_refused() -> None:
    """`MOS-TRAIN-125` blocks `state = SUCCEEDED`, which is a different statement.

    "a dirty-tree run is a legitimate experiment and an illegitimate candidate".
    Refusing at build would stop a developer building an image to debug with.
    """
    assert st.build_stamp(code_commit="b" * 40, code_dirty=True)["code_dirty"] is True


def test_the_stamps_canonical_form_agrees_with_the_platforms() -> None:
    """`stamp.py` is stdlib-only because it runs during `docker build`. This is the
    price: a second canonicaliser, held against the real one on the properties that
    decide a digest -- key order and separators (`MOS-EVID-008`)."""
    from medos.sdk.canonical import canonical_bytes

    for document in (
        {"b": 1, "a": 2},
        {"nested": {"z": [1, 2, {"y": "x"}], "a": None}},
        {"unicode": "ok"},
        {},
    ):
        assert st._canonical(document) == canonical_bytes(document), document


def test_the_content_digest_is_a_function_of_the_content_and_nothing_else(
    tmp_path: Path,
) -> None:
    """It changes when and only when the software changes. That is the whole argument
    for recording it instead of the OCI image id, which carries layer timestamps and
    would make the same experiment rebuilt from the same commit look like a different
    one (`MOS-TRAIN-124`'s `run_digest`, `training_runs_run_digest_uk`)."""
    tree = tmp_path / "pkg"
    tree.mkdir()
    (tree / "a.py").write_text("x = 1\n", encoding="utf-8")

    first, inventory = st.content_digest(trees=[str(tree)])
    again, _ = st.content_digest(trees=[str(tree)])
    assert first == again, "two readings of one image produced two digests"
    assert first.startswith("sha256:")
    assert str(tree / "a.py").replace("\\", "/") in inventory["files"]

    (tree / "a.py").write_text("x = 2\n", encoding="utf-8")
    changed, _ = st.content_digest(trees=[str(tree)])
    assert changed != first, "a source change did not change the recorded image digest"


def test_compiled_bytecode_is_outside_the_content_digest(tmp_path: Path) -> None:
    """It is not source: its bytes depend on when it was written, not on what changed."""
    tree = tmp_path / "pkg"
    (tree / "__pycache__").mkdir(parents=True)
    (tree / "a.py").write_text("x = 1\n", encoding="utf-8")
    before, _ = st.content_digest(trees=[str(tree)])
    (tree / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\x00\x01\x02")
    assert st.content_digest(trees=[str(tree)])[0] == before


def test_an_unstamped_image_refuses_rather_than_defaulting(tmp_path: Path) -> None:
    """The one behaviour entry 82 is about: no default, and the refusal names the build."""
    with pytest.raises(FileNotFoundError, match="entry 82"):
        st.read_stamp(tmp_path / "nowhere.json")


def test_a_truncated_stamp_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "build-stamp.json"
    path.write_text(json.dumps({"code_commit": "c" * 40}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        st.read_stamp(path)


def test_the_stamp_round_trips_through_the_file_the_entrypoint_reads(tmp_path: Path) -> None:
    out = tmp_path / "build-stamp.json"
    assert st.main([
        "--code-commit", "d" * 40, "--code-dirty", "false", "--out", str(out)
    ]) == 0
    document = st.read_stamp(out)
    assert document["code_commit"] == "d" * 40
    assert document["code_dirty"] is False
    # The inventory travels with the digest so a reviewer can recompute it.
    assert "image_inventory" in document


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("dirty", True),
    ("false", False), ("0", False), ("clean", False),
])
def test_the_dirty_flag_has_two_values_and_no_third(raw: str, expected: bool) -> None:
    assert st._dirty_flag(raw) is expected


def test_an_unreadable_dirty_flag_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="neither true nor false"):
        st._dirty_flag("probably")
