# Medlange Core — the SDK

`medos.sdk` is the **Medlange Core**: the Python SDK every other Medlange product builds
on, and the framework an integrator connects their infrastructure to. Under the Medlange
umbrella it sits beside [Medlange Trainer](../../trainer/README.md) (which fits models
and writes their cards) and Medlange Viewer (the standalone DICOMweb viewer).

```python
from medos.sdk.modelcard import ModelCard
from medos.sdk.pipeline import Pipeline
from medos.sdk.adapters.pacs import DicomWebPacs
from medos.sdk.adapters.inference import KServeV2Inference
from medos.sdk.runtime import ExternalWorker

card = ModelCard.load(MODEL_DIR)                     # what the trainer declared
pipe = Pipeline(
    card,
    pacs=DicomWebPacs("https://pacs.example/dicom-web", bearer_token=...),
    inference=KServeV2Inference("http://triton:8000"),
)
result = pipe.run_study("1.2.840.10008.…")           # fetch → preprocess → infer → findings
```

## What the SDK owns

| module | what it is |
|---|---|
| `medos/medos/sdk/modelcard.py` | the medlange.modelcard/1 card a trainer writes beside its model and the SDK reads to rebuild everything inference needs: the preprocessing spec, weights facts, framework versions, per-model output descriptors, build stamp |
| `medos/medos/sdk/spec.py`, `chain.py`, `preprocess.py` | the `PreprocessingSpec` format, its chain builder, and the pure-numpy executor — the one implementation of the preprocessing contract (`MOS-TRAIN-034`: the chain builder is the only place a MONAI transform may be *named*) |
| `medos/medos/sdk/canonical.py` | RFC 8785 canonical JSON + sha256 — the one digest rule both sides compute against |
| `medos/medos/sdk/bundle.py`, `autoconfig.py` | the MONAI Bundle layout (`configs/inference.json` and friends) and the nnU-Net `plans.json` mapping table |
| `medos/medos/sdk/fixtures.py` | the phantom and its pinned hashes — how two implementations are compared |
| `medos/medos/sdk/contract.py` | the run-directory exchange between platform and trainer |
| `medos/medos/sdk/pipeline.py` | the serving pipeline: study → fetched series → canonical volume → the card's chain → inference → postprocessed findings, with the download/process timings both deployment modes report |
| `medos/medos/sdk/postprocess.py` | the declarative per-model postprocessor: segments cut from the label map and measurements lifted from metrics, exactly as the card's `outputs` descriptor promises |
| `medos/medos/sdk/adapters/pacs.py` | `PacsAdapter` and the DicomWeb driver (QIDO/WADO/STOW over the platform's client) |
| `medos/medos/sdk/adapters/inference.py` | `InferenceAdapter`, the KServe v2 / Triton driver, and `EmbeddedInference` for in-process models |
| `medos/medos/sdk/adapters/bus.py` | `BusAdapter`, the schema-flexible codec (`MessageMapping`), and the Kafka / RabbitMQ drivers |
| `medos/medos/sdk/runtime.py` | the two workers: `LocalWorker` (locally triggered) and `ExternalWorker` (bus-driven), one pipeline behind both |

## The two deployment modes

- **Local** — `LocalWorker.submit(study_uid)` runs one study; wrap it in whatever
  trigger the archive provides (webhook, watch, scheduled re-query). Results go to the
  sink you pass.
- **External** — `ExternalWorker` consumes a message bus: a message names a study, the
  worker fetches it from the archive, processes it, and publishes the result. Message
  schemas are **configuration, not contract**: the shapes live in a per-deployment
  mapping profile (`MessageMapping`), and [`medos/examples/bus/mosmed/`](../../../medos/examples/bus/mosmed/profile.yaml)
  is a worked example in the style of an external management system -- yours will differ,
  which is the point.

## Installation

```bash
pip install medos          # the SDK: cards, pipeline, postprocess, chain, canonical
pip install medos[server]  # + the platform drivers' transport (requests, psycopg, FastAPI…)
pip install medos[bus-kafka]    # + the Kafka bus driver
pip install medos[bus-rabbit]   # + the RabbitMQ bus driver
```

The SDK's core closure is numpy, pydicom, highdicom. Drivers import their transport
lazily and raise `DriverMissing` at construction until the matching extra is installed.

## What the SDK must never acquire

A database driver, a web framework, torch, MONAI, nnU-Net -- and any import of the
platform above the documented pure core (`medos.core.geometry`, `dicomio`, `errors`,
`masks`, `measure`, `uids`, `concepts`). `tests/unit/test_shared_package_is_pure.py`
asserts all of it, including that nothing anywhere in the repository implements these
contracts twice. This package is installed into both the platform image and the trainer
image, so a dependency added here is a dependency added to both.

## Who imports it

| | modules |
|---|---|
| [`medos/medos/`](../../../medos/README.md) | **35** — `api/routes_training.py`, `api/routes_curation.py`, `core/__init__.py`, `db/audit.py`, nine `evidence/*` and the rest |
| [`trainer/medos_trainer/`](../../trainer/README.md) | **6** — and nothing else from outside itself. That is what makes the trainer installable by somebody with no Medlange platform |

Both Docker images install the SDK with the rest of the `medos` distribution.

## History

This package began as the standalone `medicalos_preprocessing` package that `MOS-IMG-003`
required to be published on its own; it moved inside the distribution as `medos.sdk` when
the platform became the SDK (register entry 151). The contracts did not change -- the
address did.
