# SPDX-License-Identifier: Apache-2.0
"""The self-contained export bundle. Chapter 7 section 7.12.3.

    MOS-EVID-011   a bundle that crosses a tenancy boundary MUST re-key `patient_key`
                   under a per-report salt recorded as `patient_key_scheme.report_salt_id`,
                   "so that per-case rows remain internally joinable but not linkable back
                   to the originating tenant".
    MOS-EVID-116   no PHI. Per-case rows are keyed by the re-keyed `patient_key` and a
                   per-report series pseudonym (`case_0001`...), never by source UIDs.
    MOS-EVID-122   the bundle is a self-contained `tar.gz` requiring no network access and
                   no MedicalOS instance to verify, with the member list below.
    MOS-STORE-314  every tenant-scoped object key begins `t/{tenant_id}/`; section 12.16
                   places the bundle at `t/{tenant}/validation-reports/{report_id}/`.

THE ONE PLACE THIS BLOCK HAD TO CHOOSE, AND WHY
-----------------------------------------------
`MOS-EVID-116` requires the exported manifests to carry PSEUDONYMS. `MOS-EVID-123` check 7
requires the digests in `report.json` for the cohort, the split and the annotation set to
match the digests of the BUNDLED FILES. Those cannot both hold: the sealed
`manifest_digest` of a `dataset_versions` row is the digest of the tenant-side manifest,
whose UIDs are exactly what `MOS-EVID-116` strips. A bundle that satisfied check 7 as
written would have to ship the un-pseudonymised manifest.

So the report carries both, and says which is which:

    cohort.dataset_version_digest     the SEALED content address, tenant-side. Cited, and
                                      NOT checkable offline -- the verifier prints that.
    artifacts.dataset_manifest_digest the digest of the re-keyed manifest in THIS bundle.

Check 7 verifies the second against the bundled bytes and states that the first is a
citation. The alternative -- silently digesting whichever file happens to be present --
would make check 7 pass on a bundle whose manifest had been swapped for another cohort's.
REPORTED upward as a specification defect rather than resolved quietly.

READING IS DONE IN MEMORY, ON PURPOSE
-------------------------------------
A verifier's whole job is to handle a file it does not trust. `read_bundle` never calls
`TarFile.extractall`: it validates every member name, refuses anything that is not a
regular file, caps the total expanded size, and returns bytes. A path-traversal or
decompression-bomb defence that depends on the operator running the tool in a sandbox is
not a defence.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import hmac
import io
import json
import re
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from medos.evidence.digest import sha256_of
from medos.evidence.dsse import public_key_hex
from medos.evidence.report import DISCLAIMER, SCHEMA_VERSION
from medos.sdk.canonical import canonical_bytes

__all__ = [
    "BUNDLE_MEMBERS",
    "REQUIRED_MEMBERS",
    "MAX_EXPANDED_BYTES",
    "BundleError",
    "Bundle",
    "bundle_dir_name",
    "bundle_object_key",
    "rekey_patient",
    "case_pseudonym",
    "build_bundle",
    "read_bundle",
    "checksums_text",
    "parse_checksums",
    "readme_text",
    "CASE_METRIC_COLUMNS",
    "CASE_SCORE_COLUMNS",
    "write_csv",
    "read_csv",
]

# MOS-EVID-122's layout. `figures/` is a prefix, not a fixed name: which curves exist
# depends on the capability, and a bundle for a segmentation-only subject has no ROC.
REQUIRED_MEMBERS: Final[tuple[str, ...]] = (
    "report.json",
    "report.dsse.json",
    "criteria.yaml",
    "envelope.yaml",
    "plausibility.yaml",
    "case_metrics.csv",
    "case_scores.csv",
    "dataset_manifest.jsonl",
    "split_manifest.jsonl",
    "annotation_manifest.jsonl",
    "leakage_report.json",
    "keys/publisher_ed25519.pub",
    "CHECKSUMS.sha256",
    "README.txt",
)
BUNDLE_MEMBERS: Final[tuple[str, ...]] = REQUIRED_MEMBERS

# A gzip member that expands to more than this is refused rather than buffered. 512 MiB is
# far above any real bundle (the largest member is a per-case CSV) and far below anything
# that threatens a verification workstation.
MAX_EXPANDED_BYTES: Final[int] = 512 * 1024 * 1024

_MEMBER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

CASE_METRIC_COLUMNS: Final[tuple[str, ...]] = (
    "case_key",
    "patient_key",
    "series_instance_uid",
    "metric",
    "value",
    "undefined_reason",
    "eligible",
    "gt_voxels",
    "pred_voxels",
    "intersection_voxels",
    "gt_volume_ml",
    "pred_volume_ml",
    "strata",
)
CASE_SCORE_COLUMNS: Final[tuple[str, ...]] = (
    "case_key",
    "patient_key",
    "case_score",
    "case_label",
    "candidates",
)


class BundleError(ValueError):
    """A bundle that is not a bundle: bad member name, bad archive, oversized."""


# =====================================================================================
# Names and keys.
# =====================================================================================
def bundle_dir_name(report_id: str) -> str:
    """`validation-report-vr_<ULID>` -- the single top-level directory of MOS-EVID-122."""
    return f"validation-report-{report_id}"


def bundle_object_key(tenant_id: str, report_id: str) -> str:
    """Section 12.16: `t/{tenant_id}/validation-reports/{report_id}/bundle.tar.gz`."""
    return f"t/{tenant_id}/validation-reports/{report_id}/bundle.tar.gz"


def report_object_key(tenant_id: str, report_id: str) -> str:
    return f"t/{tenant_id}/validation-reports/{report_id}/report.json"


# =====================================================================================
# Re-keying.  MOS-EVID-011 and MOS-EVID-116.
# =====================================================================================
def rekey_patient(report_salt: bytes, tenant_patient_key: str) -> str:
    """`pk_<b32>` under the REPORT's salt, not the tenant's. MOS-EVID-011.

    The construction is `MOS-EVID-010`'s, one level out: HMAC-SHA-256 of the tenant-side
    `patient_key` under a salt generated for this report and never exported. Rows stay
    joinable inside the bundle -- four studies of one patient still share a key, which is
    what the cluster bootstrap needs -- and stop being joinable against anything else,
    including a second bundle from the same tenant.

    The salt is NOT written into the bundle. Only `report_salt_id` is, so that the issuing
    tenant can prove which salt was used without the holder of the bundle being able to
    reverse it.
    """
    mac = hmac.new(report_salt, tenant_patient_key.encode("utf-8"), hashlib.sha256).digest()
    return "pk_" + base64.b32encode(mac[:10]).decode("ascii").rstrip("=").lower()


def report_salt_id(report_salt: bytes) -> str:
    """A public name for a private salt: `rsalt_<16 hex>` over its digest."""
    return "rsalt_" + hashlib.sha256(b"medicalos/report-salt/v1" + report_salt).hexdigest()[:16]


def case_pseudonym(index: int) -> str:
    """`case_0001`... MOS-EVID-116's per-report series pseudonym."""
    return f"case_{index:04d}"


