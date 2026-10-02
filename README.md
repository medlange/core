# Medlange

<p align="center">
  <img src="docs/assets/medlange-logo.png" alt="Medlange" width="180">
</p>

**Medlange is the umbrella for three products that take a medical model from training to
the radiologist's screen:**

| Product | Repository (planned) | What it is |
|---|---|---|
| **Medlange Core** | `medlange/core` | the SDK (`medos.sdk`): model cards, the serving pipeline, preprocessing contracts, adapters to a PACS, a message bus (Kafka/RabbitMQ) and an inference server (Triton / any KServe v2 speaker). This repository carries it |
| **Medlange Trainer** | `medlange/trainer` | the batch image in the lineage of nnU-Net and MONAI that fits models and writes their model cards. No web surface, ever — `trainer/` |
| **Medlange Viewer** | `medlange/viewer` | the standalone DICOMweb viewer: no build step, no bundler, no runtime dependency — `viewer/` |

**Core is the runtime between a medical model and a governed clinical service.** A web
framework turns a function into an HTTP endpoint; Medlange Core turns a model into a
service that can say where its training data came from, who de-identified it, what it was
evaluated against, and why it refused.

It ships DICOM ingest through a credentialed gateway, cohort curation with sealed
content-addressed dataset versions, a training plane that records its own hardware rather
than asserting it, an evidence plane with acceptance gates, and an audit trail — as a set
of pinned containers you bring up with one command.

> **MedicalOS integrates, governs and evidences medical AI services. It does not diagnose,
> does not replace a PACS, and is not itself a medical device.** It is infrastructure for
> building such systems. Nothing in this repository has regulatory clearance, and adopting
> it grants none: the obligations of IEC 62304, ISO 13485 and your jurisdiction's device
> regulation remain entirely yours. See [`docs/spec/`](docs/spec/) for what the platform
> claims and [`docs/spec/99-known-inconsistencies.md`](docs/spec/99-known-inconsistencies.md)
> for 153 recorded places where it does not yet meet its own specification.

Apache-2.0.

---

## The thing that makes it different, and the thing that makes it annoying

**MedicalOS refuses.** It will not seal a cohort whose de-identification provenance nobody
declared. It will not record a training run without the nine facts about the software that
fitted it. It will not let a segment named `Segment_1` become a clinical class because a
folder was called `hydrothorax`.

Those refusals are the product. They are also the first thing you will meet, and until
recently there was no way to tell a refusal that means *"you asked me to assert something I
cannot back"* from one that means *"nobody has configured me yet"* — both answer `503`.

So the first command to run is:

```bash
python -m medos.cli doctor
```

Every row comes back as one of four things, and the distinction is the point:

| | |
|---|---|
| `ok` | checked, and working |
| `!!` | **unconfigured** — a deployment has not said something only it can say. The row prints the fix. |
| `--` | **refusing** — the platform is declining to make an unbacked claim. Nothing is broken; the row says what would have to become true. |
| `?` | **unknown** — could not be checked. Never counted as ready: a probe that cannot run has not passed. |

## Quick start

```bash
docker compose -f medos/deploy/compose/docker-compose.yml up -d
python -m medos.cli doctor
```

That brings up **seven** services — API, gateway, worker, sealed service, Orthanc,
Postgres and the web front door — and shows you exactly what state they are in. The
seventh is `medos-web`, an nginx that serves the first-party viewer at `/mos-viewer/`
and the `/api/` and `/dicomweb/` proxies. It ran `ohif/app:v3.9.2` until release 0.4.0
withdrew it, and it served the training console until the console was withdrawn at
specification 0.4.0 (register entry 150); the compose block still says so in its own
header. No GPU is needed and nothing on that list requires one.

Two more planes are declared and stay off until you ask for them:

```bash
docker compose -f medos/deploy/compose/docker-compose.yml --profile inference up -d
docker compose -f medos/deploy/compose/docker-compose.yml --profile training  up -d
```

`inference` adds Triton, MinIO and the two publishing services. It is **8.1 GB of a
9.4 GB stack** — about six times everything else combined — and the three capabilities
this stack ships are deterministic and never call it. `training` adds the trainer pair,
which does need an NVIDIA runtime and an image you build yourself with
`trainer/build.sh`.

### Put something in the archive

A fresh archive is empty, and an empty viewer looks the same as a broken one. One
command fixes that:

