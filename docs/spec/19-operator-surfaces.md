<!-- MedicalOS Specification v0.4.0 — chapter 19 of 19. Normative.
     244 requirements. Do not edit without a requirement-ID review. -->

[← 18. Standards and Regulatory Conformance](18-conformance.md) · [Index](../../MEDICALOS_SPEC.md)

---

## 19. Operator Surfaces

Eighteen chapters specify a platform that a person can use only through `curl`. The product needs two
people at a keyboard: a clinician who opens a study, chooses what to run, submits and reads the result;
and a domain expert who chooses a dataset, trains a model and promotes it. Neither has a requirement ID
behind it. The only user-interface commitment anywhere in the specification is one line of the 0.1.0
release contents — "OHIF toolbar button + provenance panel" (Chapter 15 15.1.2, normative in Chapter 9
`MOS-SAFE-088` and `MOS-SAFE-089a`) — and Chapter 2's plane table hands the Presentation Plane to
"10, 13", where neither chapter claims it. That is the same condition the previous specification was in
when it had a Marketplace and no packaging format: a product surface asserted in prose with no
requirement that can fail.

This chapter owns both surfaces. It does not own a pixel of layout. A screen here is specified by what it
MUST show, what it MUST refuse, and which requirement elsewhere in this document the refusal implements.

The constraint that shapes the whole chapter belongs to the engineering surface (19.3–19.5): **it is for
people with no programming background.** Not a thin wrapper over a CLI for an ML engineer — a surface a
clinical domain expert operates alone. That is not a polish requirement. It changes what the platform
must refuse to do, because the questions a training system normally asks an expert ("which split?",
"is this leakage acceptable?", "promote anyway?") are questions this operator cannot answer, and a
surface that asks them has converted a machine-checked gate into a dialog box. What makes the surface
safe is already built and already blocking: the L1–L5 leakage check and the corpus stratification check
at seal (Chapter 17), patient-level frozen splits and the locked test partition no search may read
(Chapter 7), and the human promotion gate. For an expert audience those are guardrails. For this audience
they *are* the product — the system refuses, in a sentence a non-programmer understands, instead of
asking a question they cannot answer. 19.3 and 19.4 specify that surface; 19.5 owns acceptance.

This part (19.1–19.2) fixes the boundary both surfaces share and specifies the clinician surface.

---

### 19.1 Scope

#### 19.1.1 Two surfaces, one platform, one API

**MOS-UI-001** The platform MUST ship exactly two operator surfaces, and this chapter MUST be the only
place either is specified:

| Surface | Operator | Host | Owns | Specified in |
|---|---|---|---|---|
| **Clinician surface** | a reader, at a study | a first-party viewer the platform serves (`viewer/`, at `/mos-viewer/`) | open a study, choose what to run, submit, read the result, read provenance | 19.2 |
| ~~**Engineering surface**~~ | ~~a clinical domain expert, not a programmer~~ | ~~a MedicalOS-served web application~~ | ~~choose a dataset, train a model, promote it~~ | ~~19.3–19.4~~ |

A third operator surface MUST NOT be added without a minor version increment of this document, on the
terms `MOS-CORE-036` sets for reversing a non-goal. Operational consoles that are not operator surfaces —
Grafana dashboards, the `medicalos-reproduce` tool (`MOS-SAFE-092`), `psql` — are Chapter 13's and are
outside this chapter.

**MOS-UI-001 — AMENDED at specification 0.4.0.** The Clinician-surface `Host` cell read ~~OHIF, through a
MedicalOS extension package~~ until release 0.4.0 removed the adopted viewer from the deployment and put
the first-party surface at `viewer/` in its place, served at `/mos-viewer/` and redirected to from the
origin root. The obligation does not move with the host and is not weakened by the amendment: the count is
still exactly two, this chapter is still the only place either is specified, and a third still costs a
minor version increment under `MOS-CORE-036`. `MOS-UI-001` constrains how many surfaces exist and where
they are specified; a surface does not become a different surface because a different program serves it.

**MOS-UI-001** — **WITHDRAWN in part at specification 0.4.0.** The engineering surface of §19.3 is
withdrawn: the platform ships exactly one operator surface, the clinician surface, and the second row
of the table above is struck with this amendment. The product decision the withdrawal records: the
training pipeline is a batch product with no operator web surface of its own (`trainer/`, driven
through the Chapter 10 training-plane API by clients and scripts), `medicalos_preprocessing/` is the
SDK both images share, and MedicalOS ships viewer, trainer, platform and SDK as four products — a
no-code training console is not one of them. Every requirement of this chapter that binds "both
surfaces" or "either surface" binds the clinician surface alone from this amendment; the
engineering-surface clauses of `MOS-UI-002`, `MOS-UI-003`, `MOS-UI-004`, `MOS-UI-005`, `MOS-UI-007`
and `MOS-UI-008` are struck with it. `MOS-UI-005`'s closed value space retains `engineering_console`
as the audit label a direct client of the training-plane API MAY send; no first-party surface sends
it. §19.3's requirements are withdrawn in their entirety and retained below as the specification of
a surface that does not ship — the refusal contract, the no-code cohort builder and the one-action
Train screen are a design this repository built once (`medos/web/training-console`, deleted
2026-10-02) and may build again only under a new minor version of this chapter (`MOS-CORE-036`).

Two mounts on that origin are not the third surface~~, and the count is worth stating because a reader
counting mounts in `medos/deploy/compose/docker-compose.yml` now finds three where this table says two~~.
The extension package at `/medicalos/` is a toolbar entry and a provenance panel over `/api/v1` with no
viewport — `MOS-UI-013` records exactly what of it executes and what does not — and a reader does not open
a study at it. ~~`/training-console/` is the engineering surface, which is the second row of this table.~~
The third mount this paragraph used to count was `/training-console/`, the engineering surface; it is
withdrawn at specification 0.4.0 and the mount is deleted with it, so the count a reader makes from the
compose file is now two mounts under one surface.

**MOS-UI-002** Both surfaces MUST be clients of the Chapter 10 REST surface and of nothing else. Neither
surface, and no server-side component that exists to serve either surface, MAY:

- open a connection to PostgreSQL or to any other platform datastore;
- address the PACS, hold a PACS credential, or resolve a PACS network address (`MOS-DATA-002`,
  `MOS-SEC-088`, `MOS-OPS-005`);
- read the object store directly, or accept a presigned URL scoped more broadly than one object;
- consume the message bus;
- call `/internal/v1/...`, which `MOS-API-001a` places outside public API and outside `Z-EDGE`.

This is machine-checked, not architectural taste: Chapter 14's AT-08 plane P4 asserts that a TCP connect
from the ~~OHIF~~ clinician-surface container to PostgreSQL `5432` and to the PACS `4242`/`8042` is
refused, and declares "put ~~OHIF~~ that container on the same network as the PACS" as mutant M3
(`MOS-TEST-066`). The engineering surface MUST be placed in the same network position and MUST be covered
by the same check.

**AMENDED at specification 0.4.0, and the amendment costs this requirement its check until chapter 14
repairs it.** The words struck above are the words chapter 14 still uses: AT-08's P4 row and its mutant M3
name OHIF, and release 0.4.0 renamed the service they name — `medos/deploy/compose/docker-compose.yml` now runs
a plain nginx called `web`, container `medos-web`, and no `ohif` container exists for a P4 probe to enter.
The prohibition itself is untouched and binds `viewer/` exactly as it bound the adopted viewer,
because a network position is a property of the surface and not of the program serving it. The sentence
this paragraph opens with is what moved. A check that names a container the stack does not start does not
fail when the property is violated — it fails to run, and a gate that cannot fail proves nothing, which is
the property register entry 107 records for `MOS-TEST-068`'s `viewer-pin` job one requirement over.
Chapter 14 owns AT-08 and owns the repair; until P4 and M3 name the service the deployment runs, this
requirement MUST be recorded in a conformance report as unchecked rather than as machine-checked.

**MOS-UI-003** Every action either surface performs MUST correspond to a route in Chapter 10 table 10.2-B,
exercised with the operator's own credential and the route's declared permission (`MOS-API-005`). A
surface MUST NOT expose an operation for which no route exists, MUST NOT reach an operation through a
server-side component holding a credential broader than the operator's, and MUST NOT create a second
job-creation path — `POST /api/v1/jobs` is the only one (`MOS-API-001`, `MOS-SAFE-089a`).

**MOS-UI-004** The converse also binds: there MUST be no operator-surface-only capability. Anything either
surface can do MUST be doable by an API client with the same permissions, because a control that exists
only inside a UI is a control that cannot be audited, scripted or tested against the route table.

**MOS-UI-005** Every request either surface issues MUST carry `X-MedicalOS-Surface`, whose value space is
closed: `clinical_viewer` (19.2) and `engineering_console` (19.3–19.4). Chapter 8's worked authorisation
trace already assumes this header on the viewer's read of a `Result`. It is a provenance and audit label
only: a PEP MUST NOT grant any permission on the strength of it, and a request that omits it MUST be
served normally.

**MOS-UI-006** Authorisation MUST be enforced server-side and only server-side. A surface MAY hide or
disable a control the operator's permissions do not allow, as an ergonomic courtesy, and MUST render
whatever the API answers when it is exercised anyway — a `403` problem document is displayed, not
suppressed. A surface MUST NOT implement a client-side check as the enforcement point: a stub that looks
like an authorisation control and enforces nothing is forbidden by `MOS-REL-050` and is worse than its
absence, because a reviewer may mistake it for the real thing.

**MOS-UI-007** Both surfaces are **MedicalOS-controlled surfaces** in the sense of `MOS-SAFE-012`, and
every requirement binding "every user-facing surface that displays an AI-derived finding" therefore binds
both. Neither surface may be exempted by configuration, by an embedding host, or by being labelled a
preview.

**MOS-UI-008** Both surfaces MUST carry the `MOS-SAFE-001` statement verbatim in a persistently reachable
footer, and both MUST be covered by the claims lint of `MOS-SAFE-008` and the vocabulary table of
`MOS-CORE-004` — UI strings and i18n bundles included, which is already how `MOS-SAFE-008` scopes the ban.

#### 19.1.2 How this complies with the non-goals

Two non-goals appeared to forbid what this chapter specifies, and the original reconciliation was that
the clinician surface would ADOPT a viewer rather than write one. That is no longer what the platform
does. `MOS-CORE-038` was reversed at release 0.4.0 and the reversal is recorded in the non-goals table
at specification 0.3.0; `MOS-CORE-045` is bounded rather than reversed. This subsection is the
consequence: it withdraws `MOS-UI-009`, states what the first-party viewer MUST guarantee in its place,
and draws the line between a measurement and an annotation where the rest of this chapter already draws
it — at the OUTPUT.

The withdrawn text is kept struck through rather than deleted. A reader holding a conformance report
written against specification 0.2.0 needs to see what changed and where, and a requirement that simply
vanishes between versions is indistinguishable from one that was never checked.

**MOS-UI-009** — **WITHDRAWN at specification 0.3.0.** ~~`MOS-CORE-038` forbids the platform to build
its own DICOM viewer. The clinician surface MUST comply by **adopting** a viewer rather than writing one.
Concretely, the platform MUST NOT: implement image decoding, windowing, stack scrolling, MPR, or any
viewport rendering of pixel data; implement or vendor a renderer for SEG, SR, SC or PR objects; fork,
patch or vendor the viewer's source tree; ship a DICOMweb data source of its own into the viewer; and
MUST confine its contribution to the four kinds in the table below.~~

It is withdrawn because its authority was withdrawn under it. `MOS-CORE-038` was reversed at release
0.4.0; `MOS-UI-009` cited that non-goal as its whole justification and was not withdrawn with it, and
for twelve viewer commits the repository shipped code its own specification forbade. That gap is
register entry 103 in `docs/spec/99-known-inconsistencies.md`, which names this amendment as its remedy.

**MOS-UI-009a** The platform MAY build and ship a first-party clinician-surface viewer. Building one
is permitted; the following four guarantees are what it is held to instead, and each one MUST be
checkable rather than asserted:

- **One route to the pixels.** The viewer MUST read images only through the Gateway (`MOS-UI-002`). A
  second route to the archive from inside the viewer is exactly what AT-08 mutant M3 fails, and that
  remains true of a first-party viewer: tenancy, quarantine and audit are properties of the Gateway,
  and a viewer that reaches past it has none of them.
- **It creates and edits no pixel.** The viewer MUST NOT write a label map, a mask, a SEG, or any
  object a model could be trained on, and MUST NOT offer editing of a generated SEG (`MOS-UI-010a`,
  `MOS-UI-204`). What it produces is scalars with provenance and nothing else.
- **Every number carries its unit or says it has none.** Every value the viewer displays MUST carry the
  unit the DICOM header states, or be marked as not recorded (`MOS-UI-037`). A millimetre derived from
  a frame with no `(0028,0030)` is a pixel count wearing a unit and MUST be presented as one.
- **It refuses rather than approximates.** Where the geometry does not support a reconstruction, the
  viewer MUST refuse it and say why, rather than produce a picture whose accuracy it cannot stand
  behind. The four refusals in `viewer/src/image/mpr.js` —
  `reconstruction_needs_spatial_frames`, `spacing_non_uniform`, `gantry_tilt_uncorrectable` and
  `reconstruction_needs_uniform_rescale` — are what this clause looks like implemented, and are named
  here so the requirement has something to be checked against.

The standard-output obligation is UNCHANGED and is the reason this reversal is safe: the viewer renders
what it renders, but the objects the platform WRITES are still standard SEG/SR/SC that other viewers
read (Chapter 4), and `MOS-IMG-158` still requires verification in a second, independent viewer at
0.4.0. Building a viewer is permitted. Writing a private object format is not, and the second-viewer
check is what keeps the two apart.

The four contribution kinds below are RETAINED, and they are no longer a closure. They describe what an
extension to an ADOPTED viewer may contribute — the `medos/web/ohif-extension/` package is still governed by
them — and they no longer bound what the first-party viewer may contain.

| Contribution | Permitted | Why it is not "building a viewer" |
|---|---|---|
| An **extension package** outside the viewer's tree | MUST | `MOS-REL-027`'s Viewer row and `MOS-SAFE-089a`; the upgradability boundary that keeps the pinned viewer replaceable |
| **Configuration** of the adopted viewer | MUST | `MOS-DATA-015` already fixes the DICOMweb configuration against the Gateway |
| **Panels** rendering MedicalOS data (`Job`, `Result`, provenance) | MUST | The data is the platform's; the pixels are not |
| **Commands and toolbar entries** that call `/api/v1` | MUST | One job-creation path, `MOS-API-001` |

The non-goal is coherent only because the output is standard SEG/SR/SC that other viewers already render
(Chapter 4). That is what `~~MOS-IMG-157~~ MOS-IMG-157a` exists to check and what `MOS-IMG-158` extends to a second,
independent viewer at 0.4.0 — see `MOS-UI-013` for the verification status of that claim in this
repository today.

**MOS-UI-010** `MOS-CORE-045` forbids the platform to *build* an annotation authoring tool while
permitting it to *integrate* a third-party one as the producer of an `AnnotationSet` (Chapter 17 adopts
MONAI Label for this). Neither surface MAY contain brush, eraser, interactive-segmentation or
mask-editing functionality, **nor any gesture whose output is a label map, a mask, a stored contour or
anything else ingestible as an `AnnotationSet`.** ~~contour-drawing, scribble,~~ — see `MOS-UI-010a`,
which bounds those two words rather than removing them. The clinician surface in particular MUST NOT offer editing of
a generated SEG: a reviewer who disagrees records a `ResultReview` with `action: MODIFIED` and its
`modifications` (`MOS-SAFE-069`), which is a recorded human judgement about a result, not a redraw of it.
The engineering surface's integration of an external annotator — including who holds the reader identity
binding and who computes consensus, both of which stay with the platform per `MOS-REL-027`'s MONAI Label
row — is specified in 19.3–19.4 and MUST NOT be read into this part.

**MOS-UI-010a** The boundary `MOS-UI-010` draws is the OUTPUT, not the gesture. This is not a new
principle; it is where this chapter already draws it. `MOS-UI-200`'s disqualification table states that
"An ROI-and-measurement toolset cannot produce a label map, so the tool cannot be the producer of an
`AnnotationSet`" — and a tool that cannot be the producer of one is not the thing `MOS-CORE-045`
forbids building.

The clinician surface MAY therefore offer reader-drawn geometry — a caliper, an angle, an elliptical,
rectangular or **freehand closed-polygon** region, and a reader-typed label or text note anchored to an
image coordinate — provided **all five** of the following hold. A tool that fails any one of them is an
annotation authoring tool and MUST NOT be built:

1. **Its only output is scalar.** Length, angle, area, and first-order statistics over enclosed pixels,
   each with the provenance `MOS-UI-037` requires. A polygon's vertices MAY be retained so the shape can
   be redrawn and re-measured, and MUST NOT be exported as a contour, a mask, or an `AnnotationSet`.
2. **It writes no label map and no SEG.** Not to the archive, not to the API, not to a file the reader
   can save. The CSV and PNG exports carry measurements and pixels-as-shown, never a segmentation.
3. **It does not edit a generated SEG.** A reviewer who disagrees with a produced segmentation records a
   `ResultReview` with `action: MODIFIED` and its `modifications` (`MOS-SAFE-069`). That is a recorded
   human judgement about a result, and it is not a redraw of one.
4. **It is not a segmentation primitive.** No brush, no eraser, no threshold or region-grow, no
   scissors, no label-map interpolation, no segmentation undo stack (`MOS-UI-204`, unchanged and
   unbounded by this requirement). The distinction is not subtle: those six produce or modify a mask by
   construction, and no restriction on their output can make them anything else.
5. **A text note is text, not a drawing.** A reader-typed string MAY be anchored to an image coordinate
   and MAY carry a leader line to it. It MUST NOT be rasterised into the pixels, MUST NOT be burned into
   any exported image other than as an overlay drawn at export time, and MUST carry the same
   `clinical_use: research_only` marking every other reader-produced value carries (`MOS-SAFE-001`,
   `MOS-UI-008`).

**What enforces this.** `MOS-CORE-036` requires a reversal to be recorded, not merely taken, and a
boundary that is only prose is the condition register entry 103 describes. The five clauses above are
checked by static gates over the compiled surface in `viewer/tests/test_architecture.py`, which
MUST assert at minimum: that no measurement record reaches an export path carrying per-pixel data; that
the viewer contains no brush, eraser, threshold, region-grow, scissors or label-map interpolation; and
that every reader-produced value carries `clinical_use: research_only`. Chapter 19's acceptance check 37
is amended in consequence — it MUST assert the absence of mask-editing and annotation-AUTHORING
controls, and MUST NOT assert the absence of a measurement implementation, which specification 0.3.0
permits.

**MOS-UI-011** Neither surface MAY resolve, rank, pin or substitute a `ServiceVersion`, a `ModelVersion`
or a `Deployment`. Chapter 2's plane table puts model identity among the things the Presentation Plane
MUST NOT know about; resolution is the Control Plane's and is a pure function of
`(capability, context, registry_snapshot)` (`MOS-REG-012`). A surface displays a resolution the platform
made and, where it needs to explain one, calls the side-effect-free dry run
`GET /api/v1/capabilities/{capability_id}/resolution` (`MOS-API-058`, `MOS-REG-110`). It MUST NOT compute
a second one.

---

### 19.2 The clinician surface

The flow is four steps: open a study ~~in OHIF~~ in the clinician surface — **AMENDED at specification
0.4.0**, for the reason `MOS-UI-001` gives: the flow did not change when the host did — choose what to
run, submit, read the result. Each is a screen below. The study itself arrives through the viewer's own
DICOMweb data source pointed at the Gateway (`MOS-DATA-015`), which since 0.4.0 is
`viewer/src/dicom/dicomweb.js`; nothing in this section changes how a study is opened, and the surface
never learns a PACS credential.

#### 19.2.1 What the platform ships, and what it must not

**MOS-UI-012** — **WITHDRAWN at specification 0.4.0.** ~~The clinician surface MUST be delivered as a
viewer extension package living outside the viewer's source tree (`MOS-REL-027`, `MOS-SAFE-089a`),
contributing exactly: a toolbar entry, the commands behind it, and one panel. In this repository that
package is `medos/web/ohif-extension/` (`@medicalos/extension-medicalos`), whose `package.json` declares
`@ohif/core ^3.9.0` as a peer dependency and records `ohif.builtAgainst: v3.9.2`; it contributes
`getToolbarModule`, `getCommandsModule` and `getPanelModule`, and deliberately contributes no data source,
no mode and no `SOPClassHandler`. That set of omissions is normative:~~ **MOS-UI-012a** — **WITHDRAWN at
specification 0.4.0.** ~~the extension MUST NOT contribute a `SOPClassHandler` or any other renderer for a
generated object, because doing so would be the platform asserting a rendering it did not test
(`MOS-CORE-038`).~~

Both are withdrawn because the delivery form they fix is not the delivery form the platform ships.
`MOS-UI-012` drew a package boundary *outside an adopted viewer's source tree*, and its whole purpose was
upgradability — the pinned viewer stays replaceable because the platform's code never enters its tree.
Release 0.4.0 removed the pinned viewer: `medos/deploy/compose/docker-compose.yml` no longer runs
`ohif/app:v3.9.2`, the service is a plain nginx named `web`, and the clinician surface is `viewer/`
served at `/mos-viewer/`. A boundary drawn around a tree nothing deploys constrains nothing, and a
requirement that the surface contribute "exactly a toolbar entry, the commands behind it, and one panel"
describes a contribution to something rather than the thing itself. `MOS-UI-012a` is reversed in fact and
not merely in form: `viewer/src/image/seg.js` decodes a generated SEG and
`viewer/src/render/viewport.js` draws it, which is precisely the renderer for a generated object that
clause forbade. The package at
`medos/web/ohif-extension/` is not removed — it is still served at `/medicalos/`, still bound by the four
contribution kinds of §19.1.2, and still what `MOS-UI-013` describes.

The reason `MOS-UI-012a` gave has outlived the prohibition it carried, so it is restated rather than
dropped. "The platform asserting a rendering it did not test" was never an argument about
`SOPClassHandler`; it was an argument about evidence, and the platform now does the rendering.
`MOS-UI-012b` supersedes `MOS-UI-012` and `MOS-UI-012c` supersedes `MOS-UI-012a`, stated here because
`MOS-CORE-023` requires a withdrawn entry to carry a `superseded_by` value and not merely a successor
printed underneath it.

**MOS-UI-012b** The clinician surface MUST be delivered as one named tree in this repository, and the
deployment MUST serve that tree rather than a copy, a bundle built from it, or a subset of it. In this
repository the tree is `viewer/`, bind-mounted read-only at `/mos-viewer/`. Two properties make this
checkable rather than descriptive. The served tree MUST hold no credential, which `tests/e2e/test_demo.py`
asserts by grepping every served file for `bearer` — the Gateway key is injected server-side by nginx and
never reaches the browser (`MOS-UI-002`). And the build identity the surface displays MUST come from a
served data file rather than a constant in a source module: `viewer/build.json` carries
`viewer_version`, `platform_version` and `git_commit`, and register entry 82 records what the alternative
costs — a version written into code is correct until the next build and silently false afterwards.

**MOS-UI-012c** Where the clinician surface renders a generated object, it MUST show how the
correspondence between that object and the images was established, and MUST NOT present the rendering as
verified before the second-viewer check of `MOS-IMG-158` has been run (`MOS-UI-013b`). A renderer asserts
a rendering: an overlay drawn with no evidence of its alignment is a claim the platform has not earned,
and an overlay quietly missing forty frames looks on screen exactly like a model that under-segmented,
while the two need different actions. `viewer/src/image/seg.js` matches SEG frames to slices by
`ReferencedSOPInstanceUID` first and by patient position second and counts both, together with the frames
it could place by neither route; `viewer/src/ui/segments-panel.js` renders those three counts beside
the segment list. That is what this requirement looks like implemented, and it is named here so the obligation
is a display obligation with something to be checked against rather than a logging one.

**MOS-UI-013** — **AMENDED at specification 0.4.0.** A release whose contents include the toolbar button
MUST ship it *inside the viewer bundle the release deploys*. The following are verified facts about the
current repository and are recorded here because they bound what may be claimed:

- The extension's OHIF-host modules — `src/index.js`, `src/getToolbarModule.js`, `src/getPanelModule.js`,
  `src/getCommandsModule.js`, `src/panels/provenance-panel.js` (a custom element and deliberately not a
  React component, for the reason that file records) — are **unexecuted source**. ~~The deployed
  viewer is the pre-built image `ohif/app:v3.9.2`, which resolves extensions from its own build-time
  bundle; listing the package in `window.config.extensions` against that image does nothing. Loading it
  requires rebuilding the viewer from the OHIF monorepo, which has not been done.~~ Since release 0.4.0
  nothing resolves them at all: `medos/deploy/compose/docker-compose.yml` runs no OHIF image, and the `web`
  service serves `medos/web/ohif-extension/` at `/medicalos/` as static files. The finding is stronger than the
  struck sentence rather than weaker — these modules were unexecuted because the pinned image resolved
  extensions from its own build-time bundle, and they are unexecuted now because there is no host to
  resolve them.
- What does run is `medos/web/ohif-extension/standalone/`, a page served on the viewer's origin that imports the
  same `src/core/client.js`, `src/core/provenance.js` and `src/core/render.js` the extension imports. It
  is not a mock and not a second job-creation path; it constructs the same `MedicalOSClient` and posts to
  the same `/api/v1/jobs`.

The struck clause is struck rather than corrected in place because it was offered as a **verified fact
about the current repository** and stopped being one at 0.4.0, while §19.1.2 and `MOS-UI-205a` both cite
this requirement for what of the extension executes and what does not. A requirement whose stated purpose
is to bound what may be claimed has to be true about the thing it describes, and two sections of one
chapter giving opposite answers about what is deployed is the condition register entry 106 was opened for,
one deployment later. The MUST above is untouched, and it is why this requirement is amended rather than
withdrawn: a release that lists the toolbar button in its contents still has to ship it inside the bundle
it deploys, and `MOS-UI-013a` states what a Release Decision Record may not record instead.

**MOS-UI-013a** Consequently, a Release Decision Record that claims `MOS-SAFE-089a` is satisfied MUST
state which host was tested. The standalone page satisfies the *behavioural* half of `MOS-SAFE-089a` —
one POST per activation, the study UID in the body, the follow, the `REJECTED`/`FAILED` distinction with
the reason verbatim — and does not satisfy "for the study currently open in the viewer", because no study
is open in it. ~~Chapter 9 `MOS-SAFE-089a` permits the button to be cut under `MOS-REL-009`; it does not
permit the cut to be recorded as a pass.~~ — **AMENDED at specification 0.4.0.** Chapter 9 struck that
permission. `MOS-SAFE-089a` is unconditional and its acceptance check 24 FAILS where no surface carries
the control over an open study, so the half this requirement says is unsatisfied no longer has a cut to
fall back on. What this requirement adds is unchanged and now carries the whole load: a Release Decision
Record MUST state which host was tested, and the behavioural half passing on the standalone page is not a
pass for the whole.

**MOS-UI-013b** — **AMENDED at specification 0.4.0.** Claims about ~~the adopted viewer's own~~ any
rendering of an object the platform wrote MUST be verified, not asserted, whoever wrote the renderer.
Chapter 14 `MOS-TEST-068` pins the viewer by digest and names `@ohif/extension-cornerstone-dicom-seg` and
`@ohif/extension-cornerstone-dicom-sr` as the extensions under test, and AT-11 (`~~MOS-IMG-157~~ MOS-IMG-157a`,
`MOS-TEST-069`) is the check that the SEG overlays at the expected slice indices and the SR panel hydrates
with the expected coded measurements. In this repository that check has **not** been run, by test or by
hand. Until it is, the specification's position is that the platform writes standard objects and ~~the
adopted~~ a viewer is expected to render them, and no stronger statement may appear in a release note, a
data sheet or a UI string.

