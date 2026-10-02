/* =====================================================================================
 * The commands. This is where "press the button" becomes "POST /api/v1/jobs".
 *
 * MOS-SAFE-089a: "it MUST, for the study currently open in the viewer, issue exactly one
 * POST /api/v1/jobs (Chapter 10) carrying the study's study_instance_uid and the selected
 * target, MUST require the job.create permission and MUST render the returned job_id and
 * Job.status inline, polling or subscribing to the SSE stream of Chapter 10."
 *
 * THE WIRE SHAPE IS CHAPTER 10's
 * ------------------------------
 * `client.submit()` sends `{target, input: {study_instance_uid, ...}}` (MOS-API-043) and
 * reads `Job.status` (MOS-API-049). MOS-UI-021a ruled that the old
 * `{study_instance_uid, capabilities}` / `body.state` divergence "MUST be closed in the
 * surface, not in Chapter 10"; it is closed in `core/client.js`, and this module never
 * builds a body of its own.
 *
 * THE `job.create` PERMISSION
 * ---------------------------
 * CONTRACT.md section 0 removes auth from this slice, so there is no principal to check a
 * permission against and no endpoint that would refuse one. The honest implementation is
 * therefore NOT a client-side `if (user.can('job.create'))` -- that is a stub that looks
 * like an authorisation control and enforces nothing, which MOS-REL-050 forbids and which
 * would be worse than the absence because a reviewer could mistake it for the real thing.
 * What this module does instead: it sends the request and RENDERS whatever the API
 * answers. When weeks 3-5 adds API keys and `job.create`, the server answers 403 with an
 * RFC 9457 document and `ProblemError` already carries it to the panel unchanged. The
 * control lands in one place, server-side, which is where MOS-SAFE-089a's check 24 ("a
 * principal without job.create cannot issue the request") can actually be measured.
 *
 * WHERE THE STUDY UID COMES FROM
 * ------------------------------
 * `DisplaySetService` is OHIF's own state. The command reads the ACTIVE display set's
 * StudyInstanceUID rather than the URL query parameter, because `?StudyInstanceUIDs=a,b`
 * can name several and "the study currently open in the viewer" is one of them. On the
 * study-list route there is no display set, so the URL is the fallback and the button is
 * disabled when neither yields exactly one study (MOS-UI-023).
 *
 * Spec: MOS-SAFE-089a, MOS-API-036, MOS-API-039, MOS-API-043, MOS-API-049, MOS-EXEC-016,
 *       MOS-UI-021, MOS-UI-023, MOS-UI-024, MOS-UI-026, MOS-UI-029, MOS-REL-050.
 * ===================================================================================== */

import { MedicalOSClient, ProblemError, jobStatus } from './core/client.js';
import { MEDICALOS_JOB_EVENT } from './core/events.js';
import { PANEL_ID } from './getPanelModule.js';

export { MEDICALOS_JOB_EVENT };

/** Resolve the one study the viewer is showing. Returns null when it is ambiguous --
 *  a button that guesses which of two studies to analyse is worse than a disabled one. */
export function activeStudyInstanceUID(servicesManager, locationSearch) {
  const services = (servicesManager && servicesManager.services) || {};
  const displaySetService = services.displaySetService || services.DisplaySetService;

  if (displaySetService && typeof displaySetService.getActiveDisplaySets === 'function') {
    const sets = displaySetService.getActiveDisplaySets() || [];
    const uids = Array.from(
      new Set(sets.map(s => s && s.StudyInstanceUID).filter(Boolean))
    );
    if (uids.length === 1) return uids[0];
    if (uids.length > 1) return null; // ambiguous; the caller disables the button
  }

  const search = locationSearch !== undefined
    ? locationSearch
    : typeof window !== 'undefined'
      ? window.location.search
      : '';
  const params = new URLSearchParams(search);
  const fromUrl = params.get('StudyInstanceUIDs') || params.get('StudyInstanceUID');
  if (!fromUrl) return null;
  const parts = fromUrl.split(',').filter(Boolean);
  return parts.length === 1 ? parts[0] : null;
}