```bash
docker compose -f medos/deploy/compose/docker-compose.yml --profile demo up medos-seed-corpus
```

It stores **one synthetic CT study** — 64 slices, two lungs and an 8 mm nodule, sized to
sit inside both shipped capabilities' declared envelopes so that jobs against it return
findings rather than `outside_applicability_envelope`. `lung_segmentation` reports right
1843 ml, left 1843 ml, total 3687 ml on it.

There is no patient behind it and the tags say so: `PatientName` is
`PHANTOM^SYNTHETIC^NOT^A^PATIENT`. Every UID is minted under pydicom's registered root,
which the de-identification declaration already covers on the recorded grounds that
nothing there is a person — so the study needs no corpus manifest and makes no
de-identification claim. It is also idempotent: the UIDs are derived, so running it
twice leaves one study, not two.

**It refuses unless `MEDOS_ENV` is exactly `dev`, and unset is not `dev`.** A tool that
fabricates studies has no business running anywhere near patients, and the failure that
actually happens is somebody copying the compose file and setting nothing.

| | |
|---|---|
| Viewer | http://127.0.0.1:3000/ · redirects to `/mos-viewer/` |
| API | http://127.0.0.1:8000 |

**The MedicalOS extension will answer `401` on every job route until a credential exists,
and that is deliberate** — the served files hold no credential and the deployment ships
none. `medos doctor` says so in the row for `MEDOS_API_VIEWER_AUTHORIZATION`.

To issue that credential on a development stack:

```bash
docker compose -f medos/deploy/compose/docker-compose.yml exec medos-api \
  python -m medos.security.cli issue \
    --tenant 00000000-0000-0000-0000-000000000000 \
    --principal-kind service_account --principal-id 11111111-1111-1111-1111-111111111111 \
    --scope job.create --scope job.read \
    --expires-in-days 30 --label "local viewer" --bootstrap
```

It prints the key **once** — only its argon2id hash is stored, so there is no command
that will show it to you again. Put it in your environment and restart the proxy:

```bash
MEDOS_API_VIEWER_AUTHORIZATION="Bearer mos_dev_<key_id>_<secret>"
```

That one key is then the identity behind every action the page takes, which is the cost
you are accepting. `/docs` has an **Authorize** button that takes the same value.

## The whole loop, in four commands

This is the path a third-party developer cares about, and every number below was
measured on a clean stack rather than written from the design:

```bash
docker compose -f medos/deploy/compose/docker-compose.yml up -d
docker compose -f medos/deploy/compose/docker-compose.yml --profile demo up medos-seed-corpus
```

Mint a key as above, then submit a job against the seeded study:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H "Authorization: Bearer $MEDOS_KEY" -H "Content-Type: application/json" \
  -d '{"study_instance_uid":"1.2.826.0.1.3680043.8.498.12013997633573713867934002626655441225",
       "capabilities":["lung_segmentation"]}'
