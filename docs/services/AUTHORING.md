<!-- SPDX-License-Identifier: Apache-2.0 -->
# Authoring a MedicalOS service

> MedicalOS integrates, governs and evidences medical AI services. It does not diagnose,
> does not replace a PACS, and is not itself a medical device.

This document is for a third party shipping a capability on MedicalOS without forking it.

**`MOS-SAFE-003`: you are the manufacturer.** MedicalOS is an integrator and a
record-keeper. The clinical claim, the regulatory clearance and the clinical evidence for
anything your capability outputs are yours, declared in `intended_use`,
`regulatory_status` and a signed `ValidationReport`. The platform makes no claim of its
own and is forbidden from doing so (`MOS-SAFE-002`).

---

## The shape of the thing, and why it is not a plugin

**MedicalOS has no plugin loader, and will not grow one.** `MOS-REL-108` forbids dynamic
module import in a platform process, and the reason is `MOS-CONF-109`'s IEC 62304 §4.3
segregation argument: software of one safety class must not be able to load software of
another at run time on the strength of a string in a configuration file. An earlier
version of this platform resolved providers with `importlib.import_module()` on an
environment variable, and it was removed.

So extensibility here is **composition at build time, not loading at run time.** You do
not register with a running MedicalOS. You build an image that contains MedicalOS and your
capability, and its composition root names both. The consequence is worth stating plainly:
**an operator cannot add your capability to a deployment by editing configuration.** They
deploy your image, or they do not run your capability. That is the intended behaviour —
what is in the image is what can run, and it is auditable by inspecting the image.

## Six files

Nothing from `medos` is vendored. Your repository depends on the published base image.

### 1. The capability

```python
# medos/services/air_trapping/service.py
from dataclasses import dataclass

from medos.capabilities.base import CapabilityContext
from medos.core.bundle import CapabilityOutcome, Finding
from medos.core.geometry import CanonicalVolume

CAPABILITY_ID = "air_trapping"


@dataclass(frozen=True)
class AirTrapping:
    capability_id: str = CAPABILITY_ID
    version: str = "0.1.0"

    def applicable(self, vol: CanonicalVolume) -> str | None:
        """None when this capability can run, otherwise a reason code.

        The codes are a CLOSED set -- `medos.capabilities.base.APPLICABILITY_REASON_CODES`.
        Returning a string outside it is refused, because "why did this not run" is a
        question an operator asks about a worklist and a free-text answer cannot be
        aggregated.
        """
        return None if vol.spacing_mm[0] <= 3.0 else "outside_applicability_envelope"

    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome:
        fraction = float((vol.array < -950.0).mean())      # <- your model here
        return CapabilityOutcome(
            capability_id=self.capability_id,
            findings=(
                Finding(
                    kind="air_trapping.fraction",
                    present=fraction > 0.06,
                    score=fraction,
                    measurements=(),
                ),
            ),
            label_map=None,
            source_sop_instance_uids=tuple(ctx.source.sop_instance_uids),
        )
```

**`MOS-SVC-038`: a Service Plane package must not import `socket`, `http`, `requests`,
`psycopg`, `boto3`, `subprocess` or `ctypes` at module scope.** Your capability is handed a
volume and returns findings. It does not fetch, does not write, and does not reach a
database — the platform does all of that on either side of it, which is what makes a
`Result`'s provenance complete.

### 2. The provider

```python
# medos/services/air_trapping/registry.py
from medos.capabilities import REGISTRY as PLATFORM
from services.air_trapping.service import CAPABILITY_ID, AirTrapping


def worker_registry(concepts, base=None):
    return {**(PLATFORM if base is None else base), CAPABILITY_ID: AirTrapping()}
```

**You may ADD. You may do nothing else.** `medos/medos/capabilities/providers.py` refuses four
ways, each by name:

