# SPDX-License-Identifier: Apache-2.0
"""Run the viewer's own geometry modules, against DICOM the seeder actually produced.

WHY THIS EXISTS, AND WHY THE STATIC GATES ARE NOT ENOUGH
---------------------------------------------------------
`viewer/tests/test_architecture.py` reads the viewer's JavaScript as TEXT. That is the
right tool for a large class of defect -- a refusal that was deleted, a comment that claims
something the code does not do, an export nothing calls -- and it has caught many. It cannot
catch a defect whose symptom is a NUMBER.

Two found on one afternoon, neither visible to any amount of grepping:

  * `followIndex` reported `LINK.POSITION` for a slice 41 mm outside the target series,
    which `linkBadge` renders as a green chip reading `position-linked`, kind `exact`. Every
    line of that was individually correct. The defect was the arithmetic's RESULT.

  * `positionLinkable` took its dihedral on the ACQUIRED slice normals while `followIndex`
    compared ordinates measured along the DISPLAYED PLANE's normal. For an axial the two
    vectors are equal, so the gate was sound and the axial link was right. For a coronal
    they are not: the same volume described feet-first linked index 100 to index 100 at a
    reported 0.000 mm with the two panels 83.30 mm apart, on opposite sides of the midline,
    and `describeLink` printed no distance clause at all because 0.000 is under its 0.01 mm
    threshold. The reader got the strongest assurance the surface can give on the most wrong
    answer it can produce.

Both fell out immediately once the real functions were run on real geometry. Neither could
have been read out of the source.

WHAT IT RUNS AGAINST
--------------------
`medos/tools/demo/seed_corpus.py`'s phantom and its `--companion` study: one patient, two
acquisitions, one FrameOfReferenceUID, offset 7 mm along the slice axis and 5 mm in plane.
Both offsets are deliberately not whole multiples of the corresponding pitch -- 3.5 slices
and 7.14 rows -- so the rounding is exercised rather than a coincidence. The geometry is
read back out of the Part-10 bytes with pydicom rather than taken from the builder's own
variables, so a seeder that writes something different from what it computed is caught here
too.

The harness then restates that same physical volume with different in-plane cosines --
feet-first, and a 90-degree rotation -- which is what a real feet-first patient, a rotated MR
field of view or a reformatted secondary capture produces. Every one of those has the same
acquired slice normal, which is precisely why the old gate passed them all.

WHY NODE IN A CONTAINER
-----------------------
The viewer is ES modules with no build step and no package.json, which is a deliberate
property (`docs/adr/BUILD_VS_ADOPT.md`). Running them needs a JavaScript engine and nothing
else. The sources are streamed in over stdin as a tar rather than bind-mounted, and the
harness prints the SHA-256 of each module it loaded: a harness that silently tests an older
copy of the thing under test is worse than no harness, because it is the mechanism that
makes a break-proof pass.

Spec: MOS-UI-009a (MOS-UI-009 withdrawn at specification 0.3.0), MOS-IMG-039.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests._support.skips import skip_infra

REPO = Path(__file__).resolve().parents[2]
#: The harnesses live WITH THE VIEWER, at `viewer/tests/js/`, because they drive the
#: viewer's own modules against real geometry and read nothing else. The RUNNER is here,
#: in the platform's suite, because running them needs Docker and a corpus -- the platform
#: may read the viewer, and the viewer may not read the platform.
HARNESS_DIR = REPO / "viewer" / "tests" / "js"
#: Each run as its own test. Named rather than globbed, so a file added there is added here
#: deliberately -- a glob would silently run nothing if the directory were renamed, which is
#: the failure this suite's skip taxonomy exists for. And because a name can go stale where
#: a glob cannot, the presence of each one is asserted below rather than assumed.
HARNESSES = ("cross_series.mjs", "cursor.mjs", "planes.mjs", "regions.mjs")
NODE_IMAGE = "node:22-alpine"

#: Every attribute `volume.js` reads, by the tag string it keys the dataset with.
TAGS = {
    "00080018": "SOPInstanceUID",
    "00080060": "Modality",
    "00280010": "Rows",
    "00280011": "Columns",
    "00280002": "SamplesPerPixel",
    "00280004": "PhotometricInterpretation",
    "00280100": "BitsAllocated",
    "00280101": "BitsStored",
    "00280103": "PixelRepresentation",
    "00280030": "PixelSpacing",
    "00281052": "RescaleIntercept",
    "00281053": "RescaleSlope",
    "00281054": "RescaleType",
    "00200032": "ImagePositionPatient",
    "00200037": "ImageOrientationPatient",
    "00200052": "FrameOfReferenceUID",
    "00080008": "ImageType",
}


def _geometry(payloads: list[tuple[str, bytes]]) -> list[dict]:
    """Read the geometry back OUT of the Part-10 bytes, not from the builder's variables."""
    import pydicom

    out = []
    for _, blob in payloads:
        ds = pydicom.dcmread(io.BytesIO(blob), stop_before_pixels=True)
        record: dict = {}
        for tag, name in TAGS.items():
            value = getattr(ds, name, None)
            if value is None:
                continue
            if isinstance(value, str):
                record[tag] = value
            elif hasattr(value, "__iter__"):
                record[tag] = [x if isinstance(x, str) else float(x) for x in value]
            else:
                record[tag] = float(value)
        out.append(record)
    return out


