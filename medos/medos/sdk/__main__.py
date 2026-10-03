# SPDX-License-Identifier: Apache-2.0
"""`python -m medos.sdk` — the SDK's command line.

Today one verb exists:

    python -m medos.sdk run --profile <file.yaml> [--once]

`run` loads the deployment profile (see `medos.sdk.profiles`), builds the pipeline and
the worker the mode names, and starts processing: `mode: local` walks the profile's
study list synchronously; `mode: external` consumes the bus until signalled, or
processes a single message with `--once` (the shape cron wraps). Refusals are printed
as JSON lines on stdout, one per study — a batch that half-fails leaves a readable
record instead of losing the second half to the first exception.

WHY THIS ENTRY POINT EXISTS. Before it, the two deployment modes lived only as example
scripts: an operator who wanted the SDK running against their PACS read Python. The
profile plus this verb is the difference between "an SDK you can build a deployment
from" and "a deployment".
"""

from __future__ import annotations

import argparse
import json
import sys


def _cmd_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m medos.sdk run")
    parser.add_argument(
        "--profile",
        required=True,
        help="deployment profile YAML (see medos.sdk.profiles)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="external mode: process at most one message and exit",
    )
    args = parser.parse_args(argv)

    from medos.sdk.profiles import ProfileError, build_worker, load_profile

    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(json.dumps({"refused": str(exc)}))
        return 2

    worker = build_worker(profile)

    if profile.mode == "local":
        failed = 0
        for study_uid in profile.local_studies:
            try:
                result = worker.submit(study_uid)
            except Exception as exc:  # a refused study must not lose the batch
                failed += 1
                as_dict = getattr(exc, "as_dict", None)
                if callable(as_dict):
                    # C5: the refusal is a dictionary — code is what an external
                    # system matches on, detail is what a human reads.
                    print(json.dumps({"study": study_uid, **as_dict()}))
                else:
                    print(json.dumps({"study": study_uid, "refused": str(exc)}))
                continue
            print(json.dumps({
                "study": study_uid,
                "segments": len(result.findings.segments),
                "measurements": len(result.findings.measurements),
                "stored": [str(p) for p in (result.stored or [])],
            }))
        return 1 if failed else 0

    # external mode
    if args.once:
        worker.run_once()
        return 0
    worker.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m medos.sdk run --profile <file.yaml> [--once]")
        return 2
    verb, rest = args[0], args[1:]
    if verb == "run":
        return _cmd_run(rest)
    print(json.dumps({"refused": f"unknown verb {verb!r}; known: run"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
