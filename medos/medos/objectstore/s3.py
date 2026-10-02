# SPDX-License-Identifier: Apache-2.0
"""A minimal, dependency-free S3 client (AWS SigV4) for the model artifact store.

WHY THIS FILE EXISTS
    Chapter 2 `MOS-SVC-058`: "Neither mode may bake model weights into a runner image
    (native) nor mutate them at runtime (both)."  The weeks 3-5 move off the in-process
    loader (`docs/spec/15-delivery.md` §15.2.4) therefore needs somewhere for weights to
    live that is not a container layer, and `medicalos-tritond` needs to be able to fetch
    them at startup.  That is an object store, and this is the smallest honest client for
    one.

WHY NOT boto3
    CONTRACT.md §11 and `medos/deploy/compose/requirements.txt` pin an exact runtime dependency
    set, and every addition is carried into the image and into every developer's
    environment.  boto3 + botocore is ~90 MB of transitive surface for four verbs
    (`GET`, `PUT`, `HEAD`, `ListObjectsV2`).  SigV4 is 60 lines of `hmac`, and writing it
    out means the signing is auditable rather than opaque.  The trade is deliberate: if a
    later block needs multipart upload, presigned URLs, or an IAM credential chain, that
    is the point at which boto3 earns its place -- not before.

SCOPE, STATED PLAINLY
    Path-style addressing only (`{endpoint}/{bucket}/{key}`), because that is what MinIO
    and every S3-compatible on-premise store speaks and because virtual-host style needs
    DNS this deployment does not have.  No multipart, no presigning, no STS, no retries
    beyond the one connection-level retry `requests` does not do.  Payloads are signed
    with their real SHA-256 (never `UNSIGNED-PAYLOAD`): the digest is the thing that makes
    `verify_digest` in `medos.inference.tritond` meaningful, so it is computed anyway.

CREDENTIALS
    Read from the environment, once, at construction.  `MEDOS_OBJECT_STORE_SECRET_KEY` is
    a secret and is never logged, never placed in a URL, and never included in a repr --
    see `__repr__`.  CONTRACT.md §11 forbids logging PHI; a credential is held to the same
    standard for the same reason.

Spec: MOS-SVC-058 (weights are never baked into an image), MOS-OPS-070 (the artifact and
its `medicalos.json` are covered by one digest), MOS-STORE-* (chapter 12 owns the object
store proper; this is the client, not the storage plane).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

__all__ = [
    "S3Config",
    "S3Client",
    "S3Error",
    "ObjectNotFound",
    "sha256_hex",
]

_UNRESERVED_SAFE = "-._~"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(RuntimeError):
    """The object store answered with a non-2xx status, or could not be reached.

    Deliberately NOT a `medos.core.errors.MedosError`: this module is infrastructure
    below the job model, and the component that calls it (`tritond`) decides whether a
    missing artifact is a deployment error or a transient one.  Wrapping it here would
    force that decision into the wrong layer.
    """

    def __init__(self, status: int, method: str, key: str, body: str) -> None:
        super().__init__(f"{method} {key} -> HTTP {status}: {body[:400]}")
        self.status = status
        self.method = method
        self.key = key


class ObjectNotFound(S3Error):
    """404/NoSuchKey. Separated because "the model is not published" is a different
    operator action from "the object store is broken"."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _quote(value: str) -> str:
    """RFC 3986 percent-encoding of one path segment, as SigV4 canonicalisation wants it."""
    return urllib.parse.quote(value, safe=_UNRESERVED_SAFE)


def _canonical_key(key: str) -> str:
    """`a/b/c.onnx` -> `/a/b/c.onnx`, each segment encoded, slashes preserved."""
    return "/" + "/".join(_quote(part) for part in key.split("/") if part != "")


