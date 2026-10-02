/* =====================================================================================
 * The one DOM event this package publishes, and the one place its name is written.
 *
 * WHY A DOM EVENT RATHER THAN AN OHIF SERVICE
 * -------------------------------------------
 * The commands module produces job documents and the provenance panel consumes them. A
 * `PubSubService` of our own would be a second registration surface to keep working
 * across OHIF minors, and `MOS-REL-027` makes the pinned viewer a replaceable dependency
 * -- so the seam between our two halves is a plain `CustomEvent` on `window`, which is a
 * browser API and cannot move when the viewer does.
 *
 * It is also what lets the panel be a custom element with no React and no OHIF import
 * (see `src/panels/provenance-panel.js`).
 *
 * This module exists separately from `client.js` so that the panel can import the event
 * name without importing the job-creation path. `MOS-SAFE-089a`: "MUST NOT create a
 * second job-creation path" -- a panel that cannot reach `MedicalOSClient.submit()`
 * cannot become one by accident.
 * ===================================================================================== */

/** `detail` is one of:
 *    {phase:'submitting',    studyInstanceUID}
 *    {phase:'submitted',     studyInstanceUID, jobId, replayed}
 *    {phase:'update',        studyInstanceUID, jobId, job}      <- the job DOCUMENT
 *    {phase:'submit_failed', studyInstanceUID, problem}         <- RFC 9457, verbatim
 *    {phase:'follow_failed', jobId, problem}
 */
export const MEDICALOS_JOB_EVENT = 'medicalos:job-updated';

export default MEDICALOS_JOB_EVENT;