@pytest.fixture(scope="module")
def corpus_geometry() -> bytes:
    """The two studies' geometry, as the harness consumes it."""
    pytest.importorskip("pydicom", reason="the seeder writes Part-10 with pydicom")
    pytest.importorskip("numpy")

    from tools.demo.seed_corpus import (
        COMPANION_OFFSET_MM,
        COMPANION_SLICES,
        COMPANION_Y_OFFSET_MM,
        Phantom,
        build_series,
        build_volume,
    )

    phantom = Phantom()
    volume = build_volume(phantom)
    source, _, _ = build_series(phantom, volume)
    companion, _, _ = build_series(
        phantom,
        volume,
        variant="companion",
        z_offset_mm=COMPANION_OFFSET_MM,
        y_offset_mm=COMPANION_Y_OFFSET_MM,
        slices=COMPANION_SLICES,
    )
    return json.dumps(
        {"source": _geometry(source), "companion": _geometry(companion)}
    ).encode("utf-8")


def _require_docker() -> None:
    if shutil.which("docker") is None:
        skip_infra("the docker CLI is not on PATH", dependency="docker")
    probe = subprocess.run(
        ["docker", "image", "inspect", NODE_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        skip_infra(
            f"{NODE_IMAGE} is not present locally; `docker pull {NODE_IMAGE}` provides it",
            dependency="docker",
        )


def test_the_named_harnesses_are_the_harnesses_on_disk() -> None:
    """The list above is a list of NAMES, and this is what keeps it from going stale.

    Every test below needs Docker and a corpus. Without either they skip -- so a harness
    renamed, moved or deleted would show up as a skip, which is indistinguishable from a
    machine without Docker. This check needs neither and runs everywhere, and it is
    two-sided: a name with no file is a test that silently stopped existing, and a file
    with no name is a harness nothing runs.
    """
    assert HARNESS_DIR.is_dir(), (
        f"{HARNESS_DIR} does not exist. The geometry harnesses live with the viewer; if "
        "the tree moved again, this runner is addressing the old place and every test "
        "below would report a skip rather than a failure"
    )
    on_disk = {p.name for p in HARNESS_DIR.glob("*.mjs")}
    named = set(HARNESSES)
    assert named == on_disk, (
        f"named but absent: {sorted(named - on_disk) or 'none'}; "
        f"present but never run: {sorted(on_disk - named) or 'none'}"
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_the_viewer_geometry_computes_what_it_claims(
    harness: str, corpus_geometry: bytes,
) -> None:
    """Execute `sync.js`, `mpr.js` and `volume.js` and check what they compute.

    The harness prints one line per check and exits non-zero on the first failure; its
    output is attached to the failure verbatim, because "25 checks failed" is not
    information and the line that says which plane linked to which index is.
    """
    _require_docker()

    # Stream the sources over the Docker API rather than bind-mounting them. A bind mount
    # of the working tree is a shared-filesystem cache, and a harness that reads a stale
    # copy of the module it is testing reports green on code that no longer exists.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(REPO / "viewer" / "src", arcname="viewer/src")
        tar.add(HARNESS_DIR / harness, arcname=harness)
        info = tarfile.TarInfo("corpus_geometry.json")
        info.size = len(corpus_geometry)
        tar.addfile(info, io.BytesIO(corpus_geometry))

    result = subprocess.run(
        [
            "docker", "run", "-i", "--rm", NODE_IMAGE,
            "sh", "-c", f"mkdir -p /src && tar -xf - -C /src && node /src/{harness}",
        ],
        input=buffer.getvalue(),
        capture_output=True,
        check=False,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    output = result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0, (
        "the viewer's geometry modules did not compute what they claim.\n\n" + output
    )
    # A harness that ran nothing also exits 0. The count is asserted so an import error
    # inside a `try` or a silently-skipped section cannot read as a pass.
    # COUNTED BY LINE, not by a leading newline: the first check has none, so a
    # harness of exactly N checks counted N-1 and a floor of N failed a green run.
    ran = len(re.findall(r"(?m)^ok   ", output))
    assert ran >= 6, (
        "the harness ran far fewer checks than it contains, so it did not do its job:\n\n"
        + output
    )