```

```bash
curl -s -H "Authorization: Bearer $MEDOS_KEY" http://127.0.0.1:8000/api/v1/jobs/$JOB_ID
```

**13.7 seconds**, eight steps, all succeeded:

`fetch_series` → `build_volume` → `envelope_check` → `service_invoke` →
`validate_bundle` → `write_dicom` → `store_dicom` → `persist_result`

You get back right lung 1843.14 ml, left 1843.38 ml, total 3686.52 ml as SNOMED-coded
measurements; a DICOM SEG and SR written back into the archive under derived UIDs; and a
provenance block naming the series consumed, the service version, a trace id and an
idempotency key. Resubmitting the same request reuses the existing objects rather than
minting new ones.

The terminal state is **`COMPLETED`** — not `SUCCEEDED`, which is what you will guess,
and what a polling loop written from the guess will wait for forever. `SUCCEEDED` is
real, but it belongs to the six *run* tables (training, evaluation, conversion, seal and
the rest); `jobs` is the one state machine that does not use it, and `job_steps` use a
lowercase `succeeded` again. Register entry 101 records why that is not simply renamed.

## Development defaults, and why they cannot reach production

A development stack has to answer declarations only a real deployment can truly make. Every
one of those answers would be a lie about a production installation, so each carries the
marker `dev-stack-not-for-patients` and is **refused** unless `MEDOS_ENV` is exactly `dev`.

The inversion matters: **unset is not `dev`.** Nobody deploys to a hospital having
deliberately typed `MEDOS_ENV=dev`. What happens is that somebody copies
`docker-compose.yml`, changes the database URL and the image tag, and sets nothing else. If
unset meant dev, that deployment would accept every development default in this repository
and look healthy. Because unset means not-dev, it refuses on the first one and names it.

[`medos/medos/config/devmode.py`](medos/medos/config/devmode.py) is the mechanism and states plainly
what it cannot do: nothing stops an operator who sets `MEDOS_ENV=dev` on a deployment
holding patients. What it stops is the omission, which is the failure that actually occurs.

## Status, honestly

This repository is a **specification being implemented**, and the specification is ahead of
the code in named, recorded places. What works end to end today:

- DICOM in and out through the gateway, canonical volume, SEG and SR writing, provenance
- cohort curation, sealed dataset versions, the evidence plane and its acceptance gates
- a training plane with a separate GPU trainer image that fits real models
- over 1,100 unit and gate tests, zero skips (an exact count here goes stale every commit; `pytest tests/unit tests/gate` prints the real one)

What does not yet work out of the box: `--profile demo` seeds ONE synthetic study, which
is enough to see the pipeline run and nowhere near a cohort — nothing population-shaped
(leakage checks, splits, non-inferiority) has anything to work with; the training-plane
API answers `401` until a credential you supply; and `MEDOS_SEAL_STORE` and `MEDOS_TRAINING_ENVIRONMENT` are undeclared in the
shipped stack. `medos doctor` lists all of it.

`docs/spec/99-known-inconsistencies.md` records 153 places where this repository does not
meet its own specification, each with what it costs and what would fix it. That file is
long on purpose. A medical platform whose known-defect register is empty is a platform
nobody has read.

---

# Appendix: the weeks 1–2 vertical slice

Everything below describes the original slice this repository grew from, and is kept
because it is still an accurate description of the job path.

One study goes in through a button; a DICOM SEG, a DICOM SR and a provenance record come
out the other side, into a real PACS.

```
DICOMweb pull → series selection → canonical volume → in-process inference
   → inverse transform → ResultBundle → SEG + SR → STOW
```

Plus `POST /api/v1/jobs`, `GET /api/v1/jobs/{id}`, a `jobs` table, a
`SELECT … FOR UPDATE SKIP LOCKED` claim loop, an OHIF toolbar button and a provenance
panel.

**This slice is deliberately architecturally wrong, and that is the plan.**
`docs/spec/15-delivery.md` §15.2.3 schedules it that way on purpose: build one honest
end-to-end path first, learn what the geometry and the DICOM writing actually cost, and
only then install the foundations that are expensive to retrofit. `CONTRACT.md` is the
binding statement of what this slice is and is not; it wins over anything below.

---

## What exists

| | |
|---|---|
| `medos/medos/core/` | geometry (`CanonicalVolume`, `SourceGeometry`, the source↔canonical transform), DICOM series scan/select, deterministic UID derivation, masks, measurements, the coded-concept dictionary, `ResultBundle`, the `MedosError` hierarchy with its RFC 9457 mapping |
| `medos/medos/dicomweb/` | `DicomWebClient` (QIDO / WADO / STOW) and `DicomWebGateway`, the one module allowed to hold a PACS credential |
| `medos/medos/capabilities/` | `lung_segmentation` (HU threshold + largest components — **not** a learned model), `emphysema_laa` (percent of lung voxels below −950 HU), `pleural_effusion` (**a declared placeholder**: `present=false`, `not_implemented`) |
| `medos/medos/writer/` | DICOM SEG, DICOM SR (TID 1500), attribute inheritance and `SeriesNumber` allocation |
| `medos/medos/db/` | `schema.sql` — the only DDL — plus the `JobQueue` port and its Postgres driver, and row access for jobs, steps, events, series verdicts and results |
| `medos/medos/worker/` | the claim loop and the eight-step executor |
| `medos/medos/api/` | FastAPI: `POST /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `GET /api/v1/jobs/{id}/events` (SSE), `GET /api/v1/jobs/{id}/series-selection`, `/healthz`, `/readyz` |
| `medos/deploy/compose/` | five pinned services: Postgres, Orthanc, `medos-api`, `medos-worker`, OHIF |
| `medos/web/ohif-extension/` | the “Analyze with MedicalOS” button and the provenance panel — see the honesty note below |
| `tests/` | `unit/` (no containers), `integration/` (Postgres + Orthanc), `e2e/` (the whole compose stack) |