**The subject is widened because four clauses of this amendment already cite it that way.** `MOS-UI-012c`,
the annotation row of `MOS-UI-202`, the withdrawal of `MOS-UI-205` and `MOS-UI-373a` each lean on this
requirement to govern what may be claimed about the FIRST-PARTY viewer's rendering, and until this
amendment its subject was the adopted viewer alone — so four citations asserted an obligation the cited
requirement did not carry. That is `MOS-UI-006`'s stub in a different costume: something a reviewer reads
as the enforcement point which enforces nothing. Widening it withdraws nothing, because the hazard was
never a property of who wrote the renderer. A platform that reads its own SEG back through its own decoder
has established that the decoder is self-consistent, not that the object is correct, and that holds
whether the decoder is OHIF's or `viewer/src/image/seg.js`. The adopted-viewer wording is kept struck
rather than replaced because §19.4's annotation surface may still be delivered over one (`MOS-UI-205a`),
and because `MOS-TEST-068`'s pin now names a container the stack does not start — chapter 14 owns that
repair and register entry 107 records it.

#### 19.2.2 The capability picker

**MOS-UI-014** The picker MUST present **capabilities**, not models. Each entry MUST show, without
requiring interaction:

| Element | Source | Rule |
|---|---|---|
| Capability display name and `capability_id` | `GET /api/v1/capabilities` (row 19) | The `capability_id` is shown beside the name, never instead of it |
| Primary coded concept | `capability.primary_code` `{system, code, meaning}` (`MOS-REG-044`) | Shown as `meaning` with the code available; this is the clinical identity of the entry |
| The **resolved** `ServiceVersion` | `resolution.selected.service_version` + `service_family` | Version shown as a version, e.g. `3.2.1`, beside the service family |
| `legal_manufacturer.name` | `ServiceVersion.clinical.legal_manufacturer` (`MOS-SAFE-020`) | Required; the entry is a claim by a named party |
| `intended_use.statement` | `MOS-SAFE-014` | The publisher's own 40–2000 character prose, rendered verbatim |
| `intended_use.intended_user` and `reading_paradigm` | `MOS-SAFE-014` | e.g. "radiologist · second reader" |
| `clinical_use_mode` of the deployment that would run it | `Deployment` (`MOS-SAFE-046`) | `research_only` entries carry the RUO badge of `MOS-UI-040` |

**MOS-UI-015** A picker entry MUST NOT be a bare model id, a bare `service_version_id`, a bare
`ServiceVersion` semver, or a filename. A reader choosing between `mv_01J8ZK6R0P4A1S9Q2VTC8XNE` and
`mv_01J7RM2H8F3B6D9G1K4N7QSV` is choosing at random. The identifiers MUST be available — a support ticket
quotes them — and MUST NOT be the label.

**MOS-UI-016** The picker MUST surface, on one interaction from the entry and before submission is
possible, the publisher's declared limits: every `not_validated_for[]` entry and every
`known_failure_modes[].text`, verbatim. Both lists are guaranteed non-empty by `MOS-SAFE-015` and
`MOS-REG-032`, and `MOS-REG-032` already commits the platform to rendering the explicit
"no known failure modes have been characterised" entry verbatim rather than hiding it. Where
`training_population` is `null`, the string "Training population not declared by the publisher" MUST be
shown, per `MOS-SAFE-016`.

**MOS-UI-017** Release dependency. The picker as specified depends on the `Artifact` registry and on
capability resolution as a pure function, which Chapter 15 places in **0.3.0**, and on `Resolve()`, which
Chapter 6's readiness table marks "not shipped" before that release. Therefore:

- **Before 0.3.0** the picker MUST render a **fixed list**, configured per deployment, of the single
  `ServiceVersion` pinned for each capability. It MUST say so in words — the operator is choosing a
  capability, not choosing among versions — and MUST NOT present a version selector, a range box or a
  "latest" affordance. `MOS-REG-004` guarantees the pinned `resolution` object exists on the job even in
  this degenerate case, so the resolved version displayed before submission and the one recorded on the
  job are the same fact.
- **From 0.3.0** the picker MUST obtain what it displays from
  `GET /api/v1/capabilities/{capability_id}/resolution` (`MOS-API-058`, `MOS-REG-110`), which is
  side-effect-free and creates no job. It MUST render `selected`, and MUST make `excluded[]` with its
  per-candidate `stage` and `reason_code` available on one interaction. It MUST NOT re-rank, filter or
  re-implement the resolver (`MOS-UI-011`).

**MOS-UI-018** The picker MUST NOT offer a capability whose `Capability.status` is `reserved` or
`deprecated`; `MOS-REG-046` makes `reserved` rows non-resolvable and filter `F1` excludes them, so
offering one produces a guaranteed `no_candidate_service_version` rejection. Where a deployment ships a
capability that is declared but not implemented, the entry MUST be labelled as such in the picker rather
than hidden — a platform that silently omits `pleural_effusion` looks like a platform that detects
effusions.

**MOS-UI-019** The picker MUST refuse to expose any of the following controls, in any form, including an
"advanced" disclosure:

| Refused control | Why |
|---|---|
| Choosing or overriding `clinical_use_mode` | It is a gated property of a `Deployment` with a named approver (`MOS-SAFE-045`), not a per-run choice |
| Setting or adjusting `operating_point.score_threshold` | A threshold change is a new `ModelVersion` with its own `EvaluationRun`, never configuration (`MOS-REG-035`) |
| Selecting a `ModelVersion`, weights file or runtime | Presentation Plane MUST NOT know model identity (Chapter 2 §2.1) |
| Requesting a version outside what resolution returned | `MOS-REG-012`: resolution is pure and snapshot-bound |
| Overriding the `SeriesSelector`'s verdict | A pinned series set that fails the selector produces a `REJECTED` job and MUST NOT be silently re-selected (`MOS-API-045`) |
| Editing `input_constraints` | Prose for humans; the platform MUST NOT evaluate it (`MOS-SAFE-018`) |

**MOS-UI-020** The picker MAY call `GET /api/v1/studies/{study_id}/eligibility` (row 73) to show, before
submission, which entries can run on the open study. If it does, it MUST render an ineligible entry with
the non-error affordances of `MOS-UI-026` — row 73 is one of the two endpoints that return a synchronous
`clinical_rejection` (`MOS-API-047`) — and it MUST NOT disable submission on that basis alone. The
authoritative rejection is the one recorded on a `Job`.

#### 19.2.3 Submission

**MOS-UI-021** One activation MUST issue exactly one `POST /api/v1/jobs`, with the body of `MOS-API-043`:
`target: {kind, id, version_range?}` and `input.study_instance_uid` naming the study currently open in the
viewer. The surface MUST hold an in-flight guard keyed by the submission's content so that a double-click,
a re-render or a second activation issues no second request. The server-side guarantee is independent and
stronger — the platform-derived `jobs.idempotency_key` (`MOS-EXEC-053`) makes a repeat POST replay rather
than duplicate — but `MOS-SAFE-089a`'s acceptance check counts *requests*, so a surface that fires two and
relies on the server to discard one fails.

**MOS-UI-021a** The wire shapes are Chapter 10's. A surface MUST send `target` + `input` per `MOS-API-043`
and MUST read `Job.status` per `MOS-API-049`. The current extension client sends
`{study_instance_uid, capabilities}` and reads `body.state`, which is the weeks-1–2 slice's own contract
and is **not** the Chapter 10 shape; the divergence MUST be closed in the surface, not in Chapter 10.

**MOS-UI-022** The surface MUST NOT derive an `Idempotency-Key` from a `StudyInstanceUID`, a patient
identifier or any DICOM header value. It MAY send a key it generated; `MOS-API-029` keeps the header out
of UID derivation server-side, and sending a study-derived key places a PHI-adjacent identifier in a
header for no gain. More generally, `MOS-API-006` binds this surface: no direct-identifier PHI in a path,
a query string, a header or a rendered problem document.

**MOS-UI-023** Submission MUST be disabled, with an explanation, when the viewer does not have exactly one
study open. A control that guesses which of two open studies to analyse is a clinical hazard, not a
convenience. The disabled state MUST say why ("Open exactly one study to analyse it"), because a control
that is disabled without explanation is indistinguishable from a broken one.

**MOS-UI-024** A replayed submission MUST be rendered as a replay. `MOS-API-026` answers a repeat with the
stored response and `MedicalOS-Idempotent-Replay: true`; the surface MUST say "already running as
`job_…`" and MUST NOT report that a second analysis was started. Telling a reader that two analyses exist
when one does invites them to wait for a second result that will never arrive.

#### 19.2.4 While the job runs

**MOS-UI-025** While a job is non-terminal the surface MUST show exactly three things about its progress,
and MUST NOT show a fourth:

| Shown | Source | Rule |
|---|---|---|
| `status` | `Job.status` (`MOS-API-049`) | Closed enum; rendered as-is |
| `phase` | `Job.phase` (`MOS-API-050`, `MOS-EXEC-019`) | **Open** string. Rendered verbatim, including an unrecognised value. The surface MUST NOT branch on it and MUST NOT map it to anything |
| `steps_completed` / `steps_total` | `MOS-EXEC-020` | Rendered as a step counter. When `steps_total = 0` the job is not yet planned and the surface MUST render a counter, not a bar (`MOS-EXEC-021`) |

**MOS-UI-025a** The surface MUST NOT display a percentage, a progress fraction or a bar that is not
computed from `steps_completed / steps_total`, and MUST NOT display any of them when `steps_total = 0`. A
status-to-percentage lookup table is forbidden. There is no float `progress` field anywhere in the system
— not in the database, the API, the event envelope or the SDK — and the reason it was removed is in
`MOS-EXEC-021` and `MOS-API-051`: a number shown to a clinician must have a source, and a
status-to-number lookup shown to a clinician is a fabricated signal. `MOS-EXEC-024` permits a service to
report sub-progress into `job_steps.detail` for the `service_invoke` row; a surface MAY render that detail
verbatim and MUST NOT fold it into the job-level counters, which stay platform-owned precisely so that a
misbehaving vendor cannot fabricate platform progress.

**MOS-UI-025b** The surface MUST NOT display an estimated time remaining, an estimated completion time, or
a spinner annotated with a duration it did not measure. Nothing in the platform produces such an estimate.

**MOS-UI-026** The surface MUST follow the job to a terminal state by subscribing to
`GET /api/v1/jobs/{job_id}/events` (`MOS-API-059`) and MAY additionally poll, no faster than the
`Retry-After` returned on submission. It MUST re-read `GET /api/v1/jobs/{job_id}` to obtain what it
renders: an event says *that* something changed, the job document says *what it now is*, and rendering
from the event payload gives two representations of one job that can disagree mid-stream. It MUST NOT
reconnect after a terminal event (`MOS-API-064`), MUST discard any event whose `sequence` is not strictly
greater than the highest already applied (`MOS-API-066`), and MUST treat the normal end of a stream as an
end rather than as an error.

#### 19.2.5 The result screen

This is the screen the chapter exists for, and it has one rule above all others.

**MOS-UI-027** `REJECTED` and `FAILED` MUST be **visually and textually distinct**. `MOS-EXEC-014` states
what the two mean — `REJECTED` is *this study was not analysed, and that is a correct outcome*; `FAILED`
is *this study should have been analysed and the platform could not do it* — and states the harm:
"a radiologist who sees a red error where the truth is 'no thin axial recon exists in this study' will
either chase an IT ticket or, worse, assume the study was cleared." `MOS-EXEC-016` requires `REJECTED` to
be rendered with non-error affordances in every UI; `MOS-API-039` forbids any client, SDK or UI to render
`clinical_rejection` with the same affordance as `client_error`, `transport_failure` or `system_failure`,
and `MOS-API-100` is its code-level enforcement (a terminal `REJECTED` job MUST NOT raise in any SDK);
`MOS-SAFE-089a` makes the distinction a MUST for the viewer surface specifically. This chapter fixes the
presentation.

**MOS-UI-028** The two states MUST differ on at least **four independent channels**, of which at least one
MUST be textual. Colour alone is not distinct to a red-green colour-blind reader, and wording is the
channel that survives a stylesheet failing to load.

| Channel | `REJECTED` — a clinical outcome | `FAILED` — a technical outcome |
|---|---|---|
| Hue | non-alarm (the reference implementation uses amber) | alarm (red) |
| Container | open treatment — a rule, not a box | closed, fully bounded error box |
| Heading weight | normal | bold |
| Heading | "Not analysed" | "Analysis failed" |
| Body sentence | "The platform decided not to analyse this study. This is a result, not a failure." | "The platform could not complete the analysis. This says nothing about the study." |
| Closing sentence | "Nothing is broken, and re-running will not change the outcome." | "The study has not been assessed. Re-running may succeed." |
| Iconography | non-alarm glyph | error glyph |

The exact hues and glyphs are a design choice; the number of channels, the presence of a textual channel,
and the two propositions the copy must carry — *neither "the platform is broken" nor "the study was
cleared"* — are not. The reference implementation is `REJECTED_COPY` and `FAILED_COPY` in
`medos/web/ohif-extension/src/core/render.js`, driven by the single decision point `outcomeKind()` in
`medos/web/ohif-extension/src/core/client.js`.

**MOS-UI-029** The state's classification MUST be decided in exactly one place in the surface's code, from
`Job.status` and `problem.class`, and every renderer MUST ask that one function. A surface that
string-matches a state in more than one component will eventually disagree with itself, and the
disagreement will be invisible until it is a rejection painted red.

**MOS-UI-030** A rejection MUST be presented as a **clinical sentence**, with the machine-readable code
beside it and never instead of it. Specifically the screen MUST show:

1. The heading and body of `MOS-UI-028`.
2. A sentence, in clinical language, keyed by `Job.rejection.code` — the SCREAMING_SNAKE form of
   `jobs.reject_reason_code` (`MOS-API-040`, Chapter 5 §5.3.1).
3. `Job.rejection.code` rendered **verbatim** in a monospace element — never prettified, never
   title-cased, never translated, never mapped onto a different vocabulary. "Verbatim" is a testable word
   and `MOS-SAFE-089a` uses it.
4. `Job.rejection.detail`, which is the publisher's or the platform's instance-specific explanation and is
   PHI-free by `MOS-API-036`.
5. The `trace_id`.

**MOS-UI-030a** The sentence table MUST be a fixed, reviewed resource versioned in the repository, keyed
by the closed job-level vocabulary of Chapter 5 §5.3.1. It MUST NOT be generated, MUST NOT be produced by
a language model, and MUST NOT be editable per tenant except by addition. A code with no row MUST render
the code plus a generic sentence — "This study was not analysed. The reason code is shown above." — and
MUST NOT render a blank space, because an empty explanation is read as an error.

| `rejection.code` | Clinical sentence the surface MUST show |
|---|---|
| `NO_ELIGIBLE_SERIES` | "This study contains no series that this analysis can read, so nothing was analysed." |
| `NO_CANDIDATE_SERVICE_VERSION` | "No version of this analysis is deployed at this site, so nothing was run." |
| `OUTSIDE_APPLICABILITY_ENVELOPE` | "The selected series is outside the acquisition range this service was evaluated on, so it was not analysed." |
| `UNSUPPORTED_GEOMETRY` | "This series has an acquisition geometry the service cannot process, so it was not analysed." |
| `INPUT_CONSTRAINT_UNMET` | "This study does not meet a condition the service requires, so it was not analysed." |
| `POLICY_DENIED` | "This site's configuration does not permit this service to run on this study. Nothing was analysed." |
| `SERVICE_DECLINED` | "The service examined this study and declined to report a result." |

**MOS-UI-030b** Every rejection presentation MUST carry, adjacent to the sentence, the non-negation
clause: **the absence of a result is not a negative finding.** This is the single sentence that closes the
"assume the study was cleared" failure mode of `MOS-EXEC-014`, and it MUST NOT be collapsed into a tooltip
or a disclosure.

**MOS-UI-031** A `REJECTED` job MUST NOT be offered a retry control. `MOS-EXEC-015` forbids retrying it,
forbids it consuming retry budget, forbids dead-lettering and forbids an on-call alert; `MOS-EXEC-012`
makes it absorbing. A `FAILED` job MAY be offered a retry, which MUST be the explicit, permissioned,
audited operator retry of transition T13 and MUST NOT recompute the idempotency key or re-run resolution.

**MOS-UI-032** A `REJECTED` result screen MUST link to `GET /api/v1/jobs/{job_id}/series-selection`
(`MOS-API-054`) and MUST be able to render, on one interaction, every evaluated series with its
`decision`, `reason_code` and `reason_detail`. Selection is first-class data, not a log line, and the most
common silent clinical failure in radiology AI is analysing the wrong reconstruction (`MOS-SAFE-084`) —
so the screen must be able to answer "what else was on the table". The series-level vocabulary is Chapter
3's closed `SeriesRequirement` list (`MOS-DATA-069`) and is a **different list** from the job-level
vocabulary above; the surface MUST NOT merge them or render one through the other's sentence table.

**MOS-UI-033** A `FAILED` result screen MUST show `Job.error.class` (`transport_failure` or
`system_failure`), `Job.error.code`, the PHI-free `detail`, and the `trace_id`. It MUST NOT show a stack
trace, SQL text, an internal hostname, a file path or any part of the request body — `MOS-API-042` makes
the `trace_id` the entire diagnostic handoff. It MUST NOT show the internal `failure_class` taxonomy of
Chapter 5 §5.3.2, whose twelve values never appear on the wire (`MOS-EXEC-016a`).

**MOS-UI-034** Neither a `REJECTED` nor a `FAILED` screen may state or imply that the study is normal,
negative, clear, unremarkable or free of a finding, and neither may state or imply that a finding was
excluded or ruled out. The assertive-diagnosis patterns of `MOS-SAFE-080` — `rules out`, `excludes`,
`confirms`, `is definitely` — bind UI strings as surely as generated narrative, and the claims lint of
`MOS-SAFE-008` already greps UI strings and i18n bundles.

**MOS-UI-035** A `COMPLETED` result screen MUST name every generated DICOM object from
`GET /api/v1/results/{result_id}` (`MOS-API-055`): for each, `kind`, `series_instance_uid`,
`sop_instance_uid` and its Gateway-relative WADO-RS href. "Where did the output go" must be answerable
from the surface alone. It MUST NOT fetch or render the pixels itself ~~(`MOS-UI-009`); handing the object
to the adopted viewer is the whole point of writing standard objects.~~ — **AMENDED at specification
0.4.0** — it MUST hand each object to the surface's own viewport, which fetched it once through the
Gateway and decoded it once (`MOS-UI-009a`).

**Why this requirement survives the withdrawal of `MOS-UI-009` and is re-pointed rather than struck.** The
question is whether the pixel clause was about this screen or about the surface, and the text answers it.
The subject of the sentence is "A `COMPLETED` result screen"; the requirement sits among `MOS-UI-031`
through `MOS-UI-034`, every one of which binds a screen and none of which binds the surface; and the
struck clause described a handoff from one component to another — the adopted viewer was the host of this
screen, not a foreign product it was deferring to. What it forbade was the result panel becoming a second
path to the pixels beside the viewport's, and that hazard is not only unchanged by `MOS-UI-009a` but
sharper, because the surface now contains a renderer for the panel to duplicate. Two fetches of one SEG,
two decodes and two alignment computations give a reader two renderings of one object with nothing on
screen to say which one they are looking at — which is what `MOS-UI-202`'s OHIF row means when it says the
second-viewer gate is "designed to detect, not to institutionalise" two renderings of the same SEG. The
MUST NOT is therefore carried over at full strength with a different object on the other side of the
handoff. `viewer/src/ui/segments-panel.js` is what satisfying it looks like: a segment row toggles the
visibility of an overlay that `src/render/viewport.js` draws from a decode `src/image/seg.js` performed
once, and the panel issues no request of its own.

#### 19.2.6 The provenance panel

**MOS-UI-036** The provenance panel MUST render, **without a second request**, at least the list
`MOS-SAFE-088` fixes: service identity and manufacturer; `clinical_use_mode`; the consumed series list
with the rejected ones collapsed but present; preprocessing and model versions; the operating threshold;
the evidence dataset version and validation report id; and the output object UIDs with their storage
locations. "Collapsed but present" is binding: the rejected series may be behind a disclosure, and MUST
NOT be absent.

**MOS-UI-037** Where a field `MOS-UI-036` requires is absent from the API response, the panel MUST render
the row with an explicit "not recorded" marker rather than omit it. A panel that renders only the fields
that happen to be present cannot distinguish "the platform recorded no worker version" from "the panel
forgot to display it" — and `MOS-STORE-278` ("a result without provenance MUST NOT be observable through
any API") is exactly the claim that has to fail loudly when it is false. The reference implementation
carries this as `REQUIRED_RESULT_FIELDS` and `missingProvenanceFields()` in
`medos/web/ohif-extension/src/core/provenance.js`.

