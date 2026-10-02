# SPDX-License-Identifier: Apache-2.0
"""`capability-reachable` -- the check register entry 68 ends by saying does not exist.

NOT A §15.1.2 CHECK. Declared and argued in `tests/unit/test_gate_contract.py`'s
`LOCAL_EXTRA`, which is the only route by which `tests/gate/` may hold a module the
specification does not name. `migration-drift` is the other one and this module is its
sibling twice over -- in provenance (a green gate beside an unmet MUST) and in shape (the
subject is the DEPLOYMENT and nothing else). Read that module first.

WHAT WENT WRONG -- TWICE -- AND WHY NINE GREEN CHECKS SAW NEITHER
-------------------------------------------------------------------
(a) 0.3.0 added `lung_nodule` under `medos/services/` touching no core file. `zero-core-change`
    passed and the measurement was true. The capability was nevertheless UNREACHABLE IN
    PRODUCTION, because four separate places assembled the served set from the PLATFORM
    singleton `medos.capabilities.REGISTRY` rather than from what the deployment is
    configured to serve: admission at `POST /api/v1/jobs`, the `MOS-REG-029` publish
    check, the §6.6 resolution vocabulary, and chapter 9's metadata lookup. Register
    entry 68 records it. `zero-core-change` compares a diff's PATH SET, and a path set
    cannot see reachability.

(b) `medos/deploy/compose/docker-compose.yml` then defaulted `MEDOS_CAPABILITY_PROVIDERS` to
    `lung_nodule` while `medos/deploy/compose/medicalos-config.js` still offered three
    capabilities. `MOS-SAFE-089a` makes the OHIF toolbar button the ONLY job-creation path
    a reader has, so the shipped stack ADMITTED four capabilities and OFFERED three: the
    platform ran a capability no reader could submit. Nothing caught that either -- no
    check in the 0.3.0 row opened the viewer's configuration until this one did.

Both are the same defect wearing two coats: one rule -- "what does this deployment
serve" -- written down in more than one place, and the copies drifting. Entry 68 names the
missing check in terms, and this module is it:

    a capability this deployment serves is admitted, resolved, published against,
    executed, and offered to a reader -- in one deployment, from one configuration.

WHERE EACH PROPERTY IS READ, STATED EXACTLY, BECAUSE THEY ARE NOT ALL READ IN ONE PLACE
-----------------------------------------------------------------------------------------
An earlier revision of this docstring said the subject was "the deployed API, the deployed
worker and the deployed viewer configuration". That was true of one property, aspirational
about a second and false about the other three, and an adversarial pass said so. The
honest account is a table, and the reader is owed it before anything else:

  PROPERTY 0 (the input)   read out of the RUNNING containers with `docker inspect`. Not
                           `docker-compose.yml`: what the compose file says is what the
                           deployment was ASKED to be, and `Config.Env` is what it is.
  PROPERTY 1 ADMITTED      the DEPLOYED API, over HTTP. Nothing in this process answers it.
  PROPERTY 2 RESOLVED      THIS CHECKOUT'S CODE, IN THIS PROCESS. §6.6 resolution is a pure
                           function of a snapshot and has no HTTP surface to drive. See
                           "what an in-process property proves" below.
  PROPERTY 3 PUBLISHED     THIS CHECKOUT'S CODE, IN THIS PROCESS, for the same reason:
                           `MOS-REG-029` is a check inside `registry.publish()`.
  PROPERTY 4 EXECUTED      the DEPLOYED worker, read back out of the DEPLOYED database.
  PROPERTY 5 OFFERED       the DEPLOYED viewer: the bytes `medos-web` serves at
                           `/app-config.js`, fetched from the viewer origin.

WHAT AN IN-PROCESS PROPERTY PROVES, AND WHAT IT DOES NOT
-----------------------------------------------------------
Properties 2 and 3 run the release's own code here, with `MEDOS_CAPABILITY_PROVIDERS` set
to the value read out of the containers and every step downstream of that value left real:
the catalogue lookup, the provider contract, the composition, the vocabulary assembly, the
registry's cross-row rules. That is a real check of a real code path and it is NOT a
substitution -- `tests/_support/capability_source.py`'s `serve_one_extra_capability`
replaces `providers.capability_ids` itself, so nothing downstream of the answer runs, and a
check built on it would re-create the blindness it exists to remove.

It is also NOT a check of the deployment, and this module no longer claims otherwise:

  * it proves THIS TREE'S `medos/medos/resolution/snapshot.py` and
    `medos/medos/registry/repo.py` ask the configured resolver rather than the platform
    singleton, under the configuration the deployment is running. That is the exact shape
    of defect (a) at two of its four sites, and it is caught here -- verified by
    experiment, per site.
  * it proves nothing whatever about the IMAGE. `MOS-REL-001` makes the release the
    images; if `medos-api`'s image and this checkout disagree about
    `medos/services/catalogue.py` then these two properties are answering for the
    checkout. Property 0's image assertions and property 1's `supported_capabilities`
    equality are what narrow that gap, and they narrow it rather than closing it.
  * it is therefore CIRCULAR in one direction and not in the other: the configuration is
    an input read from the deployment, so nobody is asked "do you serve what you serve" --
    but the code that answers is this process's, not the deployment's.

`platform_dsn`'s throwaway database is the other half of property 3: publishing into the
deployment's own registry would mutate the thing under test.

ADMISSION IS A FACT ABOUT THE CAPABILITY, NOT ABOUT ANY STUDY
----------------------------------------------------------------
This is the correction that matters most, and it closes a hole of exactly the class this
module exists to close. Properties 1, 2 and 3 used to reach `primary_study`, which reaches
`tests/gate/conftest.py`'s `_cases()`, which calls `skip_no_data(corpus='lctsc-corpus')`.
`--require-stack` does not turn a DATA skip into a failure -- only `--require-corpus` does
-- and the gate's completeness guard counts COLLECTED items, not executed ones. So on a
machine with no corpus, `pytest -m gate_0_3_0 --require-stack` exited 0 with three of this
module's arms silently skipped, property 1 among them: the one defect (a) tripped first.
A fifth green-hiding-nothing-ran incident, inside the check written against that class.

Whether the deployed API ADMITS a capability id is a fact about the capability. So
properties 1, 2 and 3 now touch no corpus fixture at all, and property 1 submits against
`PHANTOM_STUDY_UID` -- a syntactically valid StudyInstanceUID that no archive holds. The
refusals are discriminated, and which branch this deployment takes was MEASURED rather
than read:

  a `400` naming the CAPABILITY (`UNKNOWN_CAPABILITY` /
      `capability_not_supported`)            -> defect (a). RED, and named.
  `202` (or a `200` replay)                  -> ADMISSION HAPPENED. `create_job` collects
                                                every unknown id into `violations[]` and
                                                returns before `repo.create_job_queued`,
                                                so a request that was not refused
                                                contained no unknown id. Measured: this
                                                deployment answers `202` -- it does not
                                                look at the study at submit, and the job
                                                lands `REJECTED` / `no_eligible_series`
                                                minutes later at `fetch_series`.
  a 4xx NOT naming the capability             -> also admission: the request got past
                                                capability admission and died on the
                                                study. Accepted for the deployment that
                                                refuses an unknown study at submit; this
                                                one does not, and the branch is kept
                                                because that is a deployment property and
                                                not a constant.
  anything else (5xx, an unparseable body)    -> this check MEASURED NOTHING and says so.

The `200`-replay case is admission evidence and not an exception to the rule below: the
unknown-capability branch runs on EVERY request, ahead of the `MOS-EXEC-053` idempotency
lookup, and never sees whether the row already existed.

Only property 4 legitimately needs a real study, and it is the one arm that skips when the
corpus is absent. A `-m gate_0_3_0 --require-stack` run on a machine with no corpus
therefore does NOT prove EXECUTED; the skip taxonomy's `DATA SKIPS` line is where that is
visible, and `--require-corpus` is the switch that makes it red.

A REPLAY IS NOT EVIDENCE, AND PROPERTY 4 REFUSES TO PASS ON ONE
-----------------------------------------------------------------
`MOS-EXEC-053` derives the idempotency key from the study, the capability set and the
service version. All three are constant across gate runs, and `tests/gate/conftest.py`'s
`clean_slate` is deliberately inert for a `-m gate_0_3_0` run -- the deployment's job
tables are the 0.1.0 row's subject and this row must not truncate them. So every run after
the first got an HTTP `200` REPLAY of a job executed minutes or days earlier, and the
previous revision of this module computed `Submitted.replay`, printed it, and asserted
nothing on it.

That is not a smaller claim, it is no claim. Measured: with `medos-worker` STOPPED the
check passed. With `deployment_registry` replaced inside the worker container by the
historic buggy version -- the one returning only the platform three -- the check reported
six passes and printed "registry-confirmed for" all four capabilities, while
`docker logs --since 5m medos-worker | grep -c job.claimed` returned `0`. A replay re-reads
an execution performed by whatever worker image was running THEN, which is precisely the
state the historic defect was invisible in.

So property 4 ASSERTS that its submissions were fresh, and a replay is a FAILURE carrying
the remedy: the job ids, the deployment's own derived key for each, and the one command
that clears them. Accepting a replay whose rows happen to look right is not an option --
the worker image may have changed since, and "the rows look right" is exactly what the
defect looked like.

There is no way to make the key legitimately fresh without paying with something this
check is not allowed to spend. `derive_idempotency_key`'s material is the tenant, the
service id and version, the study, the (empty) series set, the (refused) priors, the
requested outputs and the capability set. The outputs are a three-element space and
varying them would change the step plan, i.e. change what is proved. The capability set is
the thing under test. The study is the only real lever, and minting a fresh one per run
means STOWing the same pixels under a new UID on every gate run, which grows the archive
without bound. What IS done is the one thing that costs nothing:

    THIS CHECK SUBMITS AGAINST A STUDY OF ITS OWN (`reachability_study`, LCTSC case index
    1), not against `primary_study` (index 0).

That does not make a second run fresh and does not pretend to. What it buys is that this
row's job rows and the 0.1.0 row's are DISJOINT. One of the groups `_runnable_groups` packs
IS the 0.1.0 row's own capability set, so on `primary_study` the two rows would share an
idempotency key: a combined `-m "gate_0_1_0 or gate_0_3_0"` run would see this check replay
the 0.1.0 row's freshly completed job and go red for something that is not a reachability
defect, and the remedy would be to delete the 0.1.0 row's evidence. With a study of its own
neither happens, and clearing this check's jobs never destroys the other row's subject.

Re-running `-m gate_0_3_0` twice against one deployment without clearing IS red, by
design. A release gate is run deliberately before a tag; demanding that the worker actually
execute is proportionate, and the failure says exactly what to run.

THE FIVE PROPERTIES
----------------------
  1. ADMITTED    `POST /api/v1/jobs` naming each group answers `202` (or a `200` replay, or
                 a study-shaped 4xx), and the deployed API's own `supported_capabilities`
                 -- read off its refusal of a capability no image ships -- is exactly the
                 served set. This is the one defect (a) tripped first. No study data.
  2. RESOLVED    the §6.6 vocabulary a snapshot load produces under this configuration
                 answers something other than `capability_unknown` for each served
                 capability. IN THIS PROCESS; see above.
  3. PUBLISHED   a `ServiceVersion` manifest claiming the capability passes `MOS-REG-029`,
                 in a throwaway registry, WITH A CONTROL: one claiming a capability this
                 deployment does not serve must still be refused. IN THIS PROCESS.
  4. EXECUTED    the deployed worker reaches a terminal state for a FRESH job, and the
                 state is not "this worker does not know that capability".
  5. OFFERED     `window.MEDICALOS.capabilities` in the app config the `medos-web`
                 container SERVES is that same set. The parser is IMPORTED from
                 `tests/integration/test_viewer_capability_list.py` rather than written
                 again -- this project has six recorded instances of one rule living in
                 two copies that drift, and a seventh inside the check against that very
                 defect would be its own punchline -- and pointed at the fetched bytes
                 rather than at the file in this working tree.

WHAT PROPERTY 2 NO LONGER ASSERTS, AND WHY THE GAP IS BETTER THAN THE ASSERTION
---------------------------------------------------------------------------------
This property used to have a first half: read each submitted job's `job.requested` event
out of the deployed database and require that it names every served capability. It could
not fail. `medos/medos/db/repo.py::create_job_queued` writes that payload's `capability_ids`
from `spec.capability_ids` verbatim -- the request this harness sent -- so once the POSTs
are asserted admitted, `recorded >= served.ids` is true by construction. A tautology
dressed as a check is worse than an acknowledged gap: it occupies the place where the real
check would go, and it reports green from a mechanism that cannot report anything else.

The real check has nowhere to read from in this build, and that is `MOS-REG-004` being
unmet rather than an oversight here. `medos/medos/resolution/pin.py` states it at length:
chapter 12 §12.10's `jobs.resolution_snapshot` and its `jobs_resolution_pinned` trigger do
not exist in `schema.sql` and no migration adds them, so the deployment has no independent
statement of what it resolved for a job -- only a copy of what it was asked for. The day
that migration lands, the assertion to write here is that the `resolution` payload key
names the served set, and it will be a real one. Until then this module says so instead of
pretending.

AS FEW JOBS AS THE PLATFORM'S OWN RULES ALLOW, AND NOT ONE JOB
-----------------------------------------------------------------
Property 4 submits real work to the deployment, which no other check in this row does, so
the first question is whether ONE job carrying every served capability proves what N jobs
prove. It does not, and the reason is not cost -- it is that two platform rules make some
capability sets unsubmittable, and a job that violates either fails for a reason that has
nothing to do with reachability:

  R1  DEPENDENCY CLOSURE. `medos.worker.steps.resolve_capability_order` refuses a job that
      requests a capability whose `depends_on` was not also requested -- and it refuses it
      with the SAME `capability_resolution_failed` code an UNKNOWN capability produces.
      `emphysema_laa` declares `depends_on = ("lung_segmentation",)`, so a per-capability
      loop would report it unreachable on a perfectly healthy deployment, which is the
      worst thing a release gate can do.
  R2  ONE LABEL MAP. `medos.writer.identity.label_map_owners` raises
      `multiple_label_maps_in_bundle` when two capabilities in one job return a LabelMap,
      because `MOS-IMG-066` needs a total ordering across the combined segment set and
      CONTRACT.md section 5 does not define how two compose. So a single job carrying
      every served capability FAILS on any deployment serving two segmenting capabilities
      -- measured on this one, which serves `lung_segmentation` and `lung_nodule`.

`_runnable_groups` therefore packs the served set into the FEWEST job-sized groups that
satisfy both rules, from DECLARED data and not from a list in this file: `depends_on` for
R1 and `CapabilityMetadata.output_kinds` (`MOS-SAFE-014`/`MOS-SAFE-015`) for R2. A
deployment serving the platform three plus `lung_nodule` yields two groups.

What a group proves is not weaker than what per-capability jobs would prove, and in one
respect it is stronger: `resolve_capability_order` raises on the FIRST id its registry
does not hold, so a group that got past it proves every id in that group was in the
deployed worker's registry, together, in one pass.

PROPERTY 4'S DISCRIMINATION, PRECISELY
-----------------------------------------
`step_envelope_check` calls `resolve_capability_order` as its first statement and wraps
its `KeyError` in `SystemFailure("capability_resolution_failed")`; `failure_class_for`
maps that to `("internal", "capability_resolution_failed")` on the `jobs` row. Because
every group this module submits is dependency-closed by construction, R1 is out of the
way and that code has exactly one remaining meaning. So:

  FAILED + `failure_code = capability_resolution_failed`  -> THE DEFECT, named.
  a terminal state + an `envelope_check` step row that is `succeeded` or `failed` for any
      OTHER reason                                        -> the order resolved, so every
                                                             id in that group was in the
                                                             deployed worker's registry.
  terminal WITHOUT `envelope_check` having concluded      -> the worker stopped at
                                                             `fetch_series` or
                                                             `build_volume`; this check
                                                             proves NOTHING and says so
                                                             rather than passing.

`COMPLETED` and `REJECTED` therefore both count as "the worker ran it", exactly as they
should: a clinical rejection is an answer (`MOS-EXEC-014`), and the study being unsuitable
is not the capability being unreachable. A COMPLETED group is held to more: one `results`
row per capability in it, which is per-capability evidence rather than group evidence.

COST, MEASURED ON THE REFERENCE STACK
----------------------------------------
Everything except property 4 is under two seconds of this process's time: eight
`docker inspect` calls, three refused or accepted submissions against a study that does not
exist, one static fetch from the viewer origin, one snapshot load and five registry
publishes. Property 4 costs one ingest of its own LCTSC case (~5 s, a re-STOW onto itself
after the first run) plus a real execution of each group -- the whole eight-step plan,
inference included, on a ~130-instance series. Measured on this stack: 6 passed in 61 s,
and `pytest -m gate_0_3_0 --require-stack` as a whole in about 145 s. The execution is paid
on EVERY gate run now, because a replay is refused.

ONE COST IS PAID IN THE DEPLOYMENT AND IS WORTH STATING. Property 1's group submissions are
accepted (`202`), so they become real jobs that the deployed worker claims and rejects at
`fetch_series` a second or two later -- `no_eligible_series` for a study no archive holds,
which is `MOS-EXEC-014` working correctly. They are two rows per deployment, replayed on
every later run, and they carry no PHI because the study they name does not exist. The
alternative -- proving admission without submitting -- is the `supported_capabilities`
reading, which is here too and is not sufficient on its own: it shows what the API says it
admits, not that it admits this exact set in one request.

MOS-REL-009 RESPONSE WHEN THIS IS RED
----------------------------------------
Make the deployment serve what it says it serves -- do not cut the capability. The failure
names which of the five properties broke and for which capability id, and each has one
response: extend `MEDOS_CAPABILITY_PROVIDERS` (or the image's `medos/services/catalogue.py`) so
the API admits it; fix the caller that assembled a served set from
`medos.capabilities.REGISTRY` instead of `medos.capabilities.providers`; add the id to
`medos/deploy/compose/medicalos-config.js`'s `capabilities` array and restart
`medos-web`. This is a Tier A failure of the DEPLOYMENT rather than of the release's
contents, so the `MOS-REL-009` slip rule's cut list does not apply: a capability that is
configured, executed and unreachable is worse than one that was never enabled, because the
operator believes it runs.

The one red that is NOT a product defect is the replay refusal in property 4, and it has
its own remedy printed in the failure: clear the job tables and run the gate again.

WHAT THIS CHECK DOES NOT COVER, INCLUDING THREE THINGS IT MEASURED AND DOES NOT GATE
---------------------------------------------------------------------------------------
It does not open a browser: property 5 fetches the app config the viewer origin serves and
parses it, which is the deployed VIEWER CONFIGURATION, and stops there. Whether the
extension renders the array is `tests/e2e/test_viewer_extension.py`'s claim. It also does
not assert that a capability produces a CLINICALLY correct result; `dicom-battery`,
`per-case-metrics` and `non-inferiority` are those checks.

Register entry 68's FOURTH assembly of the served set is not gated here and the reason is
that nothing reaches it. `medos/medos/capabilities/metadata_for` is called from exactly one
place, `medos/medos/training/runs.py::_output_kind_of`, which swallows the `KeyError`. A check
that drove it would be driving a code path whose only caller cannot observe the difference.
It is recorded here rather than gated, and the honest statement is that this module covers
three of entry 68's four assemblies plus defect (b).

It gates the OFFER and not the SUBMISSION SHAPE the surface makes for that offer, and R1
and R2 above say exactly where the difference bites on this deployment. Both were observed
while building this module and both are REPORTED here rather than silently gated, for the
reason `medos/medos/resolution/pin.py` gives about `MOS-REG-004`: a check that goes red for a
question the specification leaves open is a check that gets bypassed.

  * `medos/web/ohif-extension/src/core/client.js::targetFor` submits `{kind: "capability", id}`
    with the reader's single pick and no dependency closure, so a reader who picks
    `emphysema_laa` alone gets a job that FAILS on R1. The response is a surface change
    (submit the closure, or do not offer a dependent capability alone), not a
    configuration edit, so it is not this check's red.
  * `medicalos-config.js`'s `defaultCapability: null` makes the toolbar button submit the
    `service_version` target, which `medos/medos/api/routes_jobs.py` expands to
    `sorted(known_capability_ids())` -- every served capability in one job, which is
    `MOS-SAFE-089a` acceptance check 24's "one job, every capability it implements, one
    POST". On this deployment that job fails on R2, and R2 is CONTRACT.md section 5
    declining to define a composition rather than a defect in a capability.

DEPENDENCIES, ALL DECLARED IN `tests/_support/stack.py`
----------------------------------------------------------
`tests/gate/test_capability_reachable.py` has its own row in `SUITE_DEPENDENCIES`, because
it needs two things the rest of the 0.3.0 row does not: the `docker` CLI, which every
`docker inspect` below shells out to, and `ohif`, which property 5 now fetches from. Both
are PROBED before a single test is allowed to pass, so a missing docker CLI or a stopped
viewer is a pre-run abort naming the dependency rather than a fixture error in the middle
of a gate. The 0.3.0 row as a whole declares no `ohif`, correctly: no other check in it
opens the viewer.

Needs docker, the deployment's API, worker, viewer and Postgres, and -- for property 4
only -- the LCTSC corpus.

Spec: MOS-REL-001, MOS-REL-004, MOS-REL-009, MOS-REL-012, MOS-REL-020, MOS-REL-108,
MOS-CONF-109, MOS-API-043, MOS-API-047, MOS-REG-004, MOS-REG-029, MOS-REG-051,
MOS-REG-069, MOS-EXEC-013, MOS-EXEC-014, MOS-EXEC-053, MOS-IMG-066, MOS-SAFE-014,
MOS-SAFE-089a, MOS-UI-017; register entry 68.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from medos.capabilities import providers
from medos.capabilities.base import Capability
from medos.registry import repo as registry_repo
from medos.registry.digest import content_digest_of
from medos.registry.errors import UnresolvedReference
from medos.resolution import snapshot as snapshot_module
from medos.resolution.resolve import resolve

from tests._support import stack as stack_support
from tests._support.skips import skip_infra
from tests.gate import _platform as P
from tests.gate.conftest import JOB_TABLES, Api, Ingested, why

# The viewer list's ONE parser (register entry 68's sixth instance is what happens when a
# rule gets a second one). Imported as the MODULE and not as the function, because
# property 5 has to point it at the bytes the CONTAINER serves rather than at the file in
# this working tree, and `VIEWER_CONFIG` is where that file is named. Nothing of the
# integration suite's world comes with the import -- no fixture, no stack dependency --
# which is what makes it different from the coupling `tests/gate/_platform.py` refuses at
# length.
from tests.integration import test_viewer_capability_list as viewer_list

pytestmark = pytest.mark.gate_0_3_0

#: The two processes that must answer the same question about the served set. One admits
#: at submit, the other executes; `medos/medos/capabilities/providers.py` exists so that they
#: cannot disagree, and this check reads that from the deployment rather than trusting it.
API_CONTAINER = "medos-api"
WORKER_CONTAINER = "medos-worker"

#: The third container this module reads, and the only one it does not drive with a
#: request of the platform's own: property 5 fetches one static asset from its origin.
VIEWER_CONTAINER = "medos-web"

#: Where `medos-web` serves the app config from, and the path `docker-compose.yml`
#: bind-mounts `medos/deploy/compose/medicalos-config.js` onto. Reported by property 5 so that a
#: reader of the output can see WHICH file produced the list; the list itself is read from
#: the origin, so a deployment that bakes the config into its image is read correctly too.
VIEWER_CONFIG_PATH = "/usr/share/nginx/html/app-config.js"

#: container -> the `tests/_support/stack.py` probe key it stands for, which is also the
#: key `tests/_support/skips.py`'s `START_HINTS` is written against. Without this a skip
#: naming `medos-web` would print the generic "docker compose up -d --build" hint instead
#: of the viewer's, and a message that names a dependency without saying how to get it is
#: the message people learn to ignore.
DEPENDENCY_OF: dict[str, str] = {
    API_CONTAINER: "medos-api",
    WORKER_CONTAINER: "medos-worker",
    VIEWER_CONTAINER: "web",
}

#: A capability id no image can ship, used twice as a CONTROL. Every assertion about a
#: served capability is a positive, and a positive from an instrument that is not running
#: is indistinguishable from a pass: if `POST /api/v1/jobs` admitted this, its
#: `supported_capabilities` would be decoration, and if `MOS-REG-029` published a service
#: claiming it, property 3 would be measuring nothing. `medos/services/catalogue.py`'s selector
#: shape admits this spelling, so the refusal is the vocabulary's and not a syntax check's.
UNSERVABLE_CAPABILITY = "no_such_capability_gate_control"

#: A syntactically valid StudyInstanceUID that resolves to nothing, and property 1's whole
#: point: admission is a fact about the CAPABILITY, so the study must not be one this
#: check has to arrange. Matches `routes_jobs.DICOM_UID_RE` (digits and dots, <= 64) so the
#: request reaches the capability vocabulary rather than dying in schema validation; `.68`
#: is register entry 68, and nothing STOWs it, so no archive can ever hold it.
PHANTOM_STUDY_UID = "1.2.826.0.1.3680043.8.498.68686868686868686868686868686868"

#: `medos/medos/worker/steps.py::step_envelope_check` -- the step whose FIRST statement resolves
#: the capability order, and therefore the step whose having concluded is the evidence
#: that the deployed worker's registry held every id in the group.
ORDER_STEP = "envelope_check"

#: `failure_class_for` maps `SystemFailure("capability_resolution_failed")` onto this
#: `jobs.failure_code`. With R1 satisfied by construction it has exactly one meaning left:
#: the worker does not know a capability the deployment serves.
UNKNOWN_CAPABILITY_FAILURE = "capability_resolution_failed"

#: `medos/medos/writer/identity.py::label_map_owners`. Reported separately because it means the
#: opposite of the code above -- the capabilities all ran and their OUTPUTS would not
#: compose -- and because a group this module packs should never provoke it (R2).
LABEL_MAP_FAILURE = "multiple_label_maps_in_bundle"

#: `CapabilityMetadata.output_kinds`' member that means "this capability returns a
#: LabelMap", which is R2's input. Declared data (`MOS-SAFE-014`), not a list here.
SEGMENTING_OUTPUT = "SEG"

#: Every way the platform spells "the problem is the CAPABILITY" on a refusal.
#: `routes_jobs.create_job` answers `UNKNOWN_CAPABILITY`; `capability_not_supported` is the
#: reason code the same condition carries elsewhere in the tree. Property 1 discriminates
#: on these and on nothing else, because a refusal that names the STUDY proves the opposite
#: thing -- that the request got PAST capability admission.
CAPABILITY_REFUSAL_CODES = frozenset({"UNKNOWN_CAPABILITY", "CAPABILITY_NOT_SUPPORTED"})
CAPABILITY_REFUSAL_REASONS = frozenset({"capability_not_supported", "capability_unknown"})


# =====================================================================================
# The deployment's own configuration -- the INPUT
# =====================================================================================
@dataclass(frozen=True)
class Served:
    """What this deployment is configured to serve, resolved from its own environment."""

    #: The raw `MEDOS_CAPABILITY_PROVIDERS` value both containers carry. `None` if the
    #: variable is absent altogether, which is a different statement from empty.
    spec: str | None
    #: The provider selector names that value names, in composition order.
    providers: tuple[str, ...]
    #: The capability ids those providers compose onto the platform registry.
    ids: tuple[str, ...]
    #: The composed registry, for the DECLARED data R1 and R2 are computed from.
    registry: Mapping[str, Capability]
    #: The capability sets property 4 submits as jobs. See `_runnable_groups`.
    groups: tuple[tuple[str, ...], ...]
    #: The image each capability-resolving container runs, by id.
    image: str

    @property
    def id_set(self) -> frozenset[str]:
        return frozenset(self.ids)


def _docker() -> str:
    path = shutil.which("docker")
    if path is None:  # pragma: no cover - the stack preflight aborts first
        skip_infra(
            "the docker CLI is not on PATH, so this deployment's own "
            "MEDOS_CAPABILITY_PROVIDERS cannot be read. The compose file's default is "
            "NOT a substitute: what a deployment was asked to be and what it is are the "
            "two things this check exists to compare. This module declares `docker` in "
            "`tests/_support/stack.py`, so under --require-stack the preflight probe "
            "should already have aborted the run by name -- reaching here means the CLI "
            "disappeared between collection and this call.",
            dependency="docker",
        )
    return path


def _inspect(container: str, template: str) -> str:
    proc = subprocess.run(
        [_docker(), "inspect", container, "--format", template],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        skip_infra(
            f"`docker inspect {container}` failed: "
            f"{((proc.stderr or proc.stdout) or '').strip()[:300]}",
            dependency=DEPENDENCY_OF.get(container, container),
        )
    return proc.stdout


def _container_env(container: str) -> dict[str, str]:
    """One container's environment, as the deployment created it.

    `docker inspect` and not `docker exec`: reading a variable must not run code inside
    the thing under test, and `Config.Env` is the value both the API process and the
    worker process were started with.
    """
    out: dict[str, str] = {}
    for line in _inspect(container, "{{range .Config.Env}}{{println .}}{{end}}").splitlines():
        name, sep, value = line.partition("=")
        if sep:
            out[name.strip()] = value
    return out


def _closure(registry: Mapping[str, Capability], capability_id: str) -> frozenset[str]:
    """`capability_id` plus everything it transitively `depends_on`. R1's unit.

    The declaration is the capability's own (CONTRACT.md section 7: "express that as an
    explicit step ordering ... not as a hidden import"), which is what lets this be
    computed rather than listed. A dependency the deployment does NOT serve is left in the
    set deliberately: the job then names it, the API refuses it, and property 1 reports a
    served capability whose dependency is unreachable -- which is a reachability defect
    and not something to quietly drop.
    """
    out: set[str] = set()
    frontier = [capability_id]
    while frontier:
        cid = frontier.pop()
        if cid in out:
            continue
        out.add(cid)
        capability = registry.get(cid)
        frontier.extend(getattr(capability, "depends_on", ()) if capability else ())
    return frozenset(out)


def _segmenting(registry: Mapping[str, Capability], capability_id: str) -> bool:
    """Does this capability DECLARE that it returns a segmentation? R2's input."""
    capability = registry.get(capability_id)
    kinds = getattr(getattr(capability, "metadata", None), "output_kinds", ())
    return SEGMENTING_OUTPUT in tuple(kinds)


