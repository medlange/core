/* WITHDRAWN CITATIONS. `MOS-UI-012` and `MOS-UI-012a`, cited below as the bound on what
 * this package may contribute, were WITHDRAWN at specification 0.4.0. `MOS-UI-012b` and
 * `MOS-UI-012c` replaced them for `viewer/`, the first-party surface, and say nothing
 * about an extension package -- the boundary was removed and not redrawn, so the citations
 * are marked rather than re-pointed. Nothing loads this file any more either: the host that
 * named it, `deploy/compose/ohif-config.js`, went with the OHIF withdrawal. Register entry
 * 109 in docs/spec/99-known-inconsistencies.md holds the whole account. */

/* =====================================================================================
 * @medicalos/extension-medicalos -- the OHIF extension.
 *
 * MOS-REL-027 / MOS-SAFE-089a: the button "MUST live in an OHIF extension package outside
 * the OHIF tree". This directory IS that package. Nothing here is patched into OHIF's own
 * source; the viewer stays a pinned, replaceable dependency, which is the upgradability
 * boundary the requirement is about. (Not a licence boundary: OHIF is MIT and
 * MOS-REL-034's copyleft in-process-linking rule does not reach it.)
 *
 * =====================================================================================
 * HOW THIS LOADS INTO A PRE-BUILT `ohif/app:v3.9.2`, AND WHY THAT IS ADOPTION
 * =====================================================================================
 * This file is imported AT RUNTIME by the viewer the compose stack already runs. It is
 * not compiled into OHIF, and OHIF is not rebuilt, forked or patched (MOS-CORE-038,
 * MOS-UI-204, MOS-UI-213).
 *
 * The mechanism is OHIF's own, and it is in the shipped image's bytes:
 *
 *   platform/app/src/pluginImports.js, in app.bundle.<hash>.js, ends its generated
 *   if-chain with
 *       return (await window.browserImportFunction(module)).default;
 *   and index.html defines
 *       function browserImportFunction(moduleId) { return import(moduleId); }
 *   while appInit does
 *       const loadedExtensions = await importItems([...defaultExtensions,
 *                                                   ...appConfig.extensions]);
 *
 * So any entry of `window.config.extensions` that is NOT one of OHIF's own package names
 * is passed to a native dynamic `import()` and its default export is registered as an
 * extension. A URL is such an entry. `deploy/compose/ohif-config.js` therefore lists
 * '/medicalos/src/index.js' and the viewer imports this module from the same origin it
 * serves itself from.
 *
 * That constrains this package in two ways, and both are load-bearing:
 *   * every module under `src/` MUST be a browser-native ES module -- relative specifiers
 *     with explicit `.js`, no JSX, no bare imports, no build step. `src/panels/
 *     ProvenancePanel.jsx` was deleted for this reason and replaced by a custom element.
 *   * this package MUST NOT import React. See `src/panels/provenance-panel.js`.
 *
 * `medos/web/ohif-extension/README.md` records the options that were considered (rebuilding the
 * viewer from the OHIF monorepo; moving to a newer OHIF) and why this one was chosen.
 *
 * WHAT IT CONTRIBUTES  (MOS-UI-012, withdrawn at 0.4.0: "a toolbar entry, the
 * commands behind it, and one panel" -- exactly that, and nothing else)
 *   toolbar   "Analyze with MedicalOS"  -> command `runMedicalOSAnalysis`
 *             "MedicalOS provenance"    -> command `showMedicalOSPanel`
 *   commands  runMedicalOSAnalysis / refreshMedicalOSJob / stopFollowingMedicalOSJob /
 *             showMedicalOSPanel
 *   panel     MedicalOS provenance (CONTRACT.md section 10, MOS-SAFE-088)
 *
 * WHAT IT DELIBERATELY DOES NOT CONTRIBUTE
 *   no data source  -- OHIF's own dicomweb data source addresses the archive through the
 *                      Gateway. A MedicalOS data source would be a second route to the
 *                      PACS from the viewer, which MOS-UI-002 forbids and
 *                      MOS-SAFE-089a check 24 measures ("zero viewer-to-PACS traffic
 *                      outside the Gateway").
 *   no mode         -- a mode is a deployment decision, and MOS-UI-012's list did not
 *                      include one. The toolbar entry and the panel are attached to
 *                      whichever mode the deployment runs, from `onModeEnter` below.
 *   no SOPClassHandler -- MOS-UI-012a (withdrawn at 0.4.0), a MUST NOT: the
 *                      generated SEG and SR are ordinary DICOM and OHIF's own
 *                      segmentation and SR extensions already render them.
 *                      Reimplementing that would be the platform asserting a
 *                      rendering it did not test.
 * ===================================================================================== */

