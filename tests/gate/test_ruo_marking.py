# SPDX-License-Identifier: Apache-2.0
"""`ruo-marking` -- the release-0.2.0 gate check of docs/spec/15-delivery.md section 15.1.2.

    `ruo-marking` (in `clinical_use_mode: research_only` the writer refuses to emit an
    unmarked object)

Section 15.1.3 puts `clinical_use_mode` + RUO marking in 0.2.0's **Tier A**: MUST NOT be
cut under any circumstance. `MOS-SAFE-039` states the behaviour: the writer "MUST fail the
Job rather than emit an unmarked object", through "a single function shared by the SEG, SR
and SC writers".

WHAT THIS CHECK RUNS AGAINST, AND WHY IT IS THE REAL WRITER
-----------------------------------------------------------
`medos.writer.seg.build_seg` and `medos.writer.sr.build_sr` are called here for real, on a
synthetic CT series built by this module, through `build_canonical_volume`,
`build_job_identity` and `plan_outputs` -- the same four functions `medos/medos/worker/steps.py`
calls. Nothing about the marking path is stubbed, and no marking assertion is re-implemented
here: the check is whether THOSE functions refuse, not whether a copy of their rule does.

The refusal arms work by making the assembled object lose exactly one marker, from inside
the writer, after the writer has finished marking it. That is the defect shape the check
exists for and it is not hypothetical: a truncation, an attribute copied over by an
inherited-attribute rule, a DICOM library that drops a field it does not recognise. A check
that only caught a WHOLLY unmarked object would catch none of them, and the object it did
catch is the one no plausible bug produces.

THE CONTROL GROUP IS PART OF THE CHECK
---------------------------------------
`test_ruo_marking_a_clinical_object_is_emitted_without_the_research_banner` exists because
a writer that refused everything would pass every refusal arm above. Both directions, or
the check proves nothing.

PHI (CONTRACT.md section 11)
-----------------------------
The series is synthesised in this file. There is no patient: `PatientID` is the literal
`ZZPHANTOMZZ`, `PatientBirthDate` and `PatientSex` are empty, and nothing is read from any
corpus. No patient attribute is printed by any assertion here.

Needs no container: it writes four small DICOM files to `tmp_path` and reads them back.

Spec: MOS-SAFE-039, MOS-SAFE-042, MOS-IMG-133, MOS-IMG-134, MOS-IMG-136, MOS-IMG-137,
MOS-IMG-138, MOS-REL-004, MOS-REL-012.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pydicom
import pytest
from medos.capabilities.base import load_concepts
from medos.core.bundle import CapabilityOutcome, CodedConcept, LabelMap, ResultBundle
from medos.core.geometry import build_canonical_volume
from medos.safety import marking as marking_mod
from medos.safety.marking import (
    AI_PREFIX,
    RUO_CONTENT_DESCRIPTION_PREFIX,
    RUO_PREFIX,
    RUO_SR_COMMENT_REQUIRED_SUBSTRING,
    MarkingAbsent,
    marking_violations,
)
from medos.writer import identity as identity_mod
from medos.writer import seg as seg_mod
from medos.writer import sr as sr_mod
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

pytestmark = pytest.mark.gate_0_2_0

REPO_ROOT = Path(__file__).resolve().parents[2]

RESEARCH_ONLY = "RESEARCH_ONLY"
CLINICAL = "CLINICAL"

#: Four slices, 24x24. Small enough that a whole gate run costs milliseconds, and large
#: enough that `highdicom` writes a genuine multi-frame SEG with two non-empty segments.
K_SLICES, ROWS, COLUMNS = 4, 24, 24

TENANT = "00000000-0000-0000-0000-000000000000"
JOB_ID = "job_01J000000000000000000000"


# ======================================================================================
# The synthetic source series. No corpus, no patient.
# ======================================================================================
def _write_phantom_series(directory: Path) -> tuple[str, list[Path]]:
    """A four-slice axial CT series with the attributes `highdicom` requires of a source.

    Every Patient-module value is either the literal placeholder `ZZPHANTOMZZ` or empty.
    They are present because `hd.seg.Segmentation` copies the Patient and General Study
    modules from the source image and raises on a missing Type 2 attribute -- which is the
    behaviour MOS-REL-031 adopts highdicom FOR: its constructors require the identity
    metadata a hand-rolled encoder would have let us omit.
    """
    directory.mkdir(parents=True, exist_ok=True)
    study_uid, series_uid, frame_uid = (generate_uid() for _ in range(3))
    paths: list[Path] = []
    for k in range(K_SLICES):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.ImplementationClassUID = generate_uid()
        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        ds.PatientID = "ZZPHANTOMZZ"
        ds.PatientName = "ZZPHANTOMZZ^Synthetic"
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.StudyDate = "20260601"
        ds.StudyTime = "120000"
        ds.SeriesDate = "20260601"
        ds.SeriesTime = "120000"
        ds.ContentDate = "20260601"
        ds.ContentTime = "120000"
        ds.AccessionNumber = ""
        ds.StudyID = "1"
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1
        ds.Rows = ROWS
        ds.Columns = COLUMNS
        ds.PixelSpacing = [1.0, 1.0]
        ds.SliceThickness = 2.0
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.ImagePositionPatient = [
            -(COLUMNS - 1) / 2.0,
            -(ROWS - 1) / 2.0,
            k * 2.0,
        ]
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleSlope = 1.0
        ds.RescaleIntercept = -1024.0
        ds.RescaleType = "HU"
        ds.ConvolutionKernel = "TESTKERNEL"
        ds.PixelData = np.full((ROWS, COLUMNS), 1024, np.uint16).tobytes()
        path = directory / f"slice{k:03d}.dcm"
        pydicom.dcmwrite(path, ds, enforce_file_format=True)
        paths.append(path)
    return study_uid, paths


class _Written:
    """A source series, the plan over it, and the datasets the writer consumes."""

    def __init__(self, directory: Path) -> None:
        study_uid, _paths = _write_phantom_series(directory)
        _vol, source, _rejects = build_canonical_volume(
            sorted(directory.glob("*.dcm"))
        )
        self.concepts = load_concepts()
        self.source = source
        self.study_instance_uid = study_uid
        self.source_datasets = [pydicom.dcmread(p) for p in source.paths]

        # Two non-empty segments, resolved through the ConceptDictionary. MOS-IMG-112
        # forbids inventing a code, and MOS-IMG-113 forbids materialising the list here,
        # so the concepts are looked up rather than written down.
        array = np.zeros((K_SLICES, ROWS, COLUMNS), np.uint8)
        array[:, 4:10, 4:10] = 1
        array[:, 14:20, 14:20] = 2
        segments = tuple(
            self._coded(f"anatomy.{slug}") for slug in ("lung_left", "lung_right")
        )
        self.bundle = ResultBundle(
            outcomes=(
                CapabilityOutcome(
                    capability_id="lung_segmentation",
                    findings=(),
                    label_map=LabelMap(array=array, segments=segments),
                    source_sop_instance_uids=tuple(source.sop_instance_uids),
                ),
            )
        )

    def _coded(self, key: str) -> CodedConcept:
        concept = self.concepts[key]
        return CodedConcept(
            scheme=concept["coding_scheme"],
            code=concept["code_value"],
            meaning=concept["code_meaning"],
        )

    def plan(self, mode: str) -> Any:
        identity = identity_mod.build_job_identity(
            job_id=JOB_ID,
            tenant_id=TENANT,
            service_id="medicalos.lung_oar",
            service_version="0.2.0",
            study_instance_uid=self.study_instance_uid,
            capability_ids=["lung_segmentation"],
            # Chapter 5 section 5.6.2's spelling, which `schema.sql` enforces as
            # `CHECK (requested_outputs <@ ARRAY['SEG','SR','SC'])`. This fixture said
            # `["seg", "sr"]` while the field was ignored by the writer; it is now read,
            # and `build_job_identity` refuses a member outside the closed set.
            requested_outputs=["SEG", "SR"],
            clinical_use_mode=mode,
            model_version="1.0.0",
        )
        return identity_mod.plan_outputs(identity, self.bundle, self.concepts)

    def seg(self, mode: str) -> Dataset:
        return seg_mod.build_seg(
            self.plan(mode), self.source_datasets, self.source, self.concepts
        )

    def sr(self, mode: str, seg: Dataset) -> Dataset:
        return sr_mod.build_sr(self.plan(mode), seg, self.source_datasets, self.concepts)


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> _Written:
    return _Written(tmp_path_factory.mktemp("ruo-phantom"))


def _content_items(node: Any, depth: int = 0) -> list[Any]:
    """Depth-first over an SR content tree. Bounded: a cycle must not hang the gate."""
    if depth > 12:
        return []
    out: list[Any] = []
    for item in list(getattr(node, "ContentSequence", []) or []):
        out.append(item)
        out.extend(_content_items(item, depth + 1))
    return out


def _research_text_items(sr: Dataset) -> list[Any]:
    return [
        item
        for item in _content_items(sr)
        if str(getattr(item, "ValueType", "")) == "TEXT"
        and RUO_SR_COMMENT_REQUIRED_SUBSTRING
        in str(getattr(item, "TextValue", "") or "")
    ]


# ======================================================================================
# 1. The writer emits a fully marked object in research_only. The left-hand side.
# ======================================================================================
def test_ruo_marking_the_writer_emits_a_fully_marked_research_object(
    written: _Written,
) -> None:
    """Every marker MOS-IMG-133/134/136/137 requires, on the object the writer returned.

    Asserted attribute by attribute rather than by calling `marking_violations` again:
    that function is the thing under test on the refusal arms below, and a check whose
    positive arm is "the checker agrees with itself" is not a check.
    """
    seg = written.seg(RESEARCH_ONLY)
    sr = written.sr(RESEARCH_ONLY, seg)

    # MOS-IMG-136: the research banner is in front of the AI marker, on both objects.
    assert seg.SeriesDescription.startswith(RUO_PREFIX)
    assert sr.SeriesDescription.startswith(RUO_PREFIX)
    assert seg.ContentDescription.startswith(RUO_CONTENT_DESCRIPTION_PREFIX)

    # MOS-IMG-133: the object says a machine made it, in the two places a viewer reads.
    assert "MedicalOS" in seg.Manufacturer
    assert "DERIVED" in list(seg.ImageType)

    # MOS-IMG-134: contributing equipment names the producing software.
    assert len(seg.ContributingEquipmentSequence) >= 1

    # MOS-IMG-137/138: the private block carries the mode the job ran in, so a consumer
    # that reads no free text can still tell research output from clinical output.
    block = seg.private_block(0x0099, marking_mod.PRIVATE_CREATOR)
    assert block[marking_mod._CLINICAL_USE_MODE_ELEMENT].value == RESEARCH_ONLY

    # MOS-IMG-136's SR half: a TEXT content item saying it in words. Walked rather than
    # searched in `str(sr)`, because pydicom elides nested sequences when it renders a
    # dataset and the banner lives three levels down the content tree.
    assert _research_text_items(sr), (
        "the research SR carries no TEXT content item containing "
        f"{RUO_SR_COMMENT_REQUIRED_SUBSTRING!r}"
    )

    # And the shared verifier agrees, which is what the writer itself called.
    assert marking_violations(seg, object_kind="SEG", mode=RESEARCH_ONLY) == ()
    assert marking_violations(sr, object_kind="SR", mode=RESEARCH_ONLY) == ()


# ======================================================================================
# 2. THE CHECK. One marker missing, and the writer refuses to return an object.
# ======================================================================================
#: attribute on the assembled SEG -> the requirement whose absence it is.
_SEG_MARKERS = {
    "SeriesDescription": "MOS-IMG-136",
    "ContentDescription": "MOS-IMG-136",
    "Manufacturer": "MOS-IMG-133",
    "ImageType": "MOS-IMG-133",
    "ContributingEquipmentSequence": "MOS-IMG-134",
}


@pytest.mark.parametrize(("attribute", "rule"), sorted(_SEG_MARKERS.items()))
def test_ruo_marking_the_seg_writer_refuses_when_one_marker_is_missing(
    written: _Written, monkeypatch: pytest.MonkeyPatch, attribute: str, rule: str
) -> None:
    """`MOS-SAFE-039`, through `medos.writer.seg.build_seg` itself.

    The marker is removed from INSIDE the writer -- `apply_ai_marking` is wrapped so that
    the object the writer goes on to check is one marker short. So what is asserted is
    that `build_seg` raises and returns nothing, not that a separate verifier would have
    said no if anyone had asked it.
    """
    real = identity_mod.apply_ai_marking

    def drop_one(
        obj: Dataset, identity: Any, plan: Any, *, is_seg: bool, src: Any
    ) -> None:
        real(obj, identity, plan, is_seg=is_seg, src=src)
        if attribute in obj:
            delattr(obj, attribute)

    monkeypatch.setattr(seg_mod, "apply_ai_marking", drop_one)

    with pytest.raises(MarkingAbsent) as exc:
        written.seg(RESEARCH_ONLY)

    # The refusal NAMES the rule, because "the writer refused" is not an actionable
    # message on a job that has already consumed a GPU-minute.
    rules = {v.rule for v in exc.value.violations}
    assert rule in rules, f"{attribute} removed; refusal cited {sorted(rules)}, not {rule}"
    # The violation names the attribute AND its tag ("SeriesDescription (0008,103E)"),
    # so the message is actionable without a DICOM dictionary open beside it.
    attributes = {v.attribute for v in exc.value.violations}
    assert any(a.startswith(attribute) for a in attributes), (
        f"{attribute} removed; refusal named {sorted(attributes)}"
    )


def test_ruo_marking_the_sr_writer_refuses_without_the_research_text_item(
    written: _Written, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SR half of `MOS-IMG-136`: a machine-readable prefix is not a statement.

    `apply_research_marking` is the only writer of the TEXT content item. Neutralised, the
    SR still carries `[AI][RUO]` in its SeriesDescription and every private-block value --
    so a check that looked only at the header would pass, and the object a radiologist
    opens would say nothing about research use in its own content tree.
    """
    seg = written.seg(RESEARCH_ONLY)
    monkeypatch.setattr(sr_mod, "apply_research_marking", lambda *a, **k: None)

    with pytest.raises(MarkingAbsent) as exc:
        written.sr(RESEARCH_ONLY, seg)
    assert "MOS-IMG-136" in {v.rule for v in exc.value.violations}