def _runnable_groups(
    registry: Mapping[str, Capability], ids: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    """The fewest job-sized capability sets that cover `ids` and break neither R1 nor R2.

    First-fit over the capabilities in sorted order, each contributing its dependency
    closure, merging into an existing group whenever the merge would still declare at most
    one segmenting capability. Deterministic, so two gate runs submit the same jobs --
    which property 4 now treats as a failure rather than as a replay, and property 1 does
    not care about at all.

    It is deliberately NOT optimal bin packing: the objective is a small, explainable
    cover, and an operator reading a failure needs to know which job carried which
    capability more than they need the theoretical minimum.
    """
    groups: list[set[str]] = []
    for capability_id in sorted(ids):
        want = set(_closure(registry, capability_id))
        for group in groups:
            merged = group | want
            if sum(1 for cid in merged if _segmenting(registry, cid)) <= 1:
                group |= want
                break
        else:
            groups.append(want)
    return tuple(tuple(sorted(group)) for group in groups)


@pytest.fixture(scope="session")
def served() -> Served:
    """The served set, resolved from the value the RUNNING containers hold.

    The resolution itself runs here, in this process, through
    `medos.capabilities.providers.resolve()` with the selector names passed explicitly --
    the same function, the same `medos/services/catalogue.py` lookup and the same composition
    the two containers perform. This is the check's INPUT and it is allowed to be
    computed; what may not be computed in-process is any of the five OUTPUTS below.
    """
    api_env = _container_env(API_CONTAINER)
    worker_env = _container_env(WORKER_CONTAINER)
    spec = api_env.get(providers.ENV_PROVIDERS)
    if spec != worker_env.get(providers.ENV_PROVIDERS):
        # Not an assertion, because every later fixture and test would then be running
        # against a question with two answers. The first test below reports it.
        spec = None

    names = providers.configured_providers(spec or "")
    try:
        resolved = providers.resolve(providers=names)
    except providers.CapabilityProviderError as exc:
        raise AssertionError(
            f"this deployment is configured with {providers.ENV_PROVIDERS}={spec!r} and "
            f"THIS RELEASE cannot resolve it: {exc}\n"
            f"  The containers resolved it at startup or they would not be serving, so "
            f"the image and this tree disagree about what `medos/services/catalogue.py` holds. "
            f"MOS-REL-001 makes the release the images; a release whose own code cannot "
            f"name the deployment's capabilities cannot describe what it deployed."
        ) from exc
    ids = tuple(sorted(resolved.capability_ids))
    return Served(
        spec=spec,
        providers=names,
        ids=ids,
        registry=resolved.registry,
        groups=_runnable_groups(resolved.registry, ids),
        image=_inspect(API_CONTAINER, "{{.Image}}").strip(),
    )


@pytest.fixture()
def configured_like_the_deployment(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """This process, resolving capabilities from the DEPLOYMENT's configuration value.

    For the two properties that have no running surface to drive: `MOS-REG-029` is a check
    inside `registry.publish()` and §6.6 resolution is a pure function of a snapshot. Both
    reach the served set through `medos.capabilities.providers.capability_ids()`, which
    reads the environment at call time.

    THIS IS NOT A SUBSTITUTION, AND IT IS ALSO NOT THE DEPLOYMENT. The distinction from
    `serve_one_extra_capability` is exact: that helper replaces `capability_ids` itself, so
    nothing downstream of the answer runs. This sets the one INPUT the deployment sets, to
    the value the deployment sets it to, and then lets the catalogue lookup, the provider
    contract, the composition, the vocabulary assembly and the registry's cross-row rules
    all run for real. What it cannot do is answer for the IMAGE: the code that executes is
    this checkout's. The module docstring's "what an in-process property proves" section
    is the full statement, and properties 2 and 3 are the two that carry it.

    The memo is cleared on both sides: `resolve()` keys its cache on the configuration, so
    a stale entry cannot answer for a different one, but a check that moves the process's
    environment must not leave a resolution behind it either.
    """
    if served.spec is None:
        monkeypatch.delenv(providers.ENV_PROVIDERS, raising=False)
    else:
        monkeypatch.setenv(providers.ENV_PROVIDERS, served.spec)
    providers.clear_cache()
    try:
        yield
    finally:
        providers.clear_cache()


def _missing(served: Served, present: frozenset[str] | set[str]) -> list[str]:
    return sorted(served.id_set - frozenset(present))


# =====================================================================================
# 0. THE INPUT. One configuration, read from the deployment, held by both processes.
# =====================================================================================
def test_capability_reachable_the_deployment_declares_one_served_set_to_both_processes(
    served: Served,
) -> None:
    """`medos-api` and `medos-worker` are configured identically and run one image.

    This is the premise the other five tests are about, and it is a claim about the
    deployment rather than about the code. `medos/medos/capabilities/providers.py` exists so
    that admission and execution cannot disagree; it guarantees that only for two
    processes reading the SAME value out of the SAME image. `docker-compose.yml` writes
    the value once as a YAML anchor and merges it into both `environment:` blocks, which
    is what makes that true in the file -- and a file is not a deployment.

    A disagreement here is `MOS-REL-020`'s claim failing at the deployment: the API would
    admit a job the worker cannot run (or refuse one it could), and every later assertion
    in this module would be asking a question with two answers.

    The image equality is also what limits how far properties 2 and 3 reach. They run this
    checkout's code; a deployment whose containers run an image built from a different
    tree would make their verdicts answers about the tree. This assertion does not close
    that gap -- two containers can share one image that is not this tree -- but property
    1's `supported_capabilities` equality does narrow it, because that list is rendered by
    the image.

    The last two assertions are the packer's contract, checked rather than assumed: the
    groups property 4 submits must COVER the served set -- a capability in no group is a
    capability this check would silently not execute -- and each must satisfy R1 and R2,
    or the job it becomes fails for a reason that is not about reachability.
    """
    api_spec = _container_env(API_CONTAINER).get(providers.ENV_PROVIDERS)
    worker_spec = _container_env(WORKER_CONTAINER).get(providers.ENV_PROVIDERS)
    assert api_spec == worker_spec, (
        f"the two capability-resolving processes of this deployment are configured "
        f"differently:\n"
        f"  {API_CONTAINER}    {providers.ENV_PROVIDERS}={api_spec!r}\n"
        f"  {WORKER_CONTAINER} {providers.ENV_PROVIDERS}={worker_spec!r}\n"
        f"The API admits at submit and the worker executes; a capability in one set and "
        f"not the other is a job accepted and then failed with "
        f"`{UNKNOWN_CAPABILITY_FAILURE}` for a study that was never the problem."
    )

    api_image = _inspect(API_CONTAINER, "{{.Image}}").strip()
    worker_image = _inspect(WORKER_CONTAINER, "{{.Image}}").strip()
    assert api_image == worker_image, (
        f"{API_CONTAINER} runs image {api_image} and {WORKER_CONTAINER} runs "
        f"{worker_image}. The provider NAMES resolve against `medos/services/catalogue.py`, "
        f"which is fixed when the image is built (MOS-REL-108, MOS-CONF-109), so two "
        f"images can compose two different registries from one identical configuration."
    )

    assert served.ids, (
        f"this deployment resolves {providers.ENV_PROVIDERS}={served.spec!r} to an empty "
        f"capability set, so there is nothing for a reader to run at all"
    )
    covered = {cid for group in served.groups for cid in group}
    assert not _missing(served, covered), (
        f"the runnable grouping does not cover {_missing(served, covered)}; property 4 "
        f"would never submit them and would pass without executing them"
    )
    for group in served.groups:
        unclosed = sorted(
            dep
            for cid in group
            for dep in _closure(served.registry, cid)
            if dep not in group
        )
        assert not unclosed, (
            f"group {list(group)} does not contain {unclosed}, which it depends on (R1). "
            f"`resolve_capability_order` would refuse the job with "
            f"`{UNKNOWN_CAPABILITY_FAILURE}` -- the same code an unknown capability "
            f"produces -- and this check would report a healthy deployment as broken."
        )
        segmenting = [cid for cid in group if _segmenting(served.registry, cid)]
        assert len(segmenting) <= 1, (
            f"group {list(group)} declares {segmenting} as segmenting capabilities "
            f"(MOS-SAFE-014 `output_kinds`), and `label_map_owners` refuses two in one "
            f"bundle (R2, MOS-IMG-066)"
        )
    print(
        f"[capability-reachable] {providers.ENV_PROVIDERS}={served.spec!r} -> providers "
        f"{list(served.providers)} -> capabilities {list(served.ids)} "
        f"(image {served.image[:19]}); jobs: {[list(g) for g in served.groups]}"
    )


# =====================================================================================
# 1. ADMITTED -- POST /api/v1/jobs, against the DEPLOYED API, with NO study data
# =====================================================================================
def _problem_of(response: requests.Response) -> dict[str, Any]:
    """The problem document, or `{}` when the body is not one. Never raises."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _names_a_capability(problem: Mapping[str, Any]) -> bool:
    """Does this refusal say the CAPABILITY is the problem?

    The whole of property 1's discrimination. A refusal that names the capability is
    defect (a); a refusal that names anything else happened AFTER capability admission and
    is therefore evidence that admission succeeded. Three places carry the verdict --
    `code`, `reason_code`, and each `violations[]` entry's own `code` and `pointer` -- and
    all three are read, because `MOS-API-008` puts the per-member detail in `violations[]`
    and a future refusal could carry a generic top-level code above a specific violation.
    """
    if str(problem.get("code") or "") in CAPABILITY_REFUSAL_CODES:
        return True
    if str(problem.get("reason_code") or "") in CAPABILITY_REFUSAL_REASONS:
        return True
    for violation in problem.get("violations") or ():
        if not isinstance(violation, Mapping):
            continue
        if str(violation.get("code") or "") in CAPABILITY_REFUSAL_CODES:
            return True
        pointer = str(violation.get("pointer") or "")
        if pointer.startswith("/capabilities") or pointer == "/target/id":
            return True
    return False


def test_capability_reachable_every_served_capability_is_admitted_at_submit(
    api: Api, served: Served
) -> None:
    """`MOS-API-043` admission, against the deployed API, with no corpus in sight.

    TWO READINGS, AND THE SECOND IS THE CONTROL. The first submits each runnable group
    against `PHANTOM_STUDY_UID` and requires that the refusal, if any, does not name a
    capability: `medos/medos/api/routes_jobs.py` collects EVERY unknown id into `violations[]`
    and refuses the whole request, so a request that was not refused for a capability
    reason contained no unknown id. What that cannot show is what the API would say about
    a capability it does not serve -- so a second request names one no image ships and
    reads `supported_capabilities` off the refusal. That list is the deployed API's own
    `known_capability_ids()`, rendered by the process that admits jobs, and it must be
    exactly the set this deployment's configuration produces.

    NO STUDY, ON PURPOSE. Admission is a fact about the capability. The previous revision
    reached `primary_study` here, which reaches `skip_no_data(corpus=...)`, which
    `--require-stack` does not fail -- so this arm, the one defect (a) tripped first, was
    skipped and counted as collected on every corpus-less run. The module docstring works
    that through. `create_job` never reads the study at submit (measured: `202` for a UID
    no archive holds), and the unknown-capability branch returns before
    `repo.create_job_queued` is reached, so the control submission creates no job at all.

    A `200` REPLAY IS ADMISSION EVIDENCE AND IS ACCEPTED HERE, unlike in property 4. The
    ordering is the reason and it is not a concession: the capability check runs on every
    request, ahead of the `MOS-EXEC-053` idempotency lookup, and never sees whether the row
    already existed. What a replay cannot prove is EXECUTION, which is property 4's claim
    and where it is refused.
    """
    probe = api.post_job(PHANTOM_STUDY_UID, capabilities=[UNSERVABLE_CAPABILITY])
    assert probe.status_code == 400, (
        f"the deployed API answered {probe.status_code} for a capability id no image "
        f"ships. `supported_capabilities` is this check's control, and an API that "
        f"admits anything makes every admission assertion here vacuous.\n"
        f"  {probe.text[:400]}"
    )
    problem = _problem_of(probe)
    assert problem.get("code") == "UNKNOWN_CAPABILITY", problem
    admitted = set(problem.get("supported_capabilities") or ())
    assert admitted == served.id_set, (
        f"the DEPLOYED API admits a different set than this deployment's configuration "
        f"produces.\n"
        f"  configured ({providers.ENV_PROVIDERS}={served.spec!r}): {list(served.ids)}\n"
        f"  admitted by {API_CONTAINER}:                            {sorted(admitted)}\n"
        f"  configured but NOT admitted: {_missing(served, admitted)} -- defect (a) of "
        f"register entry 68: the deployment runs it and the API refuses it at submit.\n"
        f"  admitted but NOT configured: {sorted(admitted - served.id_set)} -- the "
        f"running image resolves a set this release's code does not."
    )

    verdicts: list[str] = []
    for group in served.groups:
        response = api.post_job(PHANTOM_STUDY_UID, capabilities=list(group))
        refusal = _problem_of(response)
        assert not _names_a_capability(refusal), (
            f"THE DEFECT, at admission. `POST /api/v1/jobs` naming {list(group)} was "
            f"refused with HTTP {response.status_code} for a CAPABILITY reason, and this "
            f"deployment is configured to serve every id in that group.\n"
            f"  configured ({providers.ENV_PROVIDERS}={served.spec!r}): "
            f"{list(served.ids)}\n"
            f"  {response.text[:600]}\n"
            f"  The study is {PHANTOM_STUDY_UID}, which no archive holds, so nothing here "
            f"is about data. The group also names every capability its members "
            f"`depends_on` (R1), so a served capability whose DEPENDENCY this deployment "
            f"does not serve is refused here too -- and that is a reachability defect in "
            f"the configuration, not a harness error: nothing can run that capability."
        )
        assert response.status_code in (200, 202) or 400 <= response.status_code < 500, (
            f"`POST /api/v1/jobs` naming {list(group)} answered "
            f"{response.status_code}, which is neither an acceptance nor a refusal this "
            f"check can read. Admission is UNPROVEN for that group -- this arm measured "
            f"nothing, which it says rather than passing.\n"
            f"  {response.text[:600]}"
        )
        if response.status_code in (200, 202):
            replayed = response.headers.get("MedicalOS-Idempotent-Replay") == "true"
            verdict = (
                f"HTTP {response.status_code} "
                f"({'MOS-EXEC-053 replay' if replayed else 'accepted'}); admitted"
            )
        else:
            verdict = (
                f"HTTP {response.status_code} {refusal.get('code') or '(no code)'}; "
                f"refused on the STUDY, so admission happened"
            )
        verdicts.append(f"{list(group)} -> {verdict}")

    probed = {cid for group in served.groups for cid in group}
    assert not _missing(served, probed), (
        f"the submissions above do not between them name {_missing(served, probed)}, so "
        f"admission is proven for a smaller set than this deployment serves. The grouping "
        f"is `_runnable_groups`' and its cover is asserted by the first test in this "
        f"module; reaching this means a group was skipped between the two."
    )
    for line in verdicts:
        print(f"[capability-reachable] admitted: {line}")


# =====================================================================================
# 2. RESOLVED -- the section 6.6 vocabulary. IN THIS PROCESS; see the module docstring.
# =====================================================================================
def test_capability_reachable_section_6_6_resolves_every_served_capability(
    served: Served, platform_db: Any, configured_like_the_deployment: None
) -> None:
    """The §6.6 vocabulary, assembled under this deployment's configuration.

    THIS RUNS IN THIS PROCESS, AGAINST THIS CHECKOUT. §6.6 resolution is a pure function of
    a snapshot; there is no HTTP surface to drive and no queue to enqueue on. What is
    proved is that THIS TREE's `medos/medos/resolution/snapshot.py` builds its vocabulary from
    the configured resolver rather than from the platform singleton, for the configuration
    the containers are running. What is NOT proved is anything about the image the
    deployment runs; the module docstring states that boundary in full and does not blur
    it. This is still the site where defect (a) actually lived, and it is verified caught
    here by experiment.

    `_capability_vocabulary` assembles §6.6's vocabulary from chapter 7's
    acceptance-criteria rows UNION the served set, and it read the platform singleton. A
    deployment-configured capability with no criteria row -- the ordinary case for a
    freshly enabled one -- was therefore absent, `resolve()` answered `capability_unknown`
    (`MOS-REG-069`), and §6.6 resolution is pinned into the job at creation and replayed on
    every retry, so that wrong answer is not transient.

    The snapshot is loaded from `platform_dsn`'s THROWAWAY database, empty of artifacts:
    `capability_unknown` is a statement about the VOCABULARY and needs no registry content
    to be wrong, and loading the deployment's own registry would read the thing under
    test. `ZERO_CANDIDATES` for another reason is the correct answer for an empty registry
    and is what this asserts against.

    THE DELETED HALF. This test used to open with a read of each submitted job's
    `job.requested` event out of the deployed database, asserting that it named every
    served capability. `medos/medos/db/repo.py::create_job_queued` writes that payload from the
    submitted request verbatim, so the assertion could not fail once the POSTs were
    admitted. It is gone rather than weakened, and the gap is stated: `MOS-REG-004`'s
    §6.7.5 pin -- the deployment's own independent statement of what it resolved -- has
    no home in this build's schema. `medos/medos/resolution/pin.py` records why, names the
    columns chapter 12 §12.10 defines and this `schema.sql` does not create, and names the
    one function that will read them when the migration lands.
    """
    snapshot = snapshot_module.load_snapshot(
        platform_db, tenant_id=P.TENANT, environment="production", as_of=P.AS_OF
    )
    vocabulary = set(snapshot.capabilities)
    assert not _missing(served, vocabulary), (
        f"section 6.6's resolution vocabulary, assembled under this deployment's own "
        f"configuration, does not contain {_missing(served, vocabulary)}.\n"
        f"  configured: {list(served.ids)}\n"
        f"  vocabulary: {sorted(vocabulary)}\n"
        f"This is register entry 68's third assembly of the served set: a capability "
        f"absent from the vocabulary resolves `capability_unknown` and the answer is "
        f"pinned into the job (MOS-REG-066, MOS-REG-067)."
    )
    unknown = sorted(
        cid
        for cid in served.ids
        if resolve(cid, P.context(), snapshot).reason_code == "capability_unknown"
    )
    assert not unknown, (
        f"section 6.6 resolution answers `capability_unknown` (MOS-REG-069) for "
        f"{unknown}, which this deployment serves.\n"
        f"A ZERO_CANDIDATES outcome for an empty registry "
        f"is expected and is not this; `capability_unknown` says the resolver has never "
        f"heard of the capability the worker is about to run."
    )
    print(
        f"[capability-reachable] section 6.6 vocabulary (in-process, configured from "
        f"{API_CONTAINER}): {sorted(vocabulary)}"
    )


# =====================================================================================
# 3. PUBLISHED -- MOS-REG-029, in a throwaway registry, with a control. IN THIS PROCESS.
# =====================================================================================
def _service_manifest_claiming(capability_id: str, model_ref: str) -> dict[str, Any]:
    """`tests/gate/_platform.py`'s ServiceVersion manifest, re-aimed at one capability.

    Built from that helper rather than written again, so a schema change reaches this
    check the way it reaches the other three 0.3.0 registry checks. One family per
    capability because `MOS-REG-019` makes a republish of the same `(family, version)`
    with different content a `409`, and this publishes several in a row -- with the
    underscores of a capability id turned into hyphens, because chapter 6's `family`
    pattern admits `[a-z0-9-]` segments and a capability id is `[a-z0-9_]`.
    """
    family = "gate-reachable." + capability_id.replace("_", "-")
    manifest = P.service_manifest("1.0.0", model_ref, family=family)
    manifest["spec"]["capabilities"] = [
        {"id": capability_id, "outputs": ["segmentation", "measurement"]}
    ]
    manifest["content_digest"] = content_digest_of(
        {k: v for k, v in manifest.items() if k != "content_digest"}
    )
    return manifest


def test_capability_reachable_a_service_version_claiming_each_capability_can_be_published(
    served: Served, platform_db: Any, configured_like_the_deployment: None
) -> None:
    """`MOS-REG-029`: "a claimed capability MUST exist", where exists = this deployment
    serves it.

    LIKE PROPERTY 2, THIS RUNS IN THIS PROCESS. `MOS-REG-029` is a branch inside
    `registry.publish()`; there is no deployed surface that publishes a ServiceVersion. It
    proves that THIS TREE's `medos/medos/registry/repo.py` asks the configured resolver, under
    the configuration the containers hold -- verified caught, per site, by experiment --
    and it proves nothing about the image.

    The defect this closes is the one entry 68 records as the second surviving assembly:
    `medos/medos/registry/repo.py` read the platform singleton, so a `ServiceVersion` could not
    be published for a capability the deployment was already EXECUTING -- the capability
    produced Results that could never be given the registry row they are supposed to name.

    Into `platform_dsn`'s throwaway database, never the deployment's registry: a check
    that mutated the registry it is checking would be the last thing anyone trusts, and
    `MOS-REG-019`'s republish rule would make the second gate run behave differently from
    the first.

    THE CONTROL IS HALF THE CHECK. Every assertion above it is "the publish succeeded",
    which is also what a registry with the `MOS-REG-029` branch deleted would report. So
    one more manifest claims a capability no image ships and MUST be refused, naming it.
    """
    model = P.publish(platform_db, P.model_manifest(P.FAMILY, "1.0.0"), "mv_reachable_1_0_0")

    refused: dict[str, str] = {}
    for capability_id in served.ids:
        try:
            registry_repo.publish(
                platform_db,
                manifest=_service_manifest_claiming(capability_id, "mv_reachable_1_0_0"),
                public_id=f"sv_reachable_{capability_id}",
                actor=P.SERVICE_ACTOR,
                trace_id=P.TRACE,
                **P.SUPPLY_CHAIN,
            )
            platform_db.commit()
        except UnresolvedReference as exc:
            platform_db.rollback()
            refused[capability_id] = str(exc)
    assert not refused, (
        "the registry refuses a ServiceVersion for a capability this deployment SERVES "
        "(MOS-REG-029):\n  "
        + "\n  ".join(f"{cid}: {msg}" for cid, msg in sorted(refused.items()))
        + f"\n  configured: {list(served.ids)}\n"
        "  The capability executes and produces Results, and no registry row may claim "
        "it. That is register entry 68's second surviving assembly of the served set."
    )
    assert str(model["public_id"]) == "mv_reachable_1_0_0"

    with pytest.raises(UnresolvedReference) as refusal:
        registry_repo.publish(
            platform_db,
            manifest=_service_manifest_claiming(
                UNSERVABLE_CAPABILITY, "mv_reachable_1_0_0"
            ),
            public_id="sv_reachable_control",
            actor=P.SERVICE_ACTOR,
            trace_id=P.TRACE,
            **P.SUPPLY_CHAIN,
        )
    platform_db.rollback()
    assert UNSERVABLE_CAPABILITY in str(refusal.value), (
        f"the MOS-REG-029 control did not name the offending capability: "
        f"{refusal.value}. Every publish above it passed, and a check whose control "
        f"cannot fail is not measuring the rule."
    )
    print(f"[capability-reachable] published a ServiceVersion for each of {list(served.ids)}")


# =====================================================================================
# 5. OFFERED -- the configuration the DEPLOYED VIEWER serves, not the one in this tree
# =====================================================================================
def _viewer_config_source() -> str:
    """Which file, if any, `medos-web` bind-mounts onto `VIEWER_CONFIG_PATH`.

    Reported, never asserted on. The list is read from the ORIGIN below, so a deployment
    that bakes its config into the image is read correctly and reports "(baked into the
    image)" here. Its value is diagnostic: when the offered set is wrong, the first
    question an operator has is which file to edit.

    This call is also what makes an ABSENT viewer container a failure rather than a silent
    pass -- `_inspect` routes a missing container through `skip_infra(dependency="web")`,
    which `--require-stack` turns into a red naming the container and the command that
    starts it.
    """
    mounts = _inspect(
        VIEWER_CONTAINER, "{{range .Mounts}}{{.Destination}}={{.Source}}{{println}}{{end}}"
    )
    for line in mounts.splitlines():
        destination, sep, source = line.partition("=")
        if sep and destination.strip() == VIEWER_CONFIG_PATH:
            return source.strip()
    return "(baked into the image; no bind mount at " + VIEWER_CONFIG_PATH + ")"


def _deployed_viewer_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """`window.MEDICALOS.capabilities`, out of the bytes the VIEWER ORIGIN serves.

    THE POINT OF THE INDIRECTION. The previous revision called
    `test_viewer_capability_list._viewer_capabilities()`, which reads
    `<repo>/medos/deploy/compose/medicalos-config.js` -- this working tree, not the
    deployment. This module forbids exactly that inference for the API and the worker
    ("`Config.Env` is what it is") and had no business making it for the viewer, where the
    bytes a reader's browser loads are the only ones that decide what the toolbar offers.
    A checkout ahead of the running container, or a container started with a different
    mount, is the state defect (b) lived in.

    So the bytes come from `GET <ohif origin>/app-config.js`, through the container's own
    nginx, and the ONE parser is pointed at them by rebinding its `VIEWER_CONFIG` module
    constant for the duration of the test. Rebinding rather than re-implementing: a second
    parser would be register entry 68's seventh copy of one rule, inside the check written
    against copies of one rule.

    The Content-Type check is `tests/_support/stack.py`'s javascript rule and it is not
    belt-and-braces: the OHIF nginx answers `200 text/html` for every path that does not
    exist, so a status code alone would let this parse an SPA fallback page, find no
    `capabilities:` array, and fail with the wrong diagnosis.
    """
    url = f"{stack_support.web_url()}/app-config.js"
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        skip_infra(
            f"the deployed viewer's configuration could not be fetched: GET {url} -> "
            f"{type(exc).__name__}: {exc}. Property 5 reads what a READER's browser would "
            f"load; this tree's medos/deploy/compose/medicalos-config.js is not a "
            f"substitute for it.",
            dependency="web",
        )
    if response.status_code >= 400:
        skip_infra(
            f"GET {url} -> HTTP {response.status_code}: the {VIEWER_CONTAINER} container "
            f"is up but does not serve its app configuration, so what this deployment "
            f"offers a reader cannot be read.",
            dependency="web",
        )
    ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if "javascript" not in ctype and "ecmascript" not in ctype:
        skip_infra(
            f"GET {url} -> HTTP {response.status_code} {ctype or '(no content-type)'}: "
            f"that is not the app config. An nginx SPA fallback answers 200 text/html for "
            f"every unknown path, so this origin is answering without serving the file.",
            dependency="web",
        )

    served_config = tmp_path / "app-config.js"
    served_config.write_text(response.text, encoding="utf-8")
    monkeypatch.setattr(viewer_list, "VIEWER_CONFIG", served_config)
    return set(viewer_list._viewer_capabilities())


def test_capability_reachable_the_deployed_viewer_offers_every_served_capability(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MOS-SAFE-089a` / `MOS-UI-017`: the toolbar button is the reader's only way in.

    Defect (b), exactly: the compose file defaulted `MEDOS_CAPABILITY_PROVIDERS` to
    `lung_nodule` while this array still held CONTRACT.md section 7's three, so the
    deployment served four capabilities and no reader could submit the fourth. A
    capability that is admitted, resolved, published against and executed is still
    unreachable if nothing puts it in front of a reader.

    BOTH SIDES OF THIS EQUALITY ARE NOW READ FROM THE DEPLOYMENT: the served set out of
    `Config.Env`, the offered set out of the bytes `medos-web` serves. That is the whole
    difference from `tests/integration/test_viewer_capability_list.py`, whose parser this
    borrows: that test compares the file in the tree with the compose file's DEFAULT value,
    which is what a `docker compose up` with no `.env` would serve, and it is the right
    claim for a unit of source. This one compares what the containers are RUNNING with
    what the viewer is SERVING, which is the only pair a reader of THIS deployment meets.
    The two coincide whenever nobody has overridden the variable and nobody has restarted
    the viewer onto a different config, and on the day somebody has, only one of them is
    about the deployment.

    Equality in both directions. An offered-but-not-served capability is the mirror
    defect: the reader presses the button and the API answers `UNKNOWN_CAPABILITY`.
    """
    source = _viewer_config_source()
    offered = _deployed_viewer_capabilities(tmp_path, monkeypatch)
    assert offered == served.id_set, (
        "the viewer this deployment serves and the capability set it runs are not the "
        "same set.\n"
        f"  offered by {VIEWER_CONTAINER} at {stack_support.web_url()}/app-config.js: "
        f"{sorted(offered)}\n"
        f"  served by {API_CONTAINER}/{WORKER_CONTAINER} "
        f"({providers.ENV_PROVIDERS}={served.spec!r}): {list(served.ids)}\n"
        f"  the container reads that configuration from: {source}\n"
        f"  served but NOT offered: {_missing(served, offered)} -- the deployment runs "
        "it and no reader can reach it (MOS-SAFE-089a: the button is the only "
        "job-creation path a reader has). This is defect (b) of register entry 68.\n"
        f"  offered but NOT served: {sorted(offered - served.id_set)} -- the reader "
        "submits it and the API refuses with UNKNOWN_CAPABILITY.\n"
        "  Edit the file named above and restart the container: the viewer serves it "
        "from a bind mount, so a change in this working tree is not deployed until it "
        "does."
    )
    print(
        f"[capability-reachable] offered by {VIEWER_CONTAINER}: {sorted(offered)} "
        f"(from {source})"
    )


# =====================================================================================
# 4. EXECUTED -- last, because it waits for the deployed worker
# =====================================================================================
@dataclass(frozen=True)
class Submitted:
    """One job, and the group of capabilities it carries."""

    group: tuple[str, ...]
    job_id: str
    status_code: int
    replay: bool


@pytest.fixture(scope="module")
def submitted(
    api: Api, reachability_study: Ingested, served: Served
) -> tuple[Submitted, ...]:
    """One job per runnable group, against this check's OWN study. See the docstring.

    `reachability_study` and not `primary_study`: one of the groups below is the 0.1.0
    row's own capability set, and `MOS-EXEC-053` keys on the study plus the capability set,
    so sharing a study would make this check's submission a replay of the 0.1.0 row's job
    in any combined run -- and the remedy for that replay would be deleting the other
    row's evidence.

    This fixture only SUBMITS. The freshness verdict is asserted in the test below, so
    that a replay is a readable failure of property 4 rather than an error in a fixture.
    """
    out: list[Submitted] = []
    for group in served.groups:
        response = api.post_job(
            reachability_study.study_instance_uid, capabilities=list(group)
        )
        assert response.status_code in (200, 202), (
            f"POST /api/v1/jobs naming {list(group)} answered "
            f"{response.status_code}, not 202.\n"
            f"  {response.text[:600]}\n"
            f"  Admission is property 1's claim and it passed against a study that does "
            f"not exist, so a refusal HERE is about {reachability_study.study_instance_uid}"
            f" -- the study this check ingested for itself."
        )
        body = response.json()
        replay = response.headers.get("MedicalOS-Idempotent-Replay") == "true"
        print(
            f"[capability-reachable] job {body['job_id']} carries {list(group)}: "
            f"HTTP {response.status_code} "
            f"({'MOS-EXEC-053 replay of an earlier run' if replay else 'created now'})"
        )
        out.append(Submitted(group, body["job_id"], response.status_code, replay))
    return tuple(out)


def _terminal_evidence(db: Any, job: Submitted) -> tuple[Any, Any, set[str]]:
    """`(jobs row, envelope_check step row, capability ids with a `results` row)`."""
    row = db.execute(
        """
        SELECT state, failure_class, failure_code, failure_detail,
               reject_reason_code, reject_reason_detail, idempotency_key,
               created_at, finished_at
          FROM jobs WHERE public_id = %s
        """,
        (job.job_id,),
    ).fetchone()
    step = db.execute(
        """
        SELECT s.status, s.error_code, s.error_detail, s.detail
          FROM job_steps s JOIN jobs j ON j.id = s.job_id
         WHERE j.public_id = %s AND s.step_key = %s
        """,
        (job.job_id, ORDER_STEP),
    ).fetchone()
    produced = {
        str(r["capability_id"])
        for r in db.execute(
            """
            SELECT r.capability_id FROM results r JOIN jobs j ON j.id = r.job_id
             WHERE j.public_id = %s
            """,
            (job.job_id,),
        ).fetchall()
    }
    return row, step, produced


def _replay_refusal(replayed: list[tuple[Submitted, Any]], study_uid: str) -> str:
    """The message a replay fails with. The remedy is the whole of it.

    A failure that names a rule and not a command is a failure people route around, and
    this one is legitimately going to fire on any second gate run against one deployment.
    So it names the jobs, the deployment's OWN derived key for each (read from
    `jobs.idempotency_key`, not recomputed here -- the point is what the platform did),
    and the one statement that clears them.

    `TRUNCATE` and not `DELETE`, and that is the platform being right rather than the
    harness being blunt: `job_events` carries `job_events_no_delete` (`MOS-EXEC-013`), so
    `DELETE FROM jobs WHERE public_id = ...` raises on the cascade -- measured. TRUNCATE
    fires no row-level trigger, which is the same escape `clean_slate` uses and the reason
    a release-0.1.0 gate run is the other way to get here.
    """
    lines = [
        "PROPERTY 4 HAS NO EVIDENCE FROM THIS DEPLOYMENT.",
        "",
        f"{len(replayed)} of this check's submissions came back as a MOS-EXEC-053 REPLAY "
        f"(HTTP 200) of a job the",
        "platform ran earlier, so the deployed worker did not run anything during this "
        "gate run:",
        "",
    ]
    for job, row in replayed:
        lines.append(
            f"  {job.job_id}  {list(job.group)}\n"
            f"      idempotency_key {(row or {}).get('idempotency_key')}  "
            f"state {(row or {}).get('state')}  "
            f"finished {(row or {}).get('finished_at')}"
        )
    lines += [
        "",
        "  A replay re-reads an execution performed by whatever worker image was running "
        "THEN. That is",
        "  exactly the state the historic defect was invisible in: with `medos-worker` "
        "stopped, and with",
        "  `deployment_registry` replaced inside the worker container by the version that "
        "returned only the",
        "  platform three, this check passed and printed 'registry-confirmed' for all "
        "four capabilities while",
        "  `docker logs --since 5m medos-worker | grep -c job.claimed` returned 0.",
        "",
        "  MOS-EXEC-053 derives the key from the study, the capability set and the "
        "service version. All three",
        "  are fixed for this check, so the only way to a fresh execution is to remove "
        "the job rows.",
        f"      study: {study_uid}",
        "  A targeted DELETE is refused -- `job_events_no_delete` (MOS-EXEC-013) raises "
        "on the cascade --",
        "  so clear the tables:",
        "",
        f"      psql \"$MEDOS_TEST_DATABASE_URL\" -c 'TRUNCATE {JOB_TABLES} CASCADE'",
        "",
        "  or run `pytest -m gate_0_1_0 --require-stack` first, whose `clean_slate` does "
        "exactly that. The",
        "  DICOM archive is untouched either way and this check re-STOWs its own study in "
        "a few seconds.",
        "",
        "  This is the expected result of running `-m gate_0_3_0` twice against one "
        "deployment without",
        "  clearing, and it is not a product defect. It is also not something to work "
        "around by accepting a",
        "  replay whose rows look right: the worker image may have changed since they "
        "were written.",
    ]
    return "\n".join(lines)


def test_capability_reachable_the_deployed_worker_freshly_executed_each_group(
    api: Api,
    served: Served,
    submitted: tuple[Submitted, ...],
    reachability_study: Ingested,
    db: Any,
) -> None:
    """The deployed worker ran the jobs IN THIS RUN, and did not stop at "I do not know
    that one".

    FRESHNESS FIRST, AND IT IS AN ASSERTION RATHER THAN A PRINTED NOTE. A `200` replay
    proves that a job with this identity once reached a terminal state; it proves nothing
    about the worker that is deployed now, and this check's entire subject is the worker
    that is deployed now. `_replay_refusal` carries the remedy. The check is done before
    the wait, so a replay fails in a second rather than after a poll loop.

    The discrimination that follows is set out in the module docstring and is made against
    three sets of rows of the deployment's own database rather than against the HTTP body:
    `jobs` (`failure_class`, `failure_code`, `reject_reason_code`), the `job_steps` row for
    `envelope_check`, and the `results` rows. The step row is the load-bearing one:
    `step_envelope_check` calls `resolve_capability_order` as its first statement, that
    function raises on the FIRST id its registry does not hold, and each job requested a
    whole dependency-closed group -- so a concluded `envelope_check` that did not fail
    with `capability_resolution_failed` is proof, for every id in that group at once, that
    the DEPLOYED worker's registry held it.

    A `REJECTED` job counts and that is not leniency: `MOS-EXEC-014` makes a clinical
    rejection an answer, and this check's subject is reachability, not whether one LCTSC
    series happens to be inside a capability's applicability envelope. What does NOT count
    is a job that never reached `envelope_check` -- then the worker stopped at
    `fetch_series` or `build_volume` and this check has measured nothing, which it says
    instead of passing.
    """
    replayed = [(job, _terminal_evidence(db, job)[0]) for job in submitted if job.replay]
    assert not replayed, _replay_refusal(
        replayed, reachability_study.study_instance_uid
    )

    known_to_worker: set[str] = set()
    ran_and_recorded: set[str] = set()
    lines: list[str] = []

    for job in submitted:
        terminal = api.wait_for_terminal(job.job_id)
        row, step, produced = _terminal_evidence(db, job)
        assert row is not None, f"job {job.job_id} is not in the deployed database"

        assert row["failure_code"] != UNKNOWN_CAPABILITY_FAILURE, (
            f"THE DEFECT. The deployed worker FAILED job {job.job_id} with "
            f"`{UNKNOWN_CAPABILITY_FAILURE}` for {list(job.group)}, which this "
            f"deployment is configured to serve.\n"
            f"  configured ({providers.ENV_PROVIDERS}={served.spec!r}): "
            f"{list(served.ids)}\n"
            f"  worker said: {row['failure_detail']}\n"
            f"  That group is dependency-closed by construction (R1), so this code has "
            f"one meaning left: `WorkerDeps.registry` in the deployed worker does not "
            f"hold a capability the deployed API admitted. One resolver read by both "
            f"processes exists to make exactly that impossible (MOS-REL-020)."
        )
        assert row["failure_code"] != LABEL_MAP_FAILURE, (
            f"job {job.job_id} carried {list(job.group)} and the worker refused to "
            f"compose their outputs ({LABEL_MAP_FAILURE}, MOS-IMG-066).\n"
            f"  This module packs groups so that at most one capability per job DECLARES "
            f"`{SEGMENTING_OUTPUT}` in its `output_kinds` (R2), so a capability in this "
            f"group returned a LabelMap it does not declare -- which is MOS-SAFE-014's "
            f"declaration being false, not a reachability defect."
        )
        assert step is not None and step["status"] in ("succeeded", "failed"), (
            f"job {job.job_id} terminated {row['state']} without concluding the "
            f"`{ORDER_STEP}` step (status "
            f"{step['status'] if step is not None else 'no row'}), so the deployed worker "
            f"never resolved a capability order for {list(job.group)} and this check has "
            f"proved NOTHING about their reachability.\n"
            f"  {why(terminal)}\n"
            f"  Fix that cause first: the worker stopped in `fetch_series` or "
            f"`build_volume`, upstream of every capability. The job was created in this "
            f"run rather than replayed -- the assertion above guarantees that -- so this "
            f"is a live failure of the deployed worker and not a wedged old row."
        )
        known_to_worker |= set(job.group)

        if row["state"] == "COMPLETED":
            assert not set(job.group) - produced, (
                f"job {job.job_id} COMPLETED and wrote no `results` row for "
                f"{sorted(set(job.group) - produced)}.\n"
                f"  requested: {list(job.group)}\n"
                f"  results:   {sorted(produced)}\n"
                f"  `jobs.capability_ids` and the `results` rows are supposed to be the "
                f"same set; a capability that runs and records nothing is unreachable to "
                f"every reader of the provenance panel."
            )
            ran_and_recorded |= produced
        else:
            assert row["state"] == "REJECTED", why(terminal)
            assert row["reject_reason_code"], (
                "a REJECTED job with no reason code (MOS-EXEC-016 / the "
                "`jobs_rejected_has_reason` constraint)"
            )
        lines.append(
            f"{job.job_id} {list(job.group)} -> {row['state']}"
            f"({row['reject_reason_code'] or row['failure_code'] or 'no fault'}), "
            f"{ORDER_STEP} {step['status']}, executed in this run, "
            f"results {sorted(produced)}"
        )

    assert not _missing(served, known_to_worker), (
        f"the deployed worker was never asked about {_missing(served, known_to_worker)}: "
        f"no submitted job carried them, so property 4 is unproven for them"
    )
    for line in lines:
        print(f"[capability-reachable] worker: {line}")
    print(
        f"[capability-reachable] recorded a result for "
        f"{sorted(ran_and_recorded)}; registry-confirmed by a FRESH execution for "
        f"{sorted(known_to_worker)}"
    )
