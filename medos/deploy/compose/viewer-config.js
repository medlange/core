/* =====================================================================================
 * WHAT MAKES `viewer/` THE MEDICALOS CLINICIAN SURFACE.
 *
 * The viewer is a DICOMweb viewer. It knows PS3.18, it knows how to window a CT and lay a
 * PET over it, and it knows nothing about tenants, consumer classes or this platform. That
 * was not true until this file existed: `app.js` built its client against
 * `/dicomweb/${TENANT}` and `dicomweb.js` sent `X-MedicalOS-Surface: clinical_viewer` on
 * every request, which meant the viewer could not be pointed at an Orthanc, a dcm4chee or
 * anything else conformant. Four lines of this deployment's vocabulary, compiled in.
 *
 * They are configuration now, and this file is the configuration. Mounted at
 * `/mos-viewer/viewer-config.js`, read by a classic script before the module entry point.
 *
 * WHAT EACH ONE BUYS, because none of them is decoration:
 *
 *   dicomWebRoot  PS3.18 defines the service paths under a root and says nothing about
 *                 what precedes it. `/dicomweb` is where this stack's nginx proxies the
 *                 Gateway (MOS-DATA-006: the Gateway is the only route to the PACS).
 *
 *   tenant        The path segment MOS-DATA-009 makes the Gateway compare against the
 *                 authenticated principal's tenant. A viewer that REQUIRED one would be a
 *                 viewer only this platform can use; a Gateway that did not check one
 *                 would be a Gateway that leaks across tenants. Both facts survive.
 *
 *   surface       MOS-UI-005's closed value space, and what makes MOS-DATA-040's
 *                 `pixel_phi.action: ALLOW` -- "only when the consumer class is
 *                 clinical_viewer" -- checkable AT THE ORIGIN rather than asserted by the
 *                 surface about itself. Absent, no header is sent at all: declaring a
 *                 consumer class to an origin that never granted one is a claim, and this
 *                 file is the only place entitled to make it.
 *
 * THE QUERY STRING STILL OVERRIDES THE TENANT. `?tenant=` is how a reader reaches a second
 * tenant without a second deployment, it predates this seam, and people hold links.
 * ===================================================================================== */

window.VIEWER_CONFIG = {
  // The name in the tab and the heading. The FOOTER STATEMENT is not here and must
  // not be: it is a required safety marking, and nginx puts it into the delivered
  // bytes so it does not depend on this file having loaded.
  productName: 'MedicalOS Viewer',
  dicomWebRoot: '/dicomweb',
  tenant: '00000000-0000-0000-0000-000000000000',
  // BOTH HALVES, because the viewer holds neither. The name is this platform's and
  // so is the value; a client that knew either would be a client that knows which
  // platform it serves.
  surfaceHeader: 'X-MedicalOS-Surface',
  surface: 'clinical_viewer',
};
