# SPDX-License-Identifier: Apache-2.0
"""The package `MOS-IMG-003` requires, which this repository had built inside `medos/medos/`.

WHAT THE SPECIFICATION ASKS FOR
--------------------------------
> **MOS-IMG-003** — The platform MUST publish two versioned Python packages that implement
> this chapter exactly once: `medicalos-imaging` … and `medicalos-preprocessing`
> (PreprocessingSpec executor, §4.3). A second implementation of either contract anywhere
> in the repository … is a defect.

and `MOS-TRAIN-034` names `medos.sdk.build_chain(spec)` as the ONLY function
in the MedicalOS codebase that may instantiate a MONAI transform, quoting the file header
`# medos.sdk/chain.py`.

Neither had been built. The modules existed, correct and tested, inside `medos/medos/` — where
the only thing that could import them was something that could import the platform. So the
trainer imported the platform to reach a spec format, and `medos/medos/training/bundle.py`
referred to "a single generator in `medicalos-preprocessing`" as though the package were
already there.

WHAT IS IN HERE, AND WHY EACH ONE
----------------------------------
    spec         the PreprocessingSpec document shape (MOS-IMG-049)
    preprocess   build_chain / apply_chain / record_golden (MOS-TRAIN-034). `RESAMPLER_ID`
                 writes this module's own identity into `spec.backend.resampler` and
                 `assert_backend` checks it back, so the FORMAT NAMES THE IMPLEMENTATION:
                 it cannot be reimplemented without a spec version bump. That is a codec
                 reference implementation, not merely a shared helper.
    chain        spec -> configs/inference.json (MOS-TRAIN-131)
    bundle       the MONAI bundle layout (MOS-TRAIN-129/130)
    autoconfig   nnU-Net plans.json -> spec field paths (MOS-TRAIN-223)
    fixtures     the phantom and its pinned hashes -- how two implementations are compared
    canonical    RFC 8785 canonical JSON + sha256. THE digest rule, and `MOS-REL-032`
                 says there is exactly one canonicaliser and every digest goes through
                 it. It is here rather than in the platform's `core/` because both sides
                 digest against it, and a rule that both sides need, held by one of them,
                 is a rule the other one eventually copies.
    errors       the training refusal vocabulary (was `medos/training/errors.py`)
    refusal      the refusal base types (was `medos/evidence/errors.py`; renamed because
                 a flat package cannot hold two modules called `errors`)
    contract     the RUN-DIRECTORY EXCHANGE: what the platform writes into a run
                 directory and what the trainer reads out of it -- CONTRACT_VERSION,
                 RUN_DIRECTORY, CohortEntry, RunRequest, RunDirectory. The only module
                 here that is a contract between two PROGRAMS rather than a format. It
                 lived in the trainer's tree while the platform kept a hand-mirrored half
                 in `orchestrator.py` and a test held the two in step; two halves that a
                 test keeps in step is a contract with no home.

THE PATHS IN THE THREE LINES ABOVE ARE THE PATHS AS THEY WERE WHEN THESE MODULES MOVED.
They read `medos/training/errors.py`, not `medos/medos/...`, because `medos/` did not
become a product directory until afterwards. A mechanical rewrite had changed them, which
turned an account of where something came from into an address that never existed -- the
same defect this repository caught twice in its release records the same day.

NO SHIMS WERE LEFT BEHIND at the old paths, and that was a decision rather than an
oversight. A shim that aliases `sys.modules` gives one module two names and depends on
import-system ordering; a shim built from `import *` silently drops every private name.
Both are the kind of cleverness that this repository's gates exist to catch in other
people's code. The importers were rewritten instead — 73 files, mechanically, in one pass.

WHAT THIS PACKAGE MUST NEVER ACQUIRE: psycopg, `medos.db`, `medos.safety`, FastAPI,
`requests`, torch. It is installed into both the platform image and the trainer image, so
a dependency added here is a dependency added to both.
`tests/unit/test_shared_package_is_pure.py` asserts it.
"""

from __future__ import annotations

#: Deliberately empty, and deliberately not a re-export surface. `medos/medos/training/`'s own
#: `__init__` carries the same empty tuple for the same reason: importing a leaf module
#: must not drag its siblings in, because `medicalos-verify` needs `canonical` alone and
#: `MOS-EVID-124` forbids a socket anywhere in that import closure.
__all__: tuple[str, ...] = ()
