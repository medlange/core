# SPDX-License-Identifier: Apache-2.0
"""`python -m medos.cli <command>`."""
from __future__ import annotations

import sys

COMMANDS = {"doctor": "medos.cli.doctor"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("usage: medos <command> [options]\n\ncommands:")
        print("  doctor    what this installation can and cannot do right now, and why")
        return 0 if args else 2
    name, rest = args[0], args[1:]
    if name not in COMMANDS:
        print(f"medos: unknown command {name!r}. Known: {', '.join(sorted(COMMANDS))}",
              file=sys.stderr)
        return 2
    import importlib

    return int(importlib.import_module(COMMANDS[name]).main(rest) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