@dataclass(frozen=True)
class S3Config:
    """Where the artifact store is and how to authenticate to it.

    `endpoint` carries the scheme and no trailing slash (`http://minio:9000`).  `region`
    is part of the SigV4 credential scope; MinIO accepts any value but it MUST match
    between signer and server configuration, so it is explicit rather than guessed.
    """

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "us-east-1"
    timeout_s: float = 60.0

    @staticmethod
    def from_env(prefix: str = "MEDOS_OBJECT_STORE") -> S3Config:
        """Construct from `MEDOS_OBJECT_STORE_{ENDPOINT,ACCESS_KEY,SECRET_KEY,BUCKET,REGION}`.

        Raises rather than defaulting the credentials: a client that silently signs with
        an empty secret produces a 403 whose cause is three layers away from its symptom.
        """
        endpoint = os.environ.get(f"{prefix}_ENDPOINT", "http://127.0.0.1:9000")
        access_key = os.environ.get(f"{prefix}_ACCESS_KEY", "")
        secret_key = os.environ.get(f"{prefix}_SECRET_KEY", "")
        bucket = os.environ.get(f"{prefix}_BUCKET", "medos-models")
        if not access_key or not secret_key:
            raise ValueError(
                f"{prefix}_ACCESS_KEY and {prefix}_SECRET_KEY must both be set; "
                "an unauthenticated signer fails as a 403 far from its cause"
            )
        return S3Config(
            endpoint=endpoint.rstrip("/"),
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            region=os.environ.get(f"{prefix}_REGION", "us-east-1"),
            timeout_s=float(os.environ.get(f"{prefix}_TIMEOUT_S", "60")),
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return (
            f"S3Config(endpoint={self.endpoint!r}, bucket={self.bucket!r}, "
            f"region={self.region!r}, access_key={self.access_key!r}, "
            "secret_key=<redacted>)"
        )


class S3Client:
    """Four verbs against one bucket, signed with SigV4.

    One `requests.Session` per client so connection reuse is real; the session is not
    shared across clients because CONTRACT.md §11 forbids a global singleton.
    """

    def __init__(self, config: S3Config | None = None) -> None:
        self.config = config or S3Config.from_env()
        self._session = requests.Session()

    # -- signing -----------------------------------------------------------------
    def _sign(
        self,
        method: str,
        canonical_uri: str,
        query: dict[str, str],
        payload_sha256: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Return the headers for one signed request (AWS Signature Version 4).

        The canonical request, the string to sign and the key derivation follow the AWS
        specification exactly; the only S3-specific parts are `x-amz-content-sha256`
        (mandatory for S3, unlike most services) and path-style addressing.
        """
        cfg = self.config
        now = _dt.datetime.now(_dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = urllib.parse.urlsplit(cfg.endpoint).netloc

        headers = {
            "host": host,
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": amz_date,
        }
        for key, value in (extra_headers or {}).items():
            headers[key.lower()] = value

        signed_names = sorted(headers)
        canonical_headers = "".join(f"{n}:{headers[n].strip()}\n" for n in signed_names)
        signed_headers = ";".join(signed_names)
        canonical_query = "&".join(
            f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items())
        )
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_sha256,
            ]
        )
        scope = f"{date_stamp}/{cfg.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )

        def _hmac(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        signing_key = _hmac(
            _hmac(_hmac(_hmac(f"AWS4{cfg.secret_key}".encode(), date_stamp), cfg.region), "s3"),
            "aws4_request",
        )
        signature = hmac.new(
            signing_key, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={cfg.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    def _request(
        self,
        method: str,
        key: str,
        *,
        data: bytes | None = None,
        query: dict[str, str] | None = None,
        bucket_level: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Response:
        cfg = self.config
        query = dict(query or {})
        body = data or b""
        payload_sha = sha256_hex(body) if data is not None else _EMPTY_SHA256
        if bucket_level:
            canonical_uri = f"/{_quote(cfg.bucket)}"
            url = f"{cfg.endpoint}/{urllib.parse.quote(cfg.bucket)}"
        else:
            canonical_uri = f"/{_quote(cfg.bucket)}{_canonical_key(key)}"
            url = f"{cfg.endpoint}{canonical_uri}"
        headers = self._sign(method, canonical_uri, query, payload_sha, extra_headers)
        response = self._session.request(
            method,
            url,
            params=query or None,
            data=data,
            headers=headers,
            timeout=cfg.timeout_s,
        )
        if response.status_code == 404:
            raise ObjectNotFound(404, method, key, response.text)
        if response.status_code >= 300:
            raise S3Error(response.status_code, method, key, response.text)
        return response

    # -- verbs -------------------------------------------------------------------
    def get_object(self, key: str) -> bytes:
        return self._request("GET", key).content

    def put_object(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """Store `data` at `key`; return its sha256 hex digest.

        The digest is returned rather than discarded because every caller in this
        codebase needs it: `medicalos.json` records it (MOS-OPS-070) and `tritond`
        re-verifies it after staging (MOS-OPS-070 again -- the signature covers the
        weights, and a digest that is only computed at publish time proves nothing about
        what was served).
        """
        self._request("PUT", key, data=data, extra_headers={"content-type": content_type})
        return sha256_hex(data)

    def head_object(self, key: str) -> dict[str, str]:
        response = self._request("HEAD", key)
        return {k.lower(): v for k, v in response.headers.items()}

    def exists(self, key: str) -> bool:
        try:
            self.head_object(key)
        except ObjectNotFound:
            return False
        return True

    def list_objects(self, prefix: str = "") -> list[str]:
        """ListObjectsV2, following continuation tokens. Returns keys, sorted."""
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            response = self._request("GET", "", query=query, bucket_level=True)
            root = ET.fromstring(response.content)
            namespace = root.tag[: root.tag.index("}") + 1] if "}" in root.tag else ""
            for contents in root.findall(f"{namespace}Contents"):
                node = contents.find(f"{namespace}Key")
                if node is not None and node.text:
                    keys.append(node.text)
            truncated = root.find(f"{namespace}IsTruncated")
            if truncated is None or (truncated.text or "").lower() != "true":
                break
            next_token = root.find(f"{namespace}NextContinuationToken")
            if next_token is None or not next_token.text:
                break
            token = next_token.text
        return sorted(keys)

    def ensure_bucket(self) -> bool:
        """Create the bucket if it is absent. Returns True when it was created.

        Idempotent: `BucketAlreadyOwnedByYou` (409) is success, not an error.  This is a
        publishing-side convenience -- `tritond` only ever reads -- and it exists so the
        compose stack has no separate `mc mb` init container.
        """
        try:
            self._request("HEAD", "", bucket_level=True)
            return False
        except S3Error as exc:
            if exc.status not in (404, 403):
                raise
        try:
            self._request("PUT", "", data=b"", bucket_level=True)
        except S3Error as exc:
            if exc.status == 409:
                return False
            raise
        return True

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> S3Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
