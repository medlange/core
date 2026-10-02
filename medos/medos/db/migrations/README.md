# migrations

**Nothing in this directory may be edited after it has been committed.** Not a comment,
not a path, not a line ending.

`medos/medos/db/migrate.py` records `sha256` of each file's bytes in `schema_migrations`
beside the version it applied, and compares it on every subsequent run:

> A migration is immutable once applied.

That sentence is enforced against the **bytes**. A digest cannot tell a typo fix from a
schema change, so a one-character edit to a file that has already run stops every database
that ran it from applying anything else — permanently, with an error about an edited file.

`schema.sql` one directory up is part of this rule. It is `0001_baseline` in the ledger and
`migrate.py` digests it the same way.

## The paths inside these files are frozen, including the wrong ones

Several headers here name `medos/db/…`. That package is at `medos/medos/db/` now: the
platform became a product directory with its own README, deploy files and Dockerfile, and
the package moved down a level with it.

**Those addresses are not corrected, and correcting them is the defect, not the fix.** A
migration file is the same kind of object as a release record in `docs/releases/` — what is
written in it is what was true when it ran. A reader who follows a stale path finds the
file one directory over; a reader whose database will not migrate has no such recourse.

This is not a hypothetical trade. The path rewrite that nested `medos/` ran through this
directory and changed 14 applied migrations plus the baseline `schema.sql` in the
repository itself, and 2 more on disk only — `.gitattributes` keeps a CRLF flip out of a
commit, not out of the tree an image is built from. All 17 were restored byte-for-byte
against `269d8cf~1`. Register entry 116 has the measurement.

## What a sweep must be told

A mechanical rewrite over this repository must skip, at minimum:

| | why |
|---|---|
| `medos/medos/db/migrations/` and `medos/medos/db/schema.sql` | the bytes are a recorded digest |
| `docs/releases/` | a release record states what was measured at a tag |
| `.evidence/` | already excluded |

The first two are the ones that were learned the hard way, on the same day.

## What is checked, and where

| check | asks | needs |
|---|---|---|
| [`tests/unit/test_migration_immutability.py`](../../../../tests/unit/test_migration_immutability.py) | does each file still hold the bytes of the commit that added it | git only |
| [`tests/gate/test_migration_drift.py`](../../../../tests/gate/test_migration_drift.py) | does the deployed ledger match the shipped set | a live Postgres |
| [`tests/unit/test_migration_planes.py`](../../../../tests/unit/test_migration_planes.py) | does each migration write only into the plane it declares | git only |
| [`tests/integration/test_migration_planes.py`](../../../../tests/integration/test_migration_planes.py) | does the APPLIED schema agree with that declaration | a live Postgres |

The second one is what caught the digest change, four commits after it landed, because the
first one did not exist. It does now.

## Writing a new one

Until it is committed it is an ordinary file — the immutability check does not ask about a
migration with no adding commit, which is exactly the window in which it should be edited
freely. After the commit, a correction is a **new numbered migration**, never an edit.
