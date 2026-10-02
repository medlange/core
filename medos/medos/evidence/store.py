# SPDX-License-Identifier: Apache-2.0
"""Where a manifest object lives. Chapter 12 section 12.12's `(bucket, object_key)` pair.

MOS-EVID-015  "`dataset_versions` rows MUST be created in a single transaction that also
              writes the manifest object; a row whose manifest location does not resolve
              to an object whose SHA-256 equals `manifest_digest` MUST be treated as
              `DEFECTIVE` by every reader."

THE HONEST PART. A Postgres transaction and an S3 PUT cannot be one transaction. There is
no two-phase commit between them and pretending otherwise would be worse than saying so.
What this package does instead, and why it satisfies the requirement's actual guarantee:

  1. the object is written FIRST, at a key derived from its own digest;
  2. the row is written in one database transaction;
  3. if (2) fails, (1) is compensated by a delete, and if the compensating delete also
     fails the object is an orphan -- unreferenced bytes at a content-addressed key,
     which costs storage and breaks nothing;
  4. `verify_manifest()` is the reader-side half the requirement actually specifies, and
     `MOS-EVID-014`'s `DEFECTIVE` marking is the recorded outcome when it fails.

The failure this ordering CANNOT produce is the dangerous one: a row pointing at an object
that was never written. The failure it can produce is an orphaned object, which no reader
can mistake for evidence because nothing references it.

The key is derived from the digest, so step (1) is idempotent: re-sealing the same content
(`MOS-TRAIN-209`) rewrites identical bytes at the identical key.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from medos.evidence.digest import sha256_of

__all__ = [
    "ManifestStore",
    "InMemoryManifestStore",
    "S3ManifestStore",
    "manifest_key",
    "ObjectMissing",
]


class ObjectMissing(KeyError):
    """No object at `(bucket, key)`. A sealed version whose manifest is missing is
    `DEFECTIVE` by `MOS-EVID-015`, not an exception to be swallowed."""


@runtime_checkable
class ManifestStore(Protocol):
    """Three verbs. Deliberately not the S3 API.

    `medos.objectstore.S3Client` is the shipped driver and speaks to one bucket; this
    port carries the bucket per call because chapter 12 stores `(bucket, object_key)` on
    the row and a port that hid the bucket could not round-trip a row written by another
    deployment. Chapter 15 section 15.3.1's object-storage row is the precedent: the S3
    *API* is adopted, no implementation is.
    """

    def put(self, bucket: str, key: str, data: bytes) -> None: ...

    def get(self, bucket: str, key: str) -> bytes: ...

    def delete(self, bucket: str, key: str) -> None: ...


def manifest_key(tenant_id: str, kind: str, digest: str) -> str:
    """`t/{tenant}/evidence/{kind}/{digest}.jsonl` -- chapter 12 section 12.16's layout.

    Tenant-prefixed (chapter 15 section 15.3.1: "Tenant-prefixed key layout" is one of the
    three things genuinely added over the S3 API) and content-addressed, so that writing
    the same manifest twice is a no-op rather than a second object with a second name.

    `digest` arrives as `sha256:<hex>`; the colon is replaced because it is legal in an S3
    key but awkward in every CLI that handles one.
    """
    if not digest.startswith("sha256:"):
        raise ValueError(f"digest MUST be sha256:<hex> (MOS-EVID-007), got {digest!r}")
    return f"t/{tenant_id}/evidence/{kind}/{digest.replace(':', '_')}.jsonl"


class InMemoryManifestStore:
    """A dict. For tests and for a deployment with no object store configured yet.

    It is a real driver of the port and not a mock: the seal path exercises put/get/delete
    against it unchanged, so a test that passes here is a test of the seal and not of a
    stubbed-out seal.
    """

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self._objects[(bucket, key)] = bytes(data)

    def get(self, bucket: str, key: str) -> bytes:
        try:
            return self._objects[(bucket, key)]
        except KeyError as exc:
            raise ObjectMissing(f"{bucket}/{key}") from exc

    def delete(self, bucket: str, key: str) -> None:
        self._objects.pop((bucket, key), None)

    def __len__(self) -> int:
        return len(self._objects)

    def digest_at(self, bucket: str, key: str) -> str:
        return sha256_of(self.get(bucket, key))


class S3ManifestStore:
    """The shipped driver. Wraps `medos.objectstore.S3Client`, which is single-bucket.

    The bucket mismatch is raised rather than silently retargeted: a row that says bucket
    A and an object written to bucket B is exactly the `MOS-EVID-015` failure this module
    exists to prevent, and it would be invisible until someone tried to verify a report
    offline.
    """

    def __init__(self, client) -> None:  # `S3Client`, imported lazily by the caller
        self._client = client

    def _check(self, bucket: str) -> None:
        configured = getattr(self._client, "config", None)
        configured_bucket = getattr(configured, "bucket", None)
        if configured_bucket is not None and bucket != configured_bucket:
            raise ValueError(
                f"manifest bucket {bucket!r} is not this client's bucket "
                f"{configured_bucket!r}; a row whose manifest location does not resolve "
                "is DEFECTIVE by MOS-EVID-015"
            )

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self._check(bucket)
        self._client.put_object(key, data, content_type="application/x-ndjson")

    def get(self, bucket: str, key: str) -> bytes:
        self._check(bucket)
        return self._client.get_object(key)

    def delete(self, bucket: str, key: str) -> None:
        self._check(bucket)
        deleter = getattr(self._client, "delete_object", None)
        if deleter is None:
            # The shipped client has four verbs and delete is not one of them. An orphan
            # at a content-addressed key is the benign half of the ordering described in
            # this module's docstring, so this is a no-op and not a failure.
            return
        deleter(key)