**MOS-UI-037a** The panel MUST also show, when the two disagree, that a result's provenance does not name
exactly the objects that result wrote. Both halves are defects and both matter: a stored object with no
provenance entry means something is in the archive under this job's identity that no record accounts for;
a provenance entry with no stored object means a reviewer following the record gets a 404
(`MOS-SAFE-085`'s point that a STOW-RS 200 is not proof of landing is the same concern one step earlier).

**MOS-UI-038** The panel MUST NOT display PHI. Every field it renders is a UID, a version string, a code,
a count or a timestamp; `MOS-SAFE-086` forbids the provenance record itself to carry `PatientName`,
`PatientBirthDate`, free-text clinical history, a copied `StudyDescription` or an accession number,
because the record is exported and must be safe to hand a reviewer. Values reaching the DOM MUST be
inserted as text, never as interpolated markup — the values are the platform's own codes today, and this
is the habit that keeps it safe when the panel begins rendering a `reason_detail` that originated in a
DICOM header.

**MOS-UI-039** The panel MUST offer the provenance record itself
(`GET /api/v1/results/{result_id}/provenance`, `MOS-SAFE-087`) and the signed, offline-verifiable export
(`GET /api/v1/results/{result_id}/provenance/export`, `MOS-SAFE-089`) as retrievable artifacts. It MUST
NOT re-serialise, re-order or re-canonicalise the export: the bundle's verifiability rests on RFC 8785
canonical JSON plus a detached signature, and a surface that rewrites it destroys the property it exists
to carry.

#### 19.2.7 AI-derived and research-use marking

**MOS-UI-040** The five facts of `MOS-SAFE-012` — `service_id`, the `ServiceVersion`,
`legal_manufacturer.name`, the producing deployment's `clinical_use_mode`, and the `Result`'s
`review_status` — MUST be displayed **adjacent to the finding and without requiring interaction**, on the
result itself. Putting them in the provenance panel is necessary and not sufficient: a panel is a second
place the reader must go, and `MOS-SAFE-012` says "without requiring interaction". `MOS-SAFE-053`
guarantees these arrive at the top level of the `Result` JSON precisely so that client code cannot omit
the marking by accident.

**MOS-UI-041** A result produced under `clinical_use_mode: research_only` MUST carry a visible RUO badge
wherever the finding is shown. The surface MUST NOT rely on the burned-in SC banner of `MOS-SAFE-051` for
this: that banner exists so the warning survives a screenshot, a PDF export or a viewer that ignores
metadata, and a MedicalOS-controlled surface is not permitted to be such a viewer.

**MOS-UI-042** The result feed MUST default to `clinical_use_mode = clinical` results only. Including
research results MUST require both `include_research=true` and the permission `result.read.research`
(`MOS-SAFE-041`, E5). A surface MUST NOT set `include_research=true` by default, MUST make the inclusion
visible in the UI while it is in force, and MUST render the RUO badge on every included item. This is the
read-path filter that prevents a staging or research deployment's output from appearing in a radiologist's
viewer, which Chapter 8 names as the exact hole the previous version left open.

**MOS-UI-043** `review_status` MUST be rendered on the result, including the value `REJECTED`. A
`REJECTED` review does not delete or hide the result: the reviewer's disagreement is data, not an erasure
(`MOS-SAFE-064`). A surface MUST NOT filter out, grey out to illegibility, or collapse a result on the
basis of its review status.

**MOS-UI-043a** The clinician surface MUST NOT write `review_status`. It is a derived projection of the
highest-round `ResultReview`, maintained in the same transaction as the review transition and not writable
through any endpoint (`MOS-SAFE-062`, `MOS-API-055`). Where the surface offers review at all it MUST do so
through the `result-reviews` endpoints of `MOS-SAFE-069`, as a claimed, attributed, recorded human act.

**MOS-UI-044** The clinician surface MUST NOT present a model output as a diagnosis. Concretely it MUST
NOT:

- render a finding as an assertion about the patient rather than an output of a named service — every
  finding carries `derivation: "ai_derived"` and the manufacturer identity (`MOS-SAFE-053`,
  `MOS-CORE-004`);
- use the vocabulary `MOS-SAFE-008` bans, or the phrasings Chapter 1 §1.4 replaces — "the system
  diagnoses" and "AI diagnosis" become "AI-derived finding" and "AI-derived measurement";
- display a measurement as a bare number: every measurement outside DICOM carries
  `{value, unit, concept, derivation, score_threshold, service_version}`, `unit` is a coded concept and
  never a bare string, and a bare number is never a valid measurement anywhere in MedicalOS
  (`MOS-SAFE-054`);
- display a sensitivity, specificity or F1 without the `operating_point.score_threshold` at which it was
  measured (`MOS-REG-034`);
- present a result as verified. Research-mode SRs are written `UNVERIFIED` with no
  `VerifyingObserverSequence` (`MOS-SAFE-042`), and an SR carrying `VerificationFlag = VERIFIED` without a
  human `ResultReview` is a hard DENY (`MOS-SAFE-073` row 13).

**MOS-UI-045** The clinician surface MUST NOT auto-action a result. Results are stored, marked and visible
under the E5 filter, and **nothing is auto-actioned** (`MOS-SAFE-057`). The surface MUST NOT page, SMS,
escalate, notify a clinician of a finding, write to an external clinical system, enqueue a further job on
a result's arrival, or place a result into a worklist ordered by model score. Asserting a finding to a
clinician by page, SMS or escalation is DENY with no transport wired (`MOS-SAFE-073` row 12), and
`MOS-CORE-049` is the non-goal underneath both: the platform produces artifacts and records review, and
takes no clinical action.

**MOS-UI-046** The surface MUST NOT offer, and the platform MUST NOT accept from it, a request to run a
`ServiceVersion` whose `intended_use.autonomy` is `autonomous`. The refusal is a hard one at deployment
time (`MOS-SAFE-011`, `MOS-SAFE-073` row 19); the surface's obligation is not to build an affordance that
implies such a service could exist here.


### 19.3 The engineering surface — no-code training — **CUT at specification 0.4.0**

**CUT at specification 0.4.0, and the whole section is cut with this heading.** No engineering surface
ships. The requirements below — from `MOS-UI-100` through the end of the section — are withdrawn in
their entirety and retained, not struck line by line, as the record of a design this repository built
once (`medos/web/training-console`, deleted 2026-10-02) and does not ship; the machine interface to
the training pipeline in their place is the Chapter 10 training-plane API (its `expert` surface), and
the withdrawal itself is recorded in the `MOS-UI-001` amendment in §19.1.1 and in
`docs/spec/99-known-inconsistencies.md` entry 150. Where the text below names paths — the console
package, its refusal catalogue, the tests that walked both — they are history, not addresses.

The clinician surface of 19.2 assumes a trained model exists. This section specifies the surface that produces one, and it is written against a single constraint that determines every requirement in it:

> **The operator has no programming background.** Not a junior ML engineer, not a research fellow who writes a little Python. A clinical domain expert — a chest radiologist, a pulmonologist, a senior radiographer — working alone, without an engineer beside them, without a terminal open, without anyone to ask what a patch size is.

This is not a statement about visual design. It changes what the platform must *refuse to do*, because a surface built for that operator cannot fall back on the escape hatch every ML tool relies on: when something is ambiguous, ask the user. This user cannot answer. A dialogue that says *your cohort is unbalanced — proceed anyway?* is not a safety control in their hands; it is a button that says *yes*.

What makes the surface possible is that the platform already refuses on its own. Chapters 7 and 17 built five machine-checked, blocking gates and none of them is advisory:

| Gate | Owner | Blocks | Waivable |
|---|---|---|---|
| L1–L5 leakage checks | `MOS-EVID-034` | split freeze | L4 only, and never for training (`MOS-TRAIN-115`) |
| Corpus stratification check C1–C7 | `MOS-TRAIN-088` | dataset seal | C4, C6 are `warn`; the rest block (`MOS-TRAIN-092`) |
| Patient-level frozen split | `MOS-EVID-028`, `MOS-EVID-029`, `MOS-STORE-293` | structurally — a patient is one row | not applicable |
| Locked `test` partition | `MOS-EVID-033`, `MOS-TRAIN-216` | selection reading `test` | no |
| Human promotion gate | `MOS-TRAIN-008`, `MOS-TRAIN-010`, `MOS-SEC-158` | `VALIDATED → APPROVED` | no |

For an expert audience those are guardrails — things you mostly do not touch. For this audience **they are the product**. The operator's competence is clinical: which cases belong in the cohort, whether two images are the same acquisition, whether a mask is right. The operator's incompetence is statistical: whether 561 of 812 patients from one hospital is too many. The gates cover exactly the second set, and the surface's job is to render their verdicts in language the first set can act on.

#### 19.3.1 What this surface is, and what it refuses to be

**MOS-UI-100** — **AMENDED at specification 0.4.0.** The engineering surface MUST be a separate deployable from the clinical viewer, delivered as `medos/web/training-console`, and MUST NOT be implemented as any part of the clinician surface — not ~~as an OHIF extension~~ as an extension to an adopted viewer, and not as a module, panel, route or toolbar entry of the first-party viewer at `viewer/`. ~~Two independent reasons hold.~~ Chapter 17 keeps the pipeline off the serving path (`MOS-TRAIN-001`, `MOS-TRAIN-004`), and a training control rendered inside the viewer ~~bundle~~ puts a pipeline entry point in the clinical runtime. ~~Separately, the viewer image the stack ships (`ohif/app:v3.9.2`, pinned in `medos/deploy/compose/docker-compose.yml`) is a pre-built single-page app that resolves extensions from its own webpack bundle at build time — verified in `medos/web/ohif-extension/README.md`, which is why `deploy/compose/ohif-config.js` ships `extensions: []`. Hosting the console there would require rebuilding OHIF from its monorepo to add a surface that has no reason to live in a viewer.~~

**The separation is the property; the second reason for it is what went, and losing it costs something.** The struck paragraph was never a reason at all — it was a fact about a pre-built image, and no image is deployed to be a fact about any more. But that fact did real work while it held: hosting the console inside the viewer was not merely forbidden, it was impossible without a monorepo rebuild nobody had budgeted, and a prohibition backed by an impossibility is a prohibition nobody has to obey deliberately. It is now easy to violate. `viewer/` has no build step and no bundle — native ES modules served from a directory — so putting a training panel on the clinician surface is an import line and an afternoon. The requirement therefore loses its accidental enforcement at the exact moment the violation becomes cheap, and the enforcement has to become explicit rather than assumed: `tests/unit/test_training_console_source.py` already asserts both halves against the old host, that the console is its own package at `medos/web/training-console` and that no viewer surface area appears inside it, and it MUST be extended to assert the same against the new one — no module under `viewer/` importing from `medos/web/training-console`, and no module under `medos/web/training-console` importing from `viewer/`. `MOS-UI-170`'s call-graph assertion is the same shape one layer up and is the precedent for writing it that way. The first reason is untouched and is now carrying the whole weight, which is the honest description of this amendment rather than a reassurance about it.

**MOS-UI-100** — **CUT at specification 0.4.0.** The engineering surface this requirement delivers is
withdrawn with §19.3; `medos/web/training-console` is deleted and the paragraph above names tests
that were deleted with the tree. The separation property the requirement guarded survives in
negative form and is not cut: the clinician surface at `viewer/` MUST NOT acquire a training
control, panel, route or toolbar entry, and the enforcement of that half no longer needs a
dedicated test — a training control in `viewer/` would appear as an import or a route in a tree
this repository's own gates walk. The surface itself may return only under a new minor version of
this chapter (`MOS-CORE-036`).

**MOS-UI-101** The console MUST NOT require the operator to read or write code, YAML, JSON, SQL, a query expression, a regular expression, a file path, a digest, a UID or a requirement identifier in order to complete any task the console offers. Where one of those values is load-bearing it MUST be rendered as data the operator can copy, never as data the operator must compose. A screen that cannot satisfy this MUST NOT be part of this surface; it belongs to a different surface with a different audience.

**MOS-UI-102** Every action the console offers MUST be authorised by a permission of class `read`, `write` or `phi` from Chapter 8's catalogue. The console MUST NOT contain a screen, a control or a code path requiring a permission of class `governance` or `clinical`. The seeded `evidence_scientist` role (`dataset.*`, `dataset_version.*`, `dataset_split.read/freeze`, `annotation_set.*`, `evaluation.read/run`, `model_version.read`, `study.read`, `study.read_pixels`, `capability.read`) is the reference principal, and CI MUST assert that the union of permissions declared by the console's routes is a subset of that role plus the harvest and curation permissions of `MOS-UI-103`. This is the structural form of "this operator cannot promote": not a hidden button, an absent grant. `MOS-SEC-042` already forbids a seeded role holding both `validation_report.create` and `validation_report.approve`, and `MOS-SEC-158` already excludes `artifact.approve` and every `deployment.*` permission from the pipeline identity.

**MOS-UI-103** The console MUST NOT ship before the routes it depends on exist. `MOS-API-112` reserves `POST /api/v1/harvest-batches`, `GET` and `POST /api/v1/harvest-candidates/{harvest_candidate_id}/decision`, `PUT /api/v1/tenants/{tenant_id}/training-policy` and `GET /api/v1/harvest-batches/{harvest_batch_id}/stratification`, states that they MUST NOT be served, and notes that Chapter 8 registers no harvest, curation or training-policy permission — and `MOS-API-005` admits no route without one. Every screen in this section sits on those four routes plus a split-preview route that does not yet exist in any form. The console MUST be built against `/api/v1` only: it MUST NOT call the orchestrator port of `MOS-TRAIN-122` directly, MUST NOT read the evidence tables directly, and MUST NOT reach the batch environment by any path that bypasses the API's authorisation. Until table 10.2-B carries the rows, `medos/schemas/` carries the bodies and `medos/contracts/permissions.yaml` carries the keys (`MOS-SEC-032`), this section specifies a surface that cannot be served, and that is the correct state — not a licence to serve it another way.

**MOS-UI-104** Implementers MUST bind the seal action to Chapter 8's registered permission spelling. Chapter 10 row 38 (`POST /api/v1/dataset-versions/{dataset_version_id}/seal`) declares `dataset.write`, which `08-security.md` lists among the spellings that are **not** permission identifiers and that MUST fail the build under `MOS-SEC-033`; `99-known-inconsistencies.md` records the collision across twenty-one route rows. `MOS-SEC-158` fixes the mapping: sealing a dataset version is `dataset_version.create`. The console MUST NOT propagate the defective spelling into its route declarations.

##### The refusal contract

This is the requirement the rest of the section depends on. A gate that blocks correctly and explains badly is, for this operator, a gate that blocks arbitrarily — and an operator who experiences a control as arbitrary routes around it, usually by finding someone with more permissions.

**MOS-UI-105** Every refusal the console renders MUST carry exactly four parts, in this order, and MUST NOT carry a fifth:

| Part | Content | Constraint |
|---|---|---|
| **What was refused** | one sentence naming the operator's action, in their vocabulary | MUST NOT contain a requirement id, a check id, a field name, a digest or a UID |
| **Why** | the specific facts that triggered it, rendered from data | MUST be the actual observed values and the actual bound, never a restatement of the rule in the abstract |
| **What would fix it** | an action, with the arithmetic already done | MUST name an action reachable on this surface, or MUST name the role that can perform it |
| **Technical detail** | check id, requirement id, machine `code`, the raw statistic | MUST be in a collapsed region, closed by default |

**MOS-UI-106** A refusal MUST NOT present an override control where no override exists, MUST NOT present a disabled override control, and MUST NOT mention that an override exists elsewhere. A greyed-out **Proceed anyway** is not a safety feature; it is an instruction to go and find someone who can press it.

**MOS-UI-107** A refusal MUST NOT ask the operator a question they cannot answer. *This cohort is 69 % single-site — continue?* is such a question. *You have 561 patients from Site A and 251 from everywhere else; the limit is 60 % from any one site. Add 123 patients from other sites, or remove 185 from Site A* is not: it is a statement of fact and two executable actions. The console MUST compute the remedy, not describe the constraint.

**MOS-UI-108** Refusal copy MUST be declared as data in one module, `medos/web/training-console/src/refusals/`, as frozen exported objects, and MUST NOT be assembled by string concatenation at the call site. This follows the pattern already shipped in `medos/web/ohif-extension/src/core/render.js`, where `REJECTED_COPY` and `FAILED_COPY` are `Object.freeze`d exports precisely so the wording "can be read, reviewed and asserted". The shape is fixed:

```js
// medos/web/training-console/src/refusals/corpus.js
export const C1_SITE_CONCENTRATION = Object.freeze({
  check_id:    'C1',
  requirement: 'MOS-TRAIN-088',
  code:        'CORPUS_SITE_CONCENTRATION',
  title:       'Too many of these patients come from one hospital',
  why:         'why(observed, bound)',      // a pure function of the check result
  remedy:      'remedy(observed, bound)',   // returns actions, not prose about actions
  actions:     ['add_patients', 'exclude_patients'],
  override:    null,                        // null means there is none, anywhere
});
```

CI MUST assert three properties over this module: that every blocking check in the closed set `{C1, C2, C3, C5, C7, L1, L2, L3, L4, L5, test_partition_floor, seeded_ceiling, corpus_generation, geometry_unsupported, acceptance_binding_absent}` has an entry; that no `title`, `why` or `remedy` output contains the substring `MOS-` or a `sha256:` prefix; and that every entry whose platform check admits no waiver declares `override: null`. The third assertion is the one that catches drift: if Chapter 7 or 17 later adds a waiver, the console's claim that there is none becomes a test failure rather than a lie.

**MOS-UI-109** Refusal text MUST be a pinned string selected by check id, and MUST NOT be generated by a language model at render time. The operator's entire basis for trusting a refusal is that the same failure always produces the same words; a generated explanation of a leakage gate is an explanation nobody reviewed, in a register nobody chose, that may differ between two operators looking at the same cohort.

**MOS-UI-110** When the tenant's `training_use_allowed` is false (`MOS-TRAIN-072`), the console's first screen MUST render that state under the refusal contract of `MOS-UI-105` and MUST NOT render an HTTP status, a problem document or the string `403`. The remedy part MUST name the role that records a `TrainingDataPolicy` (`MOS-TRAIN-073`) and MUST state that there is no per-study, per-user or per-environment override and no platform-administrator bypass, because `MOS-TRAIN-072` says there is none and an operator who believes one exists will spend a week looking for it. The console MUST NOT offer a screen that edits the policy: `PUT /api/v1/tenants/{tenant_id}/training-policy` is reserved by `MOS-API-112` and the assertion it records is the site's legal one, not this operator's.

#### 19.3.2 Cohort builder

**MOS-UI-111** Cohort selection MUST be faceted. The console MUST NOT offer a query language, a free-text search box over metadata, a regular-expression field, a boolean expression builder, or a saved-query editor. Every constraint MUST be expressed by choosing among values the archive actually contains.

**MOS-UI-112** The facet set MUST be exactly the fields that the seal-time checks and the split generator read, and nothing else. This is the requirement that makes "visibly failing before seal" achievable rather than aspirational: a facet the checks do not read lets the operator build a cohort that fails for a reason they never saw, and a field a check reads but the operator cannot see is a failure they cannot fix.

| Facet | Source field | Read by |
|---|---|---|
| Modality | `modality` | applicability; `MOS-DATA-059` |
| Body part | `body_part_examined` | applicability; `MOS-DATA-059` |
| Site | `institution_key` | C1, C7 (`MOS-TRAIN-088`) |
| Scanner | `manufacturer`, `manufacturer_model_name` | C2; split stratification (`MOS-TRAIN-113`) |
| Reconstruction | `convolution_kernel_class` | C3 |
| Slice thickness | `slice_thickness_mm` | C4, C5; split stratification |
| In-plane resolution | `pixel_spacing_mm` | C5; envelope (`MOS-EVID-098`) |
| Contrast | `contrast_phase` | C5; envelope |
| Study year | `study_year` | C6 |
| Patient age, sex | `patient_age_years`, `patient_sex` | strata (`MOS-EVID-067`) |
| Coverage, instance count | `z_coverage_mm`, `instance_count` | envelope (`MOS-EVID-024`) |
| Prior outcome | `review_outcome`, `score_band`, `ran_on_platform` | `SamplingPlan` (`MOS-TRAIN-083`) |

Every field above is already required on a `HarvestCandidate` at curation time by `MOS-TRAIN-084`, copied from the source header without imputation with a missing value serialised as `null` (`MOS-EVID-020`). The console MUST render `null` as a distinct facet value — *not recorded* — and MUST NOT merge it into a default, because `MOS-TRAIN-084` records it separately for exactly the reason the operator needs to see it separately.

**MOS-UI-113** The console MUST NOT display an institution name. `MOS-TRAIN-089` makes `institution_key` an HMAC and `MOS-EVID-116` keeps the raw name out of every candidate, manifest and report. An HMAC is unreadable, and C1 is unreasonable about a value the operator cannot distinguish. The console MUST therefore assign each distinct `institution_key` a stable tenant-local display label in order of first appearance within the batch — *Site A*, *Site B* — persist the assignment with the batch so the labels do not shuffle between sessions, and MUST NOT offer any control that resolves a label to a name.

**MOS-UI-114** Every facet value MUST display its patient count before it is selected, and a value with zero matches MUST be shown as zero rather than omitted. An omitted value is indistinguishable from a value the archive does not contain, and the difference between *this archive has no 0.6 mm studies* and *your current filters exclude them* is the difference between two entirely different remedies.

**MOS-UI-115** The console MUST render a **composition panel** that updates as the selection changes, showing at minimum:

| Row | Value | Bound shown beside it |
|---|---|---|
| Patients, studies, series | `patient_count`, `study_count`, `series_count` | — |
| Largest site share | C1 statistic | ≤ 0.60 |
| Largest scanner share | C2 statistic | ≤ 0.70 |
| Reconstruction classes at ≥ 10 % | C3 statistic | ≥ 2 |
| Thickness spread | C4 statistic | ≥ 3 values or p90/p10 ≥ 1.5 |
| Thinnest envelope cell | C5 worst cell count | ≥ 20 patients |
| Largest single year share | C6 statistic | ≤ 0.75 |
| Distinct sites | C7 statistic | ≥ 2 for portable evidence |
| Projected split | `partition_patients` for `train`/`tune`/`test` | `test` ≥ 30 patients |
| Exclusions so far | count per `reason_code` | — |

**MOS-UI-116** The composition panel MUST be computed by the server, by the same implementation that runs at seal, and MUST NOT be reimplemented in the browser. C1–C7 MUST come from `GET /api/v1/harvest-batches/{harvest_batch_id}/stratification` (`MOS-API-112`), and the projected split MUST come from a server-side evaluation of `assign()` in `medicalos/pipeline/split.py` (`MOS-TRAIN-112`) over the current candidate set. A second implementation in JavaScript would disagree with the authoritative one on exactly the cohorts that sit near a bound — which is every cohort where the preview matters.

**MOS-UI-117** The composition panel MUST be labelled a preview, MUST state that the checks run again at seal, and the seal-time result MUST be authoritative. `MOS-TRAIN-093` computes the stratification report from `acquisition_profile` alone and requires it to be recomputable from the sealed manifest; the preview computes the same statistics over a set that is still changing, which is a different object with the same arithmetic.

**MOS-UI-118** The console MUST make materialisation visible. A cohort under construction is a set of `HarvestCandidate` rows (`MOS-TRAIN-078`), never a stored query — `MOS-EVID-016` forbids a DatasetVersion defined by a live query, folder path, DICOM filter or database view. The console MUST NOT present the facet selection as something that will be "re-run later", and MUST NOT offer to save a filter as a reusable cohort definition.

**MOS-UI-119** Every candidate MUST receive an explicit `CurationDecision` by the named operator before it can enter the cohort (`MOS-TRAIN-080`). The console MUST NOT offer a *select all and include* control that writes decisions without the cases having been shown. Auto-*exclusions* on a mechanical predicate — gantry tilt rejected, non-uniform spacing, missing series, duplicate `series_pixel_digest` — are permitted, and MUST be displayed in the queue as exclusions carrying the predicate identity, never as absences.

**MOS-UI-120** `reason_code` MUST be presented as labelled choices drawn from the closed set of `MOS-TRAIN-080` — `quality_artefact`, `wrong_anatomy`, `wrong_phase`, `prior_treatment`, `duplicate_patient`, `geometry_unsupported`, `annotation_infeasible`, `out_of_scope` — each with a one-line plain-language gloss. It MUST NOT be a free-text field. The `note` field MAY be free text and MUST carry a visible instruction that it contains no patient-identifying text.

**MOS-UI-121** The console MUST display the running exclusion tally by `reason_code` beside the cohort size, because `MOS-TRAIN-081` requires that tally to be reproduced in every `ValidationReport` citing the cohort. A cohort assembled by excluding a third of its candidates as `quality_artefact` describes a different population from the one the model will meet, and the operator making those exclusions is the person best placed to notice it happening — but only if the number is in front of them while they do it, not in a report six weeks later.

**MOS-UI-122** The console MUST NOT display any field `MOS-TRAIN-079` forbids on a candidate: `PatientName`, `PatientBirthDate`, `AccessionNumber`, institution free text, or any source-space UID. Where the operator needs to identify a case to a colleague, the console MUST offer the de-identified study UID and the `patient_key`, and MUST state that `patient_key` is meaningless outside this tenant (`MOS-EVID-010`, `MOS-EVID-011`).

#### 19.3.3 Annotation assignment

The annotation surface itself is specified in 19.4. This subsection specifies only the hand-off: how a cohort becomes a set of reading assignments, and what the console must refuse to configure.

**MOS-UI-123** Assignment MUST name individual readers as MedicalOS `User` principals. `MOS-TRAIN-096` requires every MONAI Label session to authenticate as one, because `MOS-EVID-038` requires the `AnnotationSet` to name its readers and a shared account makes that unsatisfiable after the fact. The console MUST NOT offer assignment to a group, a rota, a shared login, a service account or an email address.

**MOS-UI-124** The console MUST collect Chapter 7's reader fields as structured inputs at assignment time and MUST NOT invent parallel ones (`MOS-TRAIN-097`): `role` ∈ {`radiologist`, `resident`, `algorithm`, `registry_extract`}, `years_experience`, `board_certified`, `specialty`, `tool`, `instructions_uri`, `blinded_to`. `tool` MUST be filled by the platform from the deployed annotation stack, not typed by the operator.

**MOS-UI-125** When two or more readers are assigned to the same cases, the console MUST state that inter-reader agreement will be computed and will appear in every report citing the set (`MOS-EVID-043`), and MUST NOT present multi-reader assignment as merely a throughput choice. A Dice of 0.82 against a reference whose own inter-reader Dice is 0.84 is a different claim from the same 0.82 against 0.97, and the operator choosing the reader count is choosing which of those two statements the platform will be able to make.

**MOS-UI-126** If the campaign seeds readers with model output, the console MUST require the de-novo control arm of `MOS-TRAIN-101` (at least 0.10 of cases, drawn by the `SamplingPlan` rather than chosen by readers) and the paired anchoring sub-study of `MOS-TRAIN-102` to be configured in the same action. It MUST NOT offer them as optional extras, and MUST NOT allow a seeded campaign to be created without them. `MOS-TRAIN-100` makes `seed_dice` the direct measurement of the anchoring being defended against, and a campaign that discovers it has no control arm has already produced labels it cannot characterise.

**MOS-UI-127** The console MUST display the model-seeded fractions per partition against the ceilings of `MOS-TRAIN-086` — `train` ≤ 0.50, `tune` ≤ 0.25, `test` 0.00 for `model_seeded_corrected`, and 0.00 everywhere for `model_output_unreviewed` — and MUST refuse any assignment that would place a seeded case into the `test` partition. Exceeding a ceiling blocks the split from freezing rather than warning; refusing at assignment time is the same rule enforced where the operator can still act on it.

**MOS-UI-128** A case with `corpus_generation >= 2` MUST NOT be assignable and MUST be shown as an exclusion with a plain-language reason — its labels descend from a model that was itself trained on model-derived labels, twice removed — rather than silently omitted (`MOS-TRAIN-087`).

**MOS-UI-129** Closing a campaign MUST be an explicit action that freezes the `AnnotationSet` (`MOS-TRAIN-110`), and the console MUST state, before the action, that a frozen set cannot be appended to and that adding a reader or a case afterwards produces a new set.

#### 19.3.4 Seal — the refusal surface

This is the most important subsection in the chapter. Sealing is where a mutable working set becomes an immutable `DatasetVersion` plus a frozen `DatasetSplit`, and it is where the platform's gates fire. Everything before it is reversible; nothing after it is.

**MOS-UI-130** **Seal cohort** MUST be one operator action, and the console MUST NOT expose the fact that it spans two platform operations — sealing the `DatasetVersion` (`MOS-EVID-015`, running C1–C7 per `MOS-TRAIN-088`) and freezing the `DatasetSplit` (`MOS-EVID-028`, running L1–L5 per `MOS-EVID-034`) — except where a refusal comes from one of them and the distinction is part of the remedy.

**MOS-UI-131** The console MUST run the complete check battery **before** it creates anything, and MUST perform the seal only if every blocking check passes. This ordering is stricter than the platform requires — `MOS-TRAIN-088` runs C1–C7 during the seal and `MOS-EVID-034` runs L1–L5 at freeze, both after the point of no return — and the console MUST be stricter for a specific reason. A `DatasetVersion` that seals and then fails its split freeze is immutable (`MOS-EVID-013`), undeletable (`MOS-API-011` returns `409 DATASET_VERSION_SEALED`) and useless, and the operator this surface exists for cannot clean it up, cannot tell whether it matters, and will not know that the next attempt is not compounding the problem. `MOS-TRAIN-209` makes the seal idempotent on content, so re-running after a fix reuses the identity rather than minting a second one for the same bytes.

**MOS-UI-132** From the operator's view the seal MUST be atomic: either a sealed `DatasetVersion` and a frozen `DatasetSplit` both exist, or nothing was created. A partial outcome MUST be reported as a platform fault under `MOS-UI-160`, not as something the operator did.

**MOS-UI-133** The check battery and its owners:

| Order | Check | Owner | Cheap to preview | Blocking |
|---|---|---|---|---|
| 1 | Geometry admissibility under the serving `PreprocessingSpec` | `MOS-TRAIN-111` | yes | yes |
| 2 | C1, C2, C3, C5, C7 | `MOS-TRAIN-088` | yes | yes |
| 3 | C4, C6 | `MOS-TRAIN-088` | yes | no — `warn` |
| 4 | `test` partition ≥ 30 patients | `MOS-TRAIN-114` | yes | yes |
| 5 | Seeded-annotation ceilings | `MOS-TRAIN-086` | yes | yes |
| 6 | L1, L2, L5 — key-set disjointness | `MOS-EVID-034` | yes | yes |
| 7 | L3 — pixel-identity duplicates | `MOS-EVID-034` | no | yes |
| 8 | L4 — near-duplicate images | `MOS-EVID-034`, `MOS-EVID-037` | no | yes |
| 9 | Acceptance binding exists | `MOS-EVID-080`, `MOS-EVID-082` | yes | yes |

**MOS-UI-134** Every check marked *cheap to preview* MUST have been displayed in the composition panel before the operator reaches the seal screen. The seal screen MUST NOT be the first place a cohort's failure becomes visible. L3 and L4 are the exceptions — they require per-series pixel digests and a 64-bit dHash over normalised mid-axial slices — and the console MUST run them as a named, progress-reported step with the cohort size shown, never as a silent wait.

**MOS-UI-135** The console MUST NOT write a waiver and MUST NOT display a waiver control. `MOS-EVID-036` and `MOS-TRAIN-091` permit waivers, and both require the waiver to be reproduced in full in every `ValidationReport` citing the object — a waiver is a governance act with a permanent documentary consequence, taken by someone who can defend it. Further, `MOS-TRAIN-115` makes a waiver on L1, L2, L3 or L5 incapable of permitting a training run at all: *a waiver is a statement about what a report may claim; it is not a licence to fit on the test set*. A waiver control on this surface would therefore be, at best, a control that does not unblock the operator.

**MOS-UI-136** The console MUST NOT offer split-level exclusion. `MOS-EVID-031` permits a split to declare excluded patients, and using that to drop cases after a failing C-check would quietly reshape a cohort whose C1–C7 statistics are computed over the `DatasetVersion` rather than the split — the statistics would keep passing while the trained-on population changed. Exclusion before seal is a `CurationDecision` (`MOS-TRAIN-080`) and nothing else.

**MOS-UI-137** The console MUST NOT offer to edit the `PreprocessingSpec` in response to a geometry refusal. `MOS-TRAIN-111` states that such a refusal is resolvable only by excluding the offending series with a recorded `reason_code`, and MUST NOT be resolvable by editing the spec, because widening the spec to admit the training data changes what is served to every patient — and it is the cheapest-looking fix on the screen at that moment.

**MOS-UI-138** After any refusal, the working set MUST be preserved intact: facet selections, inclusion decisions, exclusion decisions and their reason codes, and the reader assignments. A refusal that discards work teaches the operator to avoid the check.

**MOS-UI-139** The seal screen MUST state, in plain language and before the action, that sealing is permanent: the cohort cannot afterwards be edited, cases cannot be added or removed, and a change produces a new version (`MOS-EVID-023`, `MOS-EVID-022`). It MUST NOT use the word *immutable* without glossing it.

##### Worked refusals

These four are normative examples of `MOS-UI-105` through `MOS-UI-107`, not illustrations. The wording may be improved; the structure and the arithmetic may not be dropped.

**MOS-UI-140** *The same patient in two groups* — L2 or L5. Note first that **L1 cannot fail in this flow**: the split is generated by `assign()` over `patient_key` and `dataset_split_members` has `PRIMARY KEY (split_id, patient_key)`, which makes a patient in two partitions structurally impossible (`MOS-STORE-293`). What does fail is L2 and L5, because `patient_key` is `HMAC(tenant_salt, issuer|patient_id)` (`MOS-EVID-010`) and one person registered under two record numbers has two keys. The refusal:

> **This collection contains the same patient twice, under two record numbers**
>
> One study appears in both the training group and the held-out testing group. The two copies are filed under different patient record numbers, so the platform's patient-level check did not catch them — but they are the same images of the same person.
>
> If this were allowed, the model would be tested on a patient it had already learned from, and its score would be higher than its real performance by an amount nobody could measure afterwards.
>
> **1 study affected.** Review the pair and exclude one copy:
>
> | Study | Record | Site | Date | Group |
> |---|---|---|---|---|
> | `…6279.6001.2988` | `pk_4tqv2n…` | Site A | 2024-03-14 | training |
> | `…6279.6001.2988` | `pk_9bfw3q…` | Site A | 2024-03-14 | testing |
>
> [Review and exclude one] [Back to the collection]
>
> <details>L2 study disjointness — MOS-EVID-034. code: SPLIT_STUDY_NOT_DISJOINT. There is no way to continue with both copies present.</details>

L3 produces a near-identical refusal with a different first line — *these two studies contain pixel-identical images filed under different identifiers*, endemic in public collections re-anonymised with fresh UIDs — and the same single remedy.

**MOS-UI-141** *An unbalanced cohort* — C1. The remedy MUST carry the arithmetic:

> **Too many of these patients come from one hospital**
>
> 561 of your 812 patients — 69 % — come from Site A. A collection may draw at most 60 % of its patients from any one site.
>
> A model trained mostly on one hospital's scanners, protocols and patient mix is measured on that same mix, scores well, and then performs worse everywhere else. Nothing in the results would warn you: every number would be correct.
>
> **Either of these fixes it:**
> - Add **123 or more** patients from sites other than Site A.
> - Remove **185** patients from Site A. (Your collection would then hold 627 patients, which is still above every other limit.)
>
> [Add patients] [Remove patients from Site A] [Back to the collection]
>
> <details>C1 site concentration — MOS-TRAIN-088. observed 0.691, bound 0.600. code: CORPUS_SITE_CONCENTRATION.</details>

C2 (scanner concentration, ≤ 0.70), C3 (at least two reconstruction classes each holding ≥ 10 % of series) and C7 (at least two sites for portable evidence) MUST follow the same shape, each with its own arithmetic.

**MOS-UI-142** *A test partition below its floor* — `MOS-TRAIN-114`. The console enforces this at seal, earlier than the requirement demands, and MUST say so rather than imply the platform refuses here:

> **This collection is too small to produce a result you could trust**
>
> The platform sets aside 20 % of patients as a held-out testing group that the model never sees. Your 118 patients give a testing group of 23. At least **30** are needed.
>
> Below 30, the range of uncertainty around any score is wider than the difference between a good model and a poor one, so the evaluation could only ever come back "cannot tell". The training itself would run and cost GPU time, and the answer at the end would be no answer.
>
> **You need at least 150 patients in total.** You have 118. Add **32 or more**.
>
> [Add patients] [Back to the collection]
>
> <details>Test partition floor — MOS-TRAIN-114; the same floor Chapter 7 sets on acceptance cohorts (MOS-EVID-026), and the reason a verdict below it can only be INDETERMINATE (MOS-EVID-083). Checked here rather than at training submission so that no collection is sealed that cannot be trained on. code: SPLIT_TEST_PARTITION_BELOW_FLOOR.</details>

**MOS-UI-143** *A gap in the declared range* — C5. `MOS-TRAIN-090` calls C5 the check that matters most and the one most easily rationalised away, so its refusal MUST be as concrete as C1's and MUST NOT offer the operator the rationalisation:

> **Part of the range this model claims to work on is barely represented**
>
> The model will be declared as working on slice thicknesses from 0.6 mm to 5.0 mm. Your collection covers most of that range, but two bands are nearly empty:
>
> | Slice thickness | Patients | Needed |
> |---|---|---|
> | 0.6 – 0.8 mm | 6 | 20 |
> | 0.8 – 1.0 mm | 11 | 20 |
>
> A model declared for 0.6 mm and trained almost entirely on 2.5 mm data will be sent 0.6 mm studies in clinical use and nothing will flag it.
>
> **Add 14 patients scanned at 0.6 – 0.8 mm and 9 at 0.8 – 1.0 mm.**
>
> The alternative — narrowing the declared range so it no longer includes those bands — is a change to what the model claims, and is made by the person who owns the model's declared applicability, not from this screen.
>
> [Add patients] [Back to the collection]
>
> <details>C5 envelope coverage — MOS-TRAIN-088; MOS-EVID-098 forbids widening a declared bound without evidence covering it. code: CORPUS_ENVELOPE_CELL_UNDERPOPULATED.</details>

**MOS-UI-144** *Near-duplicate images* — L4. This is the one check whose resolution is a visual judgement the operator is better qualified to make than any engineer, and the console MUST treat it that way. Each flagged cross-partition pair MUST be presented side by side as images with their acquisition metadata, and the only actions MUST be to exclude one of the pair with `reason_code: duplicate_patient` or to keep both. The console MUST NOT offer a waiver, even though `MOS-TRAIN-115` permits one for L4 on the grounds that the detector has a real false-positive rate on serial screening studies: *this is a false positive, proceed anyway* is a checkbox, whereas *these are the same acquisition reconstructed twice* is a judgement, and the second is what the operator is actually making. Where a series has fewer than three instances the check is skipped with an explicit reason (`MOS-EVID-037`) and the console MUST show the skip rather than a pass.

**MOS-UI-145** `warn` outcomes — C4 thickness spread and C6 temporal spread — MUST be displayed, MUST NOT block, and MUST NOT be rendered in the visual register of a refusal. `MOS-TRAIN-092` draws the distinction precisely: C4 and C6 describe a corpus that is narrow, which is a fact the reader of the eventual report needs; C1, C2, C3, C5 and C7 describe a corpus that cannot support the claim being made from it. A warning styled like a block teaches the operator to ignore blocks.

**MOS-UI-146** The console MUST verify at seal that an acceptance binding exists for `(tenant, capability)` (`MOS-EVID-080`) and that it does not name a `DatasetVersion` whose purpose is `training` or `tuning` (`MOS-EVID-082`). Discovering at evaluation time that no bar exists to evaluate against wastes the entire run, and the remedy — a binding recorded by someone holding `capability.update` — is not available on this surface, so the operator needs to know before they start.

#### 19.3.5 Train — one action

**MOS-UI-147** Training MUST be a single action with no configuration. The console MUST render the cohort, the split, the reference standard and the target capability as a read-only summary, and one control.

**MOS-UI-148** `training_backend.kind` MUST be a dataset-fingerprint auto-configuring backend — `nnunet` or `auto3dseg` — for every capability this surface can target, per `MOS-TRAIN-211`. The console MUST NOT offer `monai_supervised`. `MOS-TRAIN-211` requires a hand-configured run to record a `backend_rationale` of at least 20 characters naming what the auto-configured baseline failed to do; that is a statement this operator cannot truthfully make, and a surface that invites them to type one produces a rationale that defeats the purpose of recording it.

**MOS-UI-149** The following quantities are derived from the cohort fingerprint and frozen into the registered `PreprocessingSpec` by the exporter of `MOS-TRAIN-223`. The console MUST NOT display them as editable, MUST NOT display them as defaults, and MUST NOT offer an "advanced" region that reveals them:

| Quantity | Derived from | Frozen into |
|---|---|---|
| Target spacing | spacing distribution | `PreprocessingSpec` (`MOS-IMG-049`) |
| Intensity window, normalisation scheme and its statistics source | foreground intensity percentiles | `PreprocessingSpec`; `statistics_source` MUST be `spec` (`MOS-TRAIN-225`) |
| Foreground crop rule | class balance, foreground statistics | `PreprocessingSpec` |
| Patch size | shape distribution | `PreprocessingSpec` |
| Network topology | shape and spacing | backend plan, digested as `fingerprint_digest` |
| Training batch size | derived plan | `TrainingRun.hyperparameters`, never `patch.batch_size` (`MOS-TRAIN-224`) |

The console MAY display them as read-only facts after the run starts, labelled as derived from this cohort. It MUST NOT present the fingerprint document itself, which `MOS-TRAIN-223` retains for audit and forbids shipping as an executable configuration.

**MOS-UI-150** The console MUST NOT launch a `ConfigurationSearch`. `MOS-TRAIN-213` defines one as any procedure producing more than one trained artifact from a cohort and keeping a subset by a score computed on data, and `MOS-TRAIN-216` makes a split's `test` partition a consumable with a `test_exposure_count` that MUST NOT be reset. A search is a legitimate technique with a bounded budget, a declarative search space (`MOS-TRAIN-220`) and a nomination step — and every one of those is a decision this operator has no basis for making, spending a resource they cannot see being spent. One action, one candidate.

**MOS-UI-151** Before the run starts, the console MUST re-verify and display the preconditions the training job will itself enforce, so that a refusal arrives in the console rather than as a dead run: L1, L2, L3 and L5 re-executed against the frozen manifest (`MOS-TRAIN-115`, `MOS-TRAIN-116`), the `test` partition floor (`MOS-TRAIN-114`), the seeded-annotation ceilings (`MOS-TRAIN-086`), and `code_dirty = false` (`MOS-TRAIN-125`). A `code_dirty` refusal MUST be classified as a platform fault under `MOS-UI-160`: it means the deployed training image was built from an uncommitted tree, which is an engineering defect the operator neither caused nor can fix.

##### What a run in progress looks like

**MOS-UI-152** While a run is in progress the console MUST show: the target capability by display name, the cohort and its patient count, the reference standard, the run state in plain words, the time since submission, and — when the run is queued — that it is waiting for a GPU rather than stalled. `MOS-TRAIN-123` requires training GPUs to be a disjoint pool or the orchestrator to refuse to schedule while a clinical deployment holds the shared pool, so waiting is an ordinary state and MUST be rendered as one.

**MOS-UI-153** The five states of `TrainingRun.state` (`MOS-TRAIN-124`) MUST be rendered in plain language and MUST NOT be shown as their enum spellings:

| State | Rendered as |
|---|---|
| `PENDING` | Waiting for a free GPU |
| `RUNNING` | Training |
| `SUCCEEDED` | Training finished — evaluating |
| `FAILED` | Training did not finish |
| `CANCELLED` | Stopped by *(name)* |

**MOS-UI-154** The console MUST NOT display a training loss curve, a per-epoch validation score, a cross-validation summary, a fold-aggregated Dice, or any other figure the run computes about itself, as a measure of model performance. `MOS-TRAIN-222` forbids a selection statistic on any UI surface presenting model performance, because it is computed on data the model was fitted against under the backend's own conventions rather than the `metric_conventions` of `MOS-EVID-061`. This is the single most common element of every ML training dashboard in existence and it MUST NOT appear here. The console MAY display a progress fraction — epochs or steps completed against the plan — which is a statement about how far the run has got and not a statement about the model.

**MOS-UI-155** The console MUST NOT present a completion estimate as a fact. It MAY display elapsed time and the fraction of the planned schedule completed. An ETA on a queued GPU job is a guess, and an operator who plans around it and is wrong learns to distrust everything else on the screen.

**MOS-UI-156** The console MUST offer cancellation, backed by `Orchestrator.Cancel` (`MOS-TRAIN-122`), and MUST render `CANCELLED` as a deliberate act attributed to a person — not as a failure. It MUST state before confirming that the GPU time already spent is not recoverable and that nothing else is lost: the cohort, split and annotations are frozen objects and a new run may be started against the same ones.

**MOS-UI-157** The console MUST display whether the target capability has a seed-variance characterisation (`MOS-TRAIN-127`) and, when it does not, MUST offer to queue the two further seed-varied runs that would produce one. `MOS-TRAIN-127` requires at least three `TrainingRun`s differing only in `seeds` before a capability's first candidate is promoted, and `MOS-TRAIN-128` renders `seed_variance.sd` beside the non-inferiority margin δ in the approval dossier so an approver can see whether the gate can distinguish a real change from a re-run of the same code. An operator who does not know this is owed will produce one candidate and discover the obligation at a gate they cannot reach. The console MUST NOT compute, adjust or present δ from `seed_variance`: `MOS-EVID-087` and `MOS-TRAIN-128` both forbid it, δ being a clinical judgement owned by the Capability.

##### When a run fails

**MOS-UI-158** A failed run MUST be classified into exactly one of three categories, rendered distinctly, following the principle `MOS-SAFE-089a` already establishes for the clinician surface and which ships in `medos/web/ohif-extension/src/core/render.js` as the frozen `REJECTED_COPY` / `FAILED_COPY` pair: the reader must conclude neither *the platform is broken* nor *everything is fine*.

| Category | Meaning | Rendering | Retry offered |
|---|---|---|---|
| **Refused** | A gate blocked the run before the first batch loaded | the refusal contract of `MOS-UI-105`; an action the operator can take | no — retry cannot change it |
| **Broke** | Infrastructure failed: GPU, node, orchestrator, storage | "the training did not finish; this says nothing about your collection" | yes |
| **Unusable** | The run completed but its output cannot be registered | platform fault, routed to an engineer | no |

**MOS-UI-159** A refused run MUST name the gate in the operator's terms and MUST NOT offer retry. `MOS-TRAIN-115`'s re-execution of L1, L2, L3 and L5 against the frozen manifest is the common case, and it fires after the seal has already passed those checks only when the split spans more than one `DatasetVersion` (`MOS-EVID-023`) — a condition the console MUST explain as such rather than as a contradiction.

**MOS-UI-160** A run that fails on a quantity the fingerprint derived — an out-of-memory at the derived patch size or batch size being the archetype — MUST be classified as a platform fault, not as a refusal and not as something the operator can adjust. The operator changed nothing, because `MOS-UI-149` gave them nothing to change; the derived configuration and the available hardware are both engineering property. The console MUST say so, MUST NOT suggest reducing anything, and MUST route the run to the engineering owner.

**MOS-UI-161** Raw training logs MUST NOT be presented as the explanation of a failure. `Orchestrator.Logs` (`MOS-TRAIN-122`) exists and the console MAY offer a support bundle containing the log stream, the `TrainingRun` binding of `MOS-TRAIN-124` and the run digest, downloadable as one file for an engineer. A stack trace shown to this operator is not information; it is an invitation to conclude they did something wrong.

#### 19.3.6 Evaluate — automatic, against the locked test partition

**MOS-UI-162** Evaluation MUST be automatic on `state = SUCCEEDED` and MUST NOT be an action the operator initiates. `MOS-TRAIN-138` registers the bundle as a `ModelVersion` at `lifecycle_status = REGISTERED` with `spec.evaluation_run_id` null, and `MOS-TRAIN-140` creates the candidate `EvaluationRun` on `partition: "test"` using the same `split_digest` and `annotation_digest` the run was fitted against. The console MUST NOT offer a partition chooser, a cohort chooser, a metric chooser or an operating-threshold control.

**MOS-UI-163** The console MUST NOT offer to re-run an evaluation that has already succeeded. It MUST display `test_exposure_count` for the `(capability_id, split_digest)` pair, MUST label it as a count that only goes up, and MUST state what it means: each time a model is measured against this held-out group, the group tells you slightly less than it did before. `MOS-TRAIN-216` requires the counter to be maintained, rendered in the approval dossier and never reset — a split's `test` partition is a consumable, and the counter is how a team finds out it has been spent.

**MOS-UI-164** The `CriteriaVerdict` MUST be rendered as three distinct states, never two (`MOS-EVID-083`). `INDETERMINATE` MUST NOT be folded into `FAIL`, visually or in wording, because the two require different actions: one needs more data, the other needs a different model. A `FAIL` MUST be rendered as a legitimate result of a correctly-executed experiment, not as an error condition.

**MOS-UI-165** How metrics themselves are displayed — what MUST accompany every figure, and what MUST NOT be shown as a headline number — is the honest-metric display rule specified in 19.5, which owns it for every surface in this chapter. This section states only that the training console is bound by it and adds no display path of its own. The underlying obligations are Chapter 7's: every aggregate carries `n`, `n_patients`, a confidence interval and the convention block, and a bare scalar fails schema validation (`MOS-EVID-056`); a free-form metrics map on a `ModelVersion` is rejected outright (`MOS-EVID-071`).

**MOS-UI-166** On a `FAIL` verdict the console MUST NOT offer to change the operating threshold, edit the `AcceptanceCriteria`, or re-bind the acceptance cohort. `MOS-EVID-087` forbids tuning δ to make a candidate pass, `MOS-REG-049` puts the `AcceptanceCriteria` on the Capability, and `capability.update` is a `governance` permission this surface does not hold (`MOS-UI-102`). The available actions are to change the cohort, change the reference standard, or stop.

#### 19.3.7 Promote — blocked here, by design

**MOS-UI-167** The console MUST NOT render a promotion control. Not disabled, not hidden behind a permission check that shows a tooltip — absent. `MOS-TRAIN-003` makes the pipeline's terminal state a candidate at `lifecycle_status = VALIDATED` and states that it MUST NOT be capable of producing any state beyond that; `MOS-TRAIN-008` requires the `VALIDATED → APPROVED` transition to be performed by a human holding `artifact.approve`; `MOS-SEC-158` enforces the exclusion by the absence of the grant rather than by a check in code.

**MOS-UI-168** The console MUST render the candidate's terminal state as what it is — a model that has been built, measured and is waiting for a decision by a named person — and MUST name the role that makes it. It MUST NOT use language implying the candidate is deployed, live, in use, or available to clinicians.

**MOS-UI-169** On a `PASS` verdict the console MUST notify and MUST NOT act (`MOS-TRAIN-010`). A scheduled or event-triggered pipeline run is permitted; a scheduled or event-triggered promotion is not.

**MOS-UI-170** The approval dossier — what an approver sees at the moment of decision, and why a single aggregate number and a button is non-conformant (`MOS-TRAIN-011`) — is specified in 19.5, together with the separation of duties that keeps it on a different surface from this one. This section adds no path to it. CI MUST assert the call-graph property `MOS-TRAIN-189` already requires, extended to the console: no symbol reachable from `medos/web/training-console` transitively reaches a deployment role-mutation or `clinical_use_mode` transition endpoint.

#### 19.3.8 Capabilities are chosen here, never created here

Training targets an existing Capability. Creating one is a clinical and regulatory act, and the reason is visible in what a Capability actually carries.

**MOS-UI-171** The target chooser MUST list only Capability rows whose `status` is `supported`, by `display_name` and `definition`. `MOS-REG-029` rejects a `ServiceVersion` claiming a `reserved` or `deprecated` capability at publish, so a candidate trained against one could never be registered.

**MOS-UI-172** A capability the operator cannot choose MUST be listed with the reason rather than omitted. Omission is indistinguishable from absence, and an operator who cannot find `pneumothorax` in the list will assume the platform does not know the concept, when in fact the row exists at `status: reserved` awaiting a primary code, a measurement and an acceptance bar (`MOS-REG-046`).

**MOS-UI-173** The console MUST NOT offer to create, edit, supersede or deprecate a Capability, and MUST NOT offer a free-text *other* option in the target chooser. `capability.create` and `capability.update` are `governance`-class permissions (Chapter 8), `MOS-REG-008` makes Capability a curated vocabulary owned by the platform curator rather than a shipped unit, and `MOS-UI-102` already excludes the permission class from this surface. What a Capability carries is why:

| What it carries | Requirement | Why it is not a form field |
|---|---|---|
| `capability_id` | `MOS-REG-041` | immutable once created, never reusable for a different clinical meaning; renaming is forbidden, superseding is the only path |
| `primary_code` — `system`, `code`, `meaning`, `system_version` | `MOS-REG-042`, `MOS-REG-043` | the single source of coded concepts for that clinical function, read by the DICOM SR/SEG writer and the API; a code without a pinned terminology version is rejected, and CI validates it against the bundled subset (`MOS-REG-044`) |
| `narrative_synonyms[]` | `MOS-REG-114` | the only source of the finding-label lexicon a generated narrative may use; widening it widens what a report is permitted to say |
| `measurements[]` — UCUM `unit`, coded `concept`, `computation_geometry: source` | `MOS-REG-045` | a measurement without a UCUM unit is rejected, because a bare number cannot be written into a conformant TID 1500 SR |
| `acceptance_criteria_ref` | `MOS-REG-046`, `MOS-REG-049` | the clinical bar — sensitivity ≥ x on cohort y — expressed in the declarative grammar of `MOS-EVID-076`, never an expression string |
| `revision` | `MOS-REG-048` | changing a measurement parameter invalidates the acceptance reference until a new `EvaluationRun` exists, and must pass the same gate as a weights change |

**MOS-UI-174** Where the operator's clinical target has no Capability, the console MUST offer a request path that terminates at a person holding `capability.update` and MUST NOT create a row, a draft row, or a `reserved` row. The request MUST collect four things as prose for that person to review, and MUST label them as a description of what is wanted rather than as values that will be used: what the finding is called clinically; what it means, precisely enough to distinguish it from adjacent findings; what would be measured about it and in what unit; and what performance would count as good enough. Each of those maps onto a field above, and each is a judgement someone else must make and sign.

**MOS-UI-175** The console MUST NOT permit training against a near-miss when the exact capability does not exist, and MUST warn explicitly at selection when the chosen capability's `definition` may not match the operator's intent. The consequence is concrete rather than procedural: `MOS-REG-042` makes the Capability row the single source of codes for the SR and SEG writer, so a model trained to find one thing and registered against the capability for another produces a structurally valid, schema-conformant, signed DICOM SR asserting a SNOMED concept that is not what the model detects. Nothing downstream catches it.

**MOS-UI-176** No screen on this surface MUST accept a unit as free text. Units exist only as UCUM values on `capability.measurements[]` (`MOS-REG-045`) and are displayed, never entered.

**MOS-UI-177** The console MUST display the target capability's `revision` alongside the sealed cohort, and MUST surface it when the revision has advanced since the cohort was sealed. `MOS-REG-048` makes a change to `measurements[].parameters` increment the revision and invalidate `acceptance_criteria_ref` until a new `EvaluationRun` is recorded — which means a candidate in flight can have the bar it will be measured against change underneath it, and the operator needs to see that rather than discover it as an unexplained `INDETERMINATE`.


### 19.4 The annotation surface

Chapter 7 owns the `AnnotationSet` — its fields, its digest, its freeze semantics and the consensus enum (`MOS-EVID-038` through `MOS-EVID-044`). Chapter 17 owns the campaign: who may read, what seeding does to a corpus, and the anchoring controls that keep a seeded label from being a model's own output wearing a reader's name (`MOS-TRAIN-095` through `MOS-TRAIN-110`). Neither chapter owns a screen. This section owns the screen, and nothing else: it adds no field to `annotation_sets`, no value to `annotation_provenance`, and no rule about what a corpus may contain. Where a field name appears below it is Chapter 7's or Chapter 17's field name, cited so that the two can be read together.

The section exists because `MOS-TRAIN-095` names MONAI Label as *the annotation surface* and then specifies it entirely in terms of what the pipeline records afterwards. A requirement that the client enforce blinding (`MOS-TRAIN-104`) with no statement of what the client shows is a requirement with nowhere to land. The defining constraint of this chapter applies here more sharply than anywhere else in it: the reader is a clinical domain expert with no programming background, working alone, and every one of Chapter 17's protections has to arrive as something the surface does rather than as something the reader is trusted to remember.

#### 19.4.1 The stated preference, and the honest limit of it

The physician who will operate this surface is fluent in RadiAnt and asked for something that feels like it, preferably open source and preferably OHIF-based. Half of that is straightforwardly satisfiable and half of it is not, and the two halves must not be blurred.

RadiAnt is a closed-source, Windows-only desktop **viewer**. It has measurement and ROI tools — length, angle, elliptical ROI with HU statistics — and it does not have a volumetric segmentation editor: no label map, no brush, no per-voxel paint, no multi-segment model. Nothing that is fluent in RadiAnt is fluent in segmentation authoring, because RadiAnt does not do segmentation authoring. "RadiAnt-like" can therefore only mean **RadiAnt's ergonomics**: the mouse under the reader's hand, the way a study opens already laid out, the way window/level changes without a dialog, the way the keyboard moves through slices. It cannot mean feature parity, and a plan that promises feature parity is promising a product that does not exist in either tool.

**MOS-UI-200** — **AMENDED at specification 0.4.0.** The stated preference MUST be satisfied as an ergonomics requirement over ~~the adopted surface~~ whichever surface the reader actually works on (§19.4.4) and MUST NOT be satisfied by adopting RadiAnt, by wrapping it, by driving it, or by building a look-alike. Four independent properties disqualify it, any one of which would be sufficient, and the register row of `MOS-UI-202` MUST record all four rather than the first:

| Property | Consequence |
|---|---|
| Closed source, proprietary licence | `MOS-REL-027` forbids a register row that cannot name what is adopted; "the binary" is not an adoption boundary, and no `MOS-REL-038` SBOM entry or licence record is obtainable for a component whose contents are not enumerable. |
| Windows desktop application | Every pipeline component MUST obtain imaging through the Gateway as the `dataset_export` consumer class (`MOS-TRAIN-199`, `MOS-DATA-021`). A desktop viewer on a reader's workstation reaches imaging by a DICOM listener, a query/retrieve association or a mounted share — each of which is a second route to imaging outside the Gateway and is refused by `MOS-DATA-006` and `MOS-OPS-005` at the network layer, not by policy. |
| Viewer, not an annotation tool | `MOS-TRAIN-095` requires mask, bounding-box and point annotation. An ROI-and-measurement toolset cannot produce a label map, so the tool cannot be the producer of an `AnnotationSet` at all. |
| No `AnnotationSet` export path | `MOS-EVID-041` requires per-reader masks in source geometry with a recorded digest, and `MOS-TRAIN-096` requires each mask bound to a `reader_id` derived from a MedicalOS `User`. A tool with no session identity and no mask cannot satisfy either. |

**What the amendment changes, and the reading it rules on.** Nothing about RadiAnt changed at 0.4.0 and all four disqualifying properties still hold, so the three prohibitions on adopting, wrapping and driving it are untouched. What was re-pointed is two words: "the adopted surface" named the host §19.4.4's ergonomics set was expected to be configured over, and for the clinician surface there is no longer an adopted host to configure. The ergonomics set now binds the surface the reader opens — `viewer/` for the clinician surface, and whatever §19.4 lands on for the annotation surface, where the adopted client of MONAI Label is still the planned host and nothing in this amendment decides otherwise.

The fourth prohibition needs a ruling rather than a re-pointing, because `docs/adr/BUILD_VS_ADOPT.md` records that `viewer/` "implements RadiAnt's mouse model and preset set on a **first-party** surface, not over the adopted one", calls the conflict "arguable rather than flat", and says it "must be ruled on, not assumed away". The ruling is that ergonomics parity on a first-party surface is what this requirement asks for and not what it forbids, and the requirement's own preceding paragraph is the authority for it: *"RadiAnt-like" can therefore only mean **RadiAnt's ergonomics** … It cannot mean feature parity, and a plan that promises feature parity is promising a product that does not exist in either tool.* A look-alike is the second thing sold as the first — a surface that claims RadiAnt's capabilities and delivers its window decorations — and the mouse model, the presets and the way a study opens already laid out are the first thing, named as satisfying this requirement two sentences above it.

The contrary reading is recorded rather than dismissed, because it was a live reading and someone holding a 0.3.0 conformance report will have taken it: while `MOS-CORE-038` stood, a first-party surface was forbidden outright, so *any* first-party surface wearing RadiAnt's bindings could only be the look-alike, and no other reading was available. After the reversal it cannot be held without contradiction — it would forbid on ergonomics grounds precisely what `MOS-UI-009a` permits on rendering grounds, and between the two requirements the chapter would then say nothing about what the platform may build. Which host a given ergonomics requirement binds is now a question `MOS-UI-207` through `MOS-UI-214` have to answer per surface rather than once, and `MOS-UI-214`'s "the pinned viewer build" no longer names a single subject.

**MOS-UI-201** Every claim in this chapter about what a third-party tool can or cannot do MUST be recorded in the `MOS-REL-027` register row with the method and the date by which it was verified, and a claim that has not been verified MUST be recorded as unverified rather than stated as fact. The failure mode this prevents is specific and has already happened once in this repository: `medos/web/ohif-extension/README.md` records that `window.config.extensions` in a pre-built OHIF image does nothing at all, which is the opposite of what the configuration key's name implies, and the slice discovered it only by trying. A specification that misstates a dependency's capability sends an implementer down a dead end with the specification's authority behind them.

#### 19.4.2 The build-versus-adopt row

**MOS-UI-202** The rows below MUST be appended verbatim to `docs/adr/BUILD_VS_ADOPT.md` under `MOS-REL-027`, in the column shape `MOS-TRAIN-019` established for rows that carry a REFUSE or a PERMIT decision. They are normative in the same sense as the rows already there, and they extend the MONAI Label row of `MOS-TRAIN-019` rather than competing with it.

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **OHIF + Cornerstone3D** — display, viewport layout, tool groups, SEG rendering | MIT; MIT | **ADOPT** — already adopted, already running | The viewer, its `DisplaySetService`, `HangingProtocolService` and Cornerstone3D tool groups; `@ohif/extension-cornerstone-dicom-seg` for SEG display | The MedicalOS extension package outside the OHIF tree (`MOS-REL-027`, `MOS-SAFE-089a`), extended with an annotation panel and the ergonomics modules of §19.4.4 | The ergonomics configuration set; the blinding enforcement of `MOS-UI-216`; the edit-stream instrumentation of `MOS-UI-220` | It is already the deployed viewer and already the surface `MOS-SAFE-088`'s provenance panel lives in. A second viewer for annotation would mean two renderings of the same SEG and two places a geometry defect can hide, which is exactly what `MOS-IMG-158`'s second-viewer gate is designed to detect, not to institutionalise. |
| **MONAI Label** — annotation server, seeding, active-learning strategies | Apache-2.0 | **ADOPT** — already adopted at `MOS-TRAIN-019` | The annotation server, its OHIF client, its scribble and interactive-segmentation apps | The authentication shim of `MOS-TRAIN-019`; the `AnnotationSet` exporter; the inverse-transform step of `MOS-TRAIN-108` | Reader identity binding; `annotation_provenance`; the consensus computation, which the platform performs; the suppression of `MOS-UI-212` | This row adds nothing to the `MOS-TRAIN-019` decision. It records that the *operator surface* over MONAI Label is this chapter's, and that MONAI Label's own training loop and re-seeding remain disabled under `MOS-TRAIN-106`. |
| **3D Slicer** — desktop segmentation editor | BSD-style | **PERMIT as an expert round-trip only** | `SegmentEditor` and its DICOM SEG import/export, reached through the bounded round trip of §19.4.9 | The checkout/return protocol of `MOS-UI-236` | Nothing | It is the strongest open segmentation editor in existence and a small number of readers will already know it. It is also specialist software with a research-tool surface, and handing it to a clinical domain expert with no programming background as the primary annotation tool defeats the purpose of this chapter. It is a fallback for a hard case, never the entry point. |
| **RadiAnt** | proprietary, Windows | **REFUSE** | nothing | nothing | nothing | `MOS-UI-200`. Recorded as considered-and-rejected, with the four disqualifying properties, so that the preference behind it is not re-proposed as an unexamined simplification — the same reason `MOS-TRAIN-026` records the MAP refusal in full. |
| ~~**A first-party viewer or annotation tool**~~ **WITHDRAWN at specification 0.4.0** | — | ~~**REFUSE**~~ | ~~nothing~~ | ~~nothing~~ | ~~nothing~~ | ~~`MOS-CORE-038` forbids building a DICOM viewer and `MOS-CORE-045` forbids building an annotation authoring tool, permitting only the integration of a third party as the producer of an `AnnotationSet`. Both refusals are load-bearing rather than frugal: declining to build a viewer is only coherent because the output is standard DICOM that other viewers already render, and the moment the platform ships its own authoring tool it owns a rendering correctness problem it has no evidence for.~~ Replaced by the row below. |
| **A first-party annotation authoring tool** | — | **REFUSE** | nothing | nothing | nothing | `MOS-CORE-045`, which was BOUNDED at specification 0.3.0 rather than reversed: the prohibition is on producing an `AnnotationSet`, and `MOS-UI-010a` states the five clauses a reader-drawn shape must satisfy to be a measurement instead. The second half of the withdrawn row's reason survives verbatim and is the reason this half did not move: the moment the platform ships its own authoring tool it owns a rendering correctness problem it has no evidence for. It still has none — `MOS-IMG-158`'s second-viewer check has not been run in this repository (`MOS-UI-013b`). The viewer half is decided the other way, by the Viewer row of `docs/adr/BUILD_VS_ADOPT.md` under `MOS-CORE-038`'s reversal at release 0.4.0, and `MOS-UI-009a` states the four guarantees the first-party viewer is held to in place of the refusal. |

**Which way the contradiction moved, and why that way.** `docs/adr/BUILD_VS_ADOPT.md` states the problem in its own words: the REFUSE row "contradicts the Viewer row above head-on. One of the two has to move." This paragraph is the record of which moved, because a contradiction resolved without a record is a contradiction resolved by whoever edits last. The REFUSE row moved, and the ground is authority rather than preference. Half of its stated reason was `MOS-CORE-038`, and `MOS-CORE-038` was reversed at release 0.4.0 on the terms `MOS-CORE-036` sets — a minor version increment and a recorded decision naming the requirement withdrawn — with the reversal now written into the non-goals table at specification 0.3.0. A row whose reason is a citation of a reversed non-goal has no remaining reason; the Viewer row it contradicts carries a decision. The half that did not move is the annotation half, which cites a non-goal that was bounded and not reversed, and it is restated as its own row above so that striking the viewer half does not quietly take the annotation half with it.

Two things this amendment does not fix and does not claim to. First, `MOS-UI-202` requires these rows to be appended **verbatim** to `docs/adr/BUILD_VS_ADOPT.md`, and that file records that they "are absent from this file entirely — a pre-existing gap." The gap is older than this amendment and survives it; what changes is which rows are owed. Second, the OHIF row above still reads "ADOPT — already adopted, already running", and that is no longer true of the clinician surface: no OHIF build is deployed. Whether it remains true of the *annotation* surface depends on how §19.4 is delivered, which is not built and not decided here, so the row is left standing and the false clause is recorded rather than edited — amending a row about the annotation surface while ruling only on the clinician one is how a chapter acquires the kind of inconsistency `99-known-inconsistencies.md` exists to hold.

**MOS-UI-203** 3D Slicer MUST NOT be the default annotation surface, MUST NOT be the surface a campaign opens into, and MUST NOT be the only surface on which any annotation type in the campaign can be produced. A campaign whose work cannot be completed in the primary surface MUST be treated as a defect in the primary surface, recorded against it, and MUST NOT be resolved by moving the reader pool to Slicer.

**MOS-UI-204** — **AMENDED at specification 0.4.0.** The platform MUST NOT build ~~a DICOM viewer (`MOS-CORE-038`) or~~ an annotation authoring tool (`MOS-CORE-045`). A first-party implementation of a brush, an eraser, a threshold or region-grow primitive, a scissors tool, a label-map interpolation, or a segmentation undo stack MUST NOT be written. Where the adopted stack lacks one of these, the correct outcomes are: contribute it upstream, narrow the campaign's annotation type, or record the gap in the register row. Writing it here is forbidden regardless of how small the first version looks, because the second version is a segmentation editor.

**Which half was struck.** The viewer half, and only because its authority was reversed: `MOS-CORE-038` no longer forbids the platform to build a DICOM viewer, and `MOS-UI-009a` states the four guarantees that replaced the prohibition. The annotation half is not weakened by one degree. `MOS-CORE-045` was BOUNDED at specification 0.3.0 rather than reversed, the six named primitives remain a closed MUST NOT with no exception and no waiver, and every citation that reaches this requirement for them reaches the surviving half: `MOS-UI-009a`'s second guarantee, `MOS-UI-010a` clause 4 — which calls this requirement "unchanged and unbounded", true of what it cites and now imprecise about the requirement as a whole — and the static gate in `viewer/tests/test_architecture.py` that greps the compiled surface for each of the six. A reader holding a conformance report written against specification 0.3.0 should note that this ID changed scope without changing what any of those checks assert.

**MOS-UI-205** — **WITHDRAWN at specification 0.4.0.** ~~Exactly one OHIF build MUST be deployed per environment. If MONAI Label's OHIF client is distributed as its own OHIF build rather than as a plugin loadable into the platform's build, the platform's build MUST win and MONAI Label MUST be integrated against it; a second viewer MUST NOT be deployed alongside the first, and a fork of OHIF MUST NOT be adopted from any source, including an upstream project's own fork (`MOS-REL-027`, `MOS-TEST-068`). Two OHIF builds in one environment means two SEG renderers, two hanging-protocol registries and two sets of hotkeys, and a reader who learns one and is given the other.~~

Its subject left the deployment: there is no OHIF build in an environment to be the one or the second of. `medos/deploy/compose/docker-compose.yml` no longer runs `ohif/app:v3.9.2`; the service is a plain nginx named `web` that serves `viewer/` at `/mos-viewer/`, the extension package at `/medicalos/` and the training console at `/training-console/`, with `location = /` returning a redirect to the first of those.

**This withdrawal resolves a recorded tension rather than opening a gap, and the two are worth telling apart.** While both viewers ran, the deployment was in breach of this requirement and said so in the file that breached it — the compose comment reads "this mount is NOT sanctioned by `MOS-IMG-158` and is in tension with `MOS-UI-205` until that requirement is amended too" — and `docs/adr/BUILD_VS_ADOPT.md` recorded the same tension a second time and justified it on grounds that were about evidence: the first-party viewer had no rendering-correctness evidence, and retiring the incumbent before it had any would be worse than the breach. The breach ends here because one of the two viewers is gone, not because the requirement was bent around it. What the ADR's justification was buying has not arrived, though, and the withdrawal must not be read as supplying it: the incumbent was retired before the replacement acquired the evidence, so `MOS-IMG-158`'s second-viewer check is now the only thing standing where a live second rendering used to stand, and `MOS-UI-013b` governs what may be claimed until it is run. A second viewer used for *verification* is not a second viewer *deployed*, which is the line `MOS-UI-202`'s OHIF row draws when it says that gate is "designed to detect, not to institutionalise" two renderings of the same SEG.

**MOS-UI-205a** Exactly one reader-facing viewer MUST be deployed per environment, and a fork of an adopted viewer MUST NOT be adopted from any source, including an upstream project's own fork (`MOS-REL-027`, `MOS-TEST-068`). The hazard is a property of readers rather than of a product and is stated in the withdrawn text: two viewers in one environment means two renderers of the same SEG, two layout or hanging-protocol registries and two sets of hotkeys, and a reader who learns one and is given the other. Three consequences are checkable:

- **One path opens a study.** The origin MUST resolve exactly one path as the surface a reader opens a study at, and MUST NOT leave a second reachable by guessing. Here `location = /` returns `302 /mos-viewer/` and `location /` returns `404`.
- **A host page that opens no study is not a second viewer.** `/medicalos/standalone/` is served on the same origin and is a toolbar entry and a provenance panel over `/api/v1` with no viewport (`MOS-UI-013`). It MUST NOT acquire one: the moment it renders a pixel it is a second viewer and this requirement binds it. A reader counting mounts needs that test, not a list of exceptions.
- **The annotation surface's host is now an open question, and MUST be decided in §19.4 rather than at deployment time.** If that surface is delivered over MONAI Label's own OHIF client, the client is a second reader-facing viewer and this requirement forbids deploying it alongside the clinician surface. The withdrawn text resolved that case automatically — the platform's OHIF build wins and MONAI Label is integrated against it — and there is no longer a platform OHIF build for anything to be integrated against. This chapter does not decide the case here. Recording that it is undecided is the point of saying so, because the tie-break that used to decide it silently is gone and a campaign that opens without a decision will make one by default.

#### 19.4.3 What exists today, and what has to be built

The register row above says "already adopted and already running." ~~That is true of the viewer and false of everything annotation-shaped in it.~~ — **AMENDED at specification 0.4.0.** It was true of the viewer until release 0.4.0 removed it from the deployment, and it remains false of everything annotation-shaped in it. This table is the honest inventory, read out of the repository rather than assumed, and a table that is not re-read against the repository is the assumption it exists to replace.

| Thing | State today | Evidence in the tree | What must be built |
|---|---|---|---|
| ~~OHIF, deployed and serving studies~~ A reader-facing viewer, deployed and serving studies — **AMENDED at specification 0.4.0** | **exists** | ~~`medos/deploy/compose/docker-compose.yml` runs `ohif/app:v3.9.2`, fronted by nginx on one origin, against the Gateway's `/dicomweb/{t}` root~~ `medos/deploy/compose/docker-compose.yml` runs no OHIF image; the `web` service serves `viewer/` at `/mos-viewer/`, fronted by nginx on one origin, against the Gateway's `/dicomweb/{t}` root | nothing for the clinician surface; the annotation surface's host is undecided (`MOS-UI-205a`) |
| MedicalOS extension package outside the OHIF tree | **exists as source; does not execute** | `medos/web/ohif-extension/src/` — `index.js`, `getToolbarModule.js`, `getPanelModule.js`, `getCommandsModule.js`, `panels/ProvenancePanel.jsx`. Its own README records these as unexecuted source | the OHIF build that loads it |
| Toolbar button and provenance panel behaviour | **exists and runs, outside the viewer** | `src/core/client.js`, `src/core/provenance.js`, `src/core/render.js`, hosted by `standalone/` on the viewer's origin and covered by `tests/e2e/test_demo.py` | nothing for the clinician surface; see §19.4.5 for why the annotation surface must not reach it |
| Extension loading in the shipped image — **AMENDED at specification 0.4.0** | ~~**impossible without a rebuild**~~ **impossible — there is no image** | ~~`deploy/compose/ohif-config.js` ships `extensions: []` and `modes: []` with a comment stating that `ohif/app:v3.9.2` resolves extensions from its own webpack bundle at build time~~ that file is deleted and the compose stack deploys no OHIF image; `MOS-UI-013` and `MOS-UI-369` carry the same finding for the clinician surface | ~~a first-party OHIF build from the pinned source~~ a decision in §19.4 on the annotation surface's host (`MOS-UI-205a`) before any build is specified |
| Any annotation, segmentation or mask-editing code | **does not exist** | grepping `medos/web/ohif-extension/` for `segment`, `annotat` or `brush` returns four comment lines and a capability id string; `src/index.js` states it deliberately contributes no `SOPClassHandler` | all of it |
| MONAI Label deployment | **does not exist** | no service in `medos/deploy/compose/docker-compose.yml`; `MOS-TRAIN-190` places annotation in 0.2.0 | all of it |
| `medos/deploy/compose/ohif.lock` | **does not exist** | required by `MOS-TEST-068`, which makes CI job `viewer-pin` assert the running container's `RepoDigest` against it | the file, and the CI job |

**MOS-UI-206** The ergonomics set of §19.4.4 and the annotation surface of this section MUST be delivered as OHIF configuration and as modules contributed from the extension package of `MOS-REL-027`, and the release that first ships either MUST replace the pre-built `ohif/app` image with a build produced from the pinned OHIF source with that package in its workspace. Until that build exists, `deploy/compose/ohif-config.js` MUST continue to ship `extensions: []`: naming a package the image cannot load is a false claim in a configuration file, and it is indistinguishable at a glance from a working configuration. The viewer digest, the mode name, the extension list and the digest of the ergonomics configuration files MUST all be recorded in `medos/deploy/compose/ohif.lock` (`MOS-TEST-068`) so that a viewer upgrade changes a data file and a digest rather than test logic.

**Recorded at specification 0.4.0, and deliberately not decided here.** Two of this requirement's premises left the repository with the incumbent. `deploy/compose/ohif-config.js` is deleted, so the clause requiring it to "continue to ship `extensions: []`" has no file to bind, and its satisfaction MUST NOT be recorded on the strength of the file's absence — an obligation discharged by deleting its subject is `MOS-REL-050`'s stub with the sign reversed, and a reviewer reads the green as a check. `medos/deploy/compose/ohif.lock` never existed at all, which the table above already records against `MOS-TEST-068`. This requirement specifies the ANNOTATION surface, whose host `MOS-UI-205a` leaves open and which §19.4 must decide; re-pointing it at a host now would settle that question inside a subsection that ruled only on the clinician surface, which is precisely the move `MOS-UI-202`'s amendment note declines to make one section above. It is recorded here as unsatisfiable as written, and owed to §19.4 rather than to this amendment.

#### 19.4.4 Ergonomics: what "familiar to a radiologist" means, expressed as configuration

Everything in this subsection is a statement about what the reader's hands do. None of it is a statement about pixels, colours or layout. Each requirement names the mechanism in the adopted stack that satisfies it, so that an implementer knows where it lands, and `MOS-UI-213` binds all of them to configuration rather than to a fork.

**MOS-UI-207** On every annotation viewport the default mouse bindings on entry MUST be: left button window/level, middle button pan, right button zoom, wheel scroll through slices. They MUST be active the moment the case opens, without the reader selecting a mode, choosing a tool or opening a menu. A surface on which window/level is a tool the reader must first pick is a surface in which the single most frequent action in thoracic reading costs two clicks, and the reader will notice within the first case. The binding set MUST be one declared Cornerstone3D tool-group configuration shared by every annotation viewport, MUST be restored on every case transition, and a reader's temporary switch to an annotation tool MUST NOT persist into the next case.

**MOS-UI-208** Window/level presets MUST be available by name and MUST be selectable from the keyboard without the mouse. The preset table MUST be a named configuration file, versioned with the deployment and digested in `ohif.lock`, and MUST NOT be hard-coded. Each entry carries a name, a window width and a window centre in HU, a key binding, and the **source** of the values. `MOS-REL-077` forbids inventing a value that has no source in this specification or a registry, and a window preset is exactly such a value: the correct source is the department's incumbent viewer configuration or the reading convention the physician already uses, captured once and recorded, not a number chosen here. The file MUST cover at minimum the body regions the campaign's `capability_id` addresses; for a thoracic campaign that is lung and mediastinum at a minimum, and the two are illustrative, not normative.

**MOS-UI-209** A case MUST open with its series already laid out, under a named hanging protocol selected by the campaign's `capability_id`. The reader MUST NOT have to find the series, choose between them, or construct the layout. Where the campaign annotates one series, the protocol MUST open that series and MUST NOT open a study browser first. The protocol MUST be contributed as a `hangingProtocolModule` from the extension package and MUST be named in `ohif.lock`. This requirement is the largest single ergonomic difference between a viewer a radiologist finds familiar and one they find hostile, and it is also the one most often deferred as cosmetic.

**MOS-UI-210** The ten most frequent actions MUST each be reachable by a single keystroke with no modifier: next slice, previous slice, next/previous preset, zoom to fit, reset, toggle the current segment's visibility, undo, accept the case, and flag the case for exclusion with a `MOS-TRAIN-203` reason code. The binding table MUST be a configuration file digested in `ohif.lock`, MUST be displayable from within the surface without leaving the case, and MUST NOT conflict with a binding the pinned viewer already assigns. A chorded shortcut MUST NOT be the only route to any of these ten.

**MOS-UI-211** The surface MUST offer axial, coronal and sagittal reconstruction of the annotated volume, and an edit made in any plane MUST be reflected in the other two within the same interaction, without a save, a re-render step or a reload. A segmentation authored on axial slices alone with no cross-plane check produces a mask that is correct slice by slice and wrong in the craniocaudal direction, and the reader cannot see it. Where the pinned viewer cannot satisfy this, `MOS-UI-214` applies and the gap MUST be recorded before the campaign opens, not discovered in it.

**MOS-UI-212** The annotation viewport MUST present no control that is not on the ergonomics list of this subsection or the annotation list of §19.4.6 and §19.4.7. Specifically forbidden on the reader's screen: any control that starts, stops or configures training; any control that selects a model, a checkpoint or an operating point; any control that selects or displays a sampling strategy; any dataset, `DatasetVersion`, split or partition control; and MONAI Label's own workflow controls to the extent they expose the above — its training controls MUST be unreachable because `MOS-TRAIN-106` makes the endpoint behind them unreachable, not because they are hidden in the UI. Suppression by CSS or by a disabled attribute MUST NOT be used where the underlying route can be reached by any other means (`MOS-REL-050`: a control that looks like an enforcement point and enforces nothing is worse than its absence). Sampling-strategy selection remains a campaign configuration recorded per `MOS-TRAIN-107`; it is not a reader-facing control.

**MOS-UI-213** Every requirement in this subsection MUST be satisfied by OHIF configuration or by a module contributed from the extension package outside the OHIF tree. A patch to OHIF or Cornerstone3D source MUST NOT be merged into this repository (`MOS-REL-027`, `MOS-SAFE-089a`). Where an ergonomic requirement genuinely needs an upstream change, the change MUST be contributed upstream and the requirement MUST be recorded as unmet against the current pin until a release carries it. A vendored patch is a fork with no maintainer, and the pin stops being upgradable the day it lands.

**MOS-UI-214** Before a campaign opens, each requirement `MOS-UI-207` through `MOS-UI-211` MUST be verified against the pinned viewer build and the result recorded in the register row of `MOS-UI-202` with its date. A requirement that cannot be satisfied by the pin MUST be recorded as unmet, with the campaign's mitigation, and MUST NOT be recorded as satisfied on the strength of the mechanism existing in the library. The automated rendering check of `MOS-IMG-157` verifies that a SEG overlays; it does not verify that a brush works, and the two MUST NOT be conflated.

**MOS-UI-215** Interaction latency for slice scroll, window/level and brush stroke MUST be measured on the reference reader workstation against the largest volume in the campaign's cohort, and the measurement MUST be recorded with the campaign. This chapter sets no threshold: `MOS-REL-077` forbids inventing one, and a number chosen here without a measurement would be either unmeetable or meaningless. The obligation is that the number exists and is recorded before readers are asked to work against it, so that "it feels slow" becomes a comparison rather than an opinion.

#### 19.4.5 Blinding is a property of the surface, not of the instructions

`MOS-TRAIN-104` requires the annotation client to enforce blinding rather than rely on instructions. This is the concrete form of that requirement.

**MOS-UI-216** While a case is open for annotation, the surface MUST NOT render the model's confidence score, the model's operating point, any other reader's mask or annotation, or the clinical report, unless the corresponding value is recorded in `annotation_readers.blinded_to` as *not* blinded for that reader (`MOS-EVID-038`'s reader table, `MOS-TRAIN-097`'s field set). Enforcement MUST be by the session grant failing to resolve the value — the surface has no route by which to fetch it — and MUST NOT be by hiding a value the surface holds. A value present in the client and not painted is one inspector's console away from being read, and one refactor away from being painted.

**MOS-UI-217** The provenance panel of `MOS-SAFE-088` MUST be unreachable from an open annotation case. It is the correct surface for a clinician reading a result and it is a blinding breach in an annotation session, because it renders the service identity, the operating threshold and the output object UIDs — `MOS-TRAIN-104`'s first two forbidden values, and a route to the third. The two surfaces MUST NOT share a panel registration, and a reader holding both roles MUST have the panel suppressed for the duration of the session rather than for the duration of the case.

**MOS-UI-218** The surface MUST NOT display any `Result` produced by any `Deployment` of the campaign's `capability_id`, and MUST NOT offer a route to one. `MOS-EVID-042` forbids an `AnnotationSet` derived in whole or in part from the output of the artifact being evaluated; a reader who consulted the deployed model's result for the same case while annotating has produced exactly that derivation, and nothing downstream can detect it.

#### 19.4.6 Seeded versus de novo, and the anchoring controls

`MOS-TRAIN-098` owns the three-value `annotation_provenance` enum and `MOS-TRAIN-099` through `MOS-TRAIN-103` own the controls around seeding. Those requirements describe what must be recorded and what must be true of a campaign. These describe what the reader sees, which is where several of them are won or lost.

**MOS-UI-219** Every case MUST open in exactly one mode — `de_novo` or `model_seeded_corrected` — fixed by the campaign and the `SamplingPlan` before the case is dispatched. The reader MUST NOT be able to choose the mode, change it after opening, or request a seed for a case dispatched `de_novo`. `MOS-TRAIN-101` requires the de-novo control arm to be *drawn by the `SamplingPlan` rather than chosen by readers*; a surface with a "start from the model" button hands that choice to the reader and the control arm silently becomes the set of cases readers found easy.

**MOS-UI-220** In `de_novo` mode there MUST be no seed for the surface to load: the session grant MUST NOT resolve a seed mask, and the absence MUST be structural rather than a disabled control. The surface MUST NOT prefetch, cache or hold a seed it does not paint.

**MOS-UI-221** In `model_seeded_corrected` mode the surface MUST persist the unedited seed mask as a distinct object before the first edit, and MUST measure `voxels_added`, `voxels_removed` and `edit_seconds` from its own edit stream for the `MOS-TRAIN-099` record. The surface MUST NOT compute `seed_dice`: that is the platform's metric code under `MOS-EVID-047`, computed from the persisted seed and the accepted mask, and a tool-side figure is neither reproducible from the persisted objects nor comparable with any other number in the evidence plane.

**MOS-UI-222** `edit_seconds` MUST be active editing wall time under a declared idle cut-off recorded with the campaign, not session duration and not time-since-open. The definition is owned here because the surface is the only component that can measure it, and no other chapter defines it. A case accepted in `model_seeded_corrected` mode with `edit_seconds = 0` and `voxels_added + voxels_removed = 0` MUST be refused: it is `model_output_unreviewed`, which `MOS-TRAIN-098` permits only as a `seed` record and never as a member of an `AnnotationSet`, and which Chapter 12's `annotations` CHECK constraint rejects at the database. The refusal MUST happen at the surface so the reader learns it in the second it occurs rather than as a batch rejection weeks later.

**MOS-UI-223** A case in the de-novo control arm MUST be indistinguishable in the surface from any other `de_novo` case. The surface MUST NOT label it as a control, a check, a calibration case, or an audit case, and MUST NOT vary its chrome, its ordering position or its instructions. `MOS-TRAIN-101` makes the control arm the campaign's own noise floor; a reader who knows which cases are being watched produces a noise floor measured on their best behaviour.

**MOS-UI-224** While a campaign is open the surface MUST NOT show a reader the campaign's mean `seed_dice` (`MOS-TRAIN-100`), its `anchoring_delta` (`MOS-TRAIN-102`), their own inter-reader agreement (`MOS-EVID-043`), or any per-reader ranking, scoreboard or throughput comparison. Each of these is a number the reader can move by changing how they annotate, and `MOS-TRAIN-100` exists precisely because mean `seed_dice` above 0.98 means the readers stopped correcting. Showing it to them converts the chapter's anchoring detector into the thing being optimised.

**MOS-UI-225** `MOS-TRAIN-103`'s reader-family exclusion MUST be enforced at case assignment, before a case is dispatched, and MUST be rendered to the reader as a refusal in the vocabulary of `MOS-UI-240` — *"You corrected masks produced by this model family, so you cannot be a reader on the set used to evaluate it"* — not as an empty queue, a permission error or a blank screen. The reader has done nothing wrong and the surface must say so; an unexplained exclusion reads as a bug and generates a support ticket instead of understanding.

#### 19.4.7 Reader identity, session integrity and consensus

**MOS-UI-226** One session MUST resolve to exactly one MedicalOS `User` and therefore to exactly one `reader_id` in `annotation_readers` (`MOS-TRAIN-096`, `MOS-EVID-038`). The surface MUST refuse to open a case when the session cannot be resolved, MUST NOT offer a guest, anonymous, shared or service-account route, and MUST NOT accept a reader identity typed into a field. The surface MUST lock after a declared period of inactivity and MUST require re-authentication to continue — the realistic failure is not an attacker but a colleague taking over the mouse for ten cases, and after the fact `MOS-EVID-038`'s requirement that the set *name* its readers is unsatisfiable for those ten.

**MOS-UI-227** `annotation_readers.tool` MUST be written by the surface from its own build identity, in the literal form `MOS-TRAIN-097` fixes for this stack, and MUST NOT be typed, selected or edited by the reader. A round trip through 3D Slicer (§19.4.9) MUST be reflected in that string for the cases it touched.

**MOS-UI-228** `consensus_rule` and `consensus_params` MUST NOT be reader-facing controls. They are campaign configuration under `MOS-EVID-039` and the reduction is performed by `medicalos-evidence` under `MOS-TRAIN-105`. The surface MUST NOT display a consensus mask, a majority mask, or another reader's mask during the campaign, and MUST NOT offer a "compare with the other reader" view.

**MOS-UI-229** Arbitration, where `consensus_rule = arbitrated`, is the single exception and MUST be a distinct surface state: it is the only state in which more than one reader's mask is visible, it MUST be reachable only by the principal recorded in `consensus_params.arbitrator_id` (`MOS-EVID-039`), and entering it MUST be recorded. An arbitrator's own annotation on a case they arbitrated MUST NOT be counted as an independent reader on that case.

#### 19.4.8 The output contract: DICOM SEG, and the one place it is not

The platform's standing position is that output is standard DICOM, and it is not a preference. `MOS-CORE-038` declines to build a viewer, and Chapter 4 records why that refusal is coherent at all: because the output is standard SEG/SR/SC that other viewers already render. An annotation surface that emitted a private mask format would put the platform's own reference standard outside the one property that makes the rest of the stack replaceable.

That said, the specification already fixes two forms, and they are not the same form. Getting this wrong in either direction produces a contradiction with a chapter that ships.

**MOS-UI-230** The annotation surface MUST NOT define a mask format. Exactly two forms exist and each has one owner:

| Form | What it is for | Owner | Rule |
|---|---|---|---|
| NIfTI or NRRD, source geometry | the persisted per-reader and reference masks inside the `AnnotationSet` | Chapter 7 | `MOS-EVID-041`; unchanged by this chapter. Origin, spacing and direction preserved to 1e-4; model space refused. Object keys and digests per `MOS-STORE-203` and Chapter 12 §12.12 |
| DICOM SEG | interchange — anything that leaves the surface toward a human, a viewer or a third-party tool | this section | `MOS-UI-231` |

**MOS-UI-231** Every mask that leaves the annotation surface toward a human or a third-party tool MUST be DICOM SEG. A tool-native artifact MUST NOT be the thing handed to a reader, an arbitrator, an external reviewer or a site: not a 3D Slicer scene bundle or `.mrml`, not a MONAI Label app-local label directory, not a run-length blob in a database column, not a NumPy archive, not a screenshot. The evidence-plane persisted form of `MOS-EVID-041` is not an exception to this — it is a different thing, an internal content-addressed object that no human is handed, and it remains NIfTI or NRRD because Chapter 7 says so and `MOS-REL-079` makes the specification win over a convenience.

**MOS-UI-232** The two forms MUST be voxel-identical and geometry-identical, and the conversion MUST be a single shared code path in `medicalos-imaging` (`MOS-IMG-003`, `MOS-REL-032`). Round-tripping a mask NIfTI → SEG → NIfTI MUST assert element-wise voxel equality and affine equality within the `MOS-IMG-033` tolerance of 1e-4, by the same assertion the inverse transform of `MOS-IMG-032` already carries. A second converter anywhere in the tree — in the annotation exporter, in a notebook, in the Slicer round-trip handler — is a defect regardless of whether it agrees today.

**MOS-UI-233** An annotation SEG MUST NOT be written to the PACS and MUST NOT be routed to any STOW-RS destination. `MOS-IMG-059` fixes the platform's PACS write inventory as the three IOD families the platform produces from a `ResultBundle`, and `MOS-IMG-101` fixes `SegmentAlgorithmType (0062,0008)` as `AUTOMATIC` for every segment MedicalOS writes, stating that `MANUAL` and `SEMIAUTOMATIC` are never correct for a platform-written AI segmentation. A human-authored mask is neither of those objects, and the resolution is that it is not one of those objects: an annotation SEG is an evidence-plane interchange artifact. It MUST carry `SegmentAlgorithmType = MANUAL` for `de_novo` and `SEMIAUTOMATIC` for `model_seeded_corrected`, MUST derive its UIDs by `MOS-IMG-062` with `uid_space = "deid"` (`MOS-IMG-067` — the pipeline holds only de-identified UIDs under `MOS-TRAIN-200`, and the origin PACS is never its destination), and MUST copy `FrameOfReferenceUID` from the source series per `MOS-IMG-068`. Chapter 4's writer path MUST NOT be reused to produce it without these three differences being explicit in code, because that path's defaults are all wrong for this object and all silently wrong.

**MOS-UI-234** An exported annotation SEG MUST travel with the `MOS-EVID-040` manifest line for its case, and an export MUST be refused for a case that has no manifest line. Chapter 4 names no DICOM attribute that carries `annotation_set_id`, `reader_id` or `annotation_provenance`, and `MOS-REL-077` forbids inventing one here; until Chapter 4 names them, the manifest line is the binding record of what an exported file is and who made it, and a SEG that arrives without one MUST be treated as unattributed and MUST NOT be imported into any `AnnotationSet`.

**MOS-UI-235** DICOM SEG is the interchange form for `annotation_type = mask` only. For `bounding_box` and `point` the interchange form MUST be Comprehensive 3D SR (`1.2.840.10008.5.1.4.1.1.88.34`) carrying `SCOORD3D` content — the SOP class `MOS-IMG-060` mandates specifically so that `SCOORD3D` evidence can be added without a SOP class migration — and for `case_label` there is no DICOM interchange form and the `MOS-EVID-040` manifest line is the artifact. A bounding box MUST NOT be exported as a degenerate SEG, and a point MUST NOT be exported as a one-voxel segment: both destroy the annotation's type on the way out and neither is recoverable on the way back in.

#### 19.4.9 The 3D Slicer round trip

**MOS-UI-236** A round trip through 3D Slicer is permitted and MUST be bounded by all of the following. It MUST be initiated from the primary surface by an authenticated reader (`MOS-UI-226`); the case MUST be checked out to that `reader_id` for the duration and MUST NOT be dispatchable to another reader while checked out; the artifact out MUST be DICOM SEG and the artifact back MUST be DICOM SEG (`MOS-UI-231`); the returned mask MUST re-enter through the same import, geometry-assertion and `MOS-EVID-041` persistence path as a mask authored in the primary surface, with no second code path (`MOS-REL-032`); and the return MUST be attributed to the checking-out reader and to no other.

**MOS-UI-237** The blinding, mode and anchoring requirements of §19.4.5 and §19.4.6 MUST hold across the round trip. In `de_novo` mode the exported SEG MUST contain no seed and no model output; in `model_seeded_corrected` mode the seed MUST be exported as the starting mask and the `MOS-TRAIN-099` measurements MUST still be produced — where the surface cannot instrument Slicer's edit stream for `edit_seconds`, the field MUST be recorded as unmeasured rather than estimated, and a campaign that depends on it MUST NOT route seeded cases through Slicer.

**MOS-UI-238** A 3D Slicer instance participating in a round trip MUST obtain imaging exclusively through the Gateway as the `dataset_export` consumer class (`MOS-TRAIN-199`, `MOS-DATA-021`) and MUST NOT be configured with a DICOM listener, a query/retrieve association, a mounted share or any other route to imaging (`MOS-DATA-002`, `MOS-DATA-006`, `MOS-OPS-005`). A reader's own Slicer install pointed at the PACS is the same violation as a second viewer with a PACS credential, and it is easier to do by accident.

#### 19.4.10 What the surface refuses, and how it says so

This is where the defining constraint of the chapter cashes out. For an ML engineer the gates of Chapters 7 and 17 are guardrails around a workflow they could otherwise perform by hand. For a clinical domain expert working alone they are the product: the system refuses, in a sentence they understand, instead of asking a question they cannot answer. Every row below is an existing blocking check; none of them is new here. What is new is the obligation that the refusal reach the reader as language.

| Condition | Governing requirement | What the reader is told | What the reader can do |
|---|---|---|---|
| The session cannot be resolved to one `User` | `MOS-UI-226`, `MOS-TRAIN-096` | "Annotations have to be signed by one named person. Sign in as yourself to open this case." | sign in |
| The reader corrected masks from this model family | `MOS-UI-225`, `MOS-TRAIN-103` | "You corrected masks made by this model family, so you cannot read for the set that evaluates it. This is not a problem with your work." | nothing; the campaign owner reassigns |
| A seeded case accepted with no edit | `MOS-UI-222`, `MOS-TRAIN-098` | "This case was accepted exactly as the model produced it. That is a model output, not a reader's annotation, and it cannot go into the set." | edit and accept, or exclude with a reason |
| A mask that is not in source geometry | `MOS-EVID-041`, `MOS-TRAIN-108` | "This mask is in the model's grid, not the scan's. It cannot be stored." | none in the surface; it is an exporter defect and is reported as one |
| Adding a reader or a case to a frozen set | `MOS-TRAIN-110` | "This annotation set is closed. Adding to it makes a new set on the same cohort." | start the new set |
| A control that would train, re-seed or reorder from the reader's screen | `MOS-UI-212`, `MOS-TRAIN-106`, `MOS-TRAIN-107` | nothing — the control does not exist and the route behind it is unreachable | — |
| An export in a tool-native format | `MOS-UI-231` | "Masks leave here as DICOM SEG so any viewer can open them." | export the SEG |
| An exclusion reason that describes a model outcome | `MOS-TRAIN-204`, `MOS-TRAIN-203` | the vocabulary offered contains no such value; there is nothing to pick | pick a reason about the image or the patient |

**MOS-UI-239** Every refusal a reader can encounter MUST be rendered as a sentence naming the condition and the next action. It MUST NOT render a requirement ID, an HTTP status, an exception class, a field path or a stack trace as its primary text. The machine-readable reason MUST remain available for a support ticket — the RFC 9457 `type` and `title` of `MOS-API-036`, carried through unchanged and never rewritten into a friendly sentence in transit — and MUST be presented as secondary detail. The precedent is already in the tree and is the right one: `MOS-SAFE-089a` requires a `REJECTED` job to be visually distinct from a `FAILED` one with the machine-readable reason verbatim, and `medos/web/ohif-extension/src/core/render.js` implements it across four channels because a single hue difference is invisible to roughly 8% of men. A refusal is a result, not a failure, and the surface MUST NOT present it in the vocabulary of an error.

**MOS-UI-240** A refusal MUST name what is refused and MUST NOT imply that the reader did something wrong where they did not. Three of the rows above — the family exclusion, the frozen set, the geometry failure — are properties of the campaign or of the software, and a reader who reads them as an accusation stops trusting the surface.

**MOS-UI-241** No refusal in the table above MUST be resolvable by a control in the surface. There MUST be no override, no "proceed anyway", no "skip this check", no widened tolerance and no administrator toggle reachable from a reader's screen. This is `MOS-TRAIN-111`'s rule restated at the surface: a seal refused for a geometry violation *MUST NOT be resolvable by editing the spec*, because widening the spec to admit the training data changes what is served to every patient and it is the cheapest-looking fix on the screen at that moment. The same is true of every row here. Where a refusal genuinely must be waived, the only mechanism is `MOS-EVID-036`'s written waiver — `{check_id, waived_by, waived_at, rationale, affected_pairs}`, reproduced in full in any `ValidationReport` citing the artifact — and it is issued by a principal holding the relevant `annotation_set` or `dataset_split` permission (`MOS-SEC-158`), outside this surface, with their name on it.

### 19.5 The honest-metric display rule

`MOS-TRAIN-213` through `MOS-TRAIN-222` exist because of an arithmetic result, not because of a
policy preference, and the arithmetic is worth restating in the form a surface designer has to
answer. A search that trains N configurations and keeps the best score on a partition has taken a
maximum over N noisy estimates of one underlying quantity. For a segmentation capability with a
per-case Dice standard deviation of 0.10 scored on a `tune` partition of 80 patients, the standard
error of the mean is about 0.011; the expected maximum of 30 draws sits roughly 1.9 standard errors
above the common mean; so a search over 30 configurations that are *all genuinely equal* reports a
winner about **0.021 Dice above the truth**. Nothing is broken and no one cheated. The number simply
does not reproduce, and the first place it fails to reproduce is the second site.

An ML engineer reading a sweep dashboard discounts that automatically. A clinical domain expert
reading the same dashboard reads it as performance, because a number in a large font next to the
word *Dice* is performance in every other tool they have ever used. This section is therefore not a
labelling convention. It is the mechanism by which the arithmetic of `MOS-TRAIN-213` survives
contact with a reader who has no reason to know it.

**MOS-UI-300** Any surface that presents a figure as the performance of a `ModelVersion` MUST take
that figure from an `EvaluationRun` whose partition is `test` (`MOS-EVID-030`, `MOS-EVID-032`), or
from the deployment gate's run against a `DatasetVersion` whose `Dataset.purpose` is `acceptance`
(`MOS-EVID-026`). No other figure MAY occupy that position. This is the display half of
`MOS-TRAIN-215`: the partition that no search, no threshold choice and no checkpoint choice ever
touched is the only partition a headline may come from.

**MOS-UI-301** Every metric rendered anywhere on either surface MUST carry, adjacent to the value and
without requiring interaction, the partition it was computed on and the `evaluation_run_id` it came
from. `MOS-TRAIN-178` already forbids the dossier from computing a figure of its own; `MOS-UI-301`
extends that to every screen: a number with no run behind it MUST NOT render at all, and a number
whose partition is unknown to the client MUST render as `partition not recorded` rather than as a
bare value.

**MOS-UI-302** A metric computed on a partition that any selection was performed on MUST NOT be
rendered as headline performance, MUST NOT be rendered in a larger type size, a summary card, a
list-view column named for the capability's primary metric, a sort key on a candidate list, a
tooltip on a model picker, or an export whose column header is the metric name alone. This covers
the `tune` partition (`MOS-TRAIN-142`, `MOS-EVID-033`), cross-validation folds inside `train`
(`MOS-TRAIN-215`), nnU-Net's fold-aggregated `summary.json` Dice, `AutoRunner`'s per-algorithm
validation score, and any figure the search itself computed to rank trials. `MOS-TRAIN-222` already
forbids writing such a figure into `evaluation_case_metrics`, `evaluation_runs.aggregate_metrics`,
`capability_claims`, a `ValidationReport` aggregate or a `ModelVersion` field; `MOS-UI-302` is the
same prohibition at the render layer, which is where the value would otherwise re-enter.

**MOS-UI-303** A selection statistic MAY be shown, and only inside a block explicitly headed as
search diagnostics, carrying the literal label `selection statistic — not performance` and the
`search_trial_score` field name (`MOS-TRAIN-222`). The block MUST be collapsed by default on the
engineering surface and MUST NOT exist at all on the clinician surface.

**MOS-UI-304** Where a candidate came from a `ConfigurationSearch`, the promotion screen MUST render
the search-provenance set of `MOS-TRAIN-221` and item 15 of `MOS-TRAIN-176` in full: `search_id`,
`trials_completed`, `space_digest`, `selection_metric`, `selection_partition`, `selection_rule`,
`selection_margin`, `cost.gpu_hours_used`, `cost.stop_reason`, and `headline_metric_selected_on`.
`headline_metric_selected_on` MUST be rendered as a visible boolean, not omitted when false. Under
`MOS-TRAIN-214` it is `false` for every conformant run, and that is precisely why it must be on the
screen: a field that is always `false` when things are right is the only kind of field that can be
`true` when they are not.

**MOS-UI-305** `selection_margin` MUST be rendered on the same line as `seed_variance.sd`
(`MOS-TRAIN-127`, `MOS-TRAIN-176` item 15, `MOS-TRAIN-236`), and when
`selection_margin <= seed_variance.sd` the surface MUST render the literal marker
`selection not distinguishable from run-to-run noise` beside them. The marker MUST NOT be a tooltip,
a hover state, an icon or a colour. It is the one sentence that tells a non-expert that a search
which spent 81 GPU-hours to win by 0.006 Dice against a measured seed standard deviation of 0.011
bought nothing, and it MUST be legible without the reader performing the comparison themselves.

**MOS-UI-306** `test_exposure_count` for the split (`MOS-TRAIN-216`) MUST be rendered on the
promotion screen and on any screen from which a candidate `EvaluationRun` on `test` can be started,
with the count shown **before** the control that would increment it. The surface MUST NOT offer a
control that resets it, and MUST NOT render it only in a detail view. A split's `test` partition is
a consumable; a surface that spends one without saying so has removed the only signal the team has.

**MOS-UI-307** Neither surface MUST compute a metric, an aggregate, a delta, a percentage, a ratio or
a confidence interval client-side. Every rendered figure MUST be a field read from a
`capability_claims` row or a persisted `evaluation_runs` field (`MOS-EVID-072`, `MOS-TRAIN-178`). A
rendering layer that can compute is a second evaluation implementation with no run behind it, and on
a no-code surface it is a second implementation nobody will ever read.

**MOS-UI-308** A trial list, a sweep table or any ranked view of candidates MUST render its ordering
column with the `selection statistic` label of `MOS-UI-303`, MUST NOT default-sort a candidate list
by any metric, and MUST NOT carry a promotion, nomination or deployment control on a row
(`MOS-TRAIN-233`). Where a ranked view is shown, the surface MUST state which single trial is
`nominated` (`MOS-TRAIN-216`, `MOS-TRAIN-219`) and MUST render every other row as not eligible for
registration.

**MOS-UI-309** Per-stratum figures MUST be rendered with every stratum whose `n < 10` marked
`underpowered` in words rather than rendered as a number (`MOS-TRAIN-176` item 8), and the incumbent's
figures MUST be labelled as coming from the same-cohort re-run of `MOS-TRAIN-146`, with any
historical published figure labelled `historical` and visually separated (`MOS-TRAIN-177`). Placing a
candidate's fresh number beside an incumbent's year-old number from a different cohort is the most
common way an approval is obtained for a comparison that was never performed, and it is a layout
decision, which makes it this chapter's.

**MOS-UI-310** The vocabulary below is fixed. A surface MUST use these words for these partitions and
MUST NOT substitute synonyms, because the synonyms are where the meaning leaks.

| Partition / source | Word the surface MUST use | Where it MAY appear | Where it MUST NOT appear |
|---|---|---|---|
| `test` partition of the frozen split | **held-out result** | headline, summary card, list column, export | — |
| `acceptance` `DatasetVersion` | **acceptance result** | headline, gate screen, dossier | — |
| `tune` partition | **tuning score** | search diagnostics block only | headline, card, list column, picker, export header |
| CV folds within `train` | **tuning score (folds)** | search diagnostics block only | headline, card, list column, picker, export header |
| `search_trial_score` | **selection statistic** | search diagnostics block only | anywhere else |
| per-member run of an ensemble | **member diagnostic** | nowhere on either surface (`MOS-TRAIN-229`) | everywhere |

**MOS-UI-311** The words `validated`, `clinically validated`, `validated for clinical use`,
`certified` and `approved for diagnosis` MUST NOT appear on either surface to describe a metric, a
run, a gate outcome or a state, in any language (`MOS-SAFE-008`, `MOS-CORE-004`). CI MUST grep the
compiled bundle and the string catalogue of `MOS-UI-335` for them and fail on a hit. On a surface
whose whole point is that its operator does not know the vocabulary, the platform's own word choice
is the vocabulary they will acquire.

---

### 19.6 Separation of duties

**MOS-UI-315** Training a candidate and promoting it MUST NOT be the same act, MUST NOT be reachable
from the same control, and MUST NOT be performable by the same principal in a single authenticated
flow without that fact being rendered. `MOS-TRAIN-173` fixes three distinct human decisions with
three distinct permissions and three audit records; `MOS-UI-315` is the statement that a surface MUST
NOT collapse them back into one, which is what a no-code surface will do by default unless it is
forbidden to.

The reason this section is load-bearing rather than procedural is in chapter 18. `MOS-CONF-023`
requires the platform project and the publisher of a first-party service to be treated as distinct
parties for every purpose, even where they are the same legal entity, and `MOS-CONF-026` carries to
chapter 16 the unresolved question of whether the approver of a first-party service's clinical gate
may be an employee of the publishing entity — noting that the first-party variant is sharper than
`MOS-TRAIN-175`'s because publisher and platform operator may be the same team. `MOS-CONF-030` adds
that a site operating in shape (c) is simultaneously the SITE owner and the PUBLISHER owner and
inherits the **union** of the two columns. Put a no-code training surface on top of that and the
question stops being a governance edge case: if the flow lets whoever clicked *Train* also click
*Deploy*, then the conflict of interest chapter 18 identified as an open question becomes the
default path through the product, taken by every user who does not know it is a question. The
surface cannot answer `MOS-CONF-026`. It can, and MUST, refuse to make the unanswered version
frictionless.

**MOS-UI-316** The engineering surface MUST NOT hold, cache, proxy or act under any credential of the
principal that runs the pipeline. `MOS-SEC-158` fixes that principal as a `ServiceAccount` holding
the seeded `training_pipeline` role and no other, whose permissions are exactly
`{dataset.create, dataset_version.create, annotation_set.update, dataset_split.freeze,
evaluation.run, artifact.publish}` and whose exclusions — `artifact.approve`, every `deployment.*`
permission including `deployment.gate.override`, `result.submit` and `phi.reidentify` — are enforced
by absence from the role, never by a check in code. A surface that could act as that account would
be a second copy of the boundary, and `MOS-SEC-158` says in terms why a second copy is worse than
none. Every act the surface performs MUST be performed as the signed-in `User`, under that user's own
grants, through `/api/v1`.

**MOS-UI-317** No surface MUST offer a control whose effect is to acquire, request, elevate or
temporarily assume `artifact.approve`, `validation_report.approve`, `deployment.approve_clinical`,
`deployment.promote` or `deployment.gate.override`. Where the signed-in user lacks the permission for
the next act, the surface MUST render the refusal of `MOS-UI-338` — naming what is needed and that it
is not theirs — and MUST NOT render a disabled-looking control that implies the permission is a
setting. `MOS-TRAIN-174` makes non-human holding of these permissions a CI-falsifiable property;
`MOS-UI-317` keeps the human surface from becoming the route around it.

**MOS-UI-318** The promotion screen MUST render the candidate's full evidence set at the moment of the
decision. `MOS-TRAIN-011` names the minimum — the `ValidationReport`, the diff against the
incumbent's report, the per-stratum results (`MOS-EVID-067`), the regression comparison
(`MOS-EVID-085`) and the corpus stratification report of `MOS-TRAIN-088` — and states the consequence
in terms this chapter does not soften: *a promotion UI that shows a single aggregate number and a
button is non-conformant, because it reproduces the automated path with a human-shaped delay in it.*
The screen MUST therefore render all fifteen items of `MOS-TRAIN-176`, MUST render them from the
generated dossier rather than from a second query, and MUST NOT summarise, elide or collapse-by-
default items 5 (leakage, with every waiver reproduced in full), 6 (the `CriteriaVerdict` including
every `SKIPPED` and `INDETERMINATE`, `MOS-EVID-113`), 8 (strata), 12 (`not_validated_for` and
`known_failure_modes` verbatim, `MOS-REG-032`) or 15 (search provenance).

**MOS-UI-319** The following promotion-screen shapes MUST NOT be implemented. Each is named because
each is the shape a product team arrives at honestly.

| Forbidden shape | Why | Requirement |
|---|---|---|
| A headline metric and an **Approve** button | Reproduces the automated path with a human-shaped delay | `MOS-TRAIN-011` |
| An **Approve all passing** control, a multi-select approve, or an approval body carrying an array | `artifact.approve` takes exactly one `model_version_id` per call | `MOS-TRAIN-232` |
| A saved filter or rule that approves on match | Same defect, expressed as configuration | `MOS-TRAIN-232` |
| A promote control on a row of a ranked trial list | A ranking view with a promotion control is selection at the gate | `MOS-TRAIN-233` |
| A single **Train and deploy** action, wizard step, or "finish" that performs both | Collapses A1/A2/A3 into one act | `MOS-TRAIN-173`, `MOS-UI-315` |
| An `auto_promote` toggle offered anywhere a `clinical` deployment of the same capability exists | Refused by the platform; offering it teaches that it is a setting | `MOS-TRAIN-184` |
| A gate `PASS` that advances the flow to cutover | The gate says *permitted*, not *now* | `MOS-TRAIN-182` |
| A **Retry** control on a gate `FAIL` that re-runs the gate against the same evidence | Re-rolling a gate is selection on the acceptance cohort | `MOS-EVID-033`, `MOS-TRAIN-216` |

**MOS-UI-320** The three acts MUST be rendered as three acts. The surface MUST show, for each of A1,
A2 and A3 (`MOS-TRAIN-173`), the permission required, the named human who performed it, the
timestamp, and the record it landed on — `validation_reports.approver` with its DSSE envelope
(`MOS-EVID-117`, `MOS-EVID-118`), `artifact.lifecycle_status = APPROVED` plus its `AuditEvent`
(`MOS-REG-021`), and `deployment.clinical_use_mode.promoted` (`MOS-SAFE-036`, `MOS-SAFE-037`). An act
not yet performed MUST render as not performed, naming the permission, never as a greyed step in a
progress bar. `MOS-TRAIN-175` permits one person to exercise all three where tenant governance
allows it and requires three separate acts with three timestamps regardless; the surface MUST
therefore render the three-timestamp trail even when one name appears three times.

**MOS-UI-321** When the same `User` would perform two or more of A1, A2 and A3, or when the approving
user is the same principal that submitted the `TrainingRun` or the `ConfigurationSearch`, the surface
MUST render a persistent, non-dismissable statement naming both roles the user is occupying, before
the approval control, and MUST record the coincidence in the `approval_rationale` prompt. It MUST NOT
block: `MOS-TRAIN-175` places the question in governance and carries it to chapter 16, and a surface
that decided it would be deciding a question this specification has deliberately left open. It MUST
NOT be silent either, which is the condition chapter 18 objects to.

**MOS-UI-322** Where the deployment being promoted is a first-party service or a site-trained model,
the promotion screen MUST render the ownership consequence before the approval control:
`MOS-CONF-023` and `MOS-CONF-024` for a first-party service — published as an ordinary
`ServiceVersion` under a key bound to the publishing entity, never presented as "included", "built
in" or "part of MedicalOS" — and `MOS-CONF-027`, `MOS-CONF-028` and `MOS-CONF-030` for a site-trained
model: the site is the manufacturer of that model's clinical claim, the platform MUST NOT assert that
an in-house exemption applies, and the site inherits the union of the SITE and PUBLISHER obligation
columns. `MOS-CONF-030` observes that this is the shape in which a site most often discovers it has
acquired obligations it did not price. A no-code surface makes that discovery later and cheaper to
reach, so the surface MUST make it before the act, not after.

**MOS-UI-323** `artifact.approve` MUST require an `approval_rationale` of at least 20 characters typed
by the approver in that session (`MOS-TRAIN-179`). The surface MUST NOT prefill it, MUST NOT offer a
canned list, and MUST NOT accept a value carried forward from a previous approval. The rationale is
the only field in the whole flow that cannot be produced by the platform, which is what makes it
evidence that a human was present.

**MOS-UI-324** A tenant whose roles merge evidence production and evidence approval MUST be surfaced
as such. `MOS-SEC-042` forbids any *seeded* role from holding both `validation_report.create` and
`validation_report.approve` and requires a tenant that merges them to do so explicitly by editing a
role. The engineering surface MUST render, on the promotion screen, whether the acting user's roles
satisfy that separation, and MUST name the edited role when they do not.

**MOS-UI-325** Permission identifiers rendered or requested by either surface MUST be the registered
spellings of chapter 8 §8.3.2 (`MOS-SEC-031`, `MOS-SEC-033`). Chapter 17's A1 permission is written
`evidence.report.issue` in `MOS-TRAIN-173`, `MOS-TRAIN-174`, `MOS-TRAIN-151` and `MOS-TRAIN-232`, and
that identifier is registered nowhere; the registered pair is `validation_report.create` /
`validation_report.approve`, and `99-known-inconsistencies.md` records both the defect and its
resolution. Until chapter 17 is rewritten, this chapter's surfaces MUST use
`validation_report.approve` for A1 and MUST NOT ship a string containing `evidence.report.issue`. A
surface that renders an unregistered permission teaches its user a name that denies under
`MOS-SEC-033`.

---

### 19.7 Plain-language refusals

Every gate named below is already machine-checked and already blocking. Nothing in this section
weakens one, adds an override, or makes one advisory. What it adds is the obligation that each gate
carry an explanation a person with no programming background can act on — because for this audience
the gates are not guardrails around the product, they *are* the product: the system refuses, in
language they understand, instead of asking a question they cannot answer.

**MOS-UI-330** Every blocking gate reachable from either surface MUST carry a **refusal record** with
exactly three members, rendered together and in this order: `what_is_wrong`, `why_it_blocks`,
`what_would_resolve_it`. A gate reachable from a surface with no refusal record MUST fail CI
(`MOS-UI-336`). An error code shown to a domain expert as the primary content is a defect of this
chapter, not a cosmetic issue.

```yaml
# refusals/l1_patient_in_two_partitions.yaml — the record shape. One file per gate id.
schema_version: "1.0"
gate_id: l1_patient_disjointness
requirement: [MOS-EVID-034, MOS-TRAIN-116, MOS-TRAIN-117]
severity: blocking                 # blocking | warn
waivable_from_surface: false       # MOS-TRAIN-115: L1, L2, L3, L5 never
problem_type: https://spec.medicalos.org/problems/split-leakage
problem_code: SPLIT_L1_PATIENT_IN_TWO_PARTITIONS
class: client_error                # MOS-API-037
what_is_wrong: >
  The same patient is in both the learning group and the exam group.
why_it_blocks: >
  A model that has already seen a patient will score well on that patient for a reason that
  has nothing to do with the disease. The score you would get back would not be the score
  you would get on a new patient, and there is no way to tell the difference afterwards.
what_would_resolve_it: >
  Put every study belonging to this patient on one side, or leave the patient out of the
  dataset entirely, then freeze the groups again. The affected patient is named below.
detail_fields: [patient_key, partitions]
```

**MOS-UI-331** The machine-readable `code`, `type`, `class` and `trace_id` of `MOS-API-036` and
`MOS-API-037` MUST still be present on every refusal screen, rendered verbatim and secondary to the
three-part text — verbatim because a support conversation needs the exact string, secondary because
it is not the answer to the reader's question. The surface MUST NOT prettify, title-case, translate or
expand a `code`.

**MOS-UI-332** The refusal catalogue MUST cover at least the gates below. `Waivable` means waivable at
all, by anyone, anywhere in the platform; `from surface` means the surface MAY render the waiver
control.

| Gate | Requirement | Blocking | Waivable | From surface | Refusal text (`what_is_wrong` / `why_it_blocks` / `what_would_resolve_it`) |
|---|---|---|---|---|---|
| L1 patient disjointness | `MOS-EVID-034`, `MOS-TRAIN-116`, `MOS-TRAIN-117` | yes | no (`MOS-TRAIN-115`) | no | The same patient is in both the learning group and the exam group. / A model that has already seen a patient scores well on that patient for the wrong reason, and afterwards there is no way to tell that apart from real skill. / Put all of that patient's studies on one side, or leave the patient out, then freeze the groups again. Two studies five years apart are still the same patient. |
| L2 study disjointness | `MOS-EVID-034`, `MOS-TRAIN-118`, `MOS-TRAIN-119` | yes | no | no | One person is in the dataset twice, under two different patient numbers, so they are on both sides without looking like it. / This is a records problem, not a grouping problem: moving the study would hide the duplicate rather than remove it. / Someone with access to the patient index must record that the two numbers are the same person. The dataset is then re-sealed and the groups re-frozen for you. You cannot fix this by moving a study. |
| L3 pixel-identity duplicates | `MOS-EVID-034` | yes | no | no | The exact same images appear on both sides of the split under two different identities. / This is usually one scan anonymised twice. The exam group is no longer unseen, so its score means nothing. / Remove one copy from the dataset and re-seal it. The duplicated image series are listed below. |
| L4 near-duplicate images | `MOS-EVID-034`, `MOS-TRAIN-115` | warn | yes | yes, with a written reason | Some scans on the two sides are nearly identical — often the same acquisition reconstructed twice, or a rescan minutes later. / If they are the same acquisition, the exam group is partly not unseen. / If these are genuinely different visits, you may continue by writing down why. Your note is reproduced in full in every report about this model, permanently. |
| L5 accession disjointness | `MOS-EVID-034` | yes | no | no | The same order number appears on both sides of the split. / One study reached the platform twice by two different routes, so the exam group contains a study the model learned from. / Remove one ingestion of the affected study and re-seal the dataset. |
| C1 site concentration | `MOS-TRAIN-088` | yes | yes, recorded | yes, with a written reason | More than 60% of the patients come from a single hospital. / A model built on one hospital's traffic learns that hospital's scanners, protocols and case mix. Every number on your report will still be correct, and the model will lose several points at the next site with nothing to point at. / Add patients from other institutions, or record in writing that this is a single-site model — in which case the phrase "single-site cohort" appears in the cohort summary of every report citing this dataset. |
| C2 scanner concentration | `MOS-TRAIN-088` | yes | yes, recorded | yes, with a written reason | More than 70% of the scans come from one scanner model. / Same reason as above, one level down: the model will be measured on the scanner it was taught on. / Add scans from other scanner models, or record why this cohort is intentionally limited to one. |
| C3 kernel coverage | `MOS-TRAIN-088` | yes | yes, recorded | yes, with a written reason | Fewer than two reconstruction kernel families are meaningfully represented. / Reconstruction kernel changes the texture of the image more than most people expect; a model taught on one may not read the other. / Include studies reconstructed with a second kernel family, or record why one is sufficient here. |
| C5 envelope coverage | `MOS-TRAIN-088`, `MOS-TRAIN-090` | yes | yes, recorded | **no** | You have declared this model as suitable for a range of studies that your data do not cover — at least one declared value or numeric band has fewer than 20 patients behind it. / This is the check that turns "validated on 2.5 mm archival data, declared for 0.6 mm" into a failure now rather than a surprise at another hospital. / Either narrow what you declare the model is for, or add at least 20 patients for each declared band. The empty bands are listed below. |
| C7 single-site declaration | `MOS-TRAIN-088` | yes | yes, recorded | yes, with a written reason | This cohort comes from one institution, and the evidence you are producing is meant to be used by others. / Evidence from a single site does not support a claim made to other sites. / Add a second institution, or change what this dataset is for: a single institution is permitted for site acceptance evidence and is not permitted for publisher evidence. |
| Seal geometry refusal | `MOS-TRAIN-209`, `MOS-TRAIN-111`, `MOS-IMG-046` | yes | no | no | A study in this dataset has image geometry your preprocessing rules say to reject — for example a tilted gantry beyond the declared limit. / Sealing the dataset with it would mean the model is taught on an image the serving side would refuse. / Remove the study, or change the preprocessing rules. Changing the rules creates a new preprocessing version and therefore a new model lineage; it does not re-open this dataset. |
| `test` partition requested | `MOS-TRAIN-141`, `MOS-TRAIN-214` | yes | no | no | Something in this run asked for the exam group. / The exam group is the only honest measurement you have left. Reading it during training or tuning spends it silently. / Nothing here needs fixing by you: the request was refused and the run continues on the learning and tuning groups. If you built the run yourself, the step that asked is named below. |
| Acceptance cohort requested by the pipeline | `MOS-TRAIN-207`, `MOS-EVID-082` | yes | no | no | The training pipeline asked to read the acceptance dataset. / The acceptance dataset is what the deployment gate will judge this model with. A model that has seen it cannot be judged by it. / Nothing here needs fixing by you. The read was refused. |
| Tenant has not permitted training use | `MOS-TRAIN-072`, `MOS-API-112` | yes | no | no | This organisation has not turned on the use of its clinical data for model development. / There is no per-study, per-user or per-environment exception, and no administrator override. / An authorised person must set the organisation's training data policy, with a scope and an expiry date. Until then nothing here can harvest a study. |
| Acceptance cohort too small | `MOS-EVID-026` | yes | no | no | This acceptance dataset has fewer than 30 patients. / Below 30 no acceptance criterion can return anything except "cannot tell", so the gate would be theatre. / Add patients, or use this dataset for something other than acceptance. |
| Deployment gate `FAIL` | `MOS-EVID-090`, `MOS-EVID-092` | yes | no | no | The candidate did not meet the acceptance criteria for this capability. / The criteria are the clinical statement of what this model has to achieve; a model that does not meet them is not permitted into clinical use. / The failing criteria are listed with the numbers behind each. The model currently in service has not been touched and is still running. |
| Deployment gate `INDETERMINATE` | `MOS-EVID-090`, `MOS-EVID-113` | yes | no | no | The gate could not decide — usually because a criterion had too few cases to measure. / "Cannot tell" is not "passed", and it is not "failed" either. / The undecidable criteria are named with the reason for each. The model currently in service has not been touched. |
| Approval attempted by the wrong principal | `MOS-TRAIN-174`, `MOS-SEC-158`, `MOS-SEC-042` | yes | no | no | You do not hold the permission to approve this. / Approval is a separate human decision from producing the evidence, held by a separate permission on purpose. / A person holding `artifact.approve` in this organisation must perform it. The platform will not grant that permission to any automated account, and this surface cannot request it for you. |
| Bulk approval attempted | `MOS-TRAIN-232` | yes | no | no | More than one model was submitted for approval in one action. / Each approval is a separate statement about a separate model. Approving several at once is the shape where every approval is still a human act and nobody read anything. / Approve one model at a time. |
| `auto_promote` attempted | `MOS-TRAIN-184`, `MOS-REG-080` | yes | no | no | Automatic promotion cannot be switched on here. / Another deployment of this same capability is in clinical use in this environment. Automatic promotion beside it is one setting away from a model reaching patients with no human decision. / There is no configuration that changes this. Promote by making the decision. |
| Transform-chain byte-equality failure | `MOS-TRAIN-132`, `MOS-TRAIN-133` | yes | no | no | The image preparation used for training does not produce byte-identical output to the one used when serving. / The model would be fed subtly different images in production than it was taught on, and every version number and digest would still match. / The differing step is named below. This is a defect in the generated chain, not something to configure around. |
| Re-nominating a different trial | `MOS-TRAIN-217`, `MOS-TRAIN-216` | yes | no | recorded act only | You are choosing a different configuration after having already measured one on the exam group. / Doing that quietly is choosing on the exam group, one candidate at a time. / It is allowed as a recorded act: it creates a new search record naming the earlier one, requires a written reason, and spends the exam group again. The current count for this split is shown before you continue. |
| Annotation tool asked to train | `MOS-TRAIN-106` | yes | no | no | The annotation tool tried to train on the labels being collected. / A tool that learns from the labels it is collecting and then suggests the next case from that model is a closed loop with no dataset, no split, no evaluation and no report behind it. / This is switched off at the server and cannot be switched on here. |
| No model can run this study | `MOS-REG-069`, `MOS-REG-055` | no (clinical) | — | — | This study was not analysed, and that is a correct outcome, not a fault. / The reason is shown below verbatim. / Nothing is broken and re-running will not change the outcome. |

**MOS-UI-333** A refusal MUST NOT render a control that performs the refused act, a control that
appears to retry it unchanged, or a link to documentation as a substitute for the three-part text.
Where a resolution exists that the signed-in user can perform, the surface SHOULD offer it as a
control, and that control MUST perform the *recorded* remedy — for L2, writing a `patient_key_aliases`
row, re-sealing the `DatasetVersion` with a `parent_version_id` and a merge `derivation`, and
re-freezing the split (`MOS-TRAIN-118`, `MOS-TRAIN-119`) — never the shortcut the refusal named as
forbidden.

**MOS-UI-334** Where a gate is waivable, the waiver control MUST require a free-text rationale typed in
that session, MUST render the waiver discipline before it is taken — that the waiver is reproduced in
full in every `ValidationReport` citing the cohort and that there is no silent waiver (`MOS-EVID-036`,
`MOS-TRAIN-091`) — and MUST state which specific consequence follows, such as the string
`single-site cohort` being forced into the report's cohort summary for a waived C1 or C7. Where a gate
is not waivable, the surface MUST NOT render a waiver control at all, not even disabled. L1, L2, L3
and L5 are never waivable for a training run: `MOS-TRAIN-115` states that a waiver is a statement
about what a report may claim and is not a licence to fit on the test set.

**MOS-UI-335** Refusal text MUST live in a versioned string catalogue in the repository, one record per
gate id, MUST NOT be assembled from fragments at render time, and MUST be reviewable as prose by
someone who is not a programmer. Tenant customisation MAY add a locale or an addition to the
`what_would_resolve_it` member and MUST NOT remove or replace `what_is_wrong` or `why_it_blocks`,
following the addition-only discipline `MOS-SAFE-080` applies to the assertive-diagnosis lexicon.

**MOS-UI-336** CI MUST enumerate every gate reachable from either surface — by walking the problem
`type` URIs the surfaces can receive and the blocking checks of `MOS-EVID-034`, `MOS-TRAIN-088` and
`MOS-TRAIN-115` — and MUST fail when any has no refusal record, when a record is missing a member,
when a member is empty, or when `what_is_wrong` or `why_it_blocks` contains a `code`, a table name, a
field path, an HTTP status or a requirement ID. The last clause is the one that actually bites: it is
how `SPLIT_L1_PATIENT_IN_TWO_PARTITIONS` stays out of the sentence a clinician reads.

**MOS-UI-337** A `class: clinical_rejection` outcome MUST NOT be rendered with the affordance of
`client_error`, `transport_failure` or `system_failure` on either surface (`MOS-API-039`,
`MOS-EXEC-016`). `REJECTED` means *this study was not analysed, and that is a correct outcome*;
`FAILED` means *this study should have been analysed and the platform could not do it*
(`MOS-EXEC-014`). The distinction MUST be carried on at least four independent channels — wording,
heading treatment, box treatment and hue — so that it survives a stylesheet failing to load and a
red-green colour-blind reader. The shipped implementation already does this:
`medos/web/ohif-extension/src/core/client.js` exports `outcomeKind()` as the single decision point, and
`medos/web/ohif-extension/src/core/render.js` carries `REJECTED_COPY` and `FAILED_COPY` as named constants
for exactly that reason.

**MOS-UI-338** Where the resolution is not available to the signed-in user, the refusal MUST say so in
words and MUST name the permission or the role that can perform it, without rendering a request
control (`MOS-UI-317`). "Ask an administrator" is not a resolution; "a person holding
`artifact.approve` in this organisation must perform this, and this screen cannot request it for you"
is.

---

### 19.8 Safety and marking

**MOS-UI-345** Every surface that displays an AI-derived finding MUST display, adjacent to the finding
and without requiring interaction, the full `MOS-SAFE-012` set: `service_id`, the `ServiceVersion`,
`legal_manufacturer.name`, the `clinical_use_mode` of the producing deployment, and the
`review_status` of the `Result`. `MOS-SAFE-053` requires the API to carry `derivation: "ai_derived"`,
`clinical_use_mode`, `service_id`, `service_version`, `legal_manufacturer_name` and `review_status` at
the top level of the `Result` precisely so that a client cannot omit the marking by accident; a client
that reads them from a nested metadata bag or reconstructs them from a second request is
non-conformant.

**MOS-UI-346** Where `clinical_use_mode` is `research_only`, the surface MUST render the RUO badge
without interaction (`MOS-SAFE-041`). Research results MUST NOT appear in any list, feed, worklist or
count by default: including them requires both the explicit `include_research=true` query parameter
and the `result.read.research` permission, and when included every item MUST carry its
`clinical_use_mode`. A surface that defaults research results into a mixed list has defeated
`MOS-SAFE-041` at the one place a reader would notice.

**MOS-UI-347** Where `training_population` is `null`, the surface MUST render the literal string
`Training population not declared by the publisher` (`MOS-SAFE-016`), and MUST render
`not_validated_for[]` and `known_failure_modes[]` verbatim wherever the model is chosen or its result
is read (`MOS-SAFE-015`, `MOS-REG-032`). `MOS-SAFE-014` requires a nullable declared value to surface
as *not declared* rather than be hidden; a model picker that shows only the models with complete
metadata is hiding the declaration that matters most.

**MOS-UI-348** No surface MUST present a prediction as a diagnosis. Concretely: the platform-generated
disclaimer content — `AI-derived by <service_id> <service_version>. Machine estimate, not a diagnosis.`
— MUST be rendered wherever the result is rendered, not only in the DICOM object; no surface string
MUST assert a finding without an attribution token, under the same lexicon `MOS-SAFE-080` enforces on
generated narrative (`diagnosis of`, `diagnosed with`, `confirms`, `confirmed`, `rules out`,
`excludes`, `is definitely`, `proves`, each requiring `AI`, `automated`, `model`, `estimated`,
`suggests` or `consistent with` in the same sentence); and the tenant lexicon MUST be extensible by
addition only. CI MUST run the lexicon check over the surfaces' string catalogue, not only over
generated narrative.

**MOS-UI-349** No surface MUST offer a control that actions a result. `MOS-SAFE-057` stores and
displays results and auto-actions nothing; `MOS-SAFE-063` fixes the only permitted side effects of a
`ResultReview` transition as an `AuditEvent`, a webhook, a metric increment and — under an explicit
per-deployment setting in `clinical` mode — one new SR revision, and forbids any transition from
enqueueing a Job, altering a stored DICOM object, notifying a patient, writing to an external clinical
system, retraining anything or changing a Deployment. `MOS-SAFE-073` row 12 makes asserting a finding
to a clinician by page, SMS or escalation a DENY with no transport wired. A "notify the referring
physician" control, an "add to worklist" control or a "send to EMR" control MUST NOT exist on either
surface. `MOS-SAFE-011` refuses any `ServiceVersion` declaring `intended_use.autonomy: autonomous` at
deployment, and the surface MUST NOT render autonomy as a setting.

**MOS-UI-350** A `REJECTED` review MUST NOT hide or remove the `Result`; it MUST render as
`review_status = REJECTED` on every surface and MUST be returned by the API (`MOS-SAFE-064`). The
reviewer's disagreement is data. `review_status` is a derived projection of the highest-round
`ResultReview` row and MUST NOT be writable through any client (`MOS-SAFE-062`).

**MOS-UI-351** Neither surface MUST place direct-identifier PHI in a URL path, a query string, a
fragment, `localStorage`, `sessionStorage`, IndexedDB, a browser console log, a client-side error
report, a telemetry payload or a third-party analytics call. `MOS-API-006` already forbids
`PatientID`, `PatientName`, `PatientBirthDate`, `AccessionNumber`, `StudyDescription`,
`SeriesDescription` and any free-text DICOM field in a request path, query string, problem document,
response header, webhook or SSE payload; `StudyInstanceUID` and `SeriesInstanceUID` are permitted.
`MOS-UI-351` extends the same list to browser-side storage and egress, which `MOS-API-006` does not
reach. Persisted client state MUST be limited to non-PHI preferences; a study identifier MAY be
carried in the URL only as a `StudyInstanceUID`.

**MOS-UI-352** Neither surface MUST hold a PACS credential, a DICOMweb URL to the archive, an object
store credential or a database credential. `MOS-SAFE-089a` states it for the viewer button; it holds
for both surfaces. Imaging reaches the browser through the platform's own route, and the DICOM Gateway
remains the sole PACS credential holder. This is verifiable from the served bytes rather than from
review, and already is: `medos/web/ohif-extension/tests` assert that the served page, **with comments
stripped**, contains no `Authorization` header, no `btoa(`, no archive port and no credential, and that
`/api/v1/jobs` is reachable from the viewer's origin through the nginx proxy so the client's `fetch`
is same-origin. That assertion form MUST be extended to the engineering surface.

**MOS-UI-353** Both surfaces are chapter 10 API clients and hold sessions accordingly. A platform
session MUST be OIDC Authorization Code with PKCE; the implicit flow MUST NOT be used
(`MOS-SEC-016`). Tokens MUST NOT be persisted in `localStorage`. The surfaces MUST NOT hold an
`ApiKey` on behalf of a user, and MUST NOT present an API key as a sign-in method for a human.

**MOS-UI-354** Tenancy MUST be resolved server-side from the credential and bound to the request
context; neither surface MUST send a tenant identifier as a client-supplied header, query parameter or
body member, and neither MUST offer a control that changes tenant within a session. `MOS-SEC-019`
makes a tenant resolvable from the issuer alone and requires rejection of a token whose issuer maps to
more than one tenant; `MOS-SEC-055` fixes the evaluation order with tenant resolution as step 2;
`MOS-SEC-072` and `MOS-SEC-074` put the enforcement in row-level security bound to a
transaction-scoped session variable. A tenant picker in a client is a request to be trusted about the
one thing the platform never trusts a client about.

**MOS-UI-355** Where either surface acts through a delegated context, the effective permission set MUST
be the intersection of the delegating principal's current permissions, the credential's `scope`, and
the permissions declared by the executing manifest — never a union, and a component MUST NOT be able to
widen its own set (`MOS-SEC-045`). The surface MUST render the effective set it is acting under on any
screen that performs a governance-class act.

**MOS-UI-356** Neither surface MUST create a second path to any act the API already owns. There is
exactly one job-creation endpoint (`POST /api/v1/jobs`, `MOS-API-043`), exactly one idempotency-key
derivation (`MOS-API-030`, `MOS-EXEC-053`), and no direct database, broker, object store or Gateway
route from a browser. The shipped extension is built this way on purpose: `MedicalOSClient.submit()`
in `medos/web/ohif-extension/src/core/client.js` is the single job-creation path, holds an `inFlight` map
keyed by `${studyInstanceUID}|${capabilities.join(',')}` so a double activation returns the same
promise, and is shared byte-for-byte by the React panel and the standalone page.

**MOS-UI-357** The engineering surface MUST NOT render direct-identifier PHI at any point in the
dataset, split, annotation-campaign or training flows. Cohorts are addressed by `patient_key`,
institutions by `institution_key` — `HMAC-SHA256(tenant_salt, InstitutionName)` truncated to 10 bytes
and base32-encoded, with the raw institution name never stored on a candidate, in a split manifest or
in a report (`MOS-TRAIN-089`) — and accessions by `accession_number_hash` (`MOS-EVID-035`). A curation
queue that needs pixels to make an inclusion decision MUST obtain them through the Gateway as the
`dataset_export` consumer class (`MOS-TRAIN-095`), never through a PACS route.

**MOS-UI-358** Every annotation session reached from the engineering surface MUST authenticate as a
MedicalOS `User`, and the `reader_id` written into `annotation_readers` MUST derive from that identity.
A shared login, an anonymous session, a service account or an authentication-disabled deployment MUST
be refused (`MOS-TRAIN-096`). A no-code surface makes a shared workstation login the path of least
resistance, and `MOS-EVID-038` requires an `AnnotationSet` to *name* its readers — a requirement a
shared account makes unsatisfiable after the fact rather than at the time.

**MOS-UI-359** The platform MUST publish a **UI contribution statement** enumerating every user-visible
element it renders or writes that bears on safe interpretation of a result, and this chapter's surfaces
MUST be enumerated in it. `MOS-CONF-242` establishes the obligation and records it as a gap: under IEC
62366-1 clause 6 a user interface the manufacturer did not develop is a User Interface of Unknown
Provenance, and MedicalOS's viewer surfaces are UOUP to the publisher in the way MedicalOS is SOUP
under 62304. The statement MUST cover, at minimum, the marker sets of `MOS-SAFE-048`, `MOS-SAFE-049`
and `MOS-SAFE-050`, the burned-in RUO banner of `MOS-SAFE-051`, the `MOS-SAFE-012` adjacency set, the
`MOS-SAFE-088` provenance panel contents, the `REJECTED`-versus-`FAILED` distinction of
`MOS-UI-337`, the `MOS-DATA-074` ambiguity flag, the `MOS-SAFE-062` `review_status` projection, the
`MOS-SAFE-016` "Training population not declared by the publisher" string, and — added by this chapter
— the refusal catalogue of `MOS-UI-332` and the metric vocabulary of `MOS-UI-310`. Chapter 18 records
that `MOS-SAFE-012`, `MOS-SAFE-041`, `MOS-SAFE-047` and `MOS-TRAIN-011` are "specific UI obligations,
not a usability engineering process", and that usability engineering under IEC 62366-1 is a hard gap
with no owner on either the SITE or PUBLISHER side (`MOS-CONF-139`). This chapter does not close that
gap and MUST NOT be read as closing it; it makes the platform's contribution enumerable, which is the
precondition for a publisher to close their half.

**MOS-UI-360** A long-running act MUST NOT depend on a held browser session. Training, sealing,
annotation export, evaluation and gate evaluation MUST be server-side records the surface polls or
subscribes to, so that a session expiry, a closed tab or a reload loses nothing and duplicates nothing.
The surfaces MUST NOT hold work in client memory across a refresh, and MUST NOT re-issue a mutating
request on reload.

---

### 19.9 Release placement and non-goals

#### 19.9.1 Placement

**MOS-UI-365** This chapter's contents are assigned to releases as follows, consistent with chapter 15
§15.1.2 and chapter 17's `MOS-TRAIN-190`. The honest summary is the two lines under the table, not the
table.

**CUT at specification 0.4.0.** Every row of the table below whose subject is the engineering
surface (the training submit surface, the configuration-search surface, the promotion screen, the
conversion surface, the refusal catalogue and the §19.3 screens generally) is withdrawn with §19.3
and does not ship in 0.4.0 or any release this chapter currently names; the table is retained as
placement history, exactly as §19.3's text is retained. Only the clinician-surface rows survive the
cut, and of those only the 0.4.0 worklist row names work that is not already shipped.

| Element | 0.1.0 | 0.2.0 | 0.3.0 | 0.4.0 |
|---|---|---|---|---|
| Toolbar button + provenance panel, standalone host (`MOS-SAFE-089a`, `MOS-SAFE-088`) | **ships** | unchanged | unchanged | unchanged |
| Same two surfaces inside ~~a rebuilt OHIF bundle~~ the viewer bundle the release deploys — **AMENDED at specification 0.4.0**, so that the element and the tier `MOS-UI-371` assigns it name the same thing | Tier B, see `MOS-UI-369` | **ships** | unchanged | unchanged |
| `REJECTED`/`FAILED` distinction on four channels (`MOS-UI-337`) | **ships** | unchanged | unchanged | unchanged |
| Submit against a fixed configured capability list | **ships** | unchanged | superseded by the picker | unchanged |
| Clinician **model picker** driven by capability resolution | — | — | **ships** (`MOS-UI-366`) | unchanged |
| `MOS-SAFE-012` adjacency set on every result surface | **ships** | unchanged | unchanged | unchanged |
| RUO badge, `include_research` gating (`MOS-UI-346`) | — | **ships** | unchanged | unchanged |
| `ResultReview` surface, `REJECTED` review rendering (`MOS-UI-350`) | — | **ships** | unchanged | unchanged |
| Curation queue surface (`HarvestCandidate`, `CurationBatch`, exclusion vocabulary) | — | **ships** | unchanged | unchanged |
| Training-policy surface (`training_use_allowed`, `TrainingDataPolicy`) | — | **ships** | unchanged | unchanged |
| Seal, split-freeze and leakage/stratification refusal rendering (`MOS-UI-332` rows L1–C7) | — | **ships** | + run-time re-execution refusals | unchanged |
| Annotation campaign surface over MONAI Label (`MOS-UI-358`) | — | **ships** | unchanged | unchanged |
| Refusal catalogue + CI completeness check (`MOS-UI-335`, `MOS-UI-336`) | seed set for 0.1.0 gates | **full 0.2.0 set** | **full 0.3.0 set** | unchanged |
| Training submit surface, run monitoring | — | — | **ships** | unchanged |
| Configuration-search surface, search diagnostics block (`MOS-UI-303`, `MOS-UI-304`) | — | — | **ships** | unchanged |
| Promotion screen: dossier, three acts, `test_exposure_count` (`MOS-UI-318`, `MOS-UI-320`) | — | — | **ships** | unchanged |
| Conversion surface (E1/E2/E3 rendering) | — | — | **ships** | unchanged |
| Honest-metric display rule end to end (19.5) | partial — no search exists | partial — no training exists | **fully in force** | unchanged |
| Unattended-arrival worklist surface | — | — | — | **ships** |
| Second-viewer verification procedure (`MOS-IMG-158`) | — | — | — | **ships** |
| UI contribution statement (`MOS-UI-359`) | — | first issue | revised | revised |

**MOS-UI-366** The clinician surface is mostly reachable today and its model picker is not.
Chapter 6 records `Resolve()` as **not shipped** in 0.1.0 and 0.2.0 — the dispatcher writes the single
configured `ServiceVersion` pin directly — with full precedence, ranges, canary bucketing and the
evidence and regulatory gates arriving in 0.3.0; chapter 15 places the `Artifact` registry and
capability resolution as a pure function in the 0.3.0 contents. Until then the "choose a model" step is
honestly a choice among a configured list, and the surface MUST present it as such: it MUST render the
list as configured rather than resolved, MUST NOT imply that a model was selected for this study, and
MUST NOT render a rank, a score or a recommendation beside an entry. A picker that looks like
resolution before resolution exists is a claim about evidence gating that nothing is enforcing.

**MOS-UI-367** — **WITHDRAWN at specification 0.4.0.** The engineering surface it gated is withdrawn
with §19.3, and its dependency argument is retained as the reason the surface was never built on a
partial substrate: ~~The engineering surface needs the whole of 0.2.0 and the whole of 0.3.0 beneath it, and
no part of it MAY ship earlier. Its 0.2.0 dependencies are the evidence plane entities and the
curation, sealing, splitting and annotation elements `MOS-TRAIN-190` places there; its 0.3.0
dependencies are `TrainingRun` with the `Orchestrator` port and one driver, `ConversionRun`,
`ConfigurationSearch` with its provenance and budget bound, and the three-act promotion.~~ A no-code
training surface built on a partial substrate would be a surface whose refusals are not yet enforced,
which inverts the safety argument of this chapter: the gates are what make this audience safe, and a
gate that is not yet blocking is worse on this surface than on a CLI, because there is no operator here
who would have noticed its absence.

**MOS-UI-368** Neither surface MUST drive a reserved API path. `MOS-API-112` records that
`POST /api/v1/harvest-batches`, `GET` and `POST /api/v1/harvest-candidates/{id}/decision`,
`PUT /api/v1/tenants/{tenant_id}/training-policy` and
`GET /api/v1/harvest-batches/{id}/stratification` are **reserved and MUST NOT be served** until each
has a row in table 10.2-B, a schema in `medos/schemas/`, and a `permission:` in `medos/api/v1/routes.train.yaml` naming a
key that exists in chapter 8's catalogue — and that no harvest, curation-decision or training-policy
permission is registered there today. Until those rows and permissions exist, the engineering surface
has no API to be a client of, and this chapter MUST NOT be read as authorising one. Adding them is
0.2.0 work that `MOS-API-112` already scopes; this chapter adds only the requirement that they exist
before any screen depends on them.

**MOS-UI-369** — **AMENDED at specification 0.4.0.** The status of the OHIF extension in this repository
is a fact, not an assumption, and it constrains 0.1.0 placement. ~~`medos/deploy/compose/docker-compose.yml`
pins `ohif/app:v3.9.2`, a pre-built single-page application; the repository's own README states that OHIF
v3 resolves `extensions` and `modes` from its own webpack bundle at build time, that there is no runtime
registration hook, no UMD extension loader and no `app-config.js` key that can add one, and that
`deploy/compose/ohif-config.js` therefore ships `extensions: []`. Loading the React extension requires
rebuilding the viewer from the OHIF monorepo, which was not performed for the shipped slice.~~ Those were
the facts, and both files they were read out of moved at release 0.4.0: the compose stack pins no OHIF
image and `deploy/compose/ohif-config.js` is deleted. The status they established did not move with them,
it hardened. The React extension was unloadable because the image resolved extensions at build time and
no rebuild was budgeted; it is unloadable now because no viewer is deployed to resolve anything at all,
which is `MOS-UI-013`'s first bullet restated at the placement layer. The premises are struck rather than
corrected in place because this requirement opens by calling them facts, and a requirement that says "a
fact, not an assumption" has to be re-read against the repository or it becomes the assumption it warns
about. What runs today is the standalone host at
`/medicalos/standalone/`, which imports the identical `src/core/*.js` modules the React extension
imports, on the viewer's own origin, and is therefore the same job-creation path and the same renderer
rather than a mock. ~~Chapter 15 §15.1.3 assigns the button Tier B and `MOS-REL-009` permits a tiered item
to be cut with the cut recorded; `MOS-SAFE-089a` is inapplicable rather than failed where it is cut.~~
**AMENDED at specification 0.4.0.** 15.1.3 still assigns Tier B, but `MOS-REL-009` (b) — the only response
that makes a check inapplicable rather than merely unexecuted — reaches a Tier C item only, and chapter 9
struck this requirement's conditionality at 0.4.0. The placement fact above is unchanged; its consequence
is not. `MOS-SAFE-089a` and acceptance check 24 FAIL on a surface with no such control over an open study,
and a Release Decision Record has no cut available that would make them lapse instead.
A specification that asserted the extension loads into the ~~pinned~~ then-pinned image would send an
implementer into a rebuild they have not budgeted; this requirement says plainly that it does not, and
since 0.4.0 that there is no image for it to load into.

**MOS-UI-370** Gate checks this chapter adds to chapter 15's rows MUST be stated in observable terms and
MUST NOT name a product (`MOS-REL-008`). The names are `rejection-distinct` (already in the 0.1.0 row),
`refusal-completeness`, `headline-partition`, and `duty-separation`, each defined by the
correspondingly named acceptance criterion below and each executed in that release's CI run
(`MOS-REL-012`).

| Gate name | Release row | What it asserts |
|---|---|---|
| `rejection-distinct` | 0.1.0 (existing) | A study with no eligible series terminates `REJECTED` with a machine-readable reason, not `FAILED`, and renders distinctly |
| `refusal-completeness` | 0.2.0, extended 0.3.0 | Every blocking gate reachable from a surface has a complete, non-empty, code-free three-part refusal record |
| `headline-partition` | 0.3.0 | No figure rendered in a headline position originates from a run whose partition any selection touched |
| `duty-separation` | 0.3.0 | No single authenticated flow performs both a `TrainingRun` submission and an approval act |

**MOS-UI-371** — **AMENDED at specification 0.4.0.** Every element in the `MOS-UI-365` table MUST carry a
tier in chapter 15 §15.1.3 so that `MOS-REL-009` is decidable for it. This chapter proposes: the clinician
surface's marking, refusal and `REJECTED`/`FAILED` obligations are Tier A and MUST NOT be cut, because
they are safety-display requirements other chapters already make MUST; the surfaces' hosting form
~~(rebuilt OHIF versus standalone)~~ (the first-party surface at `/mos-viewer/` versus the standalone host
at `/medicalos/standalone/`) is Tier B; the search diagnostics block, the conversion surface and the
unattended worklist are Tier C. An acceptance criterion is never cut; only scope is (`MOS-REL-006`,
`MOS-OPEN-041`).

The tiering is unchanged and the Tier A clause is untouched — chapter 15 §15.1.3 follows it exactly, and
`docs/spec/15-delivery.md` says where it does and where it stops. What was re-pointed is the parenthetical
naming the two forms the hosting choice lay between. At 0.2.0 the choice was to rebuild the viewer from
its monorepo so the React extension loads, or to ship the standalone host that runs the same
`src/core/*.js` modules; release 0.4.0 took neither and built the surface. The element stays Tier B for
the reason it always was: which host serves the clinician surface is a delivery decision `MOS-REL-009` may
cut and re-take, and no Tier A display obligation depends on it. A tier is an attribute of an element in
the `MOS-UI-365` table, and the element is the hosting form, not the product that happened to be one of
its options.

**MOS-UI-372** Each deferred element MUST be deferred behind a named seam that exists from the release
in which its first driver ships (`MOS-REL-023`). The seams are: the `/api/v1` surface itself for every
clinician-facing element; the reserved paths of `MOS-API-112` for every curation and policy element;
the `Orchestrator` port of `MOS-TRAIN-122` for every training element; and the `CurationBatch` boundary
of `MOS-TRAIN-202` for every data-entry element. No element in the table requires a seam this
specification does not already have.

#### 19.9.2 Non-goals

**MOS-UI-373** — **WITHDRAWN at specification 0.4.0.** ~~MedicalOS MUST NOT build a viewer and MUST NOT
fork one. Chapter 15's build-versus-adopt register (`MOS-REL-027`) adopts OHIF unmodified and pinned by
version, records that nothing in rendering is built, and puts the extension in a separate package
specifically so the pinned OHIF version stays upgradable. No rendering, windowing, reformatting,
measurement or overlay implementation belongs to this chapter. Correctness of the output objects MUST be
verified in a second, independent viewer at 0.4.0 (`second-viewer`), because a single-viewer check
verifies the viewer, not the objects.~~

This is the requirement register entry 103 missed, and the miss is worth stating exactly, because that
entry was CLOSED at specification 0.3.0 on a claim of having found everything underneath `MOS-UI-009`. It
found two things the entry had not anticipated — that the non-goals table had never recorded
`MOS-CORE-038`'s reversal at all, and that `MOS-UI-010` descended from a second non-goal that had to be
bounded rather than reversed with it — and then stopped. It did not look further along this chapter.
`MOS-UI-373` states the same prohibition as `MOS-UI-009` in fewer words, in this chapter's own non-goals
subsection, seventeen hundred lines below the subsection that withdrew it, and it survived the amendment
whose whole purpose was to remove it. A chapter that withdraws a non-goal in §19.1.2 and restates it in
§19.9.2 forbids in its last section what it permits in its second; a conformance reader who reaches
§19.9.2 first finds the shipped viewer non-compliant with nothing on the page to say the question was
settled in §19.1.2.

The reason a careful amendment missed it is the reason it is recorded here rather than fixed quietly.
`MOS-UI-373` cites `MOS-CORE-038` nowhere — not by identifier, not by name — so a grep for the withdrawn
non-goal, or for the requirement that cited it, returns nothing for this line. A requirement is withdrawn
by amending every place that states it, and the places that restate its content without citing it are
exactly the ones a citation search cannot find. That is a property of this specification's method, not of
this chapter, and it belongs in the register with the entry that closed early.

**MOS-UI-373a** MedicalOS MUST NOT fork an adopted viewer, and MUST NOT rest a claim about the
correctness of an object it wrote on its own rendering of that object. Two of the withdrawn
requirement's three clauses survive the reversal intact, and they are restated so that the clause which
went does not take them with it:

- **No fork.** `MOS-REL-027`'s upgradability boundary is untouched by building a first-party surface: a
  forked dependency is one nobody can upgrade, and whose `MOS-REL-038` SBOM entry names a working tree
  rather than a release. This binds the extension package at `medos/web/ohif-extension/`, MONAI Label's client
  wherever §19.4 lands it, and any viewer adopted for the check below.
- **The second-viewer check.** Correctness of the output objects MUST be verified in a second,
  independent viewer at 0.4.0 (`second-viewer`), because a single-viewer check verifies the viewer, not
  the objects (`~~MOS-IMG-157~~ MOS-IMG-157a`, `MOS-IMG-158`). This clause did not weaken when the platform began building
  a renderer — it became the only thing standing between "our viewer draws it" and "the object is
  correct", and it is the obligation `MOS-UI-009a` leans on when it calls the reversal safe. It has not
  been run in this repository, by test or by hand, and `MOS-UI-013b` states what may not be claimed until
  it is.

What is no longer a non-goal is the first clause. Building a viewer is permitted under `MOS-UI-009a`, on
the four guarantees stated there, and no restatement of the prohibition belongs in this section.

**MOS-UI-374** MedicalOS MUST NOT build an annotation tool. MONAI Label is the adopted annotation
surface for mask, bounding-box and point types (`MOS-TRAIN-095`), and the platform's contribution is
exactly three things: the authentication shim binding every session to a `User` (`MOS-TRAIN-096`), the
export writing per-reader masks into an `AnnotationSet` manifest, and the consensus computation, which
`MOS-TRAIN-105` requires `medicalos-evidence` to perform rather than the tool. Building a fourth is out
of scope, and building a first-party in-house annotation UI would put the platform in the position
`MOS-TRAIN-106` exists to prevent.

**MOS-UI-375** Neither surface MUST offer capability authoring. A `Capability`, its
`AcceptanceCriteria` and its `ApplicabilityEnvelope` are clinical declarations with digests and
versions; `MOS-EVID-076` requires `AcceptanceCriteria` to be a declarative document rather than code
for the reason `MOS-TRAIN-220` restates for the search space — a document that is code has no digest
that means anything. A no-code form that emits one would let an operator with no clinical authority
define what "good enough" means for a clinical function. Authoring stays where the digest and the named
author are.

**MOS-UI-376** Neither surface MUST offer PACS administration: no AE title configuration, no DICOM node
management, no query/retrieve console, no storage-commitment tooling, no archive credential entry, no
routing rules. The DICOM Gateway is the sole PACS credential holder and the sole PACS route
(`MOS-UI-352`), and a browser surface that could reconfigure it would be a browser surface that could
redirect patient data.

**MOS-UI-377** Neither surface MUST offer model artifact editing, weight substitution, `config.pbtxt`
editing, `PreprocessingSpec` editing on a registered version, threshold editing outside a recorded
`tune`-partition run (`MOS-TRAIN-142`), or envelope widening outside a run that covers the widened
bound (`MOS-EVID-098`). `MOS-TRAIN-171` forbids a node-editable configuration file precisely because it
is a way to change serving numerics without changing a `ModelVersion`; a UI field is the same hazard
with a better affordance.

**MOS-UI-378** Neither surface MUST implement or expose automated retraining on a monitoring signal
(`MOS-TRAIN-194`), continual or online learning on production traffic (`MOS-TRAIN-195`), or active
learning as anything other than a ranking of unannotated candidates that each enter the `CurationBatch`
as an ordinary candidate with a recorded human disposition (`MOS-TRAIN-196`). A "retrain on drift"
button is the automated path from production data to a serving model assembled from four individually
reasonable components, and a no-code surface is where it would be assembled first.

---

### Acceptance criteria

Each check is executable by a CI job, by a headless-browser run against the compose stack, or by a
reviewer with database access. Failing any of them means this chapter is not implemented. Checks 1–7
cover the clinician surface, 8–14 the engineering surface, 15–20 the honest-metric rule, 21–25
separation of duties, 26–29 refusals, 30–34 safety and marking, 35–38 placement and non-goals.

1. **One study, one submission, one job.** With a single study open, one activation of the analyze
   control issues exactly one `POST /api/v1/jobs` carrying that study's `study_instance_uid`, and a
   second activation within the same in-flight window issues none. A principal without `job.create` is
   refused before any request is issued. Assert by counting requests in a network capture, not by
   reading the code (`MOS-SAFE-089a`, `MOS-API-043`).

2. **`REJECTED` and `FAILED` render distinctly.** Submit a study the archive does not hold and a study
   whose worker is made to fail. Assert the two terminal states differ on **four** independent
   channels — wording, heading treatment, box treatment and hue — by rendering both with the stylesheet
   disabled and asserting the headings and body copy still differ, and by rendering both in a
   greyscale-forced profile and asserting they still differ. Assert the `REJECTED` screen contains no
   error iconography and no retry control, that its machine-readable reason appears verbatim in a
   monospace element with no case change, and that the `FAILED` screen does not describe the study as
   unassessable for a clinical reason (`MOS-EXEC-014`, `MOS-EXEC-016`, `MOS-API-039`, `MOS-UI-337`).

3. **Marking is adjacent and unconditional.** For every result-bearing screen on both surfaces, assert
   the `MOS-SAFE-012` set — `service_id`, `ServiceVersion`, `legal_manufacturer.name`,
   `clinical_use_mode`, `review_status` — is present in the initial render with zero interaction and
   zero additional network requests. Remove each field from the API response in turn and assert the
   surface renders a named absence rather than omitting the row (`MOS-UI-345`, `MOS-SAFE-053`).

4. **Research results do not leak into a clinical list.** Query a result list with a mix of `clinical`
   and `research_only` results as a principal holding `result.read` but not `result.read.research`;
   assert zero research items and zero research items in any count, badge or total. Repeat with
   `result.read.research` and without `include_research=true`; assert the same. Repeat with both;
   assert every item carries `clinical_use_mode` and every research item renders the RUO badge
   (`MOS-SAFE-041`, `MOS-UI-346`).

5. **No surface asserts a diagnosis.** Run the `MOS-SAFE-080` lexicon check over the compiled string
   catalogue of both surfaces and over the refusal catalogue; assert zero hits without an attribution
   token. Assert the `Machine estimate, not a diagnosis.` disclaimer renders wherever a finding
   renders. Assert removing an entry from the tenant lexicon is rejected (`MOS-UI-348`).

6. **No result-actioning control exists.** Grep the compiled surfaces for controls whose handler
   enqueues a Job, calls an external system, or mutates a Deployment from a result context; expect
   zero. Drive a full `ResultReview` cycle through the surface and assert zero rows in `jobs` and zero
   STOW-RS calls when `emit_verified_sr_on_accept` is false (`MOS-SAFE-063`, `MOS-UI-349`).

7. **A `REJECTED` review hides nothing.** Submit a `REJECTED` review through the surface; assert the
   `Result` is still listed, still openable, and renders `review_status = REJECTED` on every surface
   that showed it before (`MOS-SAFE-064`, `MOS-UI-350`).

8. **The engineering surface cannot act as the pipeline.** Query the surface's server-side session
   handling and assert every request it issues carries the signed-in `User`'s credential. Assert no
   code path obtains a `training_pipeline` role credential. Compare the seeded `training_pipeline`
   role's `permissions` array against `MOS-SEC-158`'s list exactly, and assert `artifact.approve` and
   every `deployment.*` permission are absent (`MOS-UI-316`).

9. **No reserved path is served.** Assert the OpenAPI document contains no route for
   `POST /api/v1/harvest-batches`, `/harvest-candidates/{id}/decision`,
   `PUT /tenants/{tenant_id}/training-policy` or `/harvest-batches/{id}/stratification` unless each has
   a table 10.2-B row, a schema and a registered `permission:` key. Assert the engineering surface
   issues no request to a path absent from the document (`MOS-API-112`, `MOS-UI-368`).

10. **A non-waivable gate has no waiver control.** Construct a split failing L1, then L2, then L3, then
    L5. For each, assert the surface renders the refusal, renders no waiver control in any state
    including disabled, and that submitting a `TrainingRun` with a `MOS-EVID-036` waiver attached is
    refused before the first batch is loaded (`MOS-TRAIN-115`, `MOS-UI-334`).

11. **A waivable gate states its consequence before the waiver.** Construct a cohort failing C1. Assert
    the waiver control requires free-text typed in that session, that the screen states the waiver is
    reproduced in full in every `ValidationReport` citing the cohort, and that it names the specific
    consequence — the literal string `single-site cohort` forced into the cohort summary. Assert a
    waiver submitted with an empty or whitespace rationale is refused (`MOS-EVID-036`, `MOS-TRAIN-091`,
    `MOS-UI-334`).

12. **C5 cannot be waived from the surface.** Construct a cohort failing C5 against a declared envelope.
    Assert the surface renders the refusal, renders the empty bands, and offers no waiver control, while
    the platform-level waiver mechanism remains available to a principal acting outside this surface
    (`MOS-TRAIN-090`, `MOS-UI-332`).

13. **The engineering surface renders no PHI.** Drive the curation, split, annotation and training flows
    end to end and capture every rendered DOM string, every URL, every storage write and every outbound
    request. Assert zero occurrences of `PatientID`, `PatientName`, `PatientBirthDate`,
    `AccessionNumber`, a raw institution name, or any free-text DICOM header field. Assert cohorts
    render by `patient_key`, institutions by `institution_key` and accessions by
    `accession_number_hash` (`MOS-API-006`, `MOS-TRAIN-089`, `MOS-EVID-035`, `MOS-UI-351`,
    `MOS-UI-357`).

14. **Annotation sessions are individually attributable.** Attempt to open an annotation session with a
    shared login, with an anonymous session, and as a `ServiceAccount`; expect refusal in all three.
    Complete one session and assert `annotation_readers.reader_id` derives from the authenticated
    `User` (`MOS-TRAIN-096`, `MOS-UI-358`).

15. **A selection-partition metric never appears as headline performance.** Run a 6-trial
    `ConfigurationSearch` to completion and register the nominated candidate. Walk every screen of both
    surfaces and extract every rendered numeric together with the `evaluation_run_id` it claims. Assert
    that every figure in a headline position, a summary card, a list column named for the capability's
    primary metric, a default sort key, a picker tooltip or an export header resolves to a run whose
    partition is `test` or to an `acceptance` gate run. Assert that `search_trial_score`, nnU-Net's
    fold-aggregated `summary.json` Dice and `AutoRunner`'s per-algorithm validation score appear only
    inside a block labelled `selection statistic — not performance`, and that no such figure exists
    anywhere on the clinician surface. Then set `selection_partition` to `tune` on a fixture and assert
    no new figure is promoted into a headline position (`MOS-TRAIN-215`, `MOS-TRAIN-221`,
    `MOS-TRAIN-222`, `MOS-UI-300`, `MOS-UI-302`).

16. **Every figure names its partition and its run.** Assert every rendered numeric carries a partition
    label and an `evaluation_run_id` in the initial render. Serve a fixture whose partition is absent
    and assert the surface renders `partition not recorded` rather than a bare value (`MOS-UI-301`).

17. **The surface computes nothing.** Grep the compiled bundles for arithmetic over metric fields —
    division, subtraction, mean, percentage or interval construction applied to a value read from
    `capability_claims` or `evaluation_runs`; expect zero. Serve a dossier fixture missing a derived
    field and assert the surface renders its absence rather than deriving it (`MOS-TRAIN-178`,
    `MOS-UI-307`).

18. **Noise is named as noise.** Construct a search with `selection_margin = 0.006` and
    `seed_variance.sd = 0.011`. Assert both render on the same line and that the literal string
    `selection not distinguishable from run-to-run noise` renders as visible text — not a tooltip, not a
    title attribute, not an icon — and survives the stylesheet being disabled. Then set
    `selection_margin = 0.030` and assert the marker is absent (`MOS-TRAIN-127`, `MOS-TRAIN-236`,
    `MOS-UI-305`).

19. **The exposure counter is shown before it is spent.** Assert `test_exposure_count` for the split
    renders on the promotion screen and above any control that starts a candidate `EvaluationRun` on
    `test`. Start one, assert the counter incremented by exactly one, and assert no control anywhere
    resets it (`MOS-TRAIN-216`, `MOS-UI-306`).

20. **`headline_metric_selected_on` is rendered, including when false.** Assert the boolean renders on
    the promotion screen for a conformant run, where it is `false`. Then serve a fixture where it is
    `true` and assert the surface renders it prominently and refuses to render the affected figure in a
    headline position (`MOS-TRAIN-221`, `MOS-UI-304`).

21. **Train and approve cannot be performed by one principal in one act.** Drive a candidate end to end
    as a single authenticated user holding every permission the tenant can grant. Assert that no screen
    presents a control that both submits a `TrainingRun` and performs `artifact.approve` or
    `deployment.approve_clinical`; that A1, A2 and A3 produce three audit records with three distinct
    timestamps; and that when one person performs more than one of them the surface renders a
    persistent non-dismissable statement naming both roles before the approval control. Then run a
    call-graph analysis over the surface's handlers and assert no symbol reachable from a training
    submission handler reaches an approval or deployment-mutation handler (`MOS-TRAIN-173`,
    `MOS-TRAIN-175`, `MOS-TRAIN-189`, `MOS-UI-315`, `MOS-UI-321`).

22. **The promotion screen is not a number and a button.** Render the promotion screen for a candidate
    and assert all fifteen items of `MOS-TRAIN-176` are present; that items 5, 6, 8, 12 and 15 are not
    collapsed by default; that every figure links to an `evaluation_run_id`; that the incumbent's
    figures are labelled as the same-cohort re-run and any historical figure labelled historical; and
    that no `SKIPPED` or `INDETERMINATE` criterion is omitted. Remove any one item from the dossier
    fixture and assert the screen refuses to render the approval control (`MOS-TRAIN-011`,
    `MOS-TRAIN-176`, `MOS-TRAIN-177`, `MOS-EVID-113`, `MOS-UI-318`).

23. **Every forbidden promotion shape is absent.** For each row of `MOS-UI-319`, assert absence: no
    multi-select or array-bodied approval (submitting an array yields RFC 9457 `class:
    schema_violation`); no saved filter with an approval action; no promote control on a trial-list row;
    no single action performing both training and deployment; no `auto_promote` toggle rendered where a
    `clinical` deployment of the same capability exists; no flow in which a gate `PASS` advances to
    cutover; no retry control that re-runs the gate against unchanged evidence (`MOS-TRAIN-232`,
    `MOS-TRAIN-233`, `MOS-TRAIN-182`, `MOS-TRAIN-184`).

24. **The approval rationale is typed, not supplied.** Assert `artifact.approve` is refused with a
    rationale under 20 characters, with an empty one, and with one prefilled by the client. Assert the
    field is empty on load and is not restored from client storage after a reload (`MOS-TRAIN-179`,
    `MOS-UI-323`, `MOS-UI-360`).

25. **No unregistered permission string ships.** Grep both compiled bundles and the refusal catalogue
    for `evidence.report.issue`, `dataset.seal`, `annotation.freeze`, `split.freeze`,
    `evaluation.submit`, `result.publish`, `job.requeue`, `deployment.write`, `service.approve` and
    every other spelling `MOS-SEC-031` and chapter 8 §8.3.2 register as a non-permission; expect zero.
    Assert every permission identifier rendered resolves to a row of the catalogue (`MOS-SEC-033`,
    `MOS-UI-325`).

26. **Every enumerated gate has a plain-language refusal string.** Enumerate every gate reachable from
    either surface by walking the problem `type` URIs the surfaces can receive together with the
    blocking checks of `MOS-EVID-034`, `MOS-TRAIN-088` and `MOS-TRAIN-115`. Assert each has a refusal
    record; that `what_is_wrong`, `why_it_blocks` and `what_would_resolve_it` are all present and
    non-empty; and that none of the three contains a `code`, an HTTP status, a table name, a field path
    or a requirement ID. Delete one record and assert CI fails naming the gate (`MOS-UI-330`,
    `MOS-UI-332`, `MOS-UI-336`).

27. **The three parts render together and the code renders secondary.** Trigger each gate in check 26
    against a live stack and assert the three members render in order in the initial view, and that
    `code`, `type`, `class` and `trace_id` are present verbatim and visually secondary. Assert no `code`
    is title-cased, expanded or translated anywhere (`MOS-UI-331`).

28. **A refusal never offers the forbidden shortcut.** For L2, assert the offered remedy writes a
    `patient_key_aliases` row, re-seals the `DatasetVersion` with a `parent_version_id` and a merge
    `derivation`, and re-freezes the split — and that no control moves a study across partitions. For
    the `test`-partition and acceptance-cohort refusals, assert no retry control is offered
    (`MOS-TRAIN-118`, `MOS-TRAIN-119`, `MOS-UI-333`).

29. **A cohort request for a forbidden partition returns 403, not an empty set, and says so.** Issue a
    `partition: "test"` cohort request from the training principal, from a search driver, from a trial
    process and from the ensemble builder. Assert HTTP 403 with a reason in all four, and assert the
    surface renders the `test`-partition refusal text rather than an empty result list
    (`MOS-TRAIN-141`, `MOS-TRAIN-214`).

30. **No client holds a credential it must not hold.** Fetch every file both surfaces serve, strip
    comments, and assert the served bytes contain no `Authorization` header, no base64 credential
    construction, no archive host or port, no object-store credential and no database DSN. Assert the
    only outbound address is the configured API root and that it is same-origin (`MOS-SAFE-089a`,
    `MOS-UI-352`).

31. **Sessions are OIDC with PKCE and tokens are not persisted in web storage.** Assert the sign-in flow
    is Authorization Code with PKCE, that the implicit flow is unsupported, and that no access or
    refresh token is written to `localStorage`, `sessionStorage` or IndexedDB. Assert neither surface
    presents an API key as a human sign-in method (`MOS-SEC-016`, `MOS-UI-353`).

32. **Tenancy is server-resolved and not client-selectable.** Assert neither surface sends a tenant
    identifier in any header, query parameter or body member, and that no control changes tenant within
    a session. Present a token whose issuer maps to two tenants; expect rejection. Attempt a
    cross-tenant read of a result and a dataset; expect 403 on the REST API (`MOS-SEC-019`,
    `MOS-SEC-055`, `MOS-SEC-072`, `MOS-UI-354`).

33. **Client state survives nothing it should not.** Reload mid-training, mid-annotation and mid-gate.
    Assert no mutating request is re-issued, no work is lost, no duplicate record is created, and every
    in-progress act is reconstructed from a server-side record (`MOS-UI-360`).

34. **The UI contribution statement is complete.** Assert the published statement enumerates every item
    `MOS-UI-359` names, that each entry resolves to a requirement ID that exists, and that the statement
    is regenerated in the same CI run that builds the surfaces. Add a new user-visible safety-bearing
    element to a surface without adding an entry and assert CI fails (`MOS-CONF-242`, `MOS-UI-359`).

35. **Release placement is machine-checkable.** Parse the `MOS-UI-365` table and assert every element
    has a placement in each of the four columns, that every element placed at 0.2.0 or later has a tier
    in chapter 15 §15.1.3, and that each of `refusal-completeness`, `headline-partition` and
    `duty-separation` resolves to a test identifier that exists in the repository (`MOS-REL-008`,
    `MOS-REL-012`, `MOS-UI-370`, `MOS-UI-371`).

36. **The model picker does not claim resolution before resolution exists.** On a 0.1.0 or 0.2.0 build,
    assert the picker renders the configured list, renders no rank, score or recommendation beside an
    entry, and contains no string implying a model was selected for this study. Assert the picker's data
    source is the configured pin rather than a resolution call (`MOS-UI-366`).

37. **The non-goals are absent, not merely undocumented.** AMENDED at specification 0.3.0:
    `MOS-CORE-038` is reversed and `MOS-UI-009a` permits a first-party renderer, so a check that
    asserted the absence of rendering, windowing and measurement was asserting the absence of the
    thing the platform ships. Grep both compiled surfaces and assert absence of:
    ~~any rendering, windowing or measurement implementation;~~ any brush, eraser, threshold,
    region-grow, scissors or label-map interpolation primitive (`MOS-UI-204`); any control that
    writes a label map, a mask or a SEG, or that edits a generated one (`MOS-UI-010a`); any
    mask-editing or annotation-authoring control; any form that writes a `Capability`, `AcceptanceCriteria` or
    `ApplicabilityEnvelope`; any AE-title, DICOM-node, query/retrieve or archive-credential field; any
    editor for weights, `config.pbtxt`, a registered `PreprocessingSpec`, a threshold outside a recorded
    `tune` run, or an envelope bound; and any control described as retraining on drift, continual
    learning, or promoting the best of a sweep (`MOS-UI-374`–`MOS-UI-378`; the range no longer opens at
    ~~`MOS-UI-373`~~, withdrawn at specification 0.4.0, and no clause of this check rested on it).

38. **Every deferred element has a seam that already exists.** For each element in `MOS-UI-365` not
    placed at 0.1.0, assert the seam named in `MOS-UI-372` is present in the codebase from the release
    its first driver ships in. An element deferred without a seam is not deferred; it is omitted
    (`MOS-REL-023`, `MOS-UI-372`).

---

[← 18. Standards and Regulatory Conformance](18-conformance.md) · [Index](../../MEDICALOS_SPEC.md)
