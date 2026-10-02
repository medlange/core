/* =====================================================================================
 * MedicalOSClient -- the ONLY job-creation path in the viewer.
 *
 * MOS-SAFE-089a, verbatim on the two points this file exists to guarantee:
 *   "it MUST, for the study currently open in the viewer, issue exactly one
 *    POST /api/v1/jobs ... MUST NOT create a second job-creation path, MUST NOT hold a
 *    PACS credential"
 *
 * EXACTLY ONE POST
 *   `submit()` is not idempotent by hope. An `inFlight` map keyed by the submission's
 *   CONTENT -- the study plus the `target` (MOS-UI-021) -- holds the pending promise, so a
 *   double-click, a re-render or a second toolbar activation returns the SAME promise and
 *   issues no second request. The server-side guarantee is stronger still and independent
 *   of this one -- `repo.create_job_queued` dedupes on the platform-derived
 *   idempotency key (MOS-EXEC-053), so a repeat POST answers 200 with
 *   `MedicalOS-Idempotent-Replay: true` instead of creating a second job -- but a UI that
 *   relies on the server to undo its own double-submit is a UI that fires two requests,
 *   and acceptance check 24 counts requests.
 *
 * NO PACS CREDENTIAL, AND NO CREDENTIAL OF ITS OWN
 *   There is no `user`, no `password`, no stored token and no DICOMweb URL in this file.
 *   Every request goes to `apiRoot` (default `/api/v1`), which is MedicalOS. The viewer's
 *   route to the archive is OHIF's own data source through the Gateway, which this client
 *   never touches.
 *
 *   `Authorization` IS sent on `/api/v1` requests when the HOST supplies one, and that is
 *   a different thing: `authHeaderProvider` is a callback, wired in
 *   `src/getCommandsModule.js` to OHIF's `userAuthenticationService`, so the value is the
 *   signed-in clinician's platform token, re-read per request and never copied here. That
 *   is how `MOS-SAFE-089a`'s "MUST require the `job.create` permission" becomes real:
 *   the server checks it, and when there is no credential the API answers 401 and the
 *   panel renders the problem document. A PACS credential remains forbidden and absent.
 *
 * FRAMEWORK-FREE, ON PURPOSE
 *   Plain ES module, no React, no OHIF import. It is consumed by BOTH
 *   `src/getCommandsModule.js` (inside a built OHIF) and `standalone/app.js` (a page
 *   served next to the pre-built viewer image). One implementation, two hosts -- which is
 *   the same "exactly one job-creation path" rule applied to the code rather than to the
 *   request.
 *
 * THE WIRE SHAPE IS CHAPTER 10's
 *   `submit()` sends `{target, input: {study_instance_uid, ...}}` (MOS-API-043) and reads
 *   `Job.status` (MOS-API-049). It used to send CONTRACT.md section 9's
 *   `{study_instance_uid, capabilities}` and read `body.state`. MOS-UI-021a named that
 *   divergence and ruled that it "MUST be closed in the surface, not in Chapter 10"; this
 *   is where it is closed. See `submit()` and `jobStatus()`.
 *
 * Spec: MOS-SAFE-089a, MOS-API-023, MOS-API-026, MOS-API-043, MOS-API-044, MOS-API-045,
 *       MOS-API-046, MOS-API-047, MOS-API-049, MOS-EXEC-053, MOS-UI-011, MOS-UI-021,
 *       MOS-UI-021a, MOS-UI-022, MOS-UI-024, MOS-UI-029, CONTRACT.md sections 9 and 10.
 * ===================================================================================== */

/** Terminal states of the job machine (CONTRACT.md section 3). `CANCELLED` is RESERVED in
 *  this slice -- the enum value exists and nothing produces it -- and is listed so that a
 *  future producer does not leave the UI polling forever. */
export const TERMINAL_STATES = Object.freeze([
  'COMPLETED',
  'FAILED',
  'REJECTED',
  'CANCELLED',
]);

