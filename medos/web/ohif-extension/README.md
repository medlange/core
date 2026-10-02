# `@medicalos/extension-medicalos`

The OHIF toolbar button and provenance panel.

`MOS-SAFE-089a` (chapter 9) states what the button must do and makes it a MUST for release
0.1.0; chapter 15 §15.1.3 assigns it Tier B; chapter 19 §19.2 fixes the surface's shape and
`MOS-UI-012` fixed what this package may contribute — it is WITHDRAWN at
specification 0.4.0, and §0 says what that leaves. `CONTRACT.md` §10 states what the panel
must render.

---

## 0. The requirements this package was built to, and the host that loaded it, are gone

`MOS-UI-012` and `MOS-UI-012a` were **WITHDRAWN at specification 0.4.0**. They are what
fixed this package's shape — "a toolbar entry, the commands behind it, and one panel", no
mode, no `SOPClassHandler` — and **four** files here cite them as the authority for what
they do and do not contribute: `src/index.js`, `src/getToolbarModule.js`,
`src/getPanelModule.js` and `src/panels/provenance-panel.js`, which are exactly the four
§1 records as served and resolved by nothing. This said "eight files" from the commit
that first wrote it; measured at that commit and at every one since, it was four.
Register entry 139. What replaced them does not reach this package: `MOS-UI-012b`
requires the clinician surface to be one named tree served rather than bundled, and
`MOS-UI-012c` governs how a generated object's correspondence is shown, both written
against `viewer/`. So the citations below are **marked as pointing at withdrawn text**
rather than re-pointed, because re-pointing them would invent a requirement nobody wrote.

**And the React half no longer runs.** OHIF loaded it through
`extensions: ['/medicalos/src/index.js']` in `deploy/compose/ohif-config.js`. That file is
gone with the OHIF withdrawal, so `src/index.js`, `getToolbarModule.js`, `getPanelModule.js`
and `panels/provenance-panel.js` are still served under `/medicalos/` and resolved by
nothing. What still executes is `standalone/`, which imports `src/core/*` directly and never
needed a host — the only MedicalOS panel that has ever actually run.

The package's fate is an open **product** decision, not a consequence of the viewer change,
and it is recorded as **register entry 109** in `docs/spec/99-known-inconsistencies.md`
together with the two release criteria and five tests that still assert the extension is
loaded.

Everything below §1 is preserved as written against the OHIF deployment. Read it as the
record of what this package did while a host loaded it.

---

## 1. What is here, and what actually runs

~~**The extension loads into the viewer the compose stack runs.** That is new. An earlier
version of this file said it could not, and that claim was wrong.~~
**CORRECTED 2026-09-26: no viewer the compose stack runs loads it, because the stack runs
no OHIF build.** What runs is `standalone/`, and it reaches THREE of the ten modules under
`src/` — `core/client.js`, `core/render.js`, `core/provenance.js` — measured by walking
the import graph from `standalone/app.js` rather than read off this table. The sentence it
replaces was true while `deploy/compose/ohif-config.js` named `src/index.js` in `extensions`;
that file went with the withdrawal (§0). The history is kept because §2 argues from it.

| Path | What it is | Runs? (was **yes** through the OHIF host, until 0.4.0) |
|---|---|---|
| `src/index.js` | the extension object OHIF imported and registered; wired the toolbar and the panel at mode entry | **no** — nothing imports it |
| `src/getToolbarModule.js` | the two button definitions | **no** |
| `src/getCommandsModule.js` | `runMedicalOSAnalysis` and friends | **no** — the OHIF command registry was its only caller |
| `src/getPanelModule.js` | the panel entry | **no** |
| `src/panels/provenance-panel.js` | the panel, as a custom element | **no** |
| `src/core/client.js` | the single job-creation path: `POST /api/v1/jobs`, SSE follow, poll fallback | **yes**, through `standalone/` |
| `src/core/provenance.js` | `GET /api/v1/jobs/{id}` → the `CONTRACT.md` §10 view model | **yes**, through `standalone/` |
| `src/core/render.js` | the DOM painter, incl. the REJECTED-vs-FAILED rule | **yes**, through `standalone/` |
| `src/core/events.js` | the one `CustomEvent` that joins the command to the panel | **no** — it joined the command module to the panel, and `standalone/app.js` calls `renderPanel` directly |
| `src/id.js` | the extension id string OHIF registered under | **no** — only `src/index.js` read it |
| `standalone/` | a page that hosts three of the `src/core/*` modules, served beside the first-party viewer | **yes** |

