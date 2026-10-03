# MedicalOS — the platform

The superstructure. 165 modules, 71 506 lines: it ingests DICOM, runs medical AI services
against it, records what happened, and governs who may do which of those things.

It is the third of this repository's three products, and the only one that is not a leaf:

| | what it is | its tests |
|---|---|---|
| [`../viewer/`](../viewer/README.md) | a standalone DICOMweb viewer | `viewer/tests/` — reads `viewer/`, nothing else |
| [`../trainer/`](../trainer/README.md) | a standalone model fitter | `trainer/tests/` — reads `trainer/`, nothing else |
| **`medos/medos/`** | **the platform that hosts both** | **`../tests/`, and that is deliberate — see below** |

---

## Why this directory has no `tests/` of its own

Because the platform's subject is the whole repository, and a `medos/medos/tests/` would have to
be smaller than the thing it tests.

**Most of the platform's unit tests read more than the platform package, and that is the
property — not the tally.** A suite whose modules mostly integrate several trees belongs
where it can see them all, which is the repository root.

Measured on 2026-09-26, as a snapshot and not a claim about tomorrow: of 54 modules in
`tests/unit/`, **13 read only `medos/medos/`, 39 read something else** and 2 read
neither — the specification (16), the compose file (15), `trainer/` (12),
`medos/deploy/` (11), the register (11), the release records (9), the training console
and the OHIF extension (7), `viewer/` (7), the nginx template (4); a module can appear
in several. The sentence here used to read "of the 38 modules … 11 … Twenty-seven",
whose arithmetic was self-consistent and whose population had moved; register entry 138
records what it said and why nothing noticed. Not because they are badly scoped, but because *integrating those things is
what this product does*. A test asserting that the deployment mounts a configuration over
the viewer's defaults is the platform's test. So is one asserting that the trainer's reach
into `medos.` stays inside a declared allow-list.

The rule the other two live by — **a suite that sits with a component may read that
component and nothing else** — gives the right answer here too, and the right answer is
that the platform's suite sits at the root. Moving those 13 files into
`medos/medos/tests/` would split one suite in two and leave the larger half where it was.

The same argument applies to `../docs/`. `docs/spec/` is not this product's
documentation: `MOS-UI-009a` governs the viewer, `MOS-TRAIN-034` the trainer, and
`MOS-IMG-003` the SDK (`medos/medos/sdk/`). A specification that binds three products
cannot live inside one of them.

## Layout

This directory is the PRODUCT; `medos/medos/` inside it is the package. The two levels are
listed separately because the distinction is load-bearing: everything in the first block
ships in the wheel, and nothing in the second does.

```
medos/          the platform package -- 194 files, 71 510 lines           (the wheel)
deploy/         the compose stack, the self-test models, the sealed
                reference: how THIS deployment runs                              23 files
schemas/        the JSON schemas the API validates against                       36 files
web/            the withdrawn OHIF extension (the training console it also held
                went with the 0.4.0 withdrawal, register entry 150)             15 files
tools/          scripts that are NOT installed with the platform --
                `MOS-IMG-069`: it must never mint a StudyInstanceUID             18 files
services/       the Service Plane and its one shipped capability                  7 files
examples/       the lung-nodule capability's envelope and concepts                4 files
api/            `v1/routes.core.yaml`, `v1/routes.train.yaml` -- the route
                contracts, not Python                                             2 files
contracts/      `permissions.yaml` and its generated companion                    2 files
```

And inside the package:

```
api/            the HTTP surface (FastAPI, thin; MOS-API-008 validation)   8 738
evidence/       manifests, leakage, validation reports, DSSE signatures   10 901
training/       cohorts, policy, splits, run records, the seal, the supervisor  9 401
db/             psycopg 3, written-out SQL, RLS tenancy, the job queue     4 521
sdk/            the SDK: cards, pipeline, adapters, profiles, canonical digests  6 664
safety/         the envelope every AI-derived finding is wrapped in        3 874
inference/      drivers and the serving path                               3 606
gateway/        the DICOM Gateway's Python side                            3 345
sealed/         vendor images that never see the network                   3 161
capabilities/   what a service version declares it can do                   3 154
core/           geometry, masks, DICOM I/O, the return contract            3 107
worker/         the claim loop and the job steps                           3 100
resolution/     which model answers which study                            2 675
security/       API keys (argon2id), log redaction                         2 287
registry/       service versions and their schemas                         2 277
bus/            the outbox and its drivers                                 2 016
writer/         SEG and SR back to the PACS                                1 412
dicomweb/       the QIDO/WADO client                                       1 384
promotion/      the act of putting a model into clinical use               1 316
cli/            `python -m medos.cli doctor`                                 611
objectstore/    S3/MinIO                                                     373
config/         dev-mode refusals                                            211
```

## What is deliberately not in here

- **`medos/medos/sdk/`** — the SDK: the spec format, the bundle layout, the phantom and
  the one canonicalisation rule. `MOS-IMG-003` requires exactly one implementation of
  these contracts and `MOS-TRAIN-034` names it as the only place a MONAI transform may be
  instantiated; the subtree used to be the top-level package
  `medicalos_preprocessing` and moved inside the distribution when the platform became
  the SDK. The trainer imports it without importing the rest of this platform.
- **torch, MONAI, nnU-Net.** `MOS-TRAIN-225` forbids the nnU-Net planner from this image's
  import closure *by name*. `medos/medos/sdk/chain.py` generates MONAI Bundle
  configs as *data* and never imports MONAI. `tests/integration/test_trainer_boundary.py`
  holds the line.

## Running its tests

```bash
pytest tests/unit                          # no container needed
pytest tests/integration --require-stack   # needs Postgres
pytest -m gate_0_3_0 --require-stack       # the release gate
```

`--require-stack` turns an unreachable dependency into a FAILURE instead of a skip.
[`../tests/README.md`](../tests/README.md) explains why that switch exists and what each
suite declares.

## Known state

`../docs/spec/99-known-inconsistencies.md` records 153 places where this repository does
not yet meet its own specification, each with what it costs and what would resolve it.
Two of them — entries 111 and 112 — are executable: `tests/unit/test_trainer_platform_contract.py`
holds them as strict `xfail`s, so the day either is fixed the suite goes red and the entry
has to be closed.