/** MOS-EXEC-016 / MOS-EXEC-014: a clinical non-answer is NOT an error, and every UI MUST
 *  render it with non-error affordances. This is the one place the distinction is decided;
 *  the renderers ask this function rather than string-matching a state. */
export function outcomeKind(state) {
  if (state === 'COMPLETED') return 'success';
  if (state === 'REJECTED') return 'rejected'; // clinical. NOT an error.
  if (state === 'FAILED') return 'failed'; // technical.
  if (state === 'CANCELLED') return 'cancelled';
  return 'running';
}

export function isTerminal(state) {
  return TERMINAL_STATES.indexOf(state) !== -1;
}

/** Deployment configuration, from `window.MEDICALOS` in deploy/compose/ohif-config.js.
 *  Defaults are same-origin relative paths: the extension must work when nobody has
 *  configured anything, and an absolute default would be a cross-origin request the API
 *  has no CORS policy for. */
export function readConfig(globalScope) {
  const g = globalScope || (typeof window !== 'undefined' ? window : {});
  const cfg = g.MEDICALOS || {};
  return {
    apiRoot: cfg.apiRoot || '/api/v1',
    capabilities: cfg.capabilities || [
      'lung_segmentation',
      'emphysema_laa',
      'pleural_effusion',
    ],
    /** MOS-API-044's `service_version` target, as DEPLOYMENT CONFIGURATION.
     *  MOS-UI-011 forbids the surface to resolve, rank, pin or substitute a
     *  ServiceVersion, and MOS-UI-017 says what a surface does before 0.3.0 instead:
     *  render "a fixed list, configured per deployment, of the single ServiceVersion
     *  pinned for each capability". This is that pin. The surface reads it and sends it;
     *  it never computes it. */
    serviceVersion: cfg.serviceVersion || 'medos.slice/0.1.0',
    /** Which target the toolbar button submits when nobody chose one. An id from
     *  `capabilities`, or null for "everything this service version runs". */
    defaultCapability: cfg.defaultCapability || null,
    useEventStream: cfg.useEventStream !== false,
    pollIntervalMs: cfg.pollIntervalMs || 1500,
    clinicalUseMode: cfg.clinicalUseMode || 'research_only',
  };
}

/** `Job.status` (`MOS-API-049`) from a job document, tolerating a server that still
 *  answers CONTRACT.md section 9's `state`.
 *
 *  MOS-UI-029: "The state's classification MUST be decided in exactly one place in the
 *  surface's code ... and every renderer MUST ask that one function." This is the reader
 *  half of that rule -- one place picks the field, `outcomeKind()` above classifies the
 *  value, and no component string-matches a status anywhere else. The fallback is not
 *  politeness: a viewer that renders a blank badge against an older API looks like a
 *  platform that lost the job. */
export function jobStatus(job) {
  if (!job) return undefined;
  return job.status !== undefined && job.status !== null ? job.status : job.state;
}

/** An RFC 9457 problem document, as an Error that keeps the document.
 *  MOS-API-036/039: the `class` member separates `clinical_rejection` from
 *  `transport_failure` and `system_failure`, and the UI must never collapse them. */
export class ProblemError extends Error {
  constructor(problem, httpStatus) {
    const detail =
      (problem && (problem.detail || problem.title)) || `HTTP ${httpStatus}`;
    super(detail);
    this.name = 'ProblemError';
    this.problem = problem || {};
    this.httpStatus = httpStatus;
    this.problemClass = (problem && problem.class) || 'system_failure';
    this.code = (problem && problem.code) || null;
    this.traceId = (problem && problem.trace_id) || null;
  }
}