The three capabilities are named honestly. `lung_segmentation` is a threshold and a
connected-components pass, not a network; `emphysema_laa` is arithmetic; `pleural_effusion`
returns `present=false` with a `not_implemented` note and says so in the viewer. A real
effusion model needs clinic data, which is PHI-bearing and out of scope here. Labelling a
threshold as “AI” would be the exact dishonesty the evidence plane exists to prevent.

---

## Running it

**Requirements.** Docker with Compose v2. That is all — the Python runs in containers.
To run the tests you also need Python 3.11 on the host with `pytest`, `requests`,
`psycopg`, `pydicom`, `numpy` and `highdicom`.

```bash
docker compose -f medos/deploy/compose/docker-compose.yml up -d --build
docker compose -f medos/deploy/compose/docker-compose.yml ps
```

| | |
|---|---|
| Viewer | http://127.0.0.1:3000 · redirects to `/mos-viewer/` |
| MedicalOS panel | http://127.0.0.1:3000/medicalos/standalone/ |
| MedicalOS API | http://127.0.0.1:8000 · `/docs` · `/healthz` · `/readyz` |
| Orthanc | **no host port.** The PACS is on an `internal: true` network that only `medos-gateway` joins (MOS-DATA-006); DICOMweb is at `http://127.0.0.1:8043/dicomweb/<tenant>` with a bearer token |
| Postgres | `postgresql://medos:medos@127.0.0.1:5432/medos` |

Every port is bound to `127.0.0.1`. Nothing is exposed off-box.
If another stack on this machine claims the same `3000`, bring it down first or override
`MEDOS_WEB_PORT` / `MEDOS_API_PORT` / `MEDOS_PG_PORT`. There is no
`MEDOS_ORTHANC_PORT` any more and that is deliberate: this stack publishes no PACS port at
all, so `8042` cannot clash and must not be made to.

The schema is applied by Postgres itself: `medos/medos/db/schema.sql` and
`20-medos-migrate.sh` are mounted into `/docker-entrypoint-initdb.d/`, which the image
runs exactly once on an empty data volume — the script then applies all fourteen
migrations and records each one's sha256 in `schema_migrations`. Measured: 56 seconds.
Nobody runs a migration runner by hand.

**Do not reset it with `docker compose down -v`.** That destroys every volume in the
project, and `medicalos-slice_orthanc-db` is 4.8 GB of studies that nothing in Postgres
can regenerate — the stack comes back healthy with an empty archive, which looks exactly
like a bug in the viewer. Destroy the database alone, and remove the container as well as
the volume, because a container created before `medos/` became a product directory still
carries bind mounts to paths that no longer exist and would initialise an EMPTY schema
while reporting healthy:

```bash
docker compose -f medos/deploy/compose/docker-compose.yml rm -s -f postgres
docker volume rm medicalos-slice_postgres-data
docker compose -f medos/deploy/compose/docker-compose.yml up -d
```

The studies stay in the PACS through all of that. `python -m medos.gateway.reconcile
--tenant <uuid>` claims them back into the fresh index without re-uploading a byte;
without it they are invisible (`MOS-DATA-013`) and the archive looks empty. API keys do
NOT survive — only their argon2id hashes were ever stored — so mint a new one.

### Running the demo

**1. Put a study in the PACS.** Any DICOM source works; this is the LCTSC layout
(`<case>/<StudyInstanceUID>/<SeriesInstanceUID>/*.dcm`). The ingest goes through
`DicomWebGateway` — the one module allowed to hold a PACS credential — rather than a bare
`requests.post`, so it exercises the same transport the worker's STOW uses.

```bash
python - <<'PY'
from pathlib import Path
from medos.dicomweb.gateway import DicomWebGateway, GatewayConfig, PacsCredentials

series = Path("F:/WorkSpace/PulmoAI/TCIA/LCTSC-Test-S1-101/<study_uid>/<series_uid>")
files = sorted(series.glob("*.dcm"))
# Through the Gateway, with the scoped token compose gives medos-worker. There is no
# route to the PACS itself and MOS-DATA-006 is why (see the compose file's header).
gw = DicomWebGateway(GatewayConfig(
    base_url="http://127.0.0.1:8043/dicomweb/00000000-0000-0000-0000-000000000000",
    credentials=PacsCredentials(bearer_token="medos-dev-worker-key"),
))
for r in gw.store_files(files, study_instance_uid=series.parent.name):
    print(r.http_status, len(r.referenced), "referenced,", len(r.failed), "failed")
PY
```

