# SPDX-License-Identifier: Apache-2.0
"""`lung_nodule` -- lung nodule candidate detection on chest CT, as a Service Plane package.

    medos/services/lung_nodule/
        detector.py   the method: two HU thresholds, connected components, five filters
        service.py    the `Capability`: CanonicalVolume + CapabilityContext -> CapabilityOutcome
        concepts.py   deploy-time composition of the coded-concept dictionary
        registry.py   the injection seam into `WorkerDeps.registry`

The registry DATA lives outside this package, because it belongs to the platform curator
rather than to the vendor:

    medos/examples/lung-nodule/capability.json           the Capability row
    medos/examples/lung-nodule/concepts.overlay.json     its coded concepts and UCUM units
    medos/examples/lung-nodule/service-version.manifest.json
    medos/schemas/lung-nodule/capability-registration-1.0.0.json

Nothing is imported here. `MOS-SVC-036` requires a native service to be importable without
side effects, and an `__init__` that pulled in `service.py` would make `import
services.lung_nodule` read two JSON files and construct a concept dictionary.
"""