export class MedicalOSClient {
  /**
   * @param {object}   options
   * @param {string}   options.apiRoot   e.g. '/api/v1'
   * @param {Function} options.fetchImpl injected for tests; defaults to window.fetch
   * @param {Function} options.EventSourceImpl injected for tests
   */
  constructor(options = {}) {
    const cfg = readConfig(options.globalScope);
    this.apiRoot = options.apiRoot || cfg.apiRoot;
    this.capabilities = options.capabilities || cfg.capabilities;
    this.serviceVersion = options.serviceVersion || cfg.serviceVersion;
    this.defaultCapability =
      options.defaultCapability !== undefined ? options.defaultCapability : cfg.defaultCapability;
    this.useEventStream =
      options.useEventStream === undefined ? cfg.useEventStream : options.useEventStream;
    this.pollIntervalMs = options.pollIntervalMs || cfg.pollIntervalMs;
    this._fetch =
      options.fetchImpl ||
      (typeof fetch !== 'undefined' ? (...a) => fetch(...a) : null);
    this._EventSource =
      options.EventSourceImpl ||
      (typeof EventSource !== 'undefined' ? EventSource : null);
    /* THE PLATFORM CREDENTIAL, AND WHY IT IS A CALLBACK RATHER THAN A VALUE.
     *
     * MOS-SAFE-089a: the button "MUST require the `job.create` permission" and "MUST NOT
     * hold a PACS credential". Both hold here. This package stores no credential of any
     * kind: `getCommandsModule.js` passes OHIF's own
     * `userAuthenticationService.getAuthorizationHeader()`, so the header is the SIGNED-IN
     * CLINICIAN's, minted by the deployment's identity provider, re-read on every request
     * and never copied into this object. A token cached at construction is a token that
     * outlives its refresh.
     *
     * It is a PLATFORM credential for `/api/v1` and not a PACS one: the archive is
     * reached by OHIF's own data source through the Gateway, which this file never
     * addresses. And when the provider yields nothing, no header is sent and the API
     * answers 401 with an RFC 9457 document that `ProblemError` carries to the panel
     * unchanged -- the control stays server-side, which is the only place check 24's "a
     * principal without job.create cannot issue the request" can be measured. */
    this._authHeader = options.authHeaderProvider || null;
    /** studyKey -> Promise. The single-POST guard. */
    this._inFlight = new Map();
  }

  // ---------------------------------------------------------------------------------
  // POST /api/v1/jobs
  // ---------------------------------------------------------------------------------
  /**
   * The `target` this surface submits, from deployment configuration only.
   *
   * MOS-API-044 closes `target.kind` to `capability` and `service_version`, and requires
   * `version_range` to be ABSENT when the kind is `service_version` -- so this builds the
   * two shapes and nothing in between.
   *
   * MOS-UI-011: the surface MUST NOT resolve, rank, pin or substitute a ServiceVersion.
   * Nothing here does: `capabilityId` is what the reader picked and `this.serviceVersion`
   * is a configured string (MOS-UI-017's "fixed list, configured per deployment").
   *
   * @param {string|null} capabilityId  null means "everything this service version runs"
   */
  /** Request headers, with the viewer's platform credential when it has one.
   *
   *  `getAuthorizationHeader()` is OHIF's own shape -- `{Authorization: 'Bearer ...'}` or
   *  undefined. Anything the provider throws is swallowed into "no credential", because a
   *  viewer whose identity provider is mid-refresh must get a 401 it can render, not an
   *  unhandled rejection with no job id and no explanation. */
  _headers(extra) {
    const headers = Object.assign(
      { Accept: 'application/json, application/problem+json' },
      extra || {}
    );
    if (!this._authHeader) return headers;
    let provided;
    try {
      provided = this._authHeader();
    } catch (_e) {
      return headers;
    }
    if (provided && provided.Authorization) headers.Authorization = provided.Authorization;
    return headers;
  }

  targetFor(capabilityId, versionRange) {
    if (capabilityId) {
      const target = { kind: 'capability', id: capabilityId };
      if (versionRange) target.version_range = versionRange;
      return target;
    }
    return { kind: 'service_version', id: this.serviceVersion };
  }

