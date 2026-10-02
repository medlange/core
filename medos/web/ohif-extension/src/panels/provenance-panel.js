/* WITHDRAWN CITATIONS. `MOS-UI-012` and `MOS-UI-012a`, cited below as the bound on what
 * this package may contribute, were WITHDRAWN at specification 0.4.0. `MOS-UI-012b` and
 * `MOS-UI-012c` replaced them for `viewer/`, the first-party surface, and say nothing
 * about an extension package -- the boundary was removed and not redrawn, so the citations
 * are marked rather than re-pointed. Nothing loads this file any more either: the host that
 * named it, `deploy/compose/ohif-config.js`, went with the OHIF withdrawal. Register entry
 * 109 in docs/spec/99-known-inconsistencies.md holds the whole account. */

/* =====================================================================================
 * The MedicalOS provenance panel, as a CUSTOM ELEMENT.
 *
 * CONTRACT.md section 0 lists "an OHIF toolbar button and a provenance panel" as the two
 * viewer deliverables; CONTRACT.md section 10 and `MOS-SAFE-088` fix what the panel shows.
 *
 * WHY A CUSTOM ELEMENT AND NOT A REACT COMPONENT
 * ----------------------------------------------
 * This package is loaded into the pinned `ohif/app:v3.9.2` bundle at RUNTIME, as a native
 * browser ES module (see `src/index.js` for the mechanism and `README.md` for why that
 * route was chosen). A runtime-loaded module has no bundler and no module resolution for
 * bare specifiers, so it cannot `import React from 'react'`; and shipping a SECOND copy of
 * React would break hooks the moment the panel used one, because a component created by
 * one React copy and rendered by another react-dom has no dispatcher.
 *
 * OHIF renders a panel's `content` with `React.createElement(tab.content, {key})` and
 * passes NO other props (`ui-next/src/components/SidePanel`, read from the shipped
 * bundle). A React `type` may be a STRING, in which case React renders a host element --
 * which is the most public part of React's contract there is. So `getPanelModule.js`
 * registers the TAG NAME below as the panel's component, React renders
 * `<medicalos-provenance-panel>`, and this file's `customElements.define` gives that tag
 * its behaviour. No React, no second copy, no hook hazard, no build step.
 *
 * WHY A SHADOW ROOT
 * -----------------
 * OHIF's stylesheet is a dark Tailwind build; `standalone/styles.css` is this package's
 * own. Rendering into a shadow root means neither can reach into the other, so the panel
 * looks the same inside the viewer as it does on the standalone page -- and the REJECTED
 * versus FAILED treatment that `MOS-SAFE-089a` requires cannot be silently overridden by
 * a viewer upgrade that renames a utility class. Same markup (core/render.js) and same
 * stylesheet in both hosts; two renderings of one provenance record is exactly what this
 * package is built to avoid.
 *
 * PHI (CONTRACT.md section 11, MOS-SAFE-086)
 * ------------------------------------------
 * Everything rendered comes from `GET /api/v1/jobs/{id}`, which carries UIDs, versions,
 * codes, counts and timestamps and no patient attribute. Nothing here reads a DICOM
 * header, and nothing here holds a credential.
 *
 * Spec: MOS-SAFE-088, MOS-SAFE-089a, MOS-UI-012 (withdrawn at 0.4.0), MOS-UI-027,
 *       MOS-UI-028, MOS-UI-029,
 *       CONTRACT.md sections 10 and 11.
 * ===================================================================================== */

import { renderPanel } from '../core/render.js';
import { MEDICALOS_JOB_EVENT } from '../core/events.js';

/** The tag `getPanelModule.js` registers as the panel's `component`. */
export const PROVENANCE_PANEL_TAG = 'medicalos-provenance-panel';

/** The stylesheet both hosts use, resolved relative to THIS module rather than to the
 *  page. The extension is served from `/medicalos/src/panels/`, so this is
 *  `/medicalos/standalone/styles.css` wherever the deployment mounts the package. */
const STYLESHEET_HREF = new URL('../../standalone/styles.css', import.meta.url).href;

/* -------------------------------------------------------------------------------------
 * The last thing the command module said about a job.
 *
 * Held at module scope and updated by a listener registered at module load, NOT by the
 * element: OHIF unmounts a side-panel tab when the reader switches tabs, and a panel that
 * forgot the running job every time it was hidden would look like a platform that lost
 * it. The element re-renders from this on every `connectedCallback`.
 * ----------------------------------------------------------------------------------- */
let lastDetail = null;
const liveElements = new Set();

if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener(MEDICALOS_JOB_EVENT, event => {
    lastDetail = event.detail || null;
    liveElements.forEach(node => node.renderFromStore());
  });
}

/** What the command module last published, for the standalone page and for tests. */
export function lastPublishedDetail() {
  return lastDetail;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

class MedicalOSProvenancePanel extends HTMLElement {
  constructor() {
    super();
    this._root = this.attachShadow({ mode: 'open' });
    this._body = null;
    this._note = null;
  }

  connectedCallback() {
    if (!this._body) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = STYLESHEET_HREF;
      this._root.appendChild(link);

      const shell = element('div', 'medos-embedded-panel');
      this._note = element('p', 'medos-note');
      this._body = element('div', 'medos-panel-body');
      shell.appendChild(this._note);
      shell.appendChild(this._body);
      this._root.appendChild(shell);
    }
    liveElements.add(this);
    this.renderFromStore();
  }

  disconnectedCallback() {
    liveElements.delete(this);
  }

  /** Render whatever the command module last said. Idempotent; safe to call on any event.
   *
   *  The three branches are the three things a reader can be looking at, and they are
   *  kept apart on purpose. A submission that never reached the platform (`problem`) is
   *  NOT a job outcome and MUST NOT be painted with the REJECTED/FAILED treatment that
   *  `MOS-UI-028` reserves for a job that ran -- `MOS-API-039` forbids collapsing a
   *  transport failure onto a clinical rejection, and this is where a viewer would do it. */
  renderFromStore() {
    if (!this._body) return;
    const detail = lastDetail;

    if (!detail) {
      this._note.textContent = '';
      renderPanel(this._body, null);
      return;
    }

    if (detail.job) {
      this._note.textContent = detail.replayed
        ? `already running · ${detail.jobId}`
        : `job · ${detail.jobId}`;
      renderPanel(this._body, detail.job);
      return;
    }

    if (detail.problem) {
      this._note.className = 'medos-note medos-note-error';
      const p = detail.problem;
      this._note.textContent = `${p.code || p.class || 'ERROR'} — ${p.detail || p.title || 'request failed'}`;
      renderPanel(this._body, null);
      return;
    }

    this._note.className = 'medos-note';
    this._note.textContent =
      detail.phase === 'submitting'
        ? 'submitting…'
        : detail.jobId
          ? `job · ${detail.jobId}`
          : '';
    renderPanel(this._body, null);
  }
}

/** Registered once. A second `customElements.define` with the same name throws, and this
 *  module is imported by both `src/index.js` and (in a dev stack) a page that may already
 *  have loaded it. */
if (typeof customElements !== 'undefined' && !customElements.get(PROVENANCE_PANEL_TAG)) {
  customElements.define(PROVENANCE_PANEL_TAG, MedicalOSProvenancePanel);
}

export default PROVENANCE_PANEL_TAG;
