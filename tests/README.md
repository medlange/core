# Running the MedicalOS test suite

    pytest                                    # everything, skips what it cannot reach
    pytest --require-stack                    # CI: an unreachable dependency is a FAILURE
    pytest --require-corpus                   # nightly: absent private test data is a FAILURE

## Why there are two switches

A skipped test is a test that did not run. A suite that prints `17 skipped` and exits 0
has told you a number and not a fact. Twice on this project that number hid a dead system:

| what happened | what the run printed | exit |
| --- | --- | --- |
| 74 of 95 integration tests skipped on a Postgres port that no longer existed (`127.0.0.1:55433`) | `21 passed` | 0 |
| all 17 e2e tests skipped on `medos-api -> HTTP 503` | `242 passed, 17 skipped` | 0 |

Both were green on a system that could not serve a single request.

But the answer is not "never skip". The LCTSC/TCIA corpus is large, private and not
redistributable, and a contributor without it must still be able to run the suite. So the
taxonomy separates the two reasons a test does not run, and gives each its own switch.

| helper | meaning | default | `--require-stack` | `--require-corpus` |
| --- | --- | --- | --- | --- |
| `skip_infra(reason, dependency=...)` | a dependency that **should** be reachable is not | skip | **FAIL** | skip |
| `skip_no_data(reason, corpus=...)` | optional, non-redistributable data is absent | skip | skip | **FAIL** |
| `skip_environment(reason, detail=...)` | a caveat about this machine that no provisioning fixes | skip | skip | skip |

**Conflating the two is what makes people turn the whole thing off.** A contributor
without the corpus would otherwise have to drop `--require-stack` as well, and with it
every guarantee the flag exists to provide.

### What each switch guarantees

* `--require-stack` / `MEDOS_REQUIRE_STACK=1` — **every dependency the selected tests
  declare was probed and answered correctly, every test that needs one actually ran
  against it, and at least one test ran at all.** The failure message names the
  dependency and the command that starts it.
* `--require-corpus` / `MEDOS_REQUIRE_CORPUS=1` — **every test that needs the LCTSC/TCIA
  corpus actually read it.** For the nightly job that mounts the corpus; meaningless, and
  wrong, anywhere else.

Neither flag changes what any test asserts. They only change what happens when a test
cannot run at all.

## What `--require-stack` probes, and why "do not skip" was not enough

Converting a skip into a failure only covers a dependency some test already reaches for.
It covers nothing at all for one that no test touches. Measured on the development stack:

    docker stop medos-gateway medos-worker medos-web medos-minio medos-triton medos-tritond
    pytest tests/unit tests/integration/test_api.py tests/integration/test_queue.py \
        -q --require-stack
    -> 233 passed, exit 0, "INFRA SKIPS: 0"

`medos-gateway` is the worker's **only** route to DICOM (`MEDOS_DICOMWEB_URL:
http://medos-gateway:8043/dicomweb/<tenant>` in `docker-compose.yml`). The platform could
not fetch one instance, the viewer could not load one image, and strict mode called it
healthy — because nothing skipped, so nothing failed.

So strict mode now **probes**. `tests/_support/stack.py` holds one probe per compose
service and `SUITE_DEPENDENCIES` says which suite needs which, at module granularity:

| suite | declares |
| --- | --- |
| `tests/unit` | nothing — a unit test that needs a container is in the wrong directory |
| `tests/integration` | `postgres` |
| `tests/integration/test_dicomweb.py` | `postgres`, `orthanc` |
| `tests/integration/test_worker.py` | `postgres`, `orthanc`, `orthanc-rest`, `docker` |
| `tests/e2e` | the deployment: postgres, orthanc (DICOMweb through the Gateway + native REST from inside its container, so `docker` too), medos-gateway, medos-api, medos-worker, ohif, minio, triton, medos-tritond |
| `tests/gate` | the deployment, minus orthanc and ohif: postgres, medos-gateway, medos-api, medos-worker, minio, triton, medos-tritond |
| `tests/gate/test_{deployment_gate,non_inferiority,per_case_metrics,leakage_check}.py` | `postgres` |
| `tests/gate/test_{ruo_marking,report_offline_verify}.py` | nothing |