  /**
   * Submit ONE job for the study currently open in the viewer.
   *
   * THE WIRE SHAPE IS CHAPTER 10's, AND THAT IS A CHANGE
   * ----------------------------------------------------
   * MOS-API-043 fixes the body as `{target, input: {study_instance_uid, ...}}` and
   * MOS-API-049 fixes the status field as `Job.status`. This client used to send
   * CONTRACT.md section 9's `{study_instance_uid, capabilities}` and read `body.state`;
   * MOS-UI-021a names that divergence and rules that it "MUST be closed in the surface,
   * not in Chapter 10". It is closed here. `medos/medos/api/routes_jobs.py` accepts both
   * envelopes so that the older callers in the tree keep working.
   *
   * `input.prior_study_instance_uids` and `input.series_instance_uids` are sent EMPTY
   * rather than omitted. MOS-API-045 puts them in the schema "from 0.1.0 so that
   * prior-comparison work never requires a breaking widening"; sending them empty is how
   * a client proves it is on the widened schema rather than the pre-0.1.0 one.
   *
   * `Idempotency-Key` is sent only when the caller supplies one and is NEVER derived from
   * anything the UI knows about the patient: MOS-API-029 keeps the header out of UID
   * derivation server-side, MOS-UI-022 forbids deriving it from a StudyInstanceUID or any
   * DICOM header value, and the platform already dedupes on its own derived key
   * (MOS-EXEC-053).
   *
   * @param {string}      studyInstanceUID
   * @param {string|null} capabilityId  the reader's choice, or null for the whole
   *                                    service version (see `targetFor`)
   * @returns {Promise<{job_id: string, status: string, replayed: boolean}>}
   */
  submit(studyInstanceUID, capabilityId, options = {}) {
    if (!studyInstanceUID) {
      return Promise.reject(new Error('no study is open; nothing to submit'));
    }
    const target = this.targetFor(
      capabilityId === undefined ? this.defaultCapability : capabilityId,
      options.versionRange
    );
    // Keyed by the submission's CONTENT (MOS-UI-021: "an in-flight guard keyed by the
    // submission's content"), which is now the target plus the study rather than the
    // study plus a capability list.
    const key = `${studyInstanceUID}|${target.kind}|${target.id}|${target.version_range || ''}`;

    // THE SINGLE-POST GUARD. A second activation while the first is in flight returns the
    // first promise and issues no request (MOS-SAFE-089a acceptance check 24).
    if (this._inFlight.has(key)) {
      return this._inFlight.get(key);
    }

    const headers = this._headers({ 'Content-Type': 'application/json' });
    if (options.idempotencyKey) {
      headers['Idempotency-Key'] = options.idempotencyKey;
    }

    const body = {
      target,
      input: {
        study_instance_uid: studyInstanceUID,
        prior_study_instance_uids: [],
        series_instance_uids: [],
      },
    };
    if (options.requestedOutputs) body.requested_outputs = options.requestedOutputs;

    const promise = this._fetch(`${this.apiRoot}/jobs`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    })
      .then(async response => {
        const doc = await readJson(response);
        if (!response.ok) {
          throw new ProblemError(doc, response.status);
        }
        return {
          job_id: doc.job_id,
          // MOS-API-049. `jobStatus` is the one place the field is chosen.
          status: jobStatus(doc),
          // MOS-API-026 / MOS-UI-024: a repeat POST answers 200, not 202, and says so in
          // a header. Surfaced so the UI can say "already running as job_..." instead of
          // "submitted" -- telling a reader that two analyses exist when one does invites
          // them to wait for a second result that will never arrive.
          replayed:
            response.status === 200 ||
            response.headers.get('MedicalOS-Idempotent-Replay') === 'true',
          trace_id: response.headers.get('MedicalOS-Trace-Id'),
        };
      })
      .finally(() => {
        this._inFlight.delete(key);
      });

