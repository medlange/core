# SPDX-License-Identifier: Apache-2.0
"""The model card: what the trainer writes, what the SDK reads, and the refusals between.

THE ROUND TRIP IS THE CONTRACT. `medos_trainer.packaging.write_modelcard` emits a
`medlange.modelcard/1` document; `medos.sdk.modelcard.ModelCard.load` reads it back and
hands the caller a parsed spec whose `chain()` is the preprocessing pipeline. Both halves
are tested at their own side (trainer/tests for the writer, here for the reader); what
ONLY this file can assert is that the two halves are the same format -- that a document
the SDK built from its own fixtures is readable by the reader and that every way of
breaking the card is refused rather than guessed at.

A card is a promise about bytes: the weights it names must exist, the spec digest it
carries must match the spec document, and the format tag must be the one this reader
knows. Each broken promise is tested as a break, not described as one.

Spec: MOS-IMG-049 (the spec is the record of what training did), MOS-TRAIN-129/130/131.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from medos.sdk.fixtures import selftest_spec_document
from medos.sdk.modelcard import (
    CARD_FILENAME,
    ModelCard,
    ModelCardError,
    document_for,
)

STAMP = {"code_commit": "0" * 40, "code_dirty": False, "image_digest": "sha256:test"}
FRAMEWORKS = {"monai_bundle": "1.4.0", "torch": "2.7.1", "numpy": "1.26.4"}


def _document() -> dict:
    return document_for(
        model_id="pulmo.emphysema",
        model_version="1.0.0",
        spec_document=selftest_spec_document(),
        weights={
            "path": "bundle",
            "weights_file": "models/model.ts",
            "format": "torchscript",
            "digest": "sha256:" + "a" * 64,
        },
        frameworks=FRAMEWORKS,
        outputs=({"kind": "segmentation", "value": 1, "name": "emphysema"},),
        stamp=STAMP,
    )


def _model_dir(tmp_path: Path, document: dict | None = None) -> Path:
    root = tmp_path / "model"
    (root / "bundle").mkdir(parents=True)
    (root / CARD_FILENAME).write_text(
        json.dumps(document if document is not None else _document()),
        encoding="utf-8",
    )
    return root


def test_the_round_trip_writes_and_reads_the_same_card(tmp_path: Path) -> None:
    root = tmp_path / "model"
    (root / "bundle").mkdir(parents=True)
    document = _document()
    (root / CARD_FILENAME).write_text(json.dumps(document), encoding="utf-8")

    loaded = ModelCard.load(root)

    assert loaded.model_id == "pulmo.emphysema"
    assert loaded.model_version == "1.0.0"
    assert loaded.frameworks == FRAMEWORKS
    assert loaded.outputs == ({"kind": "segmentation", "value": 1, "name": "emphysema"},)
    assert loaded.stamp == STAMP
    assert loaded.weights["weights_file"] == "models/model.ts"


def test_the_loaded_card_builds_the_preprocessing_chain(tmp_path: Path) -> None:
    root = _model_dir(tmp_path)
    card = ModelCard.load(root)

    chain = card.chain()

    # The chain is the DECLARATIVE structure (MOS-TRAIN-034's constructor); its first
    # transform is orientation to the spec's target, which is the spec the card carried.
    assert chain.transforms[0].name == "Orientationd"
    assert chain.transforms[0].args["axcodes"] == card.spec.orientation_target


def test_a_missing_card_is_refused_not_guessed(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    (root / "bundle").mkdir(parents=True)
    with pytest.raises(ModelCardError, match="does not exist"):
        ModelCard.load(root)


def test_an_unknown_format_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["format"] = "medlange.modelcard/2"
    root = _model_dir(tmp_path, document)
    with pytest.raises(ModelCardError, match="refuses rather than guesses"):
        ModelCard.load(root)


def test_a_tampered_spec_digest_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["preprocessing"]["digest"] = "sha256:" + "f" * 64
    root = _model_dir(tmp_path, document)
    with pytest.raises(ModelCardError, match="does not match"):
        ModelCard.load(root)


def test_weights_that_are_not_there_are_refused(tmp_path: Path) -> None:
    root = _model_dir(tmp_path)
    (root / "bundle").rmdir()
    with pytest.raises(ModelCardError, match="bytes that are not there"):
        ModelCard.load(root)


def test_a_card_is_a_pure_function_of_its_facts() -> None:
    assert _document() == _document()