export default function getCommandsModule({ servicesManager }) {
  /* THE CREDENTIAL COMES FROM THE VIEWER, NOT FROM THIS PACKAGE.
   *
   * `UserAuthenticationService.getAuthorizationHeader()` is OHIF's own: it returns
   * `{Authorization: 'Bearer ...'}` for the signed-in user and undefined when nobody is
   * signed in. Passing the FUNCTION rather than its value means the token is re-read on
   * every request, so a refresh is picked up and a stale token is never cached inside
   * `MedicalOSClient`.
   *
   * MOS-SAFE-089a: "MUST require the `job.create` permission" and "MUST NOT hold a PACS
   * credential". This is a platform token for `/api/v1`, held by the viewer's own auth
   * service, and the permission is enforced by the API. When there is no credential the
   * API answers 401 and the panel renders the RFC 9457 document -- MOS-REL-050 forbids
   * the client-side stub that would hide that. */
  const authServices = (servicesManager && servicesManager.services) || {};
  const userAuthenticationService =
    authServices.userAuthenticationService || authServices.UserAuthenticationService || null;
  const client = new MedicalOSClient({
    authHeaderProvider:
      userAuthenticationService &&
      typeof userAuthenticationService.getAuthorizationHeader === 'function'
        ? () => userAuthenticationService.getAuthorizationHeader()
        : null,
  });
  /** jobId -> follow handle, so a second activation does not open a second SSE stream. */
  const following = new Map();

  const services = (servicesManager && servicesManager.services) || {};
  const uiNotificationService =
    services.uiNotificationService || services.UINotificationService || null;
  const panelService = services.panelService || services.PanelService || null;

  /** One place that tells the panel (and anything else) about a job document.
   *  See `core/events.js` for why this is a DOM event and not an OHIF service. */
  const publish = detail => {
    if (typeof window !== 'undefined' && window.dispatchEvent) {
      window.dispatchEvent(new CustomEvent(MEDICALOS_JOB_EVENT, { detail }));
    }
  };

  /** Bring the panel forward so the job id the reader just created is visible.
   *  `forceActive` because the reader pressed a button: they asked to see this. */
  const revealPanel = () => {
    if (panelService && typeof panelService.activatePanel === 'function') {
      panelService.activatePanel(PANEL_ID, true);
    }
  };

  const actions = {
    /**
     * THE button's command. Exactly one POST per activation (MOS-UI-021).
     * @param {string} [capabilityId] the reader's target; omitted means the whole
     *                                service version this deployment pins.
     */
    async runMedicalOSAnalysis({ capabilityId } = {}) {
      const studyInstanceUID = activeStudyInstanceUID(servicesManager);
      if (!studyInstanceUID) {
        notify('error', 'MedicalOS', 'No single study is open; nothing to analyse.');
        return null;
      }

      publish({ phase: 'submitting', studyInstanceUID });
      revealPanel();

      let submission;
      try {
        submission = await client.submit(studyInstanceUID, capabilityId);
      } catch (err) {
        // An RFC 9457 document reaches the panel unchanged (MOS-API-036). Never
        // rewritten into a friendly sentence here: the `class` and `code` are what a
        // support ticket quotes.
        const problem = err instanceof ProblemError ? err.problem : { title: String(err) };
        publish({ phase: 'submit_failed', studyInstanceUID, problem });
        notify('error', 'MedicalOS', problem.detail || problem.title || 'Submission failed');
        return null;
      }

      // MOS-UI-024: a replay MUST be rendered as a replay. Telling a reader that two
      // analyses exist when one does invites them to wait for a result that never arrives.
      notify(
        'info',
        'MedicalOS',
        submission.replayed
          ? `Already running as ${submission.job_id}`
          : `Submitted as ${submission.job_id}`
      );
      publish({
        phase: 'submitted',
        studyInstanceUID,
        jobId: submission.job_id,
        replayed: submission.replayed,
      });

      // One follow per job id.
      if (following.has(submission.job_id)) return submission.job_id;
      const handle = client.follow(submission.job_id, job => {
        publish({
          phase: 'update',
          studyInstanceUID,
          jobId: job.job_id,
          job,
          replayed: submission.replayed,
        });
      });
      following.set(submission.job_id, handle);
      handle.promise
        .then(job => {
          // MOS-EXEC-016: REJECTED is a clinical outcome and MUST NOT be announced as an
          // error. `info`, not `error`, and the panel carries the detail.
          const status = jobStatus(job);
          if (status === 'REJECTED') {
            notify('info', 'MedicalOS', 'Study not analysed — see the MedicalOS panel.');
          } else if (status === 'FAILED') {
            notify('error', 'MedicalOS', 'Analysis failed — see the MedicalOS panel.');
          } else if (status === 'COMPLETED') {
            notify('success', 'MedicalOS', 'Analysis complete.');
          }
        })
        .catch(err => {
          publish({
            phase: 'follow_failed',
            jobId: submission.job_id,
            problem: err instanceof ProblemError ? err.problem : { title: String(err) },
          });
        })
        .finally(() => following.delete(submission.job_id));

      return submission.job_id;
    },

    /** Re-read one job on demand. The panel's refresh control. */
    async refreshMedicalOSJob({ jobId }) {
      if (!jobId) return null;
      const job = await client.getJob(jobId);
      publish({ phase: 'update', jobId, job });
      return job;
    },

    /** Stop following, e.g. when the mode exits. */
    stopFollowingMedicalOSJob({ jobId }) {
      if (jobId === undefined) {
        following.forEach(handle => handle.cancel());
        following.clear();
        return;
      }
      const handle = following.get(jobId);
      if (handle) {
        handle.cancel();
        following.delete(jobId);
      }
    },

    /** The panel toolbar button's command. */
    showMedicalOSPanel() {
      revealPanel();
    },
  };

  function notify(type, title, message) {
    if (uiNotificationService && typeof uiNotificationService.show === 'function') {
      uiNotificationService.show({ title, message, type, duration: 5000 });
    }
  }

  return {
    actions,
    definitions: {
      runMedicalOSAnalysis: {
        commandFn: actions.runMedicalOSAnalysis,
        storeContexts: [],
        options: {},
      },
      refreshMedicalOSJob: { commandFn: actions.refreshMedicalOSJob, storeContexts: [] },
      stopFollowingMedicalOSJob: {
        commandFn: actions.stopFollowingMedicalOSJob,
        storeContexts: [],
      },
      showMedicalOSPanel: { commandFn: actions.showMedicalOSPanel, storeContexts: [] },
    },
    defaultContext: 'VIEWER',
  };
}
