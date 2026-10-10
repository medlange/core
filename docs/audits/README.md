# Medlange — product audits (2026-10-04)

Three independent audits by external experts: how ready each product is for
adoption by **third-party** developers and companies — individually and
jointly. Method: code and docs reading, suite runs, live checks.
Full reports are next to this file; the findings are entered into ROADMAP.

| Product | For in-house | For third parties | Main finding |
|---|---|---|---|
| [Viewer](viewer-2026-10-04.md) | 7–8/10 | **3/10** | A disciplined product with a seam, not a platform: no network seam, OVERLAY is dead, editing the core is mandatory, docs describe a nonexistent API |
| [Core](core-2026-10-04.md) | 8/10 | **4/10** production / 6/10 research | Contract discipline of a mature framework, but: a message-loss defect in the bus, the real kserve_v2 path not run end-to-end, card 1.1 not closed with the writer |
| [Trainer](trainer-2026-10-04.md) | 7.5–8/10 | **4.5–5/10** | Works end-to-end, unique masked-training and evidence layer, but: no predict, no standalone onboarding, depends on nnU-Net/MONAI |

The auditors' overall conclusion: the engineering culture (failures as dicts,
gates, live e2e, honest docs) is above the industry average; what is missing is
the "outer shell": packaging, API references, standalone onboarding, production
mechanics. This is the next big development phase.

**Owner decision (2026-10-04):** Trainer is rewritten on a vanilla stack —
from scratch, without MONAI and nnU-Net (see ROADMAP, section T-vanilla).