`tests/e2e/test_demo.py` does this step itself and picks the largest CT series in the case
by reading headers, so `pytest tests/e2e/test_demo.py` needs no manual ingest at all.

**2. Press the button.**

```
http://127.0.0.1:3000/medicalos/standalone/?study=<StudyInstanceUID>
```

Or do the same thing with `curl` — same endpoint, same job, no second job-creation path:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/jobs \
     -H 'Content-Type: application/json' \
     -d '{"study_instance_uid":"<uid>","capabilities":["lung_segmentation","emphysema_laa"]}'
# -> 202 {"job_id":"job_01M2…","state":"QUEUED"}     (a repeat answers 200, same job_id)

curl -sS  http://127.0.0.1:8000/api/v1/jobs/<job_id> | jq '.state, .provenance'
curl -sSN http://127.0.0.1:8000/api/v1/jobs/<job_id>/events    # SSE, closes when terminal
docker compose -f medos/deploy/compose/docker-compose.yml logs -f medos-worker
```

### Running the tests

```bash
pytest tests/unit                      # no containers
pytest tests/integration               # needs Postgres (+ Orthanc for the slow ones)
pytest tests/e2e/test_demo.py -v       # needs the whole compose stack; SKIPS if it is down

pytest --require-stack                 # CI: an unreachable dependency FAILS instead of skipping
pytest --require-corpus                # nightly: an absent LCTSC/TCIA corpus FAILS too
```

**Two switches, and they are not the same question.** `--require-stack`
(`MEDOS_REQUIRE_STACK=1`) guarantees three things: every dependency the selected tests
declare was **probed** and answered correctly, every test needing one actually ran against
it, and at least one test ran at all. `--require-corpus` (`MEDOS_REQUIRE_CORPUS=1`)
guarantees the same for the private LCTSC/TCIA corpus, and is for the nightly job that
mounts it; absent private data is not a broken system, so `--require-stack` alone never
fails on it. Every run ends with a summary naming what was not tested and why, so that a
bare `17 skipped` can never again pass for a pass. See
[`tests/README.md`](tests/README.md).

Probing, and not merely "do not skip", because converting a skip into a failure is blind
to a dependency no test reaches for. With `medos-gateway` stopped — the worker's only
route to DICOM — the earlier strict mode printed `233 passed`. And a probe that only
checks a status code is blind too: the OHIF origin answers `200 text/html` for every path
that does not exist, so `http://127.0.0.1:3000/dicom-web` looked like a healthy PACS.
Every probe now checks the shape of what came back, and reads readiness (`/readyz`) rather
than liveness (`/healthz`, which deliberately does not touch the database).

The week-0 contract script used to carry its own copy of this census, because it was a
script and not a pytest module: `python spikes/week0/test_contracts.py` printed `32/32
passed, of 32 contract checks in total` and named any group that did not run. It printed
`28/28 passed` and exited 0 whenever the private corpus was absent — which on a CI runner
is always — until it was made to state its expected total. The spike is deleted and its
checks are ordinary pytest modules now, counted by the taxonomy above like everything
else. That is the point: a script CI invokes by path keeps its own books.

`tests/e2e/test_demo.py` automates four of the five release-0.1.0 gate checks from
`docs/spec/15-delivery.md` §15.1.2 — `dicom-battery`, `idempotency-three-surface`,
`provenance-replay` and `rejection-distinct`. The fifth, `tenant-isolation`, is
**inapplicable** here rather than failed: there are no tenants to isolate yet.

Useful environment variables: `MEDOS_E2E_LCTSC_ROOT` (default
`F:/WorkSpace/PulmoAI/TCIA`), `MEDOS_E2E_CASE`, `MEDOS_E2E_API_URL`,
`MEDOS_E2E_WEB_URL`, `MEDOS_E2E_DICOMWEB_URL`, `MEDOS_E2E_DATABASE_URL`.

---

