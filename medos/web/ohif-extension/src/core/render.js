/* =====================================================================================
 * DOM rendering for the MedicalOS provenance panel.
 *
 * Plain DOM, no framework. Consumed by `standalone/app.js` directly and by
 * `src/panels/ProvenancePanel.jsx` through a single `ref` mount, so the React panel and
 * the standalone page render byte-identical markup from the same view model. Two
 * renderers would be two provenance records for one job.
 *
 * THE ONE VISUAL RULE THIS FILE EXISTS TO ENFORCE
 * -----------------------------------------------
 * MOS-SAFE-089a: "It MUST render a REJECTED terminal state visually distinct from FAILED
 * and MUST surface the machine-readable reason verbatim."
 * MOS-EXEC-014 gives the reason: "a radiologist who sees a red error where the truth is
 * 'no thin axial recon exists in this study' will either chase an IT ticket or, worse,
 * assume the study was cleared."
 *
 * So `outcomeKind()` (core/client.js) drives a class name, the stylesheet gives
 * `.medos-rejected` a neutral amber-on-parchment treatment and `.medos-failed` a red one,
 * the two carry different headings and different icons, and the `reason_code` is printed
 * in a monospace `<code>` element exactly as the API returned it -- never prettified,
 * never title-cased, never translated. "Verbatim" is a testable word.
 *
 * ESCAPING
 * --------
 * Every value that reaches the DOM goes through `text()`, which creates a text node, or
 * through `el()`'s textContent. There is no innerHTML with interpolation anywhere in this
 * file. The values are UIDs and codes from our own API, so this is not a live XSS story --
 * it is the habit that keeps it from becoming one when the panel starts rendering a
 * `reason_detail` that originated in a DICOM header.
 * ===================================================================================== */

import { outcomeKind } from './client.js';
import { buildProvenanceView, formatTimestamp } from './provenance.js';

/* THE FOURTH DISTINGUISHING CHANNEL, as data rather than as inline expressions.
 *
 * MOS-SAFE-089a requires `REJECTED` to render "visually distinct" from `FAILED`. Colour
 * alone is not distinct to a red-green colour-blind reader, so the two differ on hue, box
 * treatment, heading weight AND wording. The wording is the channel that survives a
 * stylesheet failing to load, so it is named here where it can be read, reviewed and
 * asserted -- not buried inside a concatenated argument.
 *
 * MOS-EXEC-014 states what the copy has to achieve: the reader must conclude neither "the
 * platform is broken" nor "the study was cleared". */
export const REJECTED_COPY = Object.freeze({
  title: 'Not analysed',
  body: 'The platform decided not to analyse this study. This is a result, not a failure.',
  tail: 'Nothing is broken, and re-running will not change the outcome.',
});

export const FAILED_COPY = Object.freeze({
  title: 'Analysis failed',
  body: 'The platform could not complete the analysis. This says nothing about the study.',
  tail: 'The study has not been assessed. Re-running may succeed.',
});

const NBSP = ' ';

function el(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined && textContent !== null) {
    node.textContent = String(textContent);
  }
  return node;
}

function kv(label, value, options = {}) {
  const row = el('div', 'medos-kv' + (options.missing ? ' medos-kv-missing' : ''));
  row.appendChild(el('span', 'medos-kv-k', label));
  const v = el('span', 'medos-kv-v' + (options.mono ? ' medos-mono' : ''));
  if (options.missing) {
    // NOT an empty cell. CONTRACT.md section 10 makes this field mandatory, so its
    // absence is a finding about the platform and the panel says so.
    v.textContent = 'not recorded';
    v.classList.add('medos-missing');
  } else if (Array.isArray(value)) {
    value.forEach((item, i) => {
      if (i) v.appendChild(document.createTextNode(', '));
      const code = el('code', 'medos-mono', item);
      v.appendChild(code);
    });
  } else {
    v.textContent = value === null || value === undefined || value === '' ? '—' : String(value);
  }
  if (options.title) v.title = options.title;
  row.appendChild(v);
  return row;
}

function section(title, subtitle) {
  const s = el('section', 'medos-section');
  const h = el('h3', 'medos-h3', title);
  s.appendChild(h);
  if (subtitle) s.appendChild(el('p', 'medos-sub', subtitle));
  return s;
}