**Neither `viewer/tests/` nor `trainer/tests/` is in this table, and that is the point.** The viewer is a
standalone DICOMweb viewer; its suite reads `viewer/` and nothing else, declares no
dependency, imports nothing from `tests/_support`, and runs with no repository around it
(`cd viewer && pytest tests`). `testpaths` in `pyproject.toml` names it so a bare `pytest`
here still runs it — `tests/unit/test_suite_layout.py` asserts that no suite in this
repository sits outside `testpaths`, because a suite that leaves it does not fail, it
stops being counted. What a *deployment* does to the viewer — mounting a configuration
over its defaults, substituting the MOS-SAFE-001 sentence into the body — is asserted from
this side, in `tests/unit/test_viewer_deployment.py`. The platform may read the viewer;
the viewer may not read the platform.

`trainer/tests/` is the same arrangement one directory over: four modules, 54 tests,
needing `torch`, `numpy` and `nnunetv2` and nothing from `medos`. What the platform asserts
ABOUT the trainer stays here — `test_trainer_contract.py` (both sides of the exchange
agree), `test_trainer_import_boundary.py` (what the image may reach into `medos.` for),
`test_trainer_platform_contract.py` (two register entries, as executable xfails) and
`tests/integration/test_trainer_boundary.py` (the serving image imports no torch).

Module granularity matters: declaring Orthanc for the whole integration directory would
make `pytest tests/integration/test_queue.py --require-stack` red on a machine with no
PACS, and **a switch that is red on a correct run is a switch people delete.**

The six `tests/gate` module rows are the same argument for the release-0.2.0 gate. The
directory default is the 0.1.0 row's declaration — those five checks are claims about what
the running images do — but `MOS-EVID-006` puts the whole evidence plane outside the serving
path, so the 0.2.0 checks have no API to call and no worker to wait for. Two of them declare
*nothing*, and the empty tuple is the assertion: `report-offline-verify`'s whole claim is
that a `ValidationReport` verifies with no MedicalOS in sight, so needing MedicalOS running
to believe it would be a contradiction.

### The three release gates

    pytest -m gate_0_1_0 --require-stack      # docs/spec/15-delivery.md §15.1.2, 0.1.0 row
    pytest -m gate_0_2_0 --require-stack      # ... and the 0.2.0 row
    pytest -m gate_0_3_0 --require-stack      # ... and the 0.3.0 row

The 0.3.0 row has nine checks where §15.1.2 names eight: `migration-drift` is a LOCAL
addition, declared and argued in `tests/unit/test_gate_contract.py`'s `LOCAL_EXTRA`, which is
the only route by which `tests/gate/` may hold a module the specification does not name. It
exists because 0.2.0 was fully green while the live deployment had none of 0.2.0's tables —
every schema-shaped check builds a throwaway database, so nothing was asking whether anyone
had run the migrations against the database the release serves from.

