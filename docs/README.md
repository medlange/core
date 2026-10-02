# docs

Four subdirectories with four different jobs. This file exists because they had no index
and the difference between them is not guessable from the names.

| | what it is | who may change it |
|---|---|---|
| [`spec/`](spec/01-overview.md) | **the specification.** Nineteen chapters plus the register. NORMATIVE: every `MOS-…` requirement in this repository is defined here and nowhere else | an amendment, recorded in the chapter as an amendment |
| [`adr/`](adr/) | architecture decisions with their reasoning and their rejected alternatives | append; a decision is not edited, it is superseded |
| [`releases/`](releases/README.md) | what was decided AT each release, plus a status page | **the records: nothing.** A release record is a historical statement. `releases/README.md` is not one — see below |
| [`services/`](services/AUTHORING.md) | how somebody authors a capability against the Service Plane | ordinary documentation |

## Why this directory is not inside a product

This repository ships four things — [`viewer/`](../viewer/README.md),
[`trainer/`](../trainer/README.md), [`medos/`](../medos/README.md) and
`medicalos_preprocessing/`, the SDK both images install — and each of the first three
carries its own README and its own test suite. `docs/` does not follow any of them,
because **a specification that binds three products cannot live inside one of them**:

- `MOS-UI-009a` is the viewer's — the four guarantees the first-party clinician surface
  is held to.
- `MOS-TRAIN-034` is the trainer's — `build_chain` as the only place a MONAI transform may
  be instantiated.
- `MOS-IMG-003` is the SDK's — `medicalos_preprocessing/`'s — the requirement that it be
  published as its own package at all.

Measured: no chapter is single-product. `09-clinical-safety.md` carries twenty-nine viewer
mentions and the whole of the platform's safety plane; `10-api.md` carries forty-one
training mentions and the API surface. The chapters are split by SUBJECT, not by product,
and cutting them per product would cut most of them in half.

The same argument keeps [`../tests/`](../tests/README.md) at the root: it holds
`test_viewer_deployment.py`, `test_trainer_import_boundary.py` and
`test_shared_package_is_pure.py`, whose subject is the RELATIONSHIP between products
rather than any one of them.

## Reading order

Start at [`spec/01-overview.md`](spec/01-overview.md). `../MEDICALOS_SPEC.md` is the
requirement index into these chapters — note that it is maintained by hand despite
`MOS-CORE-024` requiring it be generated, which is register entry 108.

[`spec/99-known-inconsistencies.md`](spec/99-known-inconsistencies.md) is where this
repository records what it knows it has not done. It is long on purpose: a medical
platform whose known-defect list is short is one that has not looked. `README.md`,
`DEVELOPMENT.md` and `CONTRIBUTING.md` each state its entry count in prose, and
`tests/unit/test_compose_profiles.py` asserts that all three agree with the register.

## Release records are not to be edited

`releases/0.1.0.md`, `0.2.0.md` and `0.3.0.md` record what was measured and decided at a
tag. Twice in one day a mechanical path rewrite ran through them and turned a sentence
like "the ids appearing in `medos/`, `services/`, `tools/` … at the end of this range"
into one naming directories that did not exist when the range ended. Both passes were
reverted byte-for-byte against git.

A path in these files is EVIDENCE, not an ADDRESS. Nothing mechanical can tell the two
apart; if a sweep is going to touch `docs/`, it must be told to skip the three RECORDS the
way it is already told to skip `.evidence/`.

**`releases/README.md` is the exception, and it has to be, because it is written in the
present tense.** It is a status page about the records, not a record: it says how many
there are, how the repository's images divide, and what the coverage register covers today.
Measured on 2026-09-26, three of its claims had gone false without anybody editing it — it
counted "seven images" against a compose file declaring nine, it counted "the two MedicalOS
images" against four, and it said the coverage register "exists for 0.1.0 only" after that
register had grown to classify all four release rows. A page that describes a moving tree
and is never allowed to move is a page that is eventually wrong about everything.
`tests/unit/test_release_records.py` is what keeps those claims honest, and it is also what
refuses a record for a release that has not happened.
