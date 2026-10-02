# SPDX-License-Identifier: Apache-2.0
"""The two apps' route counts are stated in five places, and all five were wrong.

`medos/medos/api/training_plane.py` says Core serves "24 route paths" and Train "51", and
`medos/deploy/compose/docker-compose.yml` repeats both numbers three more times. Measured
by building each app and counting the `/api/v1` paths it mounts: **25 and 52**.

WHY A STALE COUNT IS NOT A TYPO HERE. The numbers carry an argument. Train is described as
"a strict superset of Core's", and the whole reason there are two images is that Core's
import closure must not reach `medos.training` (`MOS-TRAIN-225`) — the counts are how a
reader checks the shape of that claim without reading two route tables. A count that
drifted by one in both apps says the tables moved and nobody looked; it is also exactly
the drift that hides a route added to Core that should have gone to Train.

WHAT THIS DOES. It builds both apps and counts. Nothing is restated: the numbers in the
prose are read back out of the files and compared against the live ones, so the comment
and the code cannot disagree without a check going red.
"""

from __future__ import annotations

import re
import subprocess
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLANE = ROOT / "medos" / "medos" / "api" / "training_plane.py"
COMPOSE = ROOT / "medos" / "deploy" / "compose" / "docker-compose.yml"


def _counts() -> tuple[int, int]:
    """(core, train), measured by mounting each app."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from medos.api.app import create_app
        from medos.api.training_plane import create_training_app

        core = create_app()
        train = create_training_app()

    def api_paths(app: object) -> set[str]:
        return {
            r.path for r in app.routes  # type: ignore[attr-defined]
            if getattr(r, "path", "").startswith("/api/v1")
        }

    return len(api_paths(core)), len(api_paths(train))


#: The phrasings a file uses to state one of the two counts.
#:
#: THIS GATE WAS THE DEFECT IT WAS WRITTEN TO FIX, and the fix is this function rather than
#: a longer list. It read exactly two files -- `training_plane.py` and the compose file --
#: because those were the two I knew about. Measured on 2026-09-26, FOUR MORE stated a
#: count and all four were stale at 24: `ARCHITECTURE.md`'s Core/Train table,
#: `tests/_support/stack.py`'s probe rationale, and `tests/unit/test_openapi_document.py`'s
#: own docstring explaining why its floor moved. A gate handed a list of sites cannot see a
#: site that is not on the list. It now enumerates every tracked file that states one.
COUNT_PHRASINGS: tuple[str, ...] = (
    r"(\d+) route paths",
    r"(\d+) `/api/v1` paths",
    r"serves (\d+) paths",
    r"shared (\d+) paths",
)

#: Files whose count is HISTORY and must not be updated. A release record states what was
#: true at its own tag; `docs/README.md` says so in terms.
COUNT_EXEMPT: tuple[str, ...] = ("docs/releases/", "docs/spec/")


def _stated_counts() -> dict[str, list[int]]:
    """Every tracked file that states one of the two counts -> the numbers it states."""
    # UNTRACKED FILES COUNT, for the reason entry 134 records: this gate already
    # enumerates rather than taking a list of sites, and an uncommitted file is a
    # site. Reading only the index reinstated the blind spot the enumeration fixed.
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         "*.md", "*.py", "*.yml", "*.yaml"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()
    out: dict[str, list[int]] = {}
    for rel in tracked:
        if any(rel.startswith(x) for x in COUNT_EXEMPT):
            continue
        if rel == Path(__file__).relative_to(ROOT).as_posix():
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        found = [
            int(m.group(1))
            for pattern in COUNT_PHRASINGS
            for m in re.finditer(pattern, text)
        ]
        if found:
            out[rel] = found
    return out


@pytest.fixture(scope="module")
def measured() -> tuple[int, int]:
    return _counts()


def test_the_route_counts_are_stated_in_more_than_one_place() -> None:
    """A green run must not be reachable by finding no claims at all."""
    sites = _stated_counts()
    assert len(sites) >= 5, (
        f"only {len(sites)} file(s) found stating a route count: {sorted(sites)}. Before "
        "this gate enumerated, it read two and missed four."
    )


def test_the_stated_route_counts_are_the_measured_ones(measured: tuple[int, int]) -> None:
    core, train = measured
    assert core >= 10 and train > core, (
        f"measured core={core} train={train}; one of the apps did not mount its routers "
        "and this check would pass for that reason alone"
    )

    stated = _stated_counts()
    for required in (PLANE, COMPOSE):
        rel = required.relative_to(ROOT).as_posix()
        assert rel in stated, f"{rel} no longer states a route count at all"

    wrong = {
        name: [n for n in numbers if n not in (core, train)]
        for name, numbers in stated.items()
    }
    wrong = {k: v for k, v in wrong.items() if v}
    assert not wrong, (
        f"these stated route counts are neither the measured core ({core}) nor the "
        f"measured train ({train}): {wrong}\n"
        "  The numbers carry an argument -- Train is 'a strict superset of Core's', and "
        "they are how a reader checks that shape without reading two route tables. Update "
        "the prose, and if the difference is not what you expected, find out which router "
        "moved before you do."
    )


def test_train_really_is_a_superset_of_core(measured: tuple[int, int]) -> None:
    """The claim the counts exist to support, checked as a claim rather than as arithmetic.

    Two numbers where one is larger prove nothing about containment. This compares the
    path SETS, which is what "strict superset" means, and is the property `MOS-TRAIN-225`
    leans on: everything Core serves, Train also serves, plus the training plane.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from medos.api.app import create_app
        from medos.api.training_plane import create_training_app

        core_paths = {
            r.path for r in create_app().routes
            if getattr(r, "path", "").startswith("/api/v1")
        }
        train_paths = {
            r.path for r in create_training_app().routes
            if getattr(r, "path", "").startswith("/api/v1")
        }

    missing = sorted(core_paths - train_paths)
    assert not missing, (
        f"Train does not serve {len(missing)} path(s) that Core does: {missing}\n"
        "  The compose file calls Train 'a strict superset of Core'. A path Core serves "
        "and Train does not means a deployment that runs Train alone answers 404 where "
        "the control plane answers, and the two-image split stops being a superset "
        "relationship at all."
    )
    assert train_paths - core_paths, (
        "Train serves nothing Core does not; why is it a second image?"
    )