The 0.3.0 row also has four subjects rather than one. `zero-core-change` and
`no-auto-promote` are claims about the source tree, `chain-equivalence` about a pure
function, `sealed-mode-isolation` and `migration-drift` about the running deployment, and the
remaining four about a throwaway schema (`platform_dsn`, a second database from the 0.2.0
row's — see `tests/gate/conftest.py` for why they are not shared).

Each selects exactly its own row and **refuses a subset of it**: `tests/gate/conftest.py`
raises a usage error when the marker was asked for and a check is missing, because a run
that exercises four of five checks prints a green `N passed` for a gate that has quietly
stopped covering something (`MOS-REL-004`, `MOS-REL-012`). Running one module by path is
still allowed — it prints `NOT A GATE RUN` in the summary and names what was covered.

`tests/unit/test_gate_contract.py` asserts the same completeness from the *default* suite,
parsing the check names out of §15.1.2 rather than out of a list this repository keeps, so a
renamed or added check in the specification fails a test that names it. A gate marker outside
`tests/gate/` fails there too: it would be selected by the gate and invisible to the
completeness guard.

`tests/unit/test_stack_contract.py` fails if a service appears in `docker-compose.yml`
that is in neither `PROBES` nor `UNCOVERED`, so the *next* service cannot be added without
someone deciding whether the suite depends on it.

### A status code is never proof

Every probe checks the **shape** of the response, not just `< 400`:

* the OHIF origin is an nginx with an SPA fallback — it answers `200 text/html` for every
  path that does not exist, including `/dicom-web/studies`. A probe that accepts any 2xx
  calls that origin a healthy PACS. Measured: `DicomWebGateway.available()` returned
  `True` against `http://127.0.0.1:3000/dicom-web`. `medos/medos/dicomweb/client.py`'s preflight
  now rejects it, and so does the probe table.
* `medos-api` splits liveness from readiness deliberately: `/healthz` does not touch the
  database, so an orchestrator does not restart a working process. That is exactly what
  makes liveness worthless as a suite gate — the probe reads `/readyz` and requires
  `status: ready`, because the second incident was a 503 there.
* `medos-worker` has no HTTP surface, so its compose healthcheck is the signal. With no
  `docker` on PATH the probe reports it **down**, never "assume up".

## Three ways a run could still report nothing, and what stops them

| hole | what it looked like | closed by |
| --- | --- | --- |
| a dependency no test classifies | `233 passed` with six services stopped | the probe table above |
| a module skipped at **collection** time (`pytest.skip(allow_module_level=True)`, `pytest.importorskip`) | pytest said `2 skipped`; the taxonomy block said `OTHER SKIPS: 0` | `pytest_collectreport`; under `--require-stack` it is a failure |
| nothing ran at all — an `-m` that matches nothing, a script-style file | all-zero taxonomy block, which reads like a clean run | the `SELECTION:` line, and exit 1 under `--require-stack` |

## The session report

Every run, in every mode, ends with a block like this — printed even when every count is
zero, so that its absence means the plugin did not load:

    =========================== MedicalOS skip taxonomy ===========================
    mode: --require-stack off, --require-corpus off
    SELECTION:   17 collected, 0 deselected, 17 ran
    STACK PROBE: 8/10 declared dependencies ready   <- UNREACHABLE: orthanc-e2e, orthanc-rest
      - orthanc-e2e: GET http://127.0.0.1:8043/dicomweb/<tenant>/studies?limit=1 -> ConnectionError
      - orthanc-rest: docker exec medos-orthanc python3 exited 1: No such container
    INFRA SKIPS: 17 (orthanc: 17)   <- strict mode (--require-stack) would FAIL these
    DATA SKIPS:  0
    ENV SKIPS:   0   <- machine caveats; never failed by a switch
    OTHER SKIPS: 0

    A dependency above was unreachable. In CI this run would be RED: pytest --require-stack (or MEDOS_REQUIRE_STACK=1).

Read `SELECTION:` first. Every other number is meaningless without it — a run that
collected nothing prints the same all-zero taxonomy as a run that tested everything and
found nothing wrong. It says `NOTHING RAN. This run asserted nothing.` when it must.

`OTHER SKIPS` counts skips that have not been routed through the taxonomy yet. It should
stay at zero: a new `pytest.skip` belongs in one of the three helpers.
`COLLECT-TIME SKIPS` appears only when a whole module left the run before any test of it
existed; under `--require-stack` that is a failure.

## Environment

| variable | what it points at | default |
| --- | --- | --- |
| `MEDOS_TEST_DATABASE_URL` | a Postgres superuser DSN; the suite creates and drops throwaway databases on it | `postgresql://medos:medos@127.0.0.1:55433/medos` (**stale** — set it) |
| `MEDOS_DICOMWEB_URL` | a DICOMweb root. The **Gateway's**, not the PACS's: `orthanc` publishes no host port and MOS-DATA-006 is why (register entry 70) | `http://127.0.0.1:8043/dicomweb/$MEDOS_TENANT_ID`, with `MEDOS_GATEWAY_WORKER_KEY` as the bearer |
| `MEDOS_E2E_LCTSC_ROOT` | the LCTSC/TCIA corpus | `F:/WorkSpace/PulmoAI/TCIA` |
| `MEDOS_APP_DB_PASSWORD` | the password the suite gives `medicalos_app` | `medos_app`, matching `docker-compose.yml` |

`MEDOS_APP_DB_PASSWORD` deserves a paragraph. `ALTER ROLE ... PASSWORD` is
**cluster**-wide, not database-wide, so the throwaway database the session creates is not
a sandbox for it: the change lands on the server `medos-api`, `medos-worker` and
`medos-gateway` are connected to, and it outlives the run. Measured:

    ALTER ROLE medicalos_app PASSWORD 'deployment_secret'   -- the stack was started with one
    pytest tests/integration/test_queue.py::... -q --require-stack
    -> 1 passed, exit 0
    medicalos_app/deployment_secret: REFUSED     <- after the green run
    medicalos_app/medos_app:         OK

One passing test silently rewrote a running deployment's credential. So the fixture no
longer writes a credential it has not first established is safe to write: it sets one that
does not exist, leaves alone one that already matches, and **refuses** — loudly, as a
fixture error — to overwrite a different one. Set `MEDOS_APP_DB_PASSWORD` to the value
your stack uses, or point `MEDOS_TEST_DATABASE_URL` at a throwaway cluster.

## Writing a new test that cannot always run

Import from the one home, `tests/_support/skips.py`, and pick the helper that states *why*:

```python
from tests._support.skips import skip_infra, skip_no_data, skip_environment

if not gateway.available():
    skip_infra(f"no DICOMweb origin at {url} (set MEDOS_DICOMWEB_URL)", dependency="orthanc")

if not corpus_root.is_dir():
    skip_no_data(f"no LCTSC tree at {corpus_root} (set MEDOS_E2E_LCTSC_ROOT)", corpus="lctsc-corpus")
```

For a whole test, use the marker form, which honours `--require-corpus` as
`pytest.mark.skipif` cannot:

```python
real_data = skipif_no_data(not ROOT.exists(), f"corpus not mounted at {ROOT}", corpus="lctsc-corpus")
```

Keep the reason informative. The ones already there name the URL or the environment
variable to set, and that is the part a reader acts on. A new dependency should also get
an entry in `START_HINTS` in `tests/_support/skips.py`, so the strict-mode message can say
how to start it.

Do **not** reach for `skip_environment` to get past a dependency. It is the one helper no
switch can fail, which makes it the only remaining way to take a test out of the run and
stay green everywhere. It is for a caveat no provisioning fixes, and one site that was
using it for a reachability condition has already been removed for exactly this reason
(`test_a_wrong_dicomweb_root_is_reported_as_such`). Every `skip_environment` is counted
and printed; a rising `ENV SKIPS` is a smell, not a clean bill of health.

## Adding a service to the deployment

`tests/unit/test_stack_contract.py` will fail until you do one of two things in
`tests/_support/stack.py`:

1. add a probe to `PROBES` — shape-checked, readiness rather than liveness — and list the
   key in `SUITE_DEPENDENCIES` for whichever suites need it; or
2. add it to `UNCOVERED` with the reason it cannot be probed.

There is no third option, and that is the point: "nobody thought about it" is what both
incidents were made of.

## CI

`.github/workflows/tests.yml`.

* `unit` — `pytest tests/unit --require-stack` with **no containers at all**. That is not
  a contradiction: `tests/unit` declares no dependencies, so the run is green, and the
  declaration is asserted by `test_stack_contract.py`.
* `stack` — brings up `medos/deploy/compose`, then runs the probe table from
  `tests/_support/stack.py` against the **host-facing** endpoints (a service can be
  healthy inside the network and unpublished outside it, and an nginx SPA fallback can
  answer 200 for a service that is not there), then `pytest tests/integration tests/e2e
  --require-stack`.
* `nightly-corpus` — adds `MEDOS_REQUIRE_CORPUS=1` on the runner that has the corpus, and
  runs `pytest tests` against it.

**Both bullets used to end "then the week-0 contract script".** That script is gone with
`spikes/week0/` (register entry 118); the eight `MOS-IMG-010` rejection codes it was the
only runner of are `tests/unit/test_geometry_contract.py` now, collected by the `unit` job.
The workflow's own comment says so and this file did not, which is the ordinary way a
document about a moving tree stops being true.

### This is not the CI topology the specification describes

Chapter 14 §14.8 (`MOS-TEST-078`) declares four pipelines — `pr`, `main`, `nightly`,
`release` — naming twelve jobs, and `MOS-TEST-079` makes the `pr` set the required status
checks on `main`. None of the twelve exists, and none of the three jobs above is named in
that chapter. The two schemes do not overlap at any point.

That is recorded as **register entry 127** and frozen by
`tests/unit/test_testing_chapter_is_unbuilt.py`, which also asserts the three jobs above
against `.github/workflows/tests.yml` so this section cannot drift from the file it
describes again. Which of the two schemes should win is a specification decision and is not
taken there.

One caution about the env-var form of the switches, which `nightly-corpus` uses: an
unrecognised `--require-stack` is a pytest usage error and impossible to miss, whereas an
environment variable nothing reads is silence. Check the `mode:` line in the taxonomy
block before believing a green run that relied on the env var.

## `_support/*_probe.py` — ten instruments, run by hand

`tests/_support/` holds ten DICOM generators, each written against a rendering defect that
was found once and could not have been found by a structural test. **No test imports or
runs any of them.** They are hand-run, and their `__main__` blocks say so:

```bash
python tests/_support/polarity_probe.py /tmp/probe   # writes the pair, prints the STUDY uid
```

| probe | the defect it was built to make visible |
|---|---|
| `burned_in_probe.py` | three series differing only in what `(0028,0301)` says about their own pixels |
| `multiframe_probe.py` | a paired control for multi-frame instances, spatial and temporal |
| `padding_probe.py` | a CT whose out-of-field area is padded, and the same image without the declaration |
| `polarity_probe.py` | MONOCHROME1 rendered as its own negative — bone black, air white, nothing broken-looking |
| `seg_truncation_probe.py` | a segmentation whose bitstream stops half way, and the CT it claims to cover |
| `spacing_probe.py` | two stacks with the same slices, one missing a block in the middle |
| `tilt_probe.py` | a tilted-gantry stack with a marker at a fixed point in the patient, and a flat control |
| `units_probe.py` | three instances whose pixels are identical and whose units are not |
| `unsigned_probe.py` | a full-range unsigned ramp that renders as a sawtooth if the pipeline wraps |
| `voi_probe.py` | one ramp, three VOI LUT functions, identical windows |

**What their status is, said plainly.** Eight of the ten are cited in
`viewer/tests/test_architecture.py` comments in the form "Measured with
`tests/_support/spacing_probe.py`: …" — a record that a human ran it once, **with no date
and no stored result**. `MOS-IMG-158` permits a manual procedure and requires it to be
documented with its output attached to the release record; that has not been done, and
`docs/adr/BUILD_VS_ADOPT.md` records the automated rendering-correctness harness as still
owed. So this table is an inventory of instruments, not a claim of coverage. The structural
checks in `viewer/tests/test_architecture.py` are what actually runs; they read source text
and cannot see a wrong picture, which is the whole reason these ten exist.

Two of the ten — `polarity_probe.py` and `unsigned_probe.py` — are cited nowhere at all, so
nothing records that their defects were ever measured. Register entry 132.
