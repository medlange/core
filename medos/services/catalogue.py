# SPDX-License-Identifier: Apache-2.0
"""The capability catalogue: every capability this image ships and can be asked to serve.

WHAT AN OPERATOR SELECTS, AND WHAT THEY CANNOT
------------------------------------------------
`MEDOS_CAPABILITY_PROVIDERS` is a list of KEYS OF THIS MAPPING. It is not a list of module
paths, and there is no mechanism anywhere in the platform that would import one.
`MOS-REL-108` is unqualified -- "MedicalOS MUST NOT offer an in-process plugin API -- no
shared-library loading, no dynamic module import, no user-supplied code executed inside a
platform process" -- and `MOS-CONF-109` then leans on that clause as the IEC 62304 section
4.3 segregation argument that lets a publisher classify platform items at a lower safety
class, calling it "a segregation argument a reviewer can execute rather than read".

This file is the executable form of that argument. The complete set of code a configured
deployment can run inside a platform process is the set of `import` statements below, fixed
when the image was built and readable with `grep import`. The previous mechanism read a
dotted path out of the environment and called `importlib.import_module()` on it, so a
reviewer executing `MOS-CONF-109` would have found exactly the thing it denies exists;
that is entry 68 of `docs/spec/99-known-inconsistencies.md`.

So an operator can turn a shipped capability ON and OFF. They cannot introduce one.
Introducing one is a rebuild of the image with a line added here, which is a code review and
a supply chain -- and that is the whole difference between configuration and a plugin API.

WHY THE CATALOGUE LIVES UNDER `medos/services/`
-------------------------------------------
Section 15.1.2's `zero-core-change` gate row allows the diff that introduces a capability to
touch `medos/services/`, `medos/schemas/`, `medos/examples/` and registry rows, and `MOS-REL-020` is the claim
it measures. Under `medos/medos/` this file would make capability number three a core change and
falsify that claim the first time anybody added one. Here, adding a capability is one import
and one row in a file the gate's allow-list already admits.

THIS FILE IS THE CURATOR'S, NOT A VENDOR'S -- STATED BECAUSE IT SITS IN THE VENDOR TREE
-----------------------------------------------------------------------------------------
`MOS-SVC-002` makes `medos/services/` the only plane that holds vendor-supplied code, and every
PACKAGE under it is one vendor's. This module is not one of them: it is the deployment's own
composition root, it belongs to whoever builds the image, it holds no capability logic, and
no package below it knows it exists. The platform reads this mapping; a vendor package never
reaches into it, which is what keeps registration something the image owner did rather than
something a dependency did to them.

WHAT THIS IS A STAND-IN FOR, AND WHAT IT MUST NOT BECOME
----------------------------------------------------------
A real third-party service is `MOS-SVC-107` registration of an OCI reference plus a
`MOS-REG-072` deployment row, executed OUT-OF-PROCESS (`MOS-REL-107`), and `MOS-REG-093`
rules out in-process loading for the proprietary case in as many words. None of that exists
in this build. This mapping composes first-party Python that the image already contains; it
verifies no signature, resolves no artifact, and MUST NOT become the installation path for a
sealed third-party service. It is to be REPLACED by those rows, not extended.

Spec: MOS-REL-107, MOS-REL-108, MOS-CONF-109 (what this shape is FOR), MOS-SVC-107,
MOS-REG-072, MOS-REG-093 (what this is NOT), MOS-REL-020, MOS-SVC-002.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType, ModuleType

from services.lung_nodule import registry as lung_nodule_registry

__all__ = ["CATALOGUE"]

#: Selector name -> the module that supplies it. ONE ROW PER SHIPPED CAPABILITY PACKAGE.
#:
#: THE KEY is what an operator writes in `MEDOS_CAPABILITY_PROVIDERS`: a lower-case selector
#: token, because `medos.capabilities.providers.configured_providers()` splits that variable
#: on commas and whitespace and a key containing either could be shipped and never selected
#: -- a capability present in the image, present here, and unreachable, which is the 0.3.0
#: defect this whole seam exists to close, in miniature. The resolver refuses such a key
#: rather than leaving it to be discovered by an operator whose configuration does nothing.
#:
#: The key is NOT required to equal a capability id. One service package may register more
#: than one capability, so the resolver records the ids a provider actually added
#: (`ResolvedCapabilities.supplied_by`) rather than trusting the name it was selected by.
#:
#: THE VALUE is a module exposing `worker_registry(concepts, base=None)` and, optionally,
#: `worker_concepts(work_dir, base=None)`. That contract is documented in
#: `medos.capabilities.providers`, and it was not invented for this file:
#: `medos/services/lung_nodule/registry.py` already had this shape before any resolver existed,
#: and nothing in it moved to be listed here.
#:
#: A `MappingProxyType` because the resolver reads this on every resolution, and a caller
#: that wrote into it would be editing what every later resolution in the process serves --
#: the global mutable state CONTRACT.md section 11 forbids, reconstructed one level down.
CATALOGUE: Mapping[str, ModuleType] = MappingProxyType(
    {
        "lung_nodule": lung_nodule_registry,
    }
)