    this._inFlight.set(key, promise);
    return promise;
  }

  // ---------------------------------------------------------------------------------
  // GET /api/v1/jobs/{job_id}
  // ---------------------------------------------------------------------------------
  async getJob(jobId) {
    const response = await this._fetch(`${this.apiRoot}/jobs/${encodeURIComponent(jobId)}`, {
      headers: this._headers(),
    });
    const body = await readJson(response);
    if (!response.ok) {
      // NOTE the asymmetry, and it is the whole point: a REJECTED job is answered 200
      // with a `rejection` member (MOS-API-047), so it never reaches this branch. A 4xx
      // here really is a fact about the REQUEST -- a bad job id, an unknown job.
      throw new ProblemError(body, response.status);
    }
    return body;
  }

  // ---------------------------------------------------------------------------------
  // Following a job to its terminal state
  // ---------------------------------------------------------------------------------
  /**
   * Follow one job until it terminates, calling `onUpdate(job)` with each fresh
   * `GET /api/v1/jobs/{id}` document.
   *
   * MOS-SAFE-089a permits "polling or subscribing to the SSE stream". This does BOTH, in
   * that order of preference: the SSE stream is the low-latency signal and the GET is the
   * source of truth. The event envelope is deliberately NOT parsed for job fields -- an
   * event says *that* something changed; the job document says *what it now is*. Rendering
   * from the event payload would give two representations of one job that can disagree
   * mid-stream, and the provenance panel would show a result the job row does not yet
   * have.
   *
   * Returns a handle with `.cancel()`; the promise resolves with the terminal job.
   */
  follow(jobId, onUpdate, options = {}) {
    const useStream =
      (options.useEventStream === undefined ? this.useEventStream : options.useEventStream) &&
      !!this._EventSource;
    let cancelled = false;
    let source = null;
    let timer = null;

    const promise = new Promise((resolve, reject) => {
      const refresh = async () => {
        if (cancelled) return;
        let job;
        try {
          job = await this.getJob(jobId);
        } catch (err) {
          if (cancelled) return;
          cleanup();
          reject(err);
          return;
        }
        if (cancelled) return;
        try {
          onUpdate && onUpdate(job);
        } catch (_e) {
          /* a renderer that throws must not strand the follow loop */
        }
        if (isTerminal(jobStatus(job))) {
          cleanup();
          resolve(job);
        }
      };

      const cleanup = () => {
        cancelled = true;
        if (source) {
          source.close();
          source = null;
        }
        if (timer) {
          clearInterval(timer);
          timer = null;
        }
      };

      // Always poll, even with SSE open. Not belt-and-braces for its own sake: nginx,
      // a corporate proxy or a paused browser tab can each silently stall an event
      // stream, and a button that hangs on "RUNNING" forever is indistinguishable from a
      // wedged platform. The poll is slow (pollIntervalMs) and the stream is what makes
      // the UI feel immediate.
      timer = setInterval(refresh, this.pollIntervalMs);

      if (useStream) {
        source = new this._EventSource(
          `${this.apiRoot}/jobs/${encodeURIComponent(jobId)}/events`
        );
        // Every job event is a reason to re-read the job. The payloads differ
        // (job.step_changed, job.completed, job.rejected, ...) and none of them is the
        // job document.
        source.onmessage = () => refresh();
        source.onerror = () => {
          // Degrade to the poll rather than fail. An SSE stream ends normally when the
          // job terminates, and EventSource reports that end as an error.
          if (source) {
            source.close();
            source = null;
          }
        };
      }

      // Read once immediately: a job that is already terminal when the button is pressed
      // (the idempotent-replay case) must not wait a poll interval to render.
      refresh();
    });

    return {
      promise,
      cancel() {
        cancelled = true;
        if (source) source.close();
        if (timer) clearInterval(timer);
      },
    };
  }

  /** `GET /api/v1/jobs/{id}/series-selection` -- MOS-API-054, "selection is first-class
   *  data, not a log line". The panel links to it for a REJECTED job, where the rejection
   *  document carries `series_selection_href`. */
  async getSeriesSelection(jobId) {
    const response = await this._fetch(
      `${this.apiRoot}/jobs/${encodeURIComponent(jobId)}/series-selection`,
      { headers: this._headers() }
    );
    const body = await readJson(response);
    if (!response.ok) throw new ProblemError(body, response.status);
    return body;
  }
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (_e) {
    // A proxy error page, not a problem document. Shape it like one so the caller has a
    // single type to handle.
    return {
      title: 'Non-JSON response',
      detail: text.slice(0, 200),
      class: 'transport_failure',
    };
  }
}

export default MedicalOSClient;