# =====================================================================================
# CSV. One reader, one writer, one column order.
# =====================================================================================
def write_csv(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    """LF-terminated, no BOM, minimal quoting: deterministic bytes, deterministic digest."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8")


def read_csv(data: bytes, columns: Sequence[str]) -> list[dict[str, str]]:
    """Parse and refuse a header that is not the declared one.

    A tolerant reader is the wrong tool here: a column that silently went missing turns an
    aggregate into a different aggregate, and `MOS-EVID-123` check 5 would then compare two
    numbers that were never meant to be the same.
    """
    reader = csv.reader(io.StringIO(data.decode("utf-8"), newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise BundleError("CSV member is empty") from None
    if tuple(header) != tuple(columns):
        raise BundleError(f"CSV header is {header}, expected {list(columns)}")
    out: list[dict[str, str]] = []
    for record in reader:
        if not record:
            continue
        if len(record) != len(columns):
            raise BundleError(f"CSV record has {len(record)} fields, expected {len(columns)}")
        out.append(dict(zip(columns, record)))
    return out


# =====================================================================================
# CHECKSUMS.sha256
# =====================================================================================
def checksums_text(members: Mapping[str, bytes]) -> bytes:
    """`sha256sum` format, sorted by path. Every member except the checksum file itself."""
    lines = [
        f"{hashlib.sha256(data).hexdigest()}  {path}"
        for path, data in sorted(members.items())
        if path != "CHECKSUMS.sha256"
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_checksums(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise BundleError(f"malformed CHECKSUMS.sha256 line: {line[:80]!r}")
        out[parts[1].strip()] = parts[0].strip().lower()
    if not out:
        raise BundleError("CHECKSUMS.sha256 lists no files")
    return out


# =====================================================================================
# README.txt -- MOS-EVID-122: "MOS-EVID-002 disclaimer, verification command".
# =====================================================================================
def readme_text(report_id: str, *, key_id: str, trust_bundle_hint: str) -> bytes:
    return (
        f"""MedicalOS ValidationReport bundle
{"=" * 64}

{DISCLAIMER}

WHAT THIS BUNDLE IS
  A signed technical-evidence report. Every INPUT the checks need is inside this archive,
  and verification requires no network access and no MedicalOS instance (MOS-EVID-122).
  The verifier itself is not a member of the archive: obtain `medicalos-verify` from the
  MedicalOS distribution before you start.

HOW TO VERIFY
  medicalos-verify report ./{bundle_dir_name(report_id)}.tar.gz \\
      --trust-bundle {trust_bundle_hint}

  Seven checks run in order and stop at the first failure (MOS-EVID-123):
    1 checksums              exit 2 on failure
    2 DSSE payload == report.json                exit 3
    3 signature against the trust bundle         exit 4
    4 schema {SCHEMA_VERSION}   exit 5
    5 aggregates recomputed from case_metrics.csv    exit 6
    6 criteria re-evaluated, verdict reproduced      exit 7
    7 input digests match the bundled files          exit 8
  Exit 0 means all seven passed. Exit 1 is a usage error.

THE SIGNING KEY
  keys/publisher_ed25519.pub holds the raw 32-byte Ed25519 public key, hex-encoded, of
  {key_id}. It is provided for convenience. IT IS NOT A TRUST ROOT: the verifier checks
  signatures against the trust bundle YOU supply, never against a key shipped inside the
  artifact it is verifying.

REVOCATION
  Offline verification cannot detect revocation (MOS-EVID-125). A report that verifies may
  since have been withdrawn. Check the issuer's revocation feed when connectivity exists.

THE .yaml MEMBERS
  criteria.yaml, envelope.yaml and plausibility.yaml are written as JCS-canonical JSON,
  which is valid YAML 1.2 and parses with any YAML reader. JSON because these files are
  digested (MOS-EVID-008 requires JCS before any digest) and a format with several
  serialisations of one document cannot be content-addressed.
"""
    ).encode()


# =====================================================================================
# Building.
# =====================================================================================
@dataclass(frozen=True)
class Bundle:
    """A bundle in memory: the member map, the archive bytes and its digest."""

    report_id: str
    members: dict[str, bytes]
    archive: bytes

    @property
    def digest(self) -> str:
        return sha256_of(self.archive)

    def member(self, path: str) -> bytes:
        try:
            return self.members[path]
        except KeyError:
            raise BundleError(f"bundle has no member {path!r}") from None

    def json_member(self, path: str) -> Any:
        try:
            return json.loads(self.member(path).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BundleError(f"{path} is not valid JSON: {exc}") from exc


def build_bundle(
    report_id: str,
    members: Mapping[str, bytes],
    *,
    mtime: int = 0,
) -> Bundle:
    """Pack `members` under `validation-report-<id>/` into a deterministic `tar.gz`.

    Deterministic because the bundle is content-addressed: `validation_reports.bundle_digest`
    is a column, and a tar whose digest depends on the hour it was packed cannot be one.
    Fixed mtime, fixed uid/gid, empty owner names, sorted member order, and gzip written
    with `mtime=0` -- gzip stamps the time into its own header, which is the part that
    catches people out.
    """
    if "CHECKSUMS.sha256" not in members:
        raise BundleError("build_bundle expects CHECKSUMS.sha256 among the members")
    root = bundle_dir_name(report_id)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path in sorted(members):
            data = members[path]
            info = tarfile.TarInfo(f"{root}/{path}")
            info.size = len(data)
            info.mtime = mtime
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    packed = gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)
    return Bundle(report_id=report_id, members=dict(members), archive=packed)


def read_bundle(archive: bytes) -> Bundle:
    """Unpack in memory, refusing anything a hostile archive could do.

    Refused: an absolute or traversing member name, a symlink, a hard link, a device node,
    a directory that is not the single declared root, a member outside that root, two
    members with the same name, and a total expansion above `MAX_EXPANDED_BYTES`.
    """
    try:
        raw = gzip.decompress(archive)
    except (OSError, EOFError) as exc:
        raise BundleError(f"not a gzip archive: {exc}") from exc
    if len(raw) > MAX_EXPANDED_BYTES:
        raise BundleError("archive expands beyond the accepted size")

    members: dict[str, bytes] = {}
    root: str | None = None
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for info in tar.getmembers():
                name = info.name
                if info.isdir():
                    continue
                if not info.isfile():
                    raise BundleError(
                        f"member {name!r} is not a regular file; a bundle carries no "
                        "symlinks, hard links or device nodes"
                    )
                if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                    raise BundleError(f"member {name!r} escapes the bundle root")
                head, _, rest = name.partition("/")
                if not rest:
                    raise BundleError(f"member {name!r} is not inside the bundle root")
                if root is None:
                    if not head.startswith("validation-report-"):
                        raise BundleError(
                            f"bundle root is {head!r}, expected validation-report-<id>"
                        )
                    root = head
                elif head != root:
                    raise BundleError(f"bundle has two roots: {root!r} and {head!r}")
                if not _MEMBER_RE.match(rest):
                    raise BundleError(f"member name {rest!r} is not acceptable")
                if rest in members:
                    raise BundleError(f"duplicate member {rest!r}")
                total += info.size
                if total > MAX_EXPANDED_BYTES:
                    raise BundleError("archive expands beyond the accepted size")
                handle = tar.extractfile(info)
                members[rest] = handle.read() if handle is not None else b""
    except tarfile.TarError as exc:
        raise BundleError(f"not a readable tar archive: {exc}") from exc
    if root is None:
        raise BundleError("archive contains no files")
    return Bundle(report_id=root[len("validation-report-") :], members=members, archive=archive)


# =====================================================================================
# Assembling a bundle from the evidence plane.
# =====================================================================================
def assemble_members(
    *,
    report: Mapping[str, Any],
    dsse_envelope: Mapping[str, Any],
    criteria_doc: Mapping[str, Any],
    applicability_envelope_doc: Mapping[str, Any],
    plausibility_doc: Mapping[str, Any],
    case_metric_rows: Sequence[Sequence[Any]],
    case_score_rows: Sequence[Sequence[Any]],
    dataset_manifest: bytes,
    split_manifest: bytes,
    annotation_manifest: bytes,
    leakage_report: Mapping[str, Any],
    public_key: bytes,
    figures: Mapping[str, bytes] | None = None,
    trust_bundle_hint: str = "./trusted_publishers.pem",
) -> dict[str, bytes]:
    """Every member of MOS-EVID-122's layout, with CHECKSUMS.sha256 computed last.

    `report` and `dsse_envelope` arrive already built and already signed -- this function
    does not sign, and deliberately cannot: `MOS-EVID-119` makes signing a separately
    authorised act, and a packer that could sign would be a second path to a signature.

    Two different things are called an envelope in this chapter. `dsse_envelope` is
    section 7.12.2's signature envelope; `applicability_envelope_doc` is section 7.10's
    `ApplicabilityEnvelope`. They are spelled apart here because they were not, once, and
    the resulting bundle shipped the wrong file under `envelope.yaml`.
    """
    from medos.evidence.dsse import key_id  # noqa: PLC0415

    report_id = str(report["report_id"])
    members: dict[str, bytes] = {
        "report.json": canonical_bytes(report),
        "report.dsse.json": canonical_bytes(dsse_envelope),
        # JCS-canonical JSON under a .yaml name: valid YAML 1.2, and digestible. See
        # README.txt, which says the same thing to the bundle's reader.
        "criteria.yaml": canonical_bytes(criteria_doc),
        "envelope.yaml": canonical_bytes(applicability_envelope_doc),
        "plausibility.yaml": canonical_bytes(plausibility_doc),
        "case_metrics.csv": write_csv(CASE_METRIC_COLUMNS, case_metric_rows),
        "case_scores.csv": write_csv(CASE_SCORE_COLUMNS, case_score_rows),
        "dataset_manifest.jsonl": dataset_manifest,
        "split_manifest.jsonl": split_manifest,
        "annotation_manifest.jsonl": annotation_manifest,
        "leakage_report.json": canonical_bytes(leakage_report),
        "keys/publisher_ed25519.pub": public_key_hex(public_key).encode("ascii"),
        "README.txt": readme_text(
            report_id, key_id=key_id(public_key), trust_bundle_hint=trust_bundle_hint
        ),
    }
    for name, svg in (figures or {}).items():
        members[f"figures/{name}"] = svg
    members["CHECKSUMS.sha256"] = checksums_text(members)
    return members
