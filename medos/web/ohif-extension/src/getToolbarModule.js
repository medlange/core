/* WITHDRAWN CITATIONS. `MOS-UI-012` and `MOS-UI-012a`, cited below as the bound on what
 * this package may contribute, were WITHDRAWN at specification 0.4.0. `MOS-UI-012b` and
 * `MOS-UI-012c` replaced them for `viewer/`, the first-party surface, and say nothing
 * about an extension package -- the boundary was removed and not redrawn, so the citations
 * are marked rather than re-pointed. Nothing loads this file any more either: the host that
 * named it, `deploy/compose/ohif-config.js`, went with the OHIF withdrawal. Register entry
 * 109 in docs/spec/99-known-inconsistencies.md holds the whole account. */

/* =====================================================================================
 * The "Analyze with MedicalOS" toolbar button.
 *
 * MOS-SAFE-089a makes the button a MUST for release 0.1.0, and chapter 9's acceptance
 * check 24 measures it: "With a study open in the viewer, one activation issues exactly
 * one POST /api/v1/jobs carrying that study's study_instance_uid and the selected target,
 * and the returned job_id and Job.status render inline and update from the SSE stream."
 *
 * The button itself does ONE thing: run the `runMedicalOSAnalysis` command. It holds no
 * state, no client and no URL. That is not minimalism for its own sake -- it is what
 * "MUST NOT create a second job-creation path" means structurally. Every path into the
 * platform, from this button, from the panel and from anything a future mode adds,
 * funnels through the one command in getCommandsModule.js and the one
 * `MedicalOSClient.submit()` behind it.
 *
 * WHY `getToolbarModule` RETURNS AN EMPTY ARRAY
 * ---------------------------------------------
 * OHIF's toolbar module contributes BUTTON UI TYPES (`ohif.radioGroup`, `ohif.splitButton`
 * ...) and named `evaluate` functions -- not buttons. This package contributes neither: it
 * reuses `ohif.radioGroup`, which `@ohif/extension-default` registers in every build, and
 * its `evaluate` is a function rather than a registered name. Contributing a UI type of
 * our own would be a second toolbar-button renderer to keep working across OHIF minors
 * for no gain (MOS-REL-027).
 *
 * WHY THE BUTTONS ARE EXPORTED RATHER THAN RETURNED
 * -------------------------------------------------
 * OHIF 3.9 takes button definitions from `toolbarService.addButtons`, which a MODE calls
 * in `onModeEnter`. This package contributes no mode (MOS-UI-012, withdrawn at
 * 0.4.0), so `src/index.js` calls it from the EXTENSION's `onModeEnter` instead.
 * The definitions stay here, where `MOS-REL-027`'s "MUST live in an OHIF extension
 * package outside the OHIF tree" puts them, and a deployment that prefers to spread
 * them into its own mode still can.
 *
 * Spec: MOS-SAFE-089a, MOS-UI-012 (withdrawn at 0.4.0), MOS-UI-021, MOS-UI-023,
 *       MOS-REL-027.
 * ===================================================================================== */

import { activeStudyInstanceUID } from './getCommandsModule.js';

/* The `servicesManager` the button's `evaluate` reads.
 *
 * `ToolbarService.refreshToolbarState(refreshProps)` calls `evaluate({...refreshProps,
 * button})`, and `refreshProps` is whatever the caller supplied -- in OHIF 3.9 that is
 * `{viewportId, toolGroup}` from the cornerstone extension, with no `servicesManager` in
 * it. Reading it from a closure set at mode entry is therefore not a shortcut; it is the
 * only thing that works. Set by `bindToolbarServices` from `src/index.js::onModeEnter`. */
let boundServicesManager = null;

export function bindToolbarServices(servicesManager) {
  boundServicesManager = servicesManager || null;
}

export default function getToolbarModule() {
  return [];
}

/**
 * THE button. `uiType: 'ohif.radioGroup'` resolves to `@ohif/ui`'s `ToolbarButton`, whose
 * props are `{id, icon, label, commands, onInteraction, className, disabled,
 * disabledText}` -- read from the shipped bundle rather than assumed, because a prop this
 * package invents renders as "Missing Icon" with no error.
 *
 * `icon` is a key of `@ohif/ui`'s `ICONS` map; `getIcon` answers an unknown key with a
 * literal "Missing Icon" div, so the value below is one that is in the served bundle.
 */
export const analyzeButton = {
  id: 'MedicalOSAnalyze',
  uiType: 'ohif.radioGroup',
  props: {
    icon: 'launch-arrow',
    label: 'Analyze with MedicalOS',
    tooltip:
      'Submit the open study to MedicalOS (lung segmentation, emphysema LAA%). ' +
      'Research use only.',
    commands: [{ commandName: 'runMedicalOSAnalysis', commandOptions: {} }],
    /* MOS-UI-023: "Submission MUST be disabled, with an explanation, when the viewer does
     * not have exactly one study open ... The disabled state MUST say why, because a
     * control that is disabled without explanation is indistinguishable from a broken
     * one." `disabledText` is `ToolbarButton`'s secondary tooltip content and is where
     * that sentence lands. A control that guesses which of two open studies to analyse is
     * a clinical hazard, not a convenience. */
    evaluate: () => {
      const uid = activeStudyInstanceUID(boundServicesManager);
      return {
        disabled: !uid,
        className: uid ? '' : 'ohif-disabled',
        disabledText: 'Open exactly one study to analyse it',
      };
    },
  },
};

/** Brings the provenance panel forward. Not a second job-creation path: it runs a command
 *  that calls `panelService.activatePanel` and nothing else. */
export const panelButton = {
  id: 'MedicalOSPanel',
  uiType: 'ohif.radioGroup',
  props: {
    icon: 'info',
    label: 'MedicalOS provenance',
    tooltip: 'Show what ran, on which series, and what it wrote',
    commands: [{ commandName: 'showMedicalOSPanel', commandOptions: {} }],
    evaluate: () => ({ disabled: false, className: '' }),
  },
};
