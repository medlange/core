/* =====================================================================================
 * The provenance view model.  CONTRACT.md section 10, verbatim:
 *
 *   "Every result MUST record: job_id, study_instance_uid, the **series actually
 *    consumed**, capability_id + version, preprocessing_version, the generated
 *    SeriesInstanceUID and SOPInstanceUID per object, worker_version, runtime_version,
 *    and timestamps. The GET /api/v1/jobs/{id} response surfaces this; the OHIF panel
 *    renders it."
 *
 * This module is the "renders it" half, minus the DOM. It turns one
 * `GET /api/v1/jobs/{id}` document into a plain, sorted, display-ready structure and
 * NOTHING else: no fetch, no React, no document. That separation is what makes the
 * contract checkable -- `REQUIRED_RESULT_FIELDS` below is a literal transcription of the
 * sentence above, `missingProvenanceFields()` reports what the API did not supply, and
 * the renderer shows the gap instead of quietly omitting a row. A panel that renders only
 * the fields that happen to be present cannot distinguish "the platform recorded no
 * worker version" from "the panel forgot to display it", and MOS-STORE-278 ("a result
 * without provenance MUST NOT be observable through any API") is exactly the claim that
 * needs to fail loudly when it is false.
 *
 * PHI (CONTRACT.md section 11, MOS-SAFE-086)
 * ------------------------------------------
 * Every field named here is a UID, a version string, a code, a count or a timestamp.
 * There is no PatientName, PatientID, PatientBirthDate, AccessionNumber or
 * StudyDescription in the field list, and `GET /api/v1/jobs/{id}` does not carry one to
 * display. MOS-SAFE-086: this record "is exported and must be safe to hand a reviewer".
 * ===================================================================================== */

import { jobStatus } from './client.js';

/** CONTRACT.md section 10's per-result field set, as data. Each entry is
 *  [key in results[].provenance, label]. Order is the reading order of the panel:
 *  what ran, on what, with what, and when.
 *
 *  `generated_objects` is deliberately NOT in this list. CONTRACT.md section 10 requires
 *  "the generated SeriesInstanceUID and SOPInstanceUID **per object**" -- per OBJECT, not
 *  per capability -- and in this slice one job writes one SEG and one SR between them:
 *  `lung_segmentation` owns both, while `emphysema_laa` and `pleural_effusion` contribute
 *  measurements and findings into the SAME SR. Demanding an object from every capability
 *  made the panel report "Provenance incomplete" for a platform that was correct, which
 *  is worse than not checking at all -- a warning that is always on is a warning nobody
 *  reads. It is checked instead by `objectProvenanceMismatch()` below, which asks the
 *  question that IS always meaningful: does each result's provenance name exactly the
 *  objects that result wrote? */
export const REQUIRED_RESULT_FIELDS = Object.freeze([
  ['capability_id', 'Capability'],
  ['capability_version', 'Capability version'],
  ['series_consumed', 'Series consumed'],
  ['preprocessing_version', 'Preprocessing version'],
  ['worker_version', 'Worker version'],
  ['runtime_version', 'Runtime version'],
  ['started_at', 'Started'],
  ['finished_at', 'Finished'],
]);

/** The job-level half. `series_consumed` appears in both on purpose: the job says which
 *  series the SELECTOR chose, each result says which series that capability actually
 *  read, and a disagreement between them is a provenance defect worth seeing. */
export const REQUIRED_JOB_FIELDS = Object.freeze([
  ['job_id', 'Job'],
  ['study_instance_uid', 'Study'],
  ['series_consumed', 'Series consumed'],
  ['service_id', 'Service'],
  ['service_version', 'Service version'],
  ['trace_id', 'Trace'],
  ['requested_at', 'Requested'],
  ['finished_at', 'Finished'],
]);

