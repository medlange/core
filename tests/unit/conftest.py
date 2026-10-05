# SPDX-License-Identifier: Apache-2.0
"""Split-checkout scoping for the unit suite.

A growing class of gates verifies the MONOREPO's structure: that CONTRACT.md's
layout section matches the trees on disk, that register numbers count the right
trees, that the CI testpaths cover every suite, that README path tables resolve
across products. In a standalone `medlange/core` checkout the sibling product
trees (`viewer/`, `trainer/`) do not exist, and those gates fail on the layout
rather than on any fact about the code.

THE DESLECTION IS EXPLICIT AND CENTRALISED HERE, not sprinkled as skips across
seventeen files: one list, one reason, visible in every split CI log. In the
monorepo checkout nothing is deselected — the gates run exactly as before.
"""

from __future__ import annotations

from pathlib import Path

#: (file, nodeid substring) pairs that are monorepo-structural. A test is
#: deslected only when BOTH match, so sibling gates in the same file that are
#: meaningful to a split (e.g. the medos README package table) keep running.
_MONOREPO_STRUCTURAL: dict[str, tuple[str, ...]] = {
    "test_every_tree_has_a_watcher.py": ("",),
    "test_moved_tree_citations.py": ("",),
    "test_register_numbers_re_derive.py": ("",),
    "test_source_control_bytes.py": ("",),
    "test_stack_contract.py": ("",),
    "test_suite_layout.py": ("",),
    "test_testing_chapter_is_unbuilt.py": ("",),
    "test_trainer_platform_contract.py": ("",),
    "test_trainer_import_boundary.py": ("",),
    "test_viewer_cache_policy.py": ("",),
    "test_viewer_deployment.py": ("",),
    "test_viewer_wire_contract.py": ("",),
    "test_product_readmes.py": (
        "[trainer]",
        "[viewer]",
        "unchecked_remainder",
        "dead_entry",
        "viewer_ships_the_number_of_files",
        "viewer_line_figure",
    ),
}


def pytest_collection_modifyitems(items: list) -> None:
    root = Path(__file__).resolve().parents[2]
    if (root / "viewer").is_dir() and (root / "trainer").is_dir():
        return  # the monorepo: every structural gate has its trees
    deselected: list[str] = []
    kept: list = []
    for item in items:
        name = Path(str(item.fspath)).name
        needles = _MONOREPO_STRUCTURAL.get(name)
        if needles is not None and any(n in item.nodeid for n in needles):
            deselected.append(item.nodeid)
        else:
            kept.append(item)
    items[:] = kept
    if deselected:
        print(
            f"\nsplit checkout: {len(deselected)} monorepo-structural gate(s) "
            "deselected (tests/unit/conftest.py lists them):\n  "
            + "\n  ".join(deselected)
        )
