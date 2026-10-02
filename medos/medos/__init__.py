# SPDX-License-Identifier: Apache-2.0
"""MedicalOS — weeks 1-2 vertical slice.

Layout is fixed by CONTRACT.md §1. Per docs/spec/15-delivery.md §15.2.3 this slice is
deliberately architecturally wrong: no auth, no tenancy, no RLS, no registries, no
capability resolution, no broker, no Triton, no sealed services, no evidence plane, no
agents, no policy engine. Those arrive in weeks 3-5 (`MOS-REL-084`), which also replaces
this Python HTTP surface with the Go control plane — hence the rule that the HTTP layer
holds no business logic.
"""

__version__ = "0.1.0.dev0"
