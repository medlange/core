# SPDX-License-Identifier: Apache-2.0
"""`medos.sdk` is installed into BOTH images, so what it imports, both get.

The package exists because `MOS-IMG-003` requires it and because the trainer needed a spec
format, a bundle layout and a digest rule without needing a platform. That second property
is the one that rots: every one of these modules is a plausible place to reach for a
tenant, a row or a request, and each such reach would be one line in a file whose name
says nothing about databases.

THE THREE THINGS ASSERTED HERE, in order of how quietly they would break:

  1. THE SDK IMPORTS NO PLATFORM BEYOND THE PURE CORE. Not "no `medos.db`" -- nothing
     outside `medos.sdk` and the PURE_CORE tuple below (`medos.core.geometry`, `dicomio`,
     `errors`, `masks`, `measure`, `uids`, `concepts` -- documented as having no I/O to
     DB or HTTP, which `tests/unit/test_core_lift.py` holds). The package moved inside
     the distribution (`medos/medos/sdk/`), so sibling imports are the package talking to
     itself; what is still forbidden is a reach into `medos.api`, `medos.db`,
     `medos.training` or anything else above the pure core. The one deliberate
     exception is `medos/sdk/adapters/`: an adapter IS a seam to an external system, and
     the shipped drivers reach the platform's DICOMweb client and KServe v2 driver
     (lazily, so the base SDK install does not require their transport libraries). A
     shared package that imports the platform is the platform, wearing a different
     directory, and the trainer would depend on it transitively while every import line
     looked innocent.

  2. ITS CLOSURE HOLDS NO DATABASE, NO SERVER FRAMEWORK AND NO TRAINING STACK. psycopg,
     `fastapi`, `starlette` -- and `torch`/`monai`, which are the ones people will argue
     about: the PLATFORM image must not carry them (`MOS-TRAIN-225` forbids the nnU-Net
     planner from the serving closure by name), and this package is installed into the
     platform image. `requests` is forbidden in the SDK root but permitted, GUARDED, in
     `adapters/` -- it is the transport a deployed adapter actually speaks, and a
     development install that never constructs a DicomWebPacs never imports it.

  3. NOTHING IN THE REPOSITORY IMPLEMENTS THESE CONTRACTS TWICE. `MOS-IMG-003` says in
     terms that "a second implementation of either contract anywhere in the repository ...
     is a defect", and `MOS-REL-032` says there is exactly one canonicaliser. A gate that
     only watched this package's imports would not notice a copy of it appearing somewhere
     else, which is the failure mode the requirement is actually about.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "medos" / "medos" / "sdk"

#: What the package's own modules are. Named, so a module added to the package is a
#: decision somebody makes here rather than a file that appears.
MODULES = {
    "__init__", "canonical", "refusal", "errors", "spec", "chain", "preprocess",
    "bundle", "autoconfig", "fixtures",
    # The run-directory exchange: what the platform writes and the trainer reads, and the
    # only module here that is a contract between two PROGRAMS rather than a format. It
    # lived in `trainer/medos_trainer/` with the platform keeping a hand-mirrored half in
    # `medos/medos/training/orchestrator.py` and a test asserting the two agreed. Two halves
    # that a test keeps in step is a contract with no home; this is the home.
    "contract",
    # The model card (`medlange.modelcard/1`): what a fitted model declares about itself
    # -- spec, weights, frameworks, outputs, stamp -- written by the trainer beside the
    # bundle and read by `ModelCard.load` to rebuild the preprocessing pipeline.
    "modelcard",
    # The serving pipeline: StudyTask -> fetched series -> canonical volume -> the card's
    # preprocessing chain -> inference -> postprocessed outputs.
    "pipeline",
    # The declarative postprocessor: turns a model's raw output (label map, metrics) into
    # the segments and measurements the card's outputs descriptor promises.
    "postprocess",
    # The runtime workers: LocalWorker (locally triggered) and ExternalWorker
    # (bus-driven), the two deployment modes of the same pipeline.
    "runtime",
    # The deployment profile (roadmap C3): one closed-schema YAML document that names
    # card/PACS/inference/writer/mode, loaded by `medos.sdk.profiles` and driven by
    # `python -m medos.sdk run`. Data, not code: the schema is closed and refusals
    # speak the profile's own vocabulary.
    "profiles",
    # The CLI entry point (`python -m medos.sdk run --profile ...`). Thin on purpose:
    # every decision lives in `profiles.py`, this only walks the studies and prints
    # JSON lines.
    "__main__",
}

#: The pure half of `medos.core` (documented: no I/O to DB or HTTP), which SDK modules may
#: import. Anything else under `medos.*` is the platform and stays forbidden.
PURE_CORE = (
    "medos.core.geometry", "medos.core.dicomio", "medos.core.errors",
    "medos.core.masks", "medos.core.measure", "medos.core.uids",
    "medos.core.concepts", "medos.core.bundle",
)

#: What `adapters/` may reach beyond PURE_CORE: the platform's DICOMweb client, its
#: KServe v2 inference driver, and its ONE DICOM writer -- all lazily so the base SDK
#: install needs neither `requests` nor a running server. `results.py` is the
#: ResultWriter driver: it writes through `medos.writer`, never beside it.
ADAPTER_ALLOWED = ("medos.dicomweb", "medos.inference", "medos.writer", "medos.capabilities")

#: Subpackages of the SDK and the modules each one holds. `bus` is the messaging
#: adapter (Kafka/RabbitMQ drivers + the schema-flexible codec); `results` is the
#: ResultWriter driver over the platform's writer. The gate fails closed either way.
SUBPACKAGES: dict[str, frozenset[str]] = {
    "adapters": frozenset({"__init__", "pacs", "inference", "bus", "results"}),
}

FORBIDDEN = ("psycopg", "fastapi", "starlette", "torch", "monai")
FORBIDDEN_IN_ADAPTERS = FORBIDDEN


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _modules() -> list[Path]:
    return sorted(p for p in PKG.glob("*.py") if "__pycache__" not in p.parts)


def test_the_package_is_where_this_gate_looks_and_holds_what_it_says() -> None:
    """Addressed by path, so it says so when the path moves rather than looping over none."""
    assert PKG.is_dir(), f"{PKG} does not exist"
    present = {p.stem for p in _modules()}
    assert present == MODULES, (
        f"undeclared: {sorted(present - MODULES)}; declared but absent: "
        f"{sorted(MODULES - present)}. A module joining this package is installed into two "
        "images, so it is a decision, not a file appearance"
    )
    for sub, members in SUBPACKAGES.items():
        directory = PKG / sub
        assert directory.is_dir(), f"sdk/{sub}/ is declared but does not exist"
        found = {p.stem for p in directory.glob("*.py")}
        assert found == members, (
            f"sdk/{sub}/ holds {sorted(found)}; the gate declares {sorted(members)}"
        )


def _is_allowed_reach(module: str, *, adapter: bool) -> bool:
    if module == "medos.sdk" or module.startswith("medos.sdk."):
        return True
    if any(module == core or module.startswith(core + ".") for core in PURE_CORE):
        return True
    if adapter and any(
        module == allowed or module.startswith(allowed + ".")
        for allowed in ADAPTER_ALLOWED
    ):
        return True
    return False


def test_the_shared_package_imports_no_platform_beyond_the_pure_core() -> None:
    """Not "no `medos.db`" -- nothing beyond the SDK itself and the documented pure core.

    The SDK used to be a separate top-level package under which ANY `medos` import was
    a reach into the platform. It has since moved INSIDE the distribution as
    `medos.sdk`, so sibling imports are the package talking to itself, and the pure
    half of `medos.core` (geometry, dicomio, errors, masks, measure, uids, concepts --
    no DB or HTTP I/O by CONTRACT.md's own table) is the ground the SDK stands on. What
    is still forbidden is everything above that: one import of `medos.api`, `medos.db`,
    `medos.training` and a consumer of the SDK depends on the platform again,
    transitively, with every import line still reading `from medos.sdk import ...`.
    `adapters/` carries the one deliberate widening, named in ADAPTER_ALLOWED.
    """
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        reached = sorted(
            m for m in _imports(path)
            if (m == "medos" or m.startswith("medos."))
            and not _is_allowed_reach(m, adapter=False)
        )
        if reached:
            offenders[path.name] = reached
    for sub in SUBPACKAGES:
        for path in sorted((PKG / sub).glob("*.py")):
            reached = sorted(
                m for m in _imports(path)
                if (m == "medos" or m.startswith("medos."))
                and not _is_allowed_reach(m, adapter=True)
            )
            if reached:
                offenders[f"{sub}/{path.name}"] = reached

    assert not offenders, (
        "the SDK imports the platform, so anything installing it installs a "
        "dependency on the rest of MedicalOS:\n"
        + "\n".join(f"    {f}: {', '.join(m)}" for f, m in sorted(offenders.items()))
    )


def test_the_shared_package_holds_no_db_no_server_framework_no_training_stack() -> None:
    """Installed into both images, so a dependency added here is added to both.

    `torch` and `monai` are in this list on purpose and they are the ones somebody will
    want to relax: `medos/medos/training/chain.py` generates MONAI Bundle configs as DATA and
    never imports MONAI, and `MOS-TRAIN-225` forbids the nnU-Net planner from the serving
    image's import closure BY NAME. This package is in the serving image. `requests` is
    deliberately NOT forbidden in `adapters/` -- it is the transport an adapter speaks at
    run time, imported lazily so a base SDK install never loads it; the database, the
    server frameworks and the training stack are forbidden EVERYWHERE in the package.
    """
    offenders: dict[str, list[str]] = {}
    roots = [(p, FORBIDDEN) for p in _modules()]
    roots += [
        (p, FORBIDDEN_IN_ADAPTERS)
        for sub in SUBPACKAGES
        for p in sorted((PKG / sub).glob("*.py"))
    ]
    for path, forbidden in roots:
        names = _imports(path)
        hit = [
            bad for bad in forbidden
            if any(n == bad or n.startswith(bad + ".") for n in names)
        ]
        if hit:
            offenders[path.name] = hit

    assert not offenders, (
        "the shared package acquired a database, a server framework or a training "
        "stack, and BOTH images inherit it:\n"
        + "\n".join(f"    {f}: {', '.join(m)}" for f, m in sorted(offenders.items()))
    )


def test_no_second_implementation_of_these_contracts_exists() -> None:
    """`MOS-IMG-003`: "a second implementation of either contract anywhere in the
    repository ... is a defect", and `MOS-REL-032` allows exactly one canonicaliser.

    Watching only this package's imports would miss a COPY of it appearing elsewhere,
    which is the failure the requirement is actually about. So this looks for the defining
    names outside the package: a second `def canonical_bytes`, a second `def build_chain`,
    a second `def write_bundle`.

    A NAME IS NOT AN IMPLEMENTATION, and the first version of this check did not know the
    difference. It fired on `medos/medos/resolution/model.py`'s `Model.canonical_bytes()` -- a
    one-line METHOD whose body is `canonical_json(self.as_document()).encode("utf-8")`,
    i.e. a call site with a convenient name, delegating to the very function this package
    owns. Renaming it would have been the wrong repair to a question wrongly asked.

    So the question asked here is: does this definition DELEGATE to the shared package, or
    does it do the work itself? A definition that names a symbol imported from
    `medos.sdk` anywhere in its body is a caller. One that does not, while
    carrying one of these names, is a second implementation.
    """
    #: symbol -> the module in this package that defines it
    OWNED = {
        "canonical_bytes": "canonical",
        "canonical_json": "canonical",
        "build_chain": "preprocess",
        "write_bundle": "bundle",
        "parse_spec": "spec",
        "serialize_chain": "chain",
    }
    roots = [
        ROOT / "medos" / "medos",
        ROOT / "trainer",
        ROOT / "medos" / "services",
        ROOT / "medos" / "tools",
    ]
    offenders: dict[str, list[str]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            # The SDK package itself defines these symbols; its definitions ARE the one
            # implementation, not a second one. Only everything else is scanned.
            if PKG in path.parents:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue

            # Every name this module pulled in from the shared package. A body that uses
            # one of them is calling the single implementation, not writing a second.
            borrowed: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("medos.sdk"):
                        borrowed.update(a.asname or a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith("medos.sdk"):
                            borrowed.add((a.asname or a.name).split(".")[0])

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name not in OWNED:
                    continue
                used = {
                    n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                } | {
                    n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
                } | {
                    n.value.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                }
                if used & borrowed:
                    continue          # delegates to the one implementation
                rel = str(path.relative_to(ROOT)).replace("\\", "/")
                offenders.setdefault(rel, []).append(f"{node.name} (line {node.lineno})")

    assert not offenders, (
        "these carry a name `medos.sdk` owns AND do the work themselves "
        "rather than calling it, which is a second implementation of a contract the "
        "specification says has exactly one:\n"
        + "\n".join(f"    {f}: {', '.join(n)}" for f, n in sorted(offenders.items()))
        + "\n  Import it from the shared package instead."
    )
