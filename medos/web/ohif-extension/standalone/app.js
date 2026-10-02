/* =====================================================================================
 * The standalone host for the MedicalOS button and provenance panel.
 *
 * Everything that MATTERS here is imported, not written:
 *   ../src/core/client.js      the single job-creation path (MOS-SAFE-089a)
 *   ../src/core/provenance.js  the CONTRACT.md section 10 view model
 *   ../src/core/render.js      the REJECTED-vs-FAILED rendering rule
 *
 * What this file adds is the part that the OHIF extension gets from OHIF: where the study
 * UID comes from, and where the button lives. See index.html's header for why a standalone
 * host exists at all.
 *
 * No build step. Native ES modules, served by the same nginx that serves OHIF. A bundler
 * here would mean the standalone page runs DIFFERENT bytes from the source in ../src/,
 * which is the one property that makes this page worth having.
 * ===================================================================================== */

import {
  MedicalOSClient,
  ProblemError,
  readConfig,
  isTerminal,
  jobStatus,
} from '../src/core/client.js';
import { renderPanel } from '../src/core/render.js';

const cfg = readConfig(window);
const client = new MedicalOSClient();

const $study = document.getElementById('study-uid');
const $caps = document.getElementById('capabilities');
const $analyze = document.getElementById('analyze');
const $refresh = document.getElementById('refresh');
const $note = document.getElementById('submit-note');
const $panel = document.getElementById('panel');
const $apiRoot = document.getElementById('api-root');
// No `$viewerLink` handle any more: the link's href is static in index.html, because
// there is no per-study URL for it to build. See `syncEnabled` below.

let currentJobId = null;
let handle = null;

$apiRoot.textContent = cfg.apiRoot;

// ---- the target picker (MOS-API-043's `target`, MOS-UI-014's picker) -----------------
// ONE choice, not a set: `target` names one capability or one service version
// (MOS-API-044). The entries come from `window.MEDICALOS` -- a fixed, per-deployment list
// (MOS-UI-017) -- and the page computes no resolution of its own (MOS-UI-011).
//
// The first entry is the whole service version, which is what the toolbar button submits
// and what produces the three-result demo job. `value` is the capability id, or '' for
// the service-version target; `MedicalOSClient.targetFor()` turns that into the wire
// shape and is the only place that mapping exists.
function addTargetOption(value, text, note) {
  const label = document.createElement('label');
  label.className = 'medos-cap';
  const input = document.createElement('input');
  input.type = 'radio';
  input.name = 'medos-target';
  input.value = value;
  input.checked = $caps.children.length === 0;
  label.appendChild(input);
  const span = document.createElement('span');
  span.textContent = text;
  label.appendChild(span);
  if (note) {
    const em = document.createElement('em');
    em.className = 'medos-cap-note';
    em.textContent = note;
    label.appendChild(em);
  }
  $caps.appendChild(label);
}

addTargetOption(
  '',
  `${cfg.serviceVersion} (every capability it runs)`,
  'kind: service_version'
);
cfg.capabilities.forEach(capabilityId => {
  addTargetOption(
    capabilityId,
    capabilityId,
    // CONTRACT.md section 7 calls this a placeholder that "returns present=False with a
    // not_implemented note". Saying so in the UI is the point of an honest placeholder;
    // hiding it would make the platform look like it detects effusions.
    capabilityId === 'pleural_effusion' ? 'placeholder — not implemented' : 'kind: capability'
  );
});

/** The chosen capability id, or null for the service-version target. */
function selectedCapability() {
  const checked = $caps.querySelector('input:checked');
  return checked && checked.value ? checked.value : null;
}

// ---- study uid, from the query string -------------------------------------------------
const params = new URLSearchParams(window.location.search);
const initialStudy = params.get('study') || params.get('StudyInstanceUIDs') || '';
if (initialStudy) $study.value = initialStudy;

