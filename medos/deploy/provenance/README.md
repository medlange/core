# Corpus provenance manifests

A manifest in this directory declares, for one named corpus, the facts `MOS-EVID-021` and
`MOS-TRAIN-073` will ask for when a cohort drawn from it is sealed: what
de-identification status it carries, which policy and UID mapping table stand behind that
status, and on whose authority the data is being used.

`tests/integration/test_deid_provenance_declaration.py` reads this directory. A study in
the archive is covered either by its UID root -- when the root itself names the authority
that issued it, as TCIA's does -- or by appearing in a manifest here. A study covered by
neither turns the suite red, which is the point: ingesting a corpus and declaring nothing
should not be quiet.

## Why this directory is empty in the repository

A manifest names a deployment's own data holdings and who supplied them. Committing one
would publish that. Deployments keep their manifests outside the checkout and point

    MEDOS_PROVENANCE_DIR=/path/to/their/manifests

at them. The mechanism is open source; the holdings are not.

## Writing one

`medos/tools/ingest/nrrd_to_dicom.py --manifest FILE` emits a manifest for the corpus it
converts. Its `known_limitations` are not decoration -- a corpus reconstructed from files
has lost something, at minimum the UIDs that would let anyone find it again in the
originating PACS, and the manifest is where that loss is recorded rather than forgotten.
