<!-- SPDX-License-Identifier: Apache-2.0 -->
# Security policy

MedicalOS is infrastructure for building medical software. A vulnerability here can reach
patient data held by everyone who deploys it, so this document states how to report one and
what you can expect back.

> MedicalOS integrates, governs and evidences medical AI services. It does not diagnose,
> does not replace a PACS, and is not itself a medical device.

## Reporting a vulnerability

**UNRESOLVED — the project must publish a reporting channel before this document is
complete.** `tests/unit/test_required_documents.py` fails while this line is present, and
that is deliberate: a security policy with a plausible-looking but unmonitored address is
worse than an obviously absent one, because a reporter believes they have discharged their
duty and nobody is reading.

Two options, and the project must pick one:

- **GitHub private vulnerability reporting** on the repository — no address to monitor, and
  the reporter gets an acknowledgement trail. Lowest operational cost.
- **A monitored security address** with a published PGP key.

Do not open a public issue for a vulnerability. Do not include real patient data in a
report, ever — a reproduction on synthetic data is worth more than one that cannot be
opened by the people who need to read it.

## What to expect

**UNRESOLVED — the response commitment is a promise the project cannot keep by accident.**
`MOS-SEC-143` sets a CVSS-banded remediation budget of **7 / 30 / 90 days** (critical /
high / moderate), and `MOS-SEC-144` requires daily rescanning of published digests. Those
are the specification's numbers. Whether the project can meet them depends on who is
maintaining it, which is a fact about people and not about software, so it is left for a
maintainer to affirm rather than asserted here.

What the specification does commit to, and what the code already does:

| | |
|---|---|
| Remediation budget | `MOS-SEC-143`: 7 days critical, 30 high, 90 moderate |
| Digest rescanning | `MOS-SEC-144`: daily, against published image digests |
| API deprecation | `MOS-API-091` / `MOS-API-095`: 180-day `Deprecation`/`Sunset` notice, and `/api/v1` supported for 12 months after `/api/v2` |

## Supported versions

**UNRESOLVED.** No platform release has an end-of-life policy or a support window.
`docs/spec/18-conformance.md` records this as **PARTIAL** against IEC 62304 §6/§9: security
defects have a budget and the HTTP API has a deprecation policy, but non-security defects
have neither a triage process nor a published response commitment.

Until a window is published, treat every release as unsupported and pin a digest.

## Scope

In scope: the `medos` package, the `medos/deploy/` images and compose stack, the web surfaces
under `medos/web/`, and the tooling under `medos/tools/`.

Out of scope, and the reason each is:

- **Third-party `ServiceVersion` implementations.** `MOS-SAFE-003` makes the publisher of a
  service its legal manufacturer. Report to them.
- **Your deployment's configuration.** A dev-mode declaration left in place on a production
  deployment is not a platform vulnerability; `medos/medos/config/devmode.py` refuses the ones it
  can and states plainly what it cannot stop.
- **The known-inconsistencies register.** `docs/spec/99-known-inconsistencies.md` records 153
  places where this repository does not meet its own specification. Those are known and
  written down; a report that one of them exists is not a disclosure. A report that one of
  them is exploitable in a way the entry does not describe very much is.

## What this project will not claim

`MOS-SEC-007`: MedicalOS must not be represented as providing HIPAA, GDPR or MDR
*compliance*. It provides controls a covered entity can use inside its own compliance
programme. A vulnerability report will not be answered with a compliance claim.