function syncEnabled() {
  const uid = $study.value.trim();
  // Same rule as the extension's toolbar `evaluate`: no study, no button.
  $analyze.disabled = !uid || !$caps.querySelector('input:checked');
  // THE LINK NO LONGER CARRIES THE STUDY, AND PRETENDING IT DOES WAS THE DEFECT.
  // This used to write `/viewer?StudyInstanceUIDs=<uid>`, an OHIF route. OHIF is
  // withdrawn, `location /` answers 404, and the first-party viewer under /mos-viewer/
  // reaches a study through `openStudy` from a worklist click -- it has no query
  // parameter for one. A link that encodes a study into a URL nothing reads is worse
  // than a link to the worklist: it looks like it will land on the patient.
}
$study.addEventListener('input', syncEnabled);
$caps.addEventListener('change', syncEnabled);
syncEnabled();

// ---- the button ------------------------------------------------------------------------
$analyze.addEventListener('click', async () => {
  const studyInstanceUID = $study.value.trim();
  if (!studyInstanceUID) return;

  // Disable for the duration of the request. The client's `inFlight` map already makes a
  // second activation a no-op; this is the visible half of the same guarantee.
  $analyze.disabled = true;
  $note.textContent = 'submitting…';
  $note.className = 'medos-note';
  $panel.textContent = '';

  if (handle) {
    handle.cancel();
    handle = null;
  }

  let submission;
  try {
    submission = await client.submit(studyInstanceUID, selectedCapability());
  } catch (err) {
    const problem = err instanceof ProblemError ? err.problem : { title: String(err) };
    $note.textContent = `${problem.code || 'ERROR'} — ${problem.detail || problem.title}`;
    $note.className = 'medos-note medos-note-error';
    $analyze.disabled = false;
    return;
  }

  currentJobId = submission.job_id;
  $note.textContent = submission.replayed
    ? `already running · ${submission.job_id}`
    : `submitted · ${submission.job_id}`;
  $note.className = 'medos-note';
  $refresh.hidden = false;

  handle = client.follow(submission.job_id, job => {
    renderPanel($panel, job);
    if (isTerminal(jobStatus(job))) {
      $analyze.disabled = false;
      syncEnabled();
    }
  });

  handle.promise.catch(err => {
    const problem = err instanceof ProblemError ? err.problem : { title: String(err) };
    $note.textContent = `follow failed — ${problem.detail || problem.title}`;
    $note.className = 'medos-note medos-note-error';
    $analyze.disabled = false;
    syncEnabled();
  });
});

$refresh.addEventListener('click', async () => {
  if (!currentJobId) return;
  try {
    renderPanel($panel, await client.getJob(currentJobId));
  } catch (err) {
    const problem = err instanceof ProblemError ? err.problem : { title: String(err) };
    $note.textContent = `${problem.code || 'ERROR'} — ${problem.detail || problem.title}`;
    $note.className = 'medos-note medos-note-error';
  }
});

// ---- deep link: ?job=<id> renders an existing job without submitting anything ----------
// Used by tests/e2e/test_demo.py and by anyone quoting a job id from a ticket. It follows
// rather than one-shots, so a link to a RUNNING job updates as it finishes.
const deepJob = params.get('job');
if (deepJob) {
  currentJobId = deepJob;
  $refresh.hidden = false;
  $note.textContent = `following · ${deepJob}`;
  handle = client.follow(deepJob, job => renderPanel($panel, job));
  handle.promise.catch(err => {
    const problem = err instanceof ProblemError ? err.problem : { title: String(err) };
    $note.textContent = `${problem.code || 'ERROR'} — ${problem.detail || problem.title}`;
    $note.className = 'medos-note medos-note-error';
  });
}

// Exposed for the e2e test's browser-side assertions and for debugging from the console.
// Read-only surface: no state is settable from here.
window.__medicalos = {
  client,
  get jobId() {
    return currentJobId;
  },
  config: cfg,
};