THE COLUMN USED TO READ **yes** ALL THE WAY DOWN, and it was true while `deploy/compose/ohif-config.js` named `src/index.js` in `extensions`. That file went with the
OHIF withdrawal (§0), so the files only OHIF ever imported are served and resolved by
nothing. ~~four~~ **SEVEN** of the ten, not four: everything under `src/` except `core/client.js`,
`core/render.js` and `core/provenance.js`. The count is derived from the import graph by
`tests/unit/test_viewer_scan.py` rather than stated here, because this one was wrong and a
previous count in §0 of this same file was wrong in the other direction. They are kept, not deleted, because whether this package has a future
is a product decision and register entry 109 is where it waits.

`src/panels/ProvenancePanel.jsx` was deleted before any of that: it was React + JSX, it had
never run, and it could not run on the delivery route below.

---

## 2. How it loads: the options, and the one that was taken

### 2.1 The claim that was wrong

`medos/deploy/compose/docker-compose.yml` pins `ohif/app:v3.9.2`, a pre-built single-page app.
This file used to say:

> There is no runtime registration hook, no UMD extension loader and no `app-config.js` key
> that can add one.

The first and third clauses are false, and the shipped bundle is the evidence. OHIF 3.9's
generated `platform/app/src/pluginImports.js` is present verbatim inside
`app.bundle.<hash>.js`, and its `loadModule` ends its compiled-in `if`-chain with a
**fall-through**:

```js
// app.bundle.<hash>.js, from the running ohif/app:v3.9.2 image
async function loadModule(module) {
  if (typeof module !== 'string') return module;
  if (module === "@ohif/extension-default") { /* webpack chunk */ }
  /* ...one branch per package compiled into the build... */
  return (await window.browserImportFunction(module)).default;   // <-- anything else
}
```

`index.html` in the same image defines the hook it calls:

```html
<script>function browserImportFunction(moduleId) { return import(moduleId); }</script>
```

and `appInit` feeds the config straight into it:

```js
const loadedExtensions = await importItems([...defaultExtensions, ...appConfig.extensions]);
await extensionManager.registerExtensions(loadedExtensions, appConfig.dataSources);
```

So **any `window.config.extensions` entry that is not one of OHIF's own package names is
handed to a native dynamic `import()`, and its default export is registered as an
extension.** A URL is such an entry. `deploy/compose/ohif-config.js` lists
`'/medicalos/src/index.js'`, nginx already serves this directory on the viewer's origin with
a JavaScript MIME type, and the viewer imports it on boot.

The middle clause was right and stays right: the `*.umd.js` files in the image's web root
are build artefacts whose externals (`self["@ohif/core"]`, `self["@ohif/ui"]`, …) this
deployment does not define, so they are not a usable loader. The usable route is a
self-contained ES module.

### 2.2 The options that were weighed

| | Option | Verdict |
|---|---|---|
| **a** | Build a custom OHIF image from source with the extension compiled in, pinned by digest | **rejected** |
| **b** | Move to a newer OHIF with a runtime plugin mechanism | **unnecessary** |
| **c** | Use the runtime `import()` the pinned image already has | **taken** |