| | |
|---|---|
| **R1** | the configured name is not one this image ships — the refusal lists what the catalogue does hold |
| **R2** | no callable `worker_registry`, or it raises, or it returns something that is not a `Mapping` of `Capability` |
| **R3** | it REPLACES a capability it did not supply |
| **R4** | it adds nothing at all |

R3 is the one to read twice. Shadowing `lung_segmentation` with your own implementation is
a supply-chain hole: every downstream `Result` would carry a provenance block naming a
capability whose code never ran. `MOS-REG-051` treats a name collision as a resolution
ambiguity, never an override. R4 is the same lie from the other side — a configured
provider that contributes nothing is a deployment that believes it is running your
capability and is not.

None of the four degrade to a default.

### 3. Your composition root

```python
# medos/services/catalogue.py    -- in YOUR image, replacing the platform's
from types import MappingProxyType

from services.air_trapping import registry as air_trapping_registry

CATALOGUE = MappingProxyType({"air_trapping": air_trapping_registry})
```

This is the whole of the extension mechanism. `MEDOS_CAPABILITY_PROVIDERS` holds a
**selector key** into this mapping — not a dotted path, and nothing resolves a string to a
module. A key that is not a selector token is itself refused, because a capability that is
in the image and unreachable is the defect nobody notices.

### 4–6. The manifest, the image, the signature

`medos/examples/<name>/capability.json` declares applicability, `intended_use`,
`regulatory_status` and `clinical.legal_manufacturer` — the identity `MOS-SAFE-003` makes
you own, and which every generated DICOM object and every provenance record carries so the
chain from a rendered overlay back to a legally responsible party is resolvable offline.

```dockerfile
FROM ghcr.io/<org>/medicalos:<digest>       # digest-pinned, never a tag
COPY services /app/services
COPY medos/examples/air-trapping /app/examples/air-trapping
ENV MEDOS_CAPABILITY_PROVIDERS=air_trapping
```

---

## What is not built yet

**This document describes a mechanism that works and a distribution story that does not.**

- **There is no published base image.** `medos/deploy/compose/Dockerfile` builds from the
  repository context with `COPY medos /app/medos` and `pip install -e .`, so today a third
  party must fork rather than layer. Publishing one digest-pinned base image is the single
  release-work item Level 1 cannot defer, and until it exists the `FROM` line above has no
  target.
- **`capability.json` has no published schema** and `medos/tools/medos_service.py` (validate,
  package, sign) is not written.
- **Signing and registration** are specified and unimplemented. `MOS-REG-*` describes an
  OCI registry layout; nothing pushes to one.

The composition mechanism itself is real and tested: `medos/medos/capabilities/providers.py` and
`medos/services/catalogue.py` are the code above, and `tests/integration/test_capability_providers.py`
is the suite that holds it — thirty tests, of which the ones that bear on your fork are
`test_what_the_resolver_can_serve_is_what_the_catalogue_statically_imports` (every catalogued
module got there through an `import` statement in that file, so a scan of `medos/services/`
or a name built from a setting would be refused — `MOS-REL-108`),
`test_a_selector_this_image_does_not_ship_refuses_startup`, and
`test_changing_the_configuration_changes_the_answer`.

**This sentence named `tests/integration/test_substituted_catalogue.py` until 2026-09-26,
and no such file has ever existed.** The property it claimed is genuinely covered, by the
file above; what was wrong was the address, in the one sentence a third party reads to
decide whether this seam can be trusted. Note what the suite does and does not establish:
it asserts the catalogue is the sole authority and that the API and the worker cannot
disagree about it. It does not stand up a second `medos/services/` tree on disk and build an
image from it, because there is no published base image to layer one onto — the first bullet
above is that gap, and it is the same gap that makes forking the only honest route today.

Until the base image is published, the honest way to build on MedicalOS is to fork it, put
your `medos/services/` tree in the fork, and track upstream. Say so to your auditors rather than
implying a supported extension path that does not yet exist.
