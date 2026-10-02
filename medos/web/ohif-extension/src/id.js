/* The extension id. Separate module because OHIF's module loaders reference it from
 * several files and a circular import through `index.js` is the classic way an OHIF
 * extension fails to register with a null-reference error nobody can place. */
export const id = '@medicalos/extension-medicalos';
export default id;