**(a) — rejected, though it would have worked.** It requires cloning the OHIF monorepo,
a `yarn install` of a large JavaScript workspace and a webpack build, and it produces an
image that is *ours*: every OHIF security release becomes a rebuild we own, and the pin that
`MOS-REL-027` calls "unmodified and pinned by version" becomes a pin on our own artefact.
`MOS-CORE-038` permits it — compiling our extension against upstream OHIF is adoption, not a
fork — so this is a cost judgement rather than a compliance one. It is also not available
here: this toolchain has no `node`, `npm` or `yarn` (`requirements-dev.txt` pins a Python
venv and nothing else), so choosing (a) would have meant shipping an unexecuted build recipe
for the second time in this package's life. Recipe kept in §7 all the same, because (c)
depends on a mechanism a future OHIF could remove and (a) is then the fallback.

**(b) — unnecessary, and checked rather than assumed.** The question was whether a later
OHIF has a *better* runtime plugin mechanism than 3.9.2. It does not need one: 3.9.2 already
has the mechanism, verified above in the bytes this deployment serves. Moving the pin to
chase a feature that is already present would be an upgrade with no requirement behind it,
and `MOS-TEST-068` pins the viewer by digest precisely so that upgrades are deliberate.

**(c) — taken.** The viewer image is byte-for-byte the pinned upstream one. Nothing is
compiled, patched, vendored or forked (`MOS-CORE-038`, `MOS-UI-204`, `MOS-UI-213`). The
extension stays "an extension package living outside the viewer's source tree"
(`MOS-REL-027`, `MOS-SAFE-089a`, `MOS-UI-012` — withdrawn at 0.4.0, §0), and the deployment named it in one line of
configuration. Replacing the pinned viewer means editing that one line.

### 2.3 What (c) costs

The module the browser imports is not built, so **every file under `src/` must be a
browser-native ES module**: relative specifiers with an explicit `.js`, no JSX, no bare
imports, **and no React**.

No React is the sharp constraint. A runtime-loaded module cannot resolve `react`, and
bundling a second copy would break the first hook the panel called — a component created by
one React and rendered by another react-dom has no dispatcher. So:

* **the toolbar button needs no React.** It reuses `uiType: 'ohif.radioGroup'`, which is
  `@ohif/extension-default`'s own `ToolbarButton`. We contribute a definition, not a
  renderer.
* **the panel is a custom element.** OHIF renders the active side-panel tab as
  `React.createElement(tab.content, {key})` and passes no other props (read out of the
  shipped `ui-next/src/components/SidePanel`). A React `type` may be a **string**, in which
  case React renders a host element — the most public part of React's contract there is. So
  `getPanelModule.js` registers the tag `medicalos-provenance-panel` as the panel's
  `component`, React renders `<medicalos-provenance-panel>`, and
  `src/panels/provenance-panel.js` gives that tag its behaviour with `customElements.define`.