// =====================================================================================
// Status block -- the half of the panel MOS-SAFE-089a specifies
// =====================================================================================
export function renderStatus(view) {
  const kind = outcomeKind(view.status);
  const box = el('div', `medos-status medos-${kind}`);
  // MOS-TEST-064 measures the REJECTED/FAILED distinction "by comparing the rendered DOM
  // node's `data-status` attribute and computed colour between a REJECTED and a FAILED
  // job -- they MUST differ on both". The attribute is emitted here so the check reads
  // the DOM rather than inferring the state from a class name.
  box.setAttribute('data-status', view.status === undefined ? '' : String(view.status));
  box.setAttribute('data-outcome-kind', kind);

  const head = el('div', 'medos-status-head');
  head.appendChild(el('span', 'medos-badge', view.status));
  // MOS-SAFE-089a: "MUST render the returned job_id ... inline".
  head.appendChild(el('code', 'medos-mono medos-jobid', view.jobId || '—'));
  box.appendChild(head);

  if (kind === 'running') {
    // MOS-EXEC-021 / MOS-API-051: steps_completed / steps_total, and NO float progress.
    // A percentage invites "83% of what?"; a step name answers it.
    const steps =
      view.stepsTotal > 0
        ? `${view.stepsCompleted}/${view.stepsTotal}`
        : `${view.stepsCompleted || 0}`;
    box.appendChild(
      el('p', 'medos-phase', `${view.phase || 'queued'}${NBSP}${NBSP}·${NBSP}${NBSP}step ${steps}`)
    );
    const bar = el('div', 'medos-bar');
    const fill = el('div', 'medos-bar-fill');
    fill.style.width =
      view.stepsTotal > 0 ? `${Math.round((view.stepsCompleted / view.stepsTotal) * 100)}%` : '0%';
    bar.appendChild(fill);
    box.appendChild(bar);
  }

  if (kind === 'rejected' && view.rejection) {
    // ---- THE CLINICAL OUTCOME. Not an error. ---------------------------------------
    box.appendChild(el('p', 'medos-outcome-title', REJECTED_COPY.title));
    box.appendChild(el('p', 'medos-outcome-body', REJECTED_COPY.body));
    box.appendChild(el('p', 'medos-outcome-body', REJECTED_COPY.tail));
    const reason = el('div', 'medos-reason');
    reason.appendChild(el('span', 'medos-kv-k', 'Reason'));
    // VERBATIM. MOS-SAFE-089a. The machine-readable code exactly as the API returned it.
    reason.appendChild(
      el('code', 'medos-mono medos-reason-code', view.rejection.reason_code || view.rejection.code)
    );
    box.appendChild(reason);
    if (view.rejection.title) {
      box.appendChild(el('p', 'medos-reason-detail', view.rejection.title));
    }
    if (view.rejection.series_selection_href) {
      const a = el('a', 'medos-link', 'Why: series selection for this job');
      a.href = view.rejection.series_selection_href;
      a.target = '_blank';
      a.rel = 'noopener';
      box.appendChild(a);
    }
  }

  if (kind === 'failed' && view.error) {
    // ---- THE TECHNICAL OUTCOME. Visually distinct, different words. -----------------
    box.appendChild(el('p', 'medos-outcome-title', FAILED_COPY.title));
    box.appendChild(el('p', 'medos-outcome-body', FAILED_COPY.body));
    box.appendChild(el('p', 'medos-outcome-body', FAILED_COPY.tail));
    const reason = el('div', 'medos-reason');
    reason.appendChild(el('span', 'medos-kv-k', 'Code'));
    reason.appendChild(el('code', 'medos-mono medos-reason-code', view.error.code || '—'));
    box.appendChild(reason);
    if (view.error.title) box.appendChild(el('p', 'medos-reason-detail', view.error.title));
    // MOS-API-036's wire class. `transport_failure` is retryable and `system_failure` is
    // not, and telling an operator which one they have is the difference between "try
    // again" and "open a ticket".
    box.appendChild(
      kv('Class', `${view.error.class}${view.error.retryable ? ' (retryable)' : ''}`)
    );
  }

  if (kind === 'success') {
    const n = view.generatedObjects.length;
    box.appendChild(el('p', 'medos-outcome-title', 'Analysis complete'));
    box.appendChild(
      el(
        'p',
        'medos-outcome-body',
        `${view.results.length} result${view.results.length === 1 ? '' : 's'}, ` +
          `${n} DICOM object${n === 1 ? '' : 's'} stored.`
      )
    );
  }

  if (view.clinicalUseMode) {
    // CONTRACT.md section 0 and MOS-REL-050: this slice is deliberately architecturally
    // wrong and none of it is a clinical device. A viewer that does not say so in the
    // clinician's field of view is the failure mode the RUO-marking gate exists for.
    box.appendChild(
      el('p', 'medos-ruo', `Research use only — clinical_use_mode: ${view.clinicalUseMode}`)
    );
  }
  return box;
}

