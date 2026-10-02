#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""`medicalos-verify` -- check a ValidationReport bundle with no MedicalOS in sight.

Chapter 7 section 7.12.3. The usage and the output shape are the chapter's:

    medicalos-verify report ./validation-report-vr_01JB5C....tar.gz \\
        --trust-bundle ./trusted_publishers.pem

    [1/7] checksums                      ok (14 files)
    [2/7] dsse payload == report.json    ok
    ...
    VERDICT PASS  subject pulmoai.effusion@2.1.0  capability pleural_effusion  criteria v3
    NOTE  revocation status NOT checked (offline). ...

WHAT THIS COMMAND NEEDS, AND WHAT IT DOES NOT
----------------------------------------------
Needs: a bundle file, a trust bundle, and CPython. Nothing else -- no database, no
configuration, no MedicalOS service, no name resolution. `MOS-EVID-124`: "Verification MUST
NOT require network access, and the verifier MUST refuse to make any outbound connection
unless `--check-revocation` is passed explicitly."

That refusal is structural rather than promised. No module in this file's import closure
can open a socket; `tests/integration/test_validation_report.py` asserts that by running
this script in a subprocess whose `socket` module has been replaced with one that raises,
and by inspecting the closure afterwards. The revocation client is imported INSIDE the
`--check-revocation` branch, so the default path cannot reach one even by mistake.

EXIT CODES -- MOS-EVID-123, and nothing may be added to this table
------------------------------------------------------------------
    0  all seven checks passed
    1  usage error (bad arguments, unreadable file, malformed archive)
    2  check 1, checksums
    3  check 2, the DSSE payload is not report.json
    4  check 3, no presented signature verifies against the trust bundle
    5  check 4, report.json does not validate against its schema
    6  check 5, an aggregate does not follow from the per-case rows
    7  check 6, the criteria do not re-evaluate to the stated verdict
    8  check 7, a bundled file does not match the digest the report states for it

`--json` prints the machine-readable result to stdout instead; the exit code is the same,
because a gate reading this command reads the code (chapter 7 acceptance check 34).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# RUN FROM A CHECKOUT WITHOUT INSTALLATION, which needs BOTH roots and had only one.
# `_HERE.parent` is `medos/`, which reaches the `medos` package; `medos.sdk`
# lives at the repository root, and `medos/medos/evidence/__init__.py` imports it. MEASURED:
# `python -E -S medos/tools/medicalos_verify.py --help` died with
# `ModuleNotFoundError: No module named 'medos.sdk'` -- so the one path this
# shim exists to serve was the one path it did not serve. `tests/_support/roots.py` records
# the correct pair and is the authority; this list must match it.
_HERE = Path(__file__).resolve().parent
for _root in (_HERE.parent.parent, _HERE.parent):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from medos.evidence.dsse import DsseError, parse_trust_bundle  # noqa: E402
from medos.evidence.verify import EXIT_USAGE, render, verify_archive  # noqa: E402

# 128 MiB. A bundle is per-case CSVs and manifests; anything larger is a mistake or an
# attack, and reading it into memory to find out is the mistake.
MAX_BUNDLE_BYTES = 128 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medicalos-verify",
        description=(
            "Verify a MedicalOS ValidationReport bundle offline. Seven checks, "
            "MOS-EVID-123. Exit 0 means every one of them passed."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="verify a validation-report tar.gz")
    report.add_argument("bundle", type=Path, help="the exported validation-report tar.gz")
    report.add_argument(
        "--trust-bundle",
        type=Path,
        required=True,
        help=(
            "PEM file of trusted Ed25519 publisher keys. The key inside the bundle is "
            "NOT a trust root and is never used for this."
        ),
    )
    report.add_argument(
        "--json", action="store_true", help="machine-readable result on stdout"
    )
    report.add_argument(
        "--check-revocation",
        metavar="URL",
        default=None,
        help=(
            "MOS-EVID-124: the ONLY flag that permits an outbound connection. Without it "
            "this command makes none. An unreachable feed is reported as unknown and "
            "never as 'not revoked' (MOS-EVID-125)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        raw = args.bundle.read_bytes()
    except OSError as exc:
        return _usage(f"cannot read {args.bundle}: {exc}", as_json=args.json)
    if len(raw) > MAX_BUNDLE_BYTES:
        return _usage(
            f"{args.bundle} is {len(raw)} bytes, above the {MAX_BUNDLE_BYTES}-byte limit",
            as_json=args.json,
        )
    try:
        trusted = parse_trust_bundle(args.trust_bundle.read_text(encoding="utf-8"))
    except (OSError, DsseError, UnicodeDecodeError) as exc:
        return _usage(f"trust bundle {args.trust_bundle}: {exc}", as_json=args.json)

    result = verify_archive(raw, trusted)

    if args.check_revocation:
        result.revocation = _check_revocation(args.check_revocation, result)

    if args.json:
        json.dump(result.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(result))
        if result.revocation != "not_checked":
            sys.stdout.write(f"REVOCATION  {result.revocation}\n")
    return result.exit_code


def _check_revocation(url: str, result: object) -> str:
    """The one branch permitted to touch the network. MOS-EVID-124 / MOS-EVID-125.

    The import is inside the function so that the default path's import closure contains
    no HTTP client at all -- a client that is never called is still a client that a later
    edit can call, and the property this command sells is that it cannot.

    An unreachable feed returns `unknown`, never `not_revoked`. `MOS-EVID-125`: "a
    deployment gate running with connectivity MUST treat an unreachable revocation feed as
    INDETERMINATE, not as 'not revoked'". The gate reads this field; this command does not
    fold it into the exit code, because `MOS-EVID-123`'s exit-code table is exhaustive and
    revocation is not one of the seven checks.
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    doc = getattr(result, "report", None) or {}
    digest = doc.get("report_digest") or ""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            feed = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return f"unknown (feed unreachable: {type(exc).__name__}); treat as INDETERMINATE"
    revoked = feed.get("revoked", []) if isinstance(feed, dict) else []
    entries = [e for e in revoked if isinstance(e, dict)]
    for entry in entries:
        same_digest = entry.get("report_digest") == digest
        if same_digest or entry.get("report_id") == doc.get("report_id"):
            return f"REVOKED: {entry.get('reason', 'no reason given')}"
    return "not revoked at the time of this check"


def _usage(message: str, *, as_json: bool) -> int:
    if as_json:
        json.dump({"verified": False, "exit_code": EXIT_USAGE, "usage_error": message},
                  sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(f"USAGE ERROR  {message}\n")
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