function isEmpty(value) {
  if (value === null || value === undefined || value === '') return true;
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

/** Which of CONTRACT.md section 10's required fields this result did NOT supply.
 *  Returned rather than thrown: the panel's job is to make the gap visible to a human,
 *  not to refuse to render. */
export function missingProvenanceFields(resultProvenance, fields = REQUIRED_RESULT_FIELDS) {
  const p = resultProvenance || {};
  return fields.filter(([key]) => isEmpty(p[key])).map(([key]) => key);
}

function objectKey(o) {
  return `${o.object_kind}|${o.series_instance_uid}|${o.sop_instance_uid}`;
}

/**
 * Whether one result's `provenance.generated_objects` fails to name exactly the DICOM
 * objects that result wrote.
 *
 * The two halves are different defects and both matter:
 *   an object with no provenance entry  -> MOS-STORE-278: something is in the archive
 *                                          under this job's identity that no provenance
 *                                          record accounts for.
 *   a provenance entry with no object   -> the record claims an object the platform did
 *                                          not store; a reviewer following it gets a 404.
 * Returns '' when they agree.
 */
export function objectProvenanceMismatch(result) {
  const written = new Set((result.dicom_objects || []).map(objectKey));
  const named = new Set(
    ((result.provenance || {}).generated_objects || []).map(objectKey)
  );
  const unnamed = [...written].filter(k => !named.has(k));
  const phantom = [...named].filter(k => !written.has(k));
  if (unnamed.length) return `${unnamed.length} stored object(s) not named in provenance`;
  if (phantom.length) return `${phantom.length} provenance entr(y/ies) name no stored object`;
  return '';
}

/** RFC 3339 (MOS-API-002: ms precision, UTC, literal Z) -> something a human reads.
 *  Deliberately keeps the original in the title attribute at the render layer: a
 *  localised string is for reading and the RFC 3339 value is for quoting in a ticket. */
export function formatTimestamp(value) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}

/** Deduplicate the generated objects by kind, preserving order.
 *  The idempotency-three-surface gate (docs/spec/15-delivery.md 15.1.2, release 0.1.0)
 *  requires "exactly one SeriesInstanceUID per generated DICOM object kind in the PACS";
 *  showing the SEG's series UID twice because two results reference the same object would
 *  make a correct platform look like it violated that. */
export function objectsByKind(results) {
  const byKind = new Map();
  (results || []).forEach(result => {
    (result.dicom_objects || []).forEach(obj => {
      const key = `${obj.object_kind}|${obj.sop_instance_uid}`;
      if (!byKind.has(key)) {
        byKind.set(key, {
          object_kind: obj.object_kind,
          series_instance_uid: obj.series_instance_uid,
          sop_instance_uid: obj.sop_instance_uid,
          sop_class_uid: obj.sop_class_uid,
          series_number: obj.series_number,
          frame_count: obj.frame_count,
          stow_state: obj.stow_state,
          research_marked: obj.research_marked,
          produced_by: [],
        });
      }
      byKind.get(key).produced_by.push(result.capability_id);
    });
  });
  return Array.from(byKind.values()).sort((a, b) =>
    a.object_kind === b.object_kind
      ? a.sop_instance_uid.localeCompare(b.sop_instance_uid)
      : a.object_kind.localeCompare(b.object_kind)
  );
}

/**
 * One `GET /api/v1/jobs/{id}` document -> the panel's view model.
 *
 * Pure. Takes the document, returns a structure. Everything the panel displays is derived
 * here so that the React panel and the standalone page cannot drift into showing
 * different things -- which would be two provenance records for one job, and provenance
 * that disagrees with itself is worse than none.
 */