## How the pieces fit

```
   browser ──┐
             │  http://127.0.0.1:3000          (ONE origin: no CORS anywhere)
             ▼
        ┌─────────┐   /             OHIF SPA
        │  nginx  │   /medicalos/   the MedicalOS button + provenance panel
        │ (web)   │   /api/         ──────────────►  medos-api ──┐
        └─────────┘   /dicom-web/   ──────────────►  orthanc     │
                                                        ▲        ▼
                                    medos-worker ───────┘    postgres
                                       claim → 8 steps → STOW      ▲
                                            └───────────────────────┘
```

The nginx in front of OHIF is standing in for `medicalos-gateway`. Chapter 13
`MOS-OPS-005` requires exactly one front door in front of the PACS and gives the viewer no
route to it; there is no gateway in this slice, so nginx proxies and does not authorise.
The browser therefore never talks to Orthanc or to `medos-api` across an origin, which is
both why no CORS policy exists and the shape production has anyway.

### The eight steps

`MOS-EXEC-023` reserves the keys and their phase strings; `medos/medos/worker/steps.py` runs
exactly these, in this order, as ordinary function calls in one process:

`fetch_series` → `build_volume` → `envelope_check` → `service_invoke` →
`validate_bundle` → `write_dicom` → `store_dicom` → `persist_result`

No DAG, no scheduler, no per-step retry, no persisted intermediate. The **attempt** is the
unit that retries, because that is the only unit whose retry semantics chapter 5 defines.

### Three decisions worth knowing before you read the code

**`REJECTED` is not an error.** A study with no eligible series, one outside the
applicability envelope, or one with unsupported geometry terminates `REJECTED` with a
machine-readable reason, and `GET /api/v1/jobs/{id}` answers `200`. `MOS-EXEC-014`: *a
radiologist who sees a red error where the truth is “no thin axial recon exists in this
study” will either chase an IT ticket or, worse, assume the study was cleared.* The
provenance panel renders the two states with four distinct visual channels.

**UIDs are derived, never generated.** Every `SOPInstanceUID` the platform mints is a pure
function of job identity (`MOS-IMG-062`). That is what makes a crash survivable without a
distributed transaction: a re-run lands on the same objects, finds them already in the
archive, and reconciles instead of writing a second SEG a radiologist has to choose
between. The client's `Idempotency-Key` header is stored for echo and **never** enters the
seed (`MOS-API-029`) — a client-settable string in that tuple would let two callers mint
one UID for two different patients.

**The job row and the queue row commit in one transaction.** `CONTRACT.md` §4. It is why
there is no outbox and no dual-write problem in this slice, and it is only available while
both live in the same database — which is why `job_queue` is a table and not a topic.

---

## What is deliberately missing

Not “not done yet”. Scheduled, with a named block. `CONTRACT.md` §0 and
`docs/spec/15-delivery.md` §15.2.3 are the authority.

| Absent | Why now | Arrives |
|---|---|---|
| **auth** | nothing to authenticate against; a client-side permission check would be a stub that enforces nothing (`MOS-REL-050`) | weeks 3–5 (API keys) |
| **tenancy + RLS** | `tenant_id` columns are **absent**, not faked. `CONTRACT.md` §8: “a fake single-tenant value … is harder to migrate than an absent column” | weeks 3–5 |
| ~~**the Gateway as sole PACS door**~~ | **ARRIVED.** This row read "`MOS-OPS-005` is violated: Orthanc is published on the host" and was still saying so after the network policy landed, three rows above a table stating the opposite. Orthanc now has no host port at all: it sits alone with the Gateway on an `internal: true` network, and the test suite reaches DICOM through the Gateway | weeks 3–5 |
| **registries + capability resolution** | `REGISTRY` is a Python dict, not a table. Resolution as a pure function needs artifacts to resolve | 0.3.0 |
| **the broker** | the queue is Postgres. A Kafka `JobQueue` driver is driver 2 against the same port | 0.3.0 |
| **Triton** | three capabilities that are numpy do not need an inference server. The in-process loader survives only as a CPU test fixture | weeks 3–5 |
| **sealed services** | no network policy to seal | 0.3.0 |
| **the evidence plane** | `Dataset`, `EvaluationRun`, `ValidationReport`, gated deployment, applicability envelopes | 0.2.0 |
| **agents, MCP, policy engine** | `MOS-AGENT-004`: no `Workflow`, `Tool` or `Agent` is backed by a table, endpoint or executor before 0.4.0 | 0.4.0 |
| **de-identification** | the LCTSC data is already public and de-identified; a de-identifier tested only against de-identified data is worse than none | weeks 3–5 |
| **object store, dashboards, tracing** | no intermediate artifacts to store; `MOS-REL-016` ships the telemetry helpers here, the dashboards later | weeks 3–5 |
| **`CANCELLED`** | the state and the column exist and **nothing produces them** (`CONTRACT.md` §3) | later |
| **prior studies** | `prior_study_instance_uids` is accepted and refused `422 PRIORS_NOT_SUPPORTED`; inter-study registration does not exist | 0.4.0 |

