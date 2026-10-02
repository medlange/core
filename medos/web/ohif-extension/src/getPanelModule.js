/* WITHDRAWN CITATIONS. `MOS-UI-012` and `MOS-UI-012a`, cited below as the bound on what
 * this package may contribute, were WITHDRAWN at specification 0.4.0. `MOS-UI-012b` and
 * `MOS-UI-012c` replaced them for `viewer/`, the first-party surface, and say nothing
 * about an extension package -- the boundary was removed and not redrawn, so the citations
 * are marked rather than re-pointed. Nothing loads this file any more either: the host that
 * named it, `deploy/compose/ohif-config.js`, went with the OHIF withdrawal. Register entry
 * 109 in docs/spec/99-known-inconsistencies.md holds the whole account. */

/* =====================================================================================
 * Panel registration. One right-hand panel: MedicalOS provenance.
 *
 * CONTRACT.md section 0 lists "an OHIF toolbar button and a provenance panel" as the two
 * viewer deliverables of the slice, and CONTRACT.md section 10 fixes what the panel shows.
 * `MOS-UI-012` (withdrawn at 0.4.0, see the banner above) fixed what this package
 * may contribute: "a toolbar entry, the commands behind it, and one panel" -- this
 * is the one panel.
 *
 * `component` IS A STRING, AND THAT IS THE POINT
 * ----------------------------------------------
 * OHIF renders the active tab as `React.createElement(tab.content, {key})`, where
 * `tab.content` is this entry's `component` (read from the shipped `ohif/app:v3.9.2`
 * bundle, `ui-next/src/components/SidePanel`). React accepts a STRING type and renders a
 * host element, so a custom-element tag name is a legal component. That is what lets this
 * package load into the pinned viewer at runtime with no bundler, no JSX and no second
 * copy of React -- see `src/panels/provenance-panel.js` for the full reasoning.
 *
 * `iconName` is one of OHIF's own icon ids rather than a bundled SVG: an extension that
 * ships assets needs a loader configuration in the host app, which is one more thing that
 * breaks when the pinned viewer version moves (MOS-REL-027). `'Info'` is a key of
 * `@ohif/ui-next`'s `Icons`, verified against the served bundle; an unknown key would
 * render `Icons.MissingIcon`, which is a silent downgrade rather than an error.
 * ===================================================================================== */

import { PROVENANCE_PANEL_TAG } from './panels/provenance-panel.js';
import { id as extensionId } from './id.js';

/** The panel's entry name. OHIF builds the module id as
 *  `${extensionId}.${moduleType}.${name}` (ExtensionManager.processExtensionModule). */
export const PANEL_ENTRY_NAME = 'provenance';

/** The id a mode's `rightPanels`, or `panelService.addPanel`, addresses. */
export const PANEL_ID = `${extensionId}.panelModule.${PANEL_ENTRY_NAME}`;

export default function getPanelModule() {
  return [
    {
      name: PANEL_ENTRY_NAME,
      iconName: 'Info',
      iconLabel: 'MedicalOS',
      label: 'MedicalOS',
      component: PROVENANCE_PANEL_TAG,
    },
  ];
}
