# SPDX-License-Identifier: Apache-2.0
"""The model card: what a fitted model declares about itself, readable by the SDK.

A model card is the small JSON document a trainer writes NEXT TO the model artifact it
produced (`bundle/` in the MONAI Bundle layout of `MOS-TRAIN-129`) and a consumer of the
SDK reads to reconstruct everything inference needs: which `PreprocessingSpec` the model
was trained against, where its weights live, which framework versions produced them, and
which outputs (segments, measurements) the model claims to produce.

The card is the contract the SDK and the trainer meet on WITHOUT meeting:

  * the TRAINER writes it at the end of `fit`, from the derived spec document and its own
    build stamp -- facts it has, and facts only it has;
  * the SDK reads it with `ModelCard.load(directory)` and hands the caller a parsed,
    validated card whose `chain()` is the preprocessing pipeline to run before inference.

    `outputs` descriptors, as written today:
      `{"kind": "segmentation", "value": int, "name": str}` -- a value of the model's
        label map. For the result-writer driver (`medos.sdk.adapters.results`), add
        `"structure"`: the structure slug the platform's concept dictionary resolves to
        a coded concept (`MOS-IMG-112`: codes come from the dictionary, never invented).
      `{"kind": "measurement", "name": str, "unit": str?, "from": str}` -- a named
        metric. For the writer driver, add `"concept_key"`: the key into the platform's
        concept dictionary for the measurement's coded name.

`format` is pinned to exactly `medlange.modelcard/1`. A card that names another format
is refused rather than guessed at, because the fields below are promises about bytes
(weights paths, spec digests) and a guessed reader is how a promise about bytes stops
being one.

Spec: MOS-IMG-049 (the spec is the record of what training did), MOS-TRAIN-129/130/131
(the bundle the card accompanies).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from medos.sdk import errors
from medos.sdk.canonical import canonical_bytes, sha256_hex
from medos.sdk.preprocess import Chain, build_chain
from medos.sdk.spec import PreprocessingSpec, parse_spec

CARD_FILENAME = "modelcard.json"
CARD_FORMAT = "medlange.modelcard/1"

__all__ = [
    "CARD_FILENAME",
    "CARD_FORMAT",
    "ModelCard",
    "ModelCardError",
    "document_for",
]


class ModelCardError(errors.TrainingError):
    """A model card is absent, malformed, names missing bytes, or digest-mismatched.

    Not a refusal: a refusal is an answer to a question a principal asked, and a broken
    artifact on disk is not that. It is the shape `registry.errors.InvalidManifest`
    already sets for "a declared thing does not hold".
    """


def _req(document: Mapping[str, Any], key: str) -> Any:
    value = document.get(key)
    if not isinstance(value, (str, int, float, dict, list)) or value in ("", [], {}):
        raise ModelCardError(
            f"modelcard: {key!r} is missing or empty; a card is a promise about "
            f"bytes and {key!r} is part of the promise"
        )
    return value


def document_for(
    *,
    model_id: str,
    model_version: str,
    spec_document: Mapping[str, Any],
    weights: Mapping[str, Any],
    frameworks: Mapping[str, str],
    outputs: tuple[Mapping[str, Any], ...],
    stamp: Mapping[str, Any],
) -> dict[str, Any]:
    """The canonical card document. THE WRITER'S HALF: pure, so the trainer and any
    other producer emit byte-identical cards for identical facts."""

    def _bad(why: str) -> ModelCardError:
        return ModelCardError(f"modelcard: {why}")

    if not isinstance(model_id, str) or not model_id.strip():
        raise _bad("model_id must be a non-empty string")
    if not isinstance(model_version, str) or not model_version.strip():
        raise _bad("model_version must be a non-empty string")
    for name, table in (("weights", weights), ("frameworks", frameworks), ("stamp", stamp)):
        if not isinstance(table, Mapping) or not table:
            raise _bad(f"{name} must be a non-empty mapping")
    if not isinstance(outputs, tuple):
        raise _bad("outputs must be a tuple of descriptors")
    return {
        "format": CARD_FORMAT,
        "model_id": model_id,
        "model_version": model_version,
        "preprocessing": {
            "format": "medlange.preprocessing_spec/1",
            "document": dict(spec_document),
            "digest": "sha256:" + sha256_hex(canonical_bytes(dict(spec_document))),
        },
        "weights": dict(weights),
        "frameworks": dict(frameworks),
        "outputs": [dict(o) for o in outputs],
        "stamp": dict(stamp),
    }


@dataclass(frozen=True)
class ModelCard:
    """A parsed, validated model card. THE READER'S HALF.

    `spec` is the parsed `PreprocessingSpec` (the parse IS the validation,
    `MOS-IMG-049`); `chain()` is the preprocessing pipeline the SDK builds from it --
    the one place a MONAI transform may be instantiated (`MOS-TRAIN-034`).
    """

    model_id: str
    model_version: str
    spec: PreprocessingSpec
    weights: Mapping[str, Any]
    frameworks: Mapping[str, str]
    outputs: tuple[Mapping[str, Any], ...]
    stamp: Mapping[str, Any]
    directory: Path | None = field(default=None, repr=False)

    @classmethod
    def load(cls, directory: str | Path) -> ModelCard:
        root = Path(directory)
        path = root / CARD_FILENAME
        if not path.is_file():
            raise ModelCardError(
                f"modelcard: {path} does not exist; a model directory is the card and "
                f"the artifact it names, and one without the other is not a model"
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelCardError(
                f"modelcard: {path} is not UTF-8 JSON: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise ModelCardError(
                f"modelcard: {path} holds {type(document).__name__}, not an object"
            )

        fmt = document.get("format")
        if fmt != CARD_FORMAT:
            raise ModelCardError(
                f"modelcard: format is {fmt!r}; this reader knows {CARD_FORMAT!r} and "
                f"refuses rather than guesses at a newer card"
            )

        model_id = _req(document, "model_id")
        model_version = _req(document, "model_version")
        preprocessing = _req(document, "preprocessing")
        if not isinstance(preprocessing, Mapping):
            raise ModelCardError("modelcard: 'preprocessing' must be an object")
        spec_document = preprocessing.get("document")
        if not isinstance(spec_document, Mapping):
            raise ModelCardError(
                "modelcard: 'preprocessing.document' must carry the PreprocessingSpec"
            )
        # THE PARSE IS THE VALIDATION. A spec this card invented would fail here.
        spec = parse_spec(dict(spec_document))
        declared = preprocessing.get("digest")
        if isinstance(declared, str) and declared.startswith("sha256:"):
            actual = "sha256:" + sha256_hex(canonical_bytes(dict(spec_document)))
            if declared != actual:
                raise ModelCardError(
                    "modelcard: preprocessing.digest does not match the spec document; "
                    "the card was edited after it was written, or written by something "
                    "that did not digest the same bytes"
                )

        weights = _req(document, "weights")
        if not isinstance(weights, Mapping) or not weights.get("path"):
            raise ModelCardError("modelcard: 'weights.path' names the artifact directory")
        weights_path = root / str(weights["path"])
        if not weights_path.is_dir():
            raise ModelCardError(
                f"modelcard: weights.path {weights['path']!r} does not exist under "
                f"{root}; the card names bytes that are not there"
            )

        frameworks = _req(document, "frameworks")
        outputs_raw = document.get("outputs") or []
        if not isinstance(outputs_raw, list):
            raise ModelCardError("modelcard: 'outputs' must be a list of descriptors")
        stamp = _req(document, "stamp")

        return cls(
            model_id=str(model_id),
            model_version=str(model_version),
            spec=spec,
            weights=dict(weights),
            frameworks={str(k): str(v) for k, v in dict(frameworks).items()},
            outputs=tuple(dict(o) for o in outputs_raw),
            stamp=dict(stamp),
            directory=root,
        )

    def chain(self) -> Chain:
        """The preprocessing pipeline this model was trained against.

        `MOS-TRAIN-034`: `build_chain` is the only place a MONAI transform may be
        instantiated, and this card's spec is the argument it exists for.
        """
        return build_chain(self.spec)

    def document(self) -> dict[str, Any]:
        """The canonical document form (what `write` emits)."""
        return document_for(
            model_id=self.model_id,
            model_version=self.model_version,
            spec_document=self.spec.as_json(),
            weights=self.weights,
            frameworks=self.frameworks,
            outputs=self.outputs,
            stamp=self.stamp,
        )

    def write(self, directory: str | Path) -> Path:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        path = root / CARD_FILENAME
        document = self.document()
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path