import { id } from './id.js';
import getCommandsModule from './getCommandsModule.js';
import getPanelModule, { PANEL_ID } from './getPanelModule.js';
import getToolbarModule, {
  analyzeButton,
  panelButton,
  bindToolbarServices,
} from './getToolbarModule.js';
import { PROVENANCE_PANEL_TAG } from './panels/provenance-panel.js';

const TOOLBAR_SECTION = 'primary';

/** Add our buttons to the mode's primary toolbar section without disturbing it.
 *
 *  ORDER, AND WHY THE GUARD IS NOT PARANOIA. `ExtensionManager.onModeEnter` runs before
 *  `mode.onModeEnter` (Mode route, `setupRouteInit`), and `ToolbarService.createButtonSection`
 *  APPENDS when the section already exists and CREATES it when it does not -- so this is
 *  correct in either order. What is not safe is running twice: a second call would push
 *  the same ids again and the button would render twice. Hence the membership check,
 *  against `toolbarService.state.buttonSections`, which is the same field
 *  `getButtonSection` reads. */
function attachToolbar(toolbarService) {
  if (!toolbarService) return;
  toolbarService.addButtons([analyzeButton, panelButton]);
  const sections = (toolbarService.state && toolbarService.state.buttonSections) || {};
  const present = sections[TOOLBAR_SECTION] || [];
  const missing = [analyzeButton.id, panelButton.id].filter(
    buttonId => present.indexOf(buttonId) === -1
  );
  if (missing.length) {
    toolbarService.createButtonSection(TOOLBAR_SECTION, missing);
  }
}

/** Add the provenance panel to the right-hand side panel.
 *
 *  The Mode route calls `panelService.reset()` and then `addPanels(Right, mode's
 *  rightPanels)` when the layout template resolves, which happens BEFORE
 *  `extensionManager.onModeEnter`. `PanelService.addPanel` appends and broadcasts
 *  `PANELS_CHANGED`, and `SidePanelWithServices` re-reads `getPanels(side)` on that event,
 *  so adding here makes the tab appear without touching the mode's own panels.
 *
 *  Both facts above were read out of the shipped `ohif/app:v3.9.2` bundle, not assumed. */
function attachPanel(panelService) {
  if (!panelService || typeof panelService.addPanel !== 'function') return;
  const position = (panelService.PanelPosition && panelService.PanelPosition.Right) || 'right';
  const already = (panelService.getPanels(position) || []).some(panel => panel.id === PANEL_ID);
  if (already) return;
  panelService.addPanel(position, PANEL_ID);
}

const medicalosExtension = {
  id,

  /** Pinned against the OHIF the stack ships (medos/deploy/compose/docker-compose.yml:
   *  ohif/app:v3.9.2). */
  version: '0.1.0',

  preRegistration() {
    // Nothing to register. Kept as an explicit no-op with this comment because an OHIF
    // extension without `preRegistration` and an OHIF extension whose `preRegistration`
    // was deleted by accident look identical in a diff.
    return undefined;
  },

  getCommandsModule,
  getPanelModule,
  getToolbarModule,

  /** Called by `ExtensionManager.onModeEnter` with `{servicesManager, commandsManager,
   *  hotkeysManager}` -- note that `extensionManager` and `appConfig` are NOT passed, so
   *  nothing here may depend on them. */
  onModeEnter({ servicesManager }) {
    const services = (servicesManager && servicesManager.services) || {};
    bindToolbarServices(servicesManager);
    attachToolbar(services.toolbarService);
    attachPanel(services.panelService);
  },

  onModeExit({ servicesManager, commandsManager }) {
    // Close every SSE stream this extension opened. A stream left open across a mode exit
    // keeps polling `/api/v1/jobs/{id}` for a job nobody is looking at.
    if (commandsManager && typeof commandsManager.runCommand === 'function') {
      try {
        commandsManager.runCommand('stopFollowingMedicalOSJob', {});
      } catch (_e) {
        /* the command is gone because the extension is being torn down; nothing to do */
      }
    }
    bindToolbarServices(null);
    void servicesManager;
  },
};

export { analyzeButton, panelButton, id, PANEL_ID, PROVENANCE_PANEL_TAG };
export default medicalosExtension;
