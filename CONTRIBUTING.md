<!-- SPDX-License-Identifier: Apache-2.0 -->
# Contributing to MedicalOS

> MedicalOS integrates, governs and evidences medical AI services. It does not diagnose,
> does not replace a PACS, and is not itself a medical device.

## Sign-off

Every commit must carry a `Signed-off-by` line:

```bash
git commit -s
```

That line is the [Developer Certificate of Origin 1.1](https://developercertificate.org/):
you are asserting that you wrote the change or have the right to submit it under
Apache-2.0. `MOS-REL-099` fixes the licence; `MOS-REL-100` requires every first-party
source file to carry `SPDX-License-Identifier: Apache-2.0` as a header comment.

**UNRESOLVED — DCO or CLA.** This project uses the DCO by default. Open question OQ-19 in
`docs/spec/16-open-questions.md` records the alternative (a Contributor Licence Agreement)
and it is a decision for the maintainers, not for an engineer. Until it is recorded, the
DCO above is what applies.

## The one rule that is not about style

**A check that cannot fail is not a check.**

This repository has found the same defect at least eight times: a test that passes while
describing a state that is not real. Twenty-nine tests unrunnable because of network
topology. A credential scan permanently red on correct code. A console refusal catalogue
keyed on identifiers the engine never emits. A guard on `2.25.` that belonged to no
authority, so it covered nothing and admitted everything.

So: **when you add a guard, break the thing it guards and watch it go red.** Several suites
here do this to themselves and record the result —
`tests/unit/test_permission_contract.py` holds §19.3.7 shut with a mutation because the
committed files cannot exhibit the violation, and
`tests/unit/test_dev_mode_cannot_reach_production.py` exists because one of its own
mutations escaped on the first attempt and the check had to be rewritten.

If a check cannot be made to fail, say so in its docstring and explain what the behavioural
half is. An honest weak check is useful. A weak check presented as a strong one is the
thing we keep finding.

## Running the suites

```bash
pytest                           # everything testpaths names
pytest tests/unit tests/gate     # no containers needed
pytest viewer/tests              # the viewer's own suite
pytest trainer/tests             # needs the torch / nnunetv2 pins
pytest tests/integration         # needs the compose stack
python -m medos.cli doctor       # what your deployment can and cannot do
```

Zero skips is the standard. A skip is a test that did not run, and the suite prints a skip
taxonomy at the end of every run for exactly that reason. `skip_infra` is for a genuinely
absent container and nothing else.

## Writing it down when the code cannot be fixed

`docs/spec/99-known-inconsistencies.md` is where a defect goes when fixing it is a larger
change than the one in hand — with what it costs, why it survived review, and what would
resolve it. It has 153 entries and that is a feature. A medical platform whose known-defect
register is empty is one nobody has read.

Adding an entry is not a way to avoid fixing something. It is a way to avoid *pretending*
something is fixed.

## Third-party code

`MOS-REL-101`: vendored code lives under `third_party/<name>/` with its upstream `LICENSE`,
its version, and the reason it is vendored rather than depended on.