export function buildProvenanceView(job) {
  if (!job) return null;
  const results = job.results || [];
  const jobProvenance = job.provenance || {};
  // MOS-API-049 fixes the field name as `status`; `jobStatus()` in core/client.js is the
  // ONE place that reads it (MOS-UI-029), tolerating a server still answering CONTRACT.md
  // section 9's `state`. Nothing below touches `job.status` or `job.state` again.
  const status = jobStatus(job);

  return {
    jobId: job.job_id,
    status,
    phase: job.phase,
    stepsCompleted: job.steps_completed,
    stepsTotal: job.steps_total,
    attempt: job.attempt,
    clinicalUseMode: job.clinical_use_mode,

    // MOS-EXEC-016 / MOS-API-040: at most one of these is ever non-null, and REJECTED is
    // a clinical finding rendered with non-error affordances.
    rejection: job.rejection || null,
    error: job.error || null,

    job: {
      // `missing` is only asserted once the job COMPLETED. Before that nothing is owed
      // yet, and for a REJECTED or FAILED job the absence IS the explanation: a study
      // with no eligible series consumed no series, and flagging that in red as "not
      // recorded" tells a radiologist the platform lost something when the truth is that
      // there was nothing to record. MOS-EXEC-014 is the same mistake in a different
      // place -- a technical alarm on a clinical outcome -- and it costs the panel its
      // credibility on the warnings that do matter.
      fields: REQUIRED_JOB_FIELDS.map(([key, label]) => ({
        key,
        label,
        value: jobProvenance[key],
        missing: status === 'COMPLETED' && isEmpty(jobProvenance[key]),
      })),
      seriesConsumed: jobProvenance.series_consumed || [],
      studyInstanceUID: jobProvenance.study_instance_uid || job.study_instance_uid,
      traceId: jobProvenance.trace_id,
      idempotencyKey: jobProvenance.idempotency_key,
    },

    // Selection is first-class data (MOS-API-054), and for a REJECTED job it is the whole
    // explanation. Shown for a COMPLETED job too, because "which of the seven series did
    // it pick, and why not the others" is the first question a radiologist asks.
    seriesSelection: (job.series_selection || []).map(row => ({
      seriesInstanceUID: row.series_instance_uid,
      decision: row.decision,
      selector: row.selector_name,
      rank: row.rank,
      reasonCode: row.reason_code,
      reasonDetail: row.reason_detail,
      instanceCount: row.instance_count,
      modality: row.modality,
    })),

    // A `results` entry only exists once the capability produced one, so per-result
    // fields are always owed -- no state condition applies here, unlike the job block.
    results: results.map(result => {
      const p = result.provenance || {};
      return {
        resultId: result.result_id,
        capabilityId: result.capability_id,
        capabilityVersion: result.capability_version,
        resultKind: result.result_kind,
        plausibilityState: result.plausibility_state,
        clinicalUseMode: result.clinical_use_mode,
        findings: result.findings || [],
        measurements: (result.measurements || []).map(m => ({
          code: m.concept_code,
          scheme: m.concept_scheme,
          display: m.concept_display,
          value: m.value,
          unit: m.ucum_unit,
          // MOS-IMG-044 / CONTRACT.md section 5: "MUST be computed in SOURCE geometry.
          // Never in model space." Displayed because a measurement whose geometry space
          // is unstated is a number a reviewer cannot check.
          geometrySpace: m.geometry_space,
          sourceSeriesInstanceUID: m.source_series_instance_uid,
        })),
        objects: (result.dicom_objects || []).map(o => ({
          kind: o.object_kind,
          seriesInstanceUID: o.series_instance_uid,
          sopInstanceUID: o.sop_instance_uid,
          sopClassUID: o.sop_class_uid,
          seriesNumber: o.series_number,
          frameCount: o.frame_count,
          stowState: o.stow_state,
          researchMarked: o.research_marked,
        })),
        provenance: {
          fields: REQUIRED_RESULT_FIELDS.map(([key, label]) => ({
            key,
            label,
            value: p[key],
            missing: isEmpty(p[key]),
          })),
          seriesConsumed: p.series_consumed || [],
          instancesConsumed: p.instances_consumed,
          inputUidDigest: p.input_uid_digest,
          inputPixelDigest: p.input_pixel_digest,
          preprocessingVersion: p.preprocessing_version,
          preprocessingDigest: p.preprocessing_digest,
          workerVersion: p.worker_version,
          runtimeVersion: p.runtime_version,
          platformCommit: p.platform_commit,
          geometry: p.geometry || null,
          startedAt: p.started_at,
          finishedAt: p.finished_at,
        },
        missing: missingProvenanceFields(p),
        objectMismatch: objectProvenanceMismatch(result),
      };
    }),

    generatedObjects: objectsByKind(results),

    /** True when every result carried every field CONTRACT.md section 10 requires AND
     *  each result's provenance names exactly the objects that result wrote.
     *  The panel shows this as a single line, because "provenance complete" is the claim
     *  MOS-STORE-278 makes and a reviewer should not have to audit nine rows to check it. */
    provenanceComplete:
      results.length > 0 &&
      results.every(
        r =>
          missingProvenanceFields(r.provenance).length === 0 &&
          objectProvenanceMismatch(r) === ''
      ),
  };
}

export default buildProvenanceView;