def test_ruo_marking_a_mode_mismatch_in_the_private_block_is_refused(
    written: _Written, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MOS-IMG-138`: the recorded mode is the JOB's mode, not a string someone chose.

    A SEG whose free text says RESEARCH USE ONLY while `(0099,xx02)` says `CLINICAL` is
    worse than an unmarked object: it is an object that reads as research to a human and
    as clinical to every machine downstream.
    """
    real = identity_mod.apply_ai_marking

    def relabel(
        obj: Dataset, identity: Any, plan: Any, *, is_seg: bool, src: Any
    ) -> None:
        real(obj, identity, plan, is_seg=is_seg, src=src)
        block = obj.private_block(0x0099, marking_mod.PRIVATE_CREATOR)
        block[marking_mod._CLINICAL_USE_MODE_ELEMENT].value = CLINICAL

    monkeypatch.setattr(seg_mod, "apply_ai_marking", relabel)

    with pytest.raises(MarkingAbsent):
        written.seg(RESEARCH_ONLY)


# ======================================================================================
# 3. The control group, and the absence of an off switch.
# ======================================================================================
def test_ruo_marking_a_clinical_object_is_emitted_without_the_research_banner(
    written: _Written,
) -> None:
    """The other direction. A writer that refused everything would pass section 2 above.

    In `clinical` the AI marker stays (MOS-IMG-133 is unconditional) and the research
    banner goes, and the object is EMITTED. `MOS-SAFE-042` is the rule the other way
    round: research marking is not something a clinical deployment may carry decoratively.
    """
    seg = written.seg(CLINICAL)
    sr = written.sr(CLINICAL, seg)

    assert seg.SeriesDescription.startswith(AI_PREFIX)
    assert not seg.SeriesDescription.startswith(RUO_PREFIX)
    assert not seg.ContentDescription.startswith(RUO_CONTENT_DESCRIPTION_PREFIX)
    assert _research_text_items(sr) == []

    block = seg.private_block(0x0099, marking_mod.PRIVATE_CREATOR)
    assert block[marking_mod._CLINICAL_USE_MODE_ELEMENT].value == CLINICAL
    assert marking_violations(seg, object_kind="SEG", mode=CLINICAL) == ()
    assert marking_violations(sr, object_kind="SR", mode=CLINICAL) == ()


#: The three modules the marking path lives in. `(import path, builder)`.
_MARKING_SOURCES = (
    ("medos/medos/safety/marking.py", None),
    ("medos/medos/writer/seg.py", "build_seg"),
    ("medos/medos/writer/sr.py", "build_sr"),
)


def test_ruo_marking_has_no_off_switch_and_one_shared_verifier() -> None:
    """`MOS-SAFE-039`: "a single function shared by the SEG, SR and SC writers".

    Three properties of the SOURCE, parsed with `ast` and not grepped, because comments
    and docstrings legitimately contain the words a grep would match -- `marking.py`'s own
    docstring says "no environment variable" -- and a check that a developer has to work
    around is a check that gets deleted. All three defects are future EDITS that every
    behavioural fixture above would still pass:

      * a writer told to skip the check by configuration. A marking rule with an
        environment read in front of it is a marking rule that is off in the one
        deployment nobody looked at.
      * a writer that re-implements the rule. The copy is what rots: the day a sixth
        marker is added to `medos/medos/safety/marking.py`, a private copy in `writer/sr.py`
        does not learn about it and the SR ships one marker short of the SEG.
      * a return that precedes the check. `MOS-SAFE-039` is about EMITTING, so a builder
        with an early exit satisfies every assertion about the object it does return and
        none about the object it returns on the other branch.
    """
    import ast

    for relative, builder in _MARKING_SOURCES:
        path = REPO_ROOT / relative
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        # (a) no configuration read anywhere in the module.
        reads: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                reads.append(f"{node.attr} at line {node.lineno}")
            if isinstance(node, ast.Name) and node.id in ("getenv", "environ"):
                reads.append(f"{node.id} at line {node.lineno}")
        assert not reads, (
            f"{relative} reads configuration on the marking path: {reads}. "
            f"MOS-SAFE-039 admits no flag that disables the check."
        )

        if builder is None:
            continue

        # (b) the writer calls the shared verifier and declares no rival.
        defined = {
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        }
        assert "marking_violations" not in defined, (
            f"{relative} defines its own marking_violations; that is the second copy "
            f"MOS-SAFE-039 exists to prevent"
        )
        imported = {
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "medos.safety.marking"
            for alias in n.names
        }
        assert "assert_marked" in imported, (
            f"{relative} does not import the shared verifier; MOS-SAFE-039 asks for one "
            f"function, not one per writer"
        )

        # (c) every return in the builder is after the check.
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == builder
        )
        checks = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "assert_marked"
        ]
        assert len(checks) == 1, (
            f"{relative}:{builder} calls assert_marked {len(checks)} times; one "
            f"chokepoint, or the next edit adds a path that misses it"
        )
        returns = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Return)]
        assert returns, f"{relative}:{builder} has no return statement"
        assert min(returns) > checks[0], (
            f"{relative}:{builder} can return an object at line {min(returns)}, before "
            f"the marking check at line {checks[0]} has run"
        )
