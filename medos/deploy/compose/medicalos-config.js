/* =====================================================================================
 * MedicalOS extension configuration.
 *
 * WHAT THIS FILE USED TO BE. `deploy/compose/ohif-config.js`: an OHIF `app-config.js`
 * carrying OHIF's own `window.config` -- router basename, study list, hanging protocols,
 * data sources, the `extensions` array -- and, appended at the bottom, the MedicalOS
 * extension's deployment configuration. OHIF is withdrawn, so the first half is gone and
 * the second half is this file.
 *
 * STILL SERVED AT /app-config.js, WHICH IS OHIF'S NAME FOR IT. The path is kept because
 * two release gates read the bytes this origin returns there
 * (`tests/gate/test_capability_reachable.py` property 5,
 * `tests/integration/test_viewer_capability_list.py`), and because the surface that reads
 * it -- `medos/web/ohif-extension/`, served at /medicalos/ -- has not itself been retired. When
 * that decision is taken, the path goes with it. A legacy name that is written down is
 * cheaper than a rename that silently unhooks a gate.
 *
 * SAME-ORIGIN, ON PURPOSE. Every URL below is relative, so the browser posts to
 * http://127.0.0.1:3000/api/v1/jobs and nginx proxies to medos-api. Nothing crosses an
 * origin, so no CORS preflight is ever issued and medos-api needs no CORS policy.
 *
 * NO CREDENTIAL LIVES HERE. This file is served to the browser. The Gateway token is
 * injected by nginx server-side (MOS-DATA-017); `tests/e2e/test_demo.py` greps every
 * served file for `bearer` to keep it that way.
 * ===================================================================================== */

/* ---------------------------------------------------------------------------------
 * MedicalOS extension configuration. Read by medos/web/ohif-extension (and by its standalone
 * fallback). Same-origin relative paths for the same reason as above: the browser posts
 * to http://127.0.0.1:3000/api/v1/jobs and nginx proxies to the medos-api container, so
 * the extension issues no cross-origin request and needs no CORS policy on the API.
 * ------------------------------------------------------------------------------- */
window.MEDICALOS = {
  /* Where POST /api/v1/jobs, GET /api/v1/jobs/{id} and the SSE stream live.
   * MOS-SAFE-089a: "MUST NOT create a second job-creation path" -- this is the only
   * address the extension knows, and it is an API address, not a PACS one. */
  apiRoot: '/api/v1',

  /* What this deployment's API will ADMIT, offered as MOS-API-043 `target`s of
   * `kind: "capability"`. MOS-UI-017 says a surface before the registry lands renders
   * "a fixed list, configured per deployment"; this is that list, and it is deployment
   * configuration in the same file as the rest of it.
   *
   * IT MUST EQUAL THE SET `MEDOS_CAPABILITY_PROVIDERS` PRODUCES IN THE SAME COMPOSE
   * FILE, and it is a separate copy of that set only because a browser cannot read an
   * environment variable. The two drifted the moment the variable defaulted to
   * `lung_nodule`: the API admitted four capabilities and this list offered three, so a
   * reader could not submit the one capability 0.3.0 was built to prove. Nothing caught
   * it -- `zero-core-change` measures a diff's path set, and a path set cannot see a
   * list in a config file. `tests/integration/test_viewer_capability_list.py` now
   * compares the two and fails on any future divergence. Register entry 68.
   *
   * `pleural_effusion` is included deliberately: it is a declared placeholder that
   * returns present=false with a not_implemented note, and hiding it from the UI would
   * make the honest placeholder invisible, which is the opposite of what CONTRACT.md
   * section 7 and MOS-UI-018 ask for. */
  capabilities: [
    'lung_segmentation',
    'emphysema_laa',
    'pleural_effusion',
    'lung_nodule',
  ],

  /* MOS-API-044's `target` of `kind: "service_version"`, and the pin MOS-UI-017 requires
   * a pre-0.3.0 surface to read from deployment configuration rather than compute:
   * "before 0.3.0 the picker MUST render a fixed list, configured per deployment, of the
   * single ServiceVersion pinned for each capability". This deployment runs exactly one,
   * and the toolbar button submits it -- one job, every capability it implements, one
   * POST (MOS-SAFE-089a acceptance check 24). `medos/medos/api/routes_jobs.py` holds the same
   * string as SLICE_SERVICE_VERSION_ID and refuses any other.
   *
   * MOS-UI-011 forbids the SURFACE to resolve, rank, pin or substitute a ServiceVersion.
   * It does not: it reads this string and sends it. The pin lives here. */
  serviceVersion: 'medos.slice/0.1.0',

  /* Which target the toolbar button submits when the reader has not chosen one. `null`
   * means the service version above -- everything it runs, in one job. Set this to a
   * capability id to make the button submit that capability alone. */
  defaultCapability: null,

  /* SSE first (MOS-SAFE-089a permits "polling or subscribing"), with a poll fallback so a
   * proxy that buffers text/event-stream degrades to a working button rather than a dead
   * one. Interval in milliseconds. */
  useEventStream: true,
  pollIntervalMs: 1500,

  /* Rendered in the provenance panel next to every result. CONTRACT.md section 0: this
   * slice is deliberately architecturally wrong, and a viewer that does not say so in the
   * clinician's field of view is the failure mode MOS-REL-050 is about. */
  clinicalUseMode: 'research_only',
};