### And one honest gap in the viewer

The OHIF extension under `medos/web/ohif-extension/src/` — the React toolbar button and panel —
**has not been built into a running OHIF**. `ohif/app:v3.9.2` is a pre-built SPA; OHIF v3
resolves extensions from its own webpack bundle at build time and has no runtime loader, so
loading it means rebuilding the viewer from the OHIF monorepo. That build is not part of
this stack and was not performed.

What **does** run, and is verified: `medos/web/ohif-extension/standalone/`, a page served by the
same nginx on the same origin, which imports the **same** `src/core/*.js` modules the React
extension imports. The job submission, the SSE follow, the `REJECTED`-vs-`FAILED` rendering
and the whole `CONTRACT.md` §10 field set are one implementation in two hosts.
`medos/web/ohif-extension/README.md` states exactly what is verified, what is not, the OHIF build
procedure, and the manual browser steps for the SEG overlay and SR hydration that no
automated test here covers.

---

## What weeks 3–5 adds

`docs/spec/15-delivery.md` §15.2.4 — *“turn the script into a service and install the four
irreversible foundations”*, ending at tag **0.1.0**.

1. **Tenancy and Postgres RLS.** `tenant_id` on every table, row-level security, and the
   rule that the effective tenant comes from the authenticated principal and never from the
   URL (`MOS-DATA-009`). Retrofitting this later means auditing every query ever written;
   doing it now means adding a column and a policy.
2. **API-key auth and an append-only audit log.** `job.create` becomes a real permission
   refused server-side, which is where `MOS-SAFE-089a`'s acceptance check can actually
   measure it.
3. **The Go control plane** (`MOS-REL-084`). It replaces `medos/medos/api/` wholesale — which is
   why that layer is thin, why no business logic lives in a handler, and why every decision
   already sits in `medos/medos/db/` or `medos/medos/worker/`.
4. **The DICOM Gateway as the only PACS door.** Orthanc moves onto an `internal: true`
   network, the viewer is pointed at the gateway, and `MOS-OPS-005` stops being violated.
   `medos/medos/dicomweb/gateway.py` is already the single choke point, so this is a network
   policy change plus de-identification and consumer-class egress rules.
5. **Triton** for native-mode inference, administered by `medicalos-tritond`. The
   in-process loader stops being a runtime and becomes a CPU test fixture.
6. **Study-arrival triage** and the `SeriesSelector` as first-class configuration, so the
   entry point is no longer only a human pressing a button — `MOS-REL-021` makes that a
   gate check because *a demo whose only entry point is a human pressing a button optimises
   the product against the one deployment mode hospitals do not buy*.

The block ends when the 0.1.0 gate is green: `dicom-battery`,
`idempotency-three-surface`, `tenant-isolation`, `provenance-replay` and
`rejection-distinct`. Four of the five are already automated in `tests/e2e/test_demo.py`;
the fifth becomes applicable the moment tenancy exists.

After that, 0.2.0 brings the evidence plane and gated deployment, and 0.3.0 proves the
platform is a platform: a second capability (lung nodule on LIDC-IDRI) added with **zero
core code changes**, asserted by a path allow-list over `git diff --name-only`
(`MOS-REL-020`).

---

## Conventions

Python 3.11, type hints everywhere, `ruff` clean. No global mutable state — the connection
is passed, never imported. Structured JSON logs, and **never a PHI value**: UIDs, counts,
millimetres and codes only. Every module that implements a spec requirement cites the
requirement id in its docstring, and the docstrings carry the *reasons*, which are the part
that would be expensive to rediscover.