// =====================================================================================
// Provenance block -- CONTRACT.md section 10
// =====================================================================================
export function renderProvenance(view) {
  const wrap = el('div', 'medos-provenance');

  // ---- what ran, on what ------------------------------------------------------------
  const job = section('Job', 'What was asked, and of which study.');
  view.job.fields.forEach(f => {
    job.appendChild(
      kv(f.label, f.value, { missing: f.missing, mono: f.key !== 'requested_at' })
    );
  });
  wrap.appendChild(job);

  // ---- the series ACTUALLY consumed --------------------------------------------------
  const sel = section(
    'Series selection',
    'Which series the selector chose, and why the others were not.'
  );
  if (view.seriesSelection.length === 0) {
    sel.appendChild(el('p', 'medos-empty', 'No series were evaluated yet.'));
  } else {
    const table = el('table', 'medos-table');
    const thead = el('thead');
    const hr = el('tr');
    ['Series', 'Modality', 'Instances', 'Decision', 'Reason'].forEach(h =>
      hr.appendChild(el('th', null, h))
    );
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el('tbody');
    view.seriesSelection.forEach(row => {
      const tr = el('tr', row.decision === 'selected' ? 'medos-row-selected' : null);
      const uid = el('td');
      uid.appendChild(el('code', 'medos-mono', row.seriesInstanceUID));
      tr.appendChild(uid);
      tr.appendChild(el('td', null, row.modality || '—'));
      tr.appendChild(el('td', null, row.instanceCount === null ? '—' : row.instanceCount));
      tr.appendChild(el('td', 'medos-decision', row.decision));
      const reason = el('td');
      if (row.reasonCode) reason.appendChild(el('code', 'medos-mono', row.reasonCode));
      if (row.reasonDetail) {
        reason.appendChild(el('div', 'medos-reason-detail-sm', row.reasonDetail));
      }
      tr.appendChild(reason);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    sel.appendChild(wrapScroll(table));
  }
  wrap.appendChild(sel);

  // ---- per capability ----------------------------------------------------------------
  view.results.forEach(result => {
    const s = section(
      `${result.capabilityId} ${result.capabilityVersion || ''}`.trim(),
      'What this capability read, what it produced, and with what code.'
    );
    result.provenance.fields.forEach(f => {
      const isTime = f.key === 'started_at' || f.key === 'finished_at';
      s.appendChild(
        kv(f.label, isTime ? formatTimestamp(f.value) : f.value, {
          missing: f.missing,
          mono: !isTime,
          title: isTime && f.value ? String(f.value) : undefined,
        })
      );
    });
    s.appendChild(kv('Instances consumed', result.provenance.instancesConsumed));
    if (result.provenance.inputUidDigest) {
      s.appendChild(kv('Input UID digest', result.provenance.inputUidDigest, { mono: true }));
    }
    if (result.provenance.platformCommit) {
      s.appendChild(kv('Platform commit', result.provenance.platformCommit, { mono: true }));
    }
    if (result.provenance.geometry) {
      const g = result.provenance.geometry;
      // CONTRACT.md section 5: measurements "MUST be computed in SOURCE geometry. Never in
      // model space." Rendered so a reviewer can see the platform agrees.
      s.appendChild(kv('Computation geometry', g.computation_geometry || '—'));
    }

    // What this capability wrote, if anything. Stated either way: "wrote no DICOM object"
    // is a fact a reviewer needs, and an empty row would read as a rendering gap.
    s.appendChild(
      kv(
        'Objects written',
        result.objects.length
          ? result.objects.map(o => `${o.kind} ${o.sopInstanceUID}`)
          : 'none — this capability contributes to the shared SR',
        { mono: result.objects.length > 0 }
      )
    );

    if (result.missing.length) {
      s.appendChild(
        el(
          'p',
          'medos-warn',
          `Provenance incomplete — missing: ${result.missing.join(', ')} (CONTRACT.md §10)`
        )
      );
    }
    if (result.objectMismatch) {
      s.appendChild(el('p', 'medos-warn', `Provenance/archive mismatch — ${result.objectMismatch}`));
    }

    // Measurements
    if (result.measurements.length) {
      const table = el('table', 'medos-table');
      const thead = el('thead');
      const hr = el('tr');
      ['Measurement', 'Value', 'Unit', 'Code', 'Space'].forEach(h =>
        hr.appendChild(el('th', null, h))
      );
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el('tbody');
      result.measurements.forEach(m => {
        const tr = el('tr');
        tr.appendChild(el('td', null, m.display || m.code));
        tr.appendChild(el('td', 'medos-num', formatNumber(m.value)));
        tr.appendChild(el('td', null, m.unit));
        const code = el('td');
        code.appendChild(el('code', 'medos-mono', `${m.scheme}:${m.code}`));
        tr.appendChild(code);
        tr.appendChild(el('td', null, m.geometrySpace || '—'));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      s.appendChild(wrapScroll(table));
    }

    // Findings, including the honest placeholder.
    result.findings.forEach(f => {
      const line = el('div', 'medos-finding');
      line.appendChild(el('code', 'medos-mono', f.kind));
      line.appendChild(
        el('span', f.present ? 'medos-present' : 'medos-absent', f.present ? 'present' : 'absent')
      );
      if (f.note) line.appendChild(el('span', 'medos-note', f.note));
      s.appendChild(line);
    });

    wrap.appendChild(s);
  });

  // ---- what it wrote into the PACS ---------------------------------------------------
  const objs = section(
    'Generated DICOM objects',
    'The SeriesInstanceUID and SOPInstanceUID of every object this job stored.'
  );
  if (view.generatedObjects.length === 0) {
    objs.appendChild(el('p', 'medos-empty', 'No DICOM objects were stored.'));
  } else {
    const table = el('table', 'medos-table');
    const thead = el('thead');
    const hr = el('tr');
    ['Kind', 'SeriesInstanceUID', 'SOPInstanceUID', 'Frames', 'Stored', 'By'].forEach(h =>
      hr.appendChild(el('th', null, h))
    );
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el('tbody');
    view.generatedObjects.forEach(o => {
      const tr = el('tr');
      tr.appendChild(el('td', 'medos-kind', o.object_kind));
      const su = el('td');
      su.appendChild(el('code', 'medos-mono', o.series_instance_uid));
      tr.appendChild(su);
      const iu = el('td');
      iu.appendChild(el('code', 'medos-mono', o.sop_instance_uid));
      tr.appendChild(iu);
      tr.appendChild(el('td', null, o.frame_count === null ? '—' : o.frame_count));
      tr.appendChild(el('td', null, o.stow_state));
      tr.appendChild(el('td', null, o.produced_by.join(', ')));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    objs.appendChild(wrapScroll(table));
  }
  wrap.appendChild(objs);

  // ---- the one-line claim -------------------------------------------------------------
  // Three outcomes, not two. A job with NO results -- rejected, or still running -- has no
  // provenance to be complete or incomplete, and saying "incomplete" there is a warning
  // about nothing that trains the reader to ignore the warning that means something.
  if (view.results.length === 0) {
    wrap.appendChild(
      el(
        'p',
        'medos-empty',
        outcomeKind(view.status) === 'running'
          ? 'No results yet.'
          : 'This job produced no results, so there is no per-result provenance to show.'
      )
    );
  } else {
    wrap.appendChild(
      el(
        'p',
        view.provenanceComplete ? 'medos-ok' : 'medos-warn',
        view.provenanceComplete
          ? 'Provenance complete: every field CONTRACT.md §10 requires is recorded for every result.'
          : 'Provenance incomplete — see the per-capability sections above.'
      )
    );
  }

  return wrap;
}

function wrapScroll(table) {
  // Wide UIDs must scroll inside their own container rather than making the panel scroll.
  const box = el('div', 'medos-scroll');
  box.appendChild(table);
  return box;
}

function formatNumber(value) {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  return Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(3);
}

/**
 * Render the whole panel into `container` for one `GET /api/v1/jobs/{id}` document.
 * Replaces the container's children; safe to call on every update.
 */
export function renderPanel(container, job) {
  const view = buildProvenanceView(job);
  container.textContent = '';
  if (!view) {
    container.appendChild(
      el('p', 'medos-empty', 'No analysis yet. Press “Analyze with MedicalOS”.')
    );
    return null;
  }
  container.appendChild(renderStatus(view));
  container.appendChild(renderProvenance(view));
  return view;
}

export default renderPanel;