The panel renders into a **shadow root** and links `standalone/styles.css` inside it, so the
viewer's Tailwind build and this package's stylesheet cannot reach into each other. Both
hosts therefore paint the same markup (`src/core/render.js`) with the same CSS, which is
what keeps the `REJECTED`/`FAILED` treatment `MOS-SAFE-089a` gates from being silently
overridden by a viewer upgrade that renames a utility class. (`styles.css` declares its
tokens on `:root, :host` for exactly this reason: inside a shadow tree `:root` matches
nothing, and the first browser pass showed black text on OHIF's dark ground.)

### 2.4 Where the two contributions attach

`MOS-UI-012` (withdrawn at 0.4.0, §0) allowed "a toolbar entry, the commands behind
it, and one panel" and no mode.
OHIF 3.9 takes toolbar buttons and right panels from the **mode**, so the extension attaches
them from its own `onModeEnter`, to whichever mode the deployment runs:

* `toolbarService.addButtons([...])` then `createButtonSection('primary', [...])`.
  `ExtensionManager.onModeEnter` runs *before* `mode.onModeEnter`, and
  `createButtonSection` appends when the section exists and creates it when it does not — so
  this is correct in either order. It is guarded against running twice, which would render
  the button twice.
* `panelService.addPanel(Right, PANEL_ID)`. The Mode route resets and fills the panels when
  the layout template resolves, which is *before* `onModeEnter`; `addPanel` appends and
  broadcasts `PANELS_CHANGED`, and `SidePanelWithServices` re-reads on that event.

Both orderings were read out of the shipped bundle, not assumed, and both were then observed
in a browser (§6).

---

## 3. The wire contract: what changed, and the defect that remains

Chapter 19 found the client and the API describing one request two ways:

> **`MOS-UI-021a`** — "The wire shapes are Chapter 10's. A surface MUST send `target` +
> `input` per `MOS-API-043` and MUST read `Job.status` per `MOS-API-049`. The current
> extension client sends `{study_instance_uid, capabilities}` and reads `body.state`, which
> is the weeks-1–2 slice's own contract and is **not** the Chapter 10 shape; the divergence
> MUST be closed in the surface, not in Chapter 10."

**The API was the divergent one, not just the client.** `medos/medos/api/routes_jobs.py`
implemented `CONTRACT.md` §9 — body `{study_instance_uid, capabilities}`, response member
`state` — with `additionalProperties: false`. A client that started sending Chapter 10's body
to that server would get a `400`, so "close it in the surface" was not achievable in the
surface alone. What was done:

* **the client now sends `MOS-API-043`'s envelope** and reads `Job.status`
  (`src/core/client.js`);
* **the API accepts both envelopes**, each closed, with a body that mixes them refused;
* **the API answers `status` beside `state`**, from one value, so they cannot disagree.
  `MOS-API-049` names the field and `MOS-TEST-064` spells the distinction out — "the DB
  column is `state` (`MOS-EXEC-001`); `status` is the JSON field name of `MOS-API-049`".
  Adding the member rather than renaming it is `MOS-API-093`'s safe direction and keeps the
  other callers in this tree working.

**The defect that is NOT closed, and must not be reported as closed.** `MOS-API-043`'s
`target` names exactly one capability (or one service version). This slice's job runs a
capability **set** — `jobs.capability_ids`, `UNIQUE (job_id, capability_id)`,
`CONTRACT.md` §7 — and Chapter 10 has no member for a per-job capability subset. The
reconciliation in `routes_jobs.py` is:

| `target` | maps to |
|---|---|
| `{kind: "capability", id: X}` | `capability_ids = [X]` |
| `{kind: "service_version", id: "medos.slice/0.1.0"}` | every capability that service version implements |

which is faithful (`MOS-API-044`: a `service_version` target is "the highest-precedence
explicit pin") and is *not* a general answer: a job over two of three capabilities is not
expressible in `MOS-API-043`. **Reported, not worked around.** The surface never invents a
shape for it; the picker offers the service version or one capability, and nothing else.

`target.version_range` is accepted only with `kind: "capability"`, because `MOS-API-044`
says in so many words that it "MUST be absent" for the other kind.

---

## 4. The `job.create` permission, and the credential

`MOS-SAFE-089a` requires the button to "require the `job.create` permission". Weeks 3–5
landed the `Authenticator`, so `POST /api/v1/jobs` now answers `401` without a credential.
That is the requirement working, not a bug, and it is enforced **server-side** — a
client-side `if (user.can('job.create'))` is a stub that looks like an authorisation control
and enforces nothing, which `MOS-REL-050` forbids.

The extension **holds no credential**. `MedicalOSClient` takes an `authHeaderProvider`
callback; `getCommandsModule.js` passes OHIF's own
`userAuthenticationService.getAuthorizationHeader`, so the token is the signed-in clinician's,
re-read on every request and never cached here. In a real deployment that is an OIDC token
and every job carries the identity of the person who pressed the button.

On this laptop stack nobody is signed in, so nginx supplies a stand-in — the same pattern the
Gateway viewer token already uses, and the same reported deviation:

```nginx
map $http_authorization $medos_api_authorization {
  default   $http_authorization;                       # the caller's own token wins
  ""        "${MEDOS_API_VIEWER_AUTHORIZATION}";       # laptop stand-in, unset by default
}
```

Unset by default, deliberately: a compose default would be a credential hardcoded in a
tracked file. Unset, the button gets `401` and the panel renders the RFC 9457 document
verbatim — which is what "requires `job.create`" looks like from the browser when nobody has
been granted it.

**REPORTED.** A shared viewer key attributes every job to one principal, so the audit trail
cannot say *which* clinician pressed the button. A real deployment leaves
`MEDOS_API_VIEWER_AUTHORIZATION` unset and lets the clinician's own token pass through.

It is **not** a PACS credential (`MOS-SAFE-089a`, `MOS-DATA-002`): it is a platform API key
for `/api/v1`. Nothing in this package knows a DICOMweb URL. The viewer's route to the
archive is OHIF's own data source through the Gateway, which this package never touches.

---

## 5. Running it

The stack must be up (`medos/deploy/compose/docker-compose.yml`, see the root `README.md`).

**§0's note — "everything below §1 is preserved as written against the OHIF deployment" —
is true of this section only in part, and a blanket over a section headed "Running it" is
worse than no note: a reader here is looking for what to type.** So the three subsections
below say individually what they are. §5.1 is LIVE and needed. §5.2 is DEAD. §5.3 is LIVE
and is the surface that actually runs.

### 5.1 Grant the viewer a credential (once per stack) — LIVE

This is still the procedure, and still needed: `medos doctor` reports
`MEDOS_API_VIEWER_AUTHORIZATION` as a blocker with "every call the web console makes: 401
AUTHENTICATION_REQUIRED" until it is set.

```bash
python -m medos.security.cli issue \
    --tenant 00000000-0000-0000-0000-000000000000 \
    --principal-kind service_account --principal-id <uuid> \
    --scope job.create --scope job.read \
    --expires-in-days 1 --label "ohif viewer" --bootstrap
# the plaintext is printed ONCE (MOS-SEC-010); there is no way to read it back
export MEDOS_API_VIEWER_AUTHORIZATION="Bearer <that value>"
docker compose -f medos/deploy/compose/docker-compose.yml up -d --force-recreate web
```

**That last line named `ohif` until 2026-09-26, and there has been no `ohif` service since
the withdrawal.** The service that serves this origin is `web`, running
`nginx:1.27-alpine`; `docker compose ... ohif` exits with "no such service". A live
procedure whose last command cannot run is the failure a reader meets, not one they read
about.

Skip this and everything below still runs; the button will render a `401` problem document
in the panel instead of a job.

### 5.2 In the viewer — DEAD, and measured

**Both addresses below are gone with OHIF.** Measured and recorded as register entry 122:
`GET /viewer?StudyInstanceUIDs=1.2.3` answers **404**, because the origin's nginx has
`location / { return 404; }` and there is no OHIF bundle behind it. The first-party viewer
is at `/mos-viewer/` and reaches a study through `openStudy` from a worklist click, with no
query parameter for one. This subsection is kept, struck, because a reader holding a
conformance report written against 0.2.0 or 0.3.0 checked these two addresses and needs to
find what was checked.

```
http://127.0.0.1:3000/                          # study list
http://127.0.0.1:3000/viewer?StudyInstanceUIDs=<uid>
```

**Analyze with MedicalOS** is the first button in the primary toolbar; **MedicalOS
provenance** is beside it and brings the panel forward. The button is disabled, with
"Open exactly one study to analyse it" as its tooltip, when the viewer does not have exactly
one study open (`MOS-UI-023`) — a control that guesses which of two studies to analyse is a
clinical hazard, not a convenience.

One activation issues exactly one `POST /api/v1/jobs`. The panel shows the job id and status
immediately and updates from `GET /api/v1/jobs/{id}/events` with a poll fallback.

### 5.3 Standalone — LIVE, and the only surface here that runs

Register entry 122: `/medicalos/standalone/` "is served and reachable -- it is the Analyze &
Provenance surface, and walking the product end to end lands on it".

```
http://127.0.0.1:3000/medicalos/standalone/
http://127.0.0.1:3000/medicalos/standalone/?study=<StudyInstanceUID>
http://127.0.0.1:3000/medicalos/standalone/?job=job_01JQ...      # follow an existing job
```

The same `src/core/*` modules, outside the viewer, for a study that is not open in one. It
is not a mock and not a second job-creation path: it constructs the same `MedicalOSClient`
and posts to the same `/api/v1/jobs`. `MOS-UI-013a` is explicit that it does not satisfy "for
the study currently open in the viewer" — that half is now satisfied by the extension itself.

Its target picker is a single choice, not a set of checkboxes, because `MOS-API-043`'s
`target` is a single member. `pleural_effusion` is labelled *placeholder — not implemented*,
because `CONTRACT.md` §7 makes it one and `MOS-UI-018` requires a declared-but-unimplemented
capability to be labelled rather than hidden: a platform that silently omits it looks like a
platform that detects effusions.

---

## 6. What has and has not been verified

### Verified automatically

`tests/unit/test_viewer_wire_contract.py` — no containers:

* the client's request body names `target`, `input.study_instance_uid`,
  `input.prior_study_instance_uids`, `input.series_instance_uids`, and **no longer** carries
  `capabilities`;
* every member the JavaScript puts on the wire is a field of the pydantic model that will
  receive it, and every `target.kind` it can build is in `MOS-API-044`'s closed enum — the
  two halves of the contract checked against each other rather than each against the prose;
* `jobStatus()` prefers `status` over `state`, and it is the **only** place in the package
  that reads either (`MOS-UI-029`);
* no `.jsx` file and no bare import specifier survives under `src/`, which is the constraint
  the whole delivery route rests on.

`tests/e2e/test_viewer_extension.py` + `tests/_support/viewer.py` — against the running
stack: the extension is loaded in the served viewer by the runtime-URL path, every module the
import graph reaches is actually served, and a config entry alone is never accepted as
evidence of a load. Reports `load_path=runtime-url runtime_entry=/medicalos/src/index.js`.

`tests/integration/test_api.py` — over real HTTP: the `MOS-API-043` envelope creates a job,
a `service_version` target runs every capability it implements, `version_range` with that
kind is refused, an unknown `target.kind` is refused, mixing the two envelopes is refused,
an unknown member inside `input` is refused with a pointer at `/input/...`, and
`input.series_instance_uids` is refused as `client_error` rather than as a clinical
rejection.

`tests/e2e/test_demo.py` — the older delivery checks: every file is served with a JavaScript
MIME type, `standalone/app.js` imports `../src/core/client.js` from the **served** bytes, the
served bytes contain no `Authorization`, no `btoa(`, no `8042` and no credential,
`/api/v1/jobs` and the SSE stream are reachable from the viewer's origin, `render.js` and
`styles.css` give `REJECTED` and `FAILED` four distinct channels with the reason code
verbatim, and `REQUIRED_RESULT_FIELDS` is parsed out of the served `provenance.js` and
checked against a real `COMPLETED` job.

### Verified manually, in a real browser, on 2026-09-16

Chromium against this compose stack, driven from this session. This is `MOS-SAFE-089a`
acceptance check 24's evidence and it is **manual**; nothing automated above asserts it.

1. `window.extensionManager.registeredExtensionIds` contains
   `@medicalos/extension-medicalos` alongside OHIF's own two, and
   `modulesMap` contains `@medicalos/extension-medicalos.panelModule.provenance`.
2. `toolbarService.state.buttonSections.primary` is
   `['MedicalOSAnalyze', 'MedicalOSPanel', 'MeasurementTools', 'Zoom', …]` — ours first,
   OHIF's own untouched — and the button is visible in the toolbar with the tooltip
   "Analyze with MedicalOS".
3. With one CT study open, **one** click produced **exactly one** `POST /api/v1/jobs`,
   followed by one `GET /api/v1/jobs/{id}` and one `GET …/events`, observed in the network
   log. Nothing else was issued.
4. The right panel opened on its own and rendered: the `COMPLETED` badge, the job id inline,
   "Analysis complete — 3 results, 2 DICOM objects stored", the RUO line, and the Job /
   Series selection / per-capability / Generated-objects sections with
   "Provenance complete: every field CONTRACT.md §10 requires is recorded for every result."
5. The POST answered `200` with `MedicalOS-Idempotent-Replay: true` and the panel said
   "already running · job_…" rather than reporting a second analysis (`MOS-UI-024`).
6. A real `REJECTED` job (`no_eligible_series`, from the RTSTRUCT-only study) rendered in the
   same panel with `data-status="REJECTED"`, amber `rgb(216,166,87)`, a 3 px left rule and a
   `0px` top border, heading "Not analysed" at weight 500, and `no_eligible_series` verbatim
   in a monospace `<code>` — structurally different from the `COMPLETED` treatment on all
   four channels `MOS-UI-028` requires.
7. The standalone page still submits and renders the same panel from the same modules.

**Numbered manual procedure**, to repeat 3–6 on any stack — this is the part of acceptance
check 24 that needs a human:

1. Complete §5.1 so the viewer has a credential.
2. Open `http://127.0.0.1:3000/` and pick a study with an axial CT series.
3. Open the browser's network panel and filter on `/api/v1/jobs`.
4. Press **Analyze with MedicalOS**. Confirm **exactly one** `POST /api/v1/jobs`, then one
   `GET /api/v1/jobs/{id}` and one `GET …/events`.
5. Confirm the MedicalOS panel shows the job id and status immediately, and that the phase
   advances through `retrieving → building_volume → … → persisting` to `COMPLETED`.
6. Confirm the completed panel names the consumed series, both capability versions, the
   worker and runtime versions, the measurements with their UCUM units, and both generated
   `SeriesInstanceUID`s.
7. Submit a study with no eligible CT series (any RTSTRUCT-only study in the LCTSC set) and
   confirm amber, "Not analysed", and `no_eligible_series` in a monospace element. **Note:**
   OHIF's own `isValidMode` excludes an RTSTRUCT-only study from the longitudinal mode, so
   the viewer will not open one — submit it from the standalone page, or follow the job id in
   the viewer's panel with the `refreshMedicalOSJob` command, which is what was done above.
8. Press the button twice in quick succession and confirm the network panel still shows one
   POST (the in-flight guard, `MOS-UI-021`).

### Not verified, and not claimed

* **The SEG overlay and the SR hydration.** That the generated SEG overlays on the CT stack
  in the OHIF viewport and that the SR hydrates OHIF's measurement panel with the expected
  coded measurements is `MOS-IMG-157` / AT-11, and `MOS-UI-013b` is explicit that it "has
  **not** been run, by test or by hand" in this repository. It still has not. The display
  sets are listed in the study panel; nothing here checks what they render.
  `src/index.js` deliberately contributes no `SOPClassHandler` (`MOS-UI-012a`, withdrawn at 0.4.0), so this is
  OHIF's rendering of ordinary DICOM, not this package's.
* **A network capture showing zero viewer-to-PACS traffic outside the Gateway.** Check 24
  asks for one. The architecture makes it true — `orthanc` is not on the viewer's network
  (`MOS-DATA-006`) — and no capture was taken.
* **A principal without `job.create` being refused.** The control is server-side and
  `tests/integration/test_auth.py` exercises the authenticator, but the specific case "the
  button's request from a principal lacking `job.create`" was not driven from the browser.
* **Any second viewer.** `MOS-IMG-158`'s 0.4.0 `second-viewer` gate is untouched. A
  single-viewer check verifies the viewer, not the objects.

---

## 7. If the runtime route ever stops working

Option (a) from §2.2, kept because (c) depends on a mechanism a future OHIF could remove.
This has **not** been performed.

```bash
git clone --branch v3.9.2 --depth 1 https://github.com/OHIF/Viewers.git
cd Viewers && yarn install

mkdir -p extensions/medicalos
cp -r /path/to/MedOS/web/ohif-extension/* extensions/medicalos/

# platform/app/pluginConfig.json:
#   { "extensions": [ { "packageName": "@medicalos/extension-medicalos" } ] }

yarn run cli list                 # confirm it is picked up
APP_CONFIG=config/default.js yarn run build
# -> platform/app/dist/  : build an image from this and pin it BY DIGEST (MOS-TEST-068)
```

Two things change under (a) and both are improvements, not obstacles: JSX and bare imports
become available again, and the panel could be an ordinary React component. Neither is
required — the custom element works in both worlds, and keeping it means the two delivery
routes run the same code.

---

## 8. The rendering rule this package is accountable for

`MOS-SAFE-089a`: *"It MUST render a `REJECTED` terminal state visually distinct from
`FAILED` and MUST surface the machine-readable reason verbatim."* `MOS-UI-028` fixes the
minimum at **four independent channels, at least one textual**.

`MOS-EXEC-014` gives the reason: *a radiologist who sees a red error where the truth is "no
thin axial recon exists in this study" will either chase an IT ticket or, worse, assume the
study was cleared.*

| | `REJECTED` (clinical) | `FAILED` (technical) |
|---|---|---|
| hue | amber `--medos-rej` | red `--medos-fail` |
| shape | 3px left rule, no box | full 1px box |
| heading weight | 500 | 700 |
| wording | “Not analysed” + *“This is a result, not a failure”* | “Analysis failed” + *“says nothing about the study”* |

The classification is decided in exactly one place — `outcomeKind()` in `src/core/client.js`,
fed by `jobStatus()` — and every renderer asks it (`MOS-UI-029`). The `reason_code` is
printed in a `<code>` element exactly as the API returned it: never prettified, never
title-cased, never translated. *Verbatim* is a testable word, and `tests/e2e/test_demo.py`
tests it against the served bytes. The status node also carries `data-status`, which is what
`MOS-TEST-064` compares between a `REJECTED` and a `FAILED` job.

---

## 9. Provenance: what the panel renders

`CONTRACT.md` §10's field set is transcribed literally into
`src/core/provenance.js::REQUIRED_RESULT_FIELDS`, and `missingProvenanceFields()` reports
what the API did **not** supply so the panel can show the gap rather than quietly omitting a
row. A panel that renders only the fields that happen to be present cannot distinguish *“the
platform recorded no worker version”* from *“the panel forgot to display it”*, and
`MOS-STORE-278` (“a result without provenance MUST NOT be observable through any API”) is
exactly the claim that has to fail loudly when it is false.

Rendered, per result: capability id + version, the **series actually consumed**, instances
consumed, preprocessing version + digest, worker version, runtime version, platform commit,
computation geometry, every measurement with its UCUM unit and geometry space, and the
`SeriesInstanceUID` + `SOPInstanceUID` of every generated DICOM object. Plus, job-wide: the
study, the selector's verdict on every candidate series with its reason code, the trace id
and the derived idempotency key.

**No PHI.** Every field is a UID, a version, a code, a count or a timestamp
(`MOS-SAFE-086`, `CONTRACT.md` §11). `GET /api/v1/jobs/{id}` carries no `PatientName`,
`PatientID`, `AccessionNumber` or `StudyDescription`, so there is none to display.
