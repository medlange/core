# SPDX-License-Identifier: Apache-2.0
"""The channel declaration refuses what it does not know, and never guesses.

The one property this file exists to protect: an unlisted raw segment string produces a
REFUSAL NAMING THE LITERAL, and never a fold, a default, a skip or a near-match. Everything
else here is scaffolding around that.

The threat is not that someone writes a bad matcher on purpose. It is that a sixteenth
spelling of `Pulmonary trunk` shows up, a test goes red, and the cheapest way to make it
green is one `.strip().casefold()`. That edit would also fold `S1` onto `S2` the next time
two lung segments differ by one character, and nothing would go red then.

Spec: MOS-TRAIN-098, MOS-UI-149.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from medos.training.channelmap import (
    BASES,
    CHANNEL_MAP_DIR_VAR,
    UnlistedSegment,
    load_channel_map,
)
from medos.training.seal import DeploymentNotDeclared


def _write(tmp_path: Path, rows: list[dict], name: str = "corpus.json") -> dict[str, str]:
    (tmp_path / name).write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return {CHANNEL_MAP_DIR_VAR: str(tmp_path)}


def _row(**kw) -> dict:
    base = {
        "corpus_id": "c",
        "raw_name": "neo",
        "observed_count": 100,
        "channel": "lung_neoplasm",
        "basis": "author_named",
        "status": "provisional",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------------------
# THE TEST THIS FILE EXISTS FOR
# --------------------------------------------------------------------------------------


def test_an_unlisted_spelling_is_refused_by_its_literal(tmp_path: Path) -> None:
    """A spelling nobody declared resolves to nothing, loudly.

    THIS TEST GOES GREEN FOR THE WRONG REASON if a normaliser is ever added to `resolve`.
    `'Pulmonaty trunk'` would then fold onto `'Pulmonary trunk'` and resolve instead of
    refusing. If you are here because this test failed after you added a strip/casefold/
    fuzzy match: that is the test working. Add a row instead.
    """
    env = _write(tmp_path, [_row(raw_name="Pulmonary trunk", channel="pulmonary_trunk")])
    channel_map = load_channel_map(env)

    with pytest.raises(UnlistedSegment) as caught:
        channel_map.resolve("c", "Pulmonaty trunk")
    message = str(caught.value)
    assert "'Pulmonaty trunk'" in message, message
    assert "normaliser" in message


def test_whitespace_is_part_of_the_literal(tmp_path: Path) -> None:
    """`'Pulmonary trunk '` really exists in this collection, once. It renders identically
    to `'Pulmonary trunk'` in every log, so the only place the difference can be caught is
    at the lookup -- which means the lookup must not trim."""
    env = _write(tmp_path, [_row(raw_name="Pulmonary trunk", channel="pulmonary_trunk")])
    channel_map = load_channel_map(env)
    with pytest.raises(UnlistedSegment):
        channel_map.resolve("c", "Pulmonary trunk ")
    with pytest.raises(UnlistedSegment):
        channel_map.resolve("c", " Pulmonary trunk")


def test_case_is_part_of_the_literal(tmp_path: Path) -> None:
    env = _write(tmp_path, [_row(raw_name="Arch of Aorta", channel="aorta_arch")])
    channel_map = load_channel_map(env)
    assert channel_map.resolve("c", "Arch of Aorta").channel == "aorta_arch"
    with pytest.raises(UnlistedSegment):
        channel_map.resolve("c", "Arch of aorta")


def test_a_corpus_cannot_borrow_another_corpus_declaration(tmp_path: Path) -> None:
    """`neo` in one corpus is not evidence about `neo` in another: the same word from two
    annotation campaigns can mean two things, and the corpus is the only scope in which
    anybody actually agreed."""
    env = _write(
        tmp_path,
        [_row(corpus_id="lung-nods-100", raw_name="neo", channel="lung_neoplasm")],
    )
    channel_map = load_channel_map(env)
    assert channel_map.resolve("lung-nods-100", "neo").channel == "lung_neoplasm"
    with pytest.raises(UnlistedSegment):
        channel_map.resolve("some-other-corpus", "neo")


# --------------------------------------------------------------------------------------
# the declaration must not be absent, empty, or ambiguous
# --------------------------------------------------------------------------------------


def test_an_undeclared_deployment_is_a_fault_and_not_a_default() -> None:
    with pytest.raises(DeploymentNotDeclared) as caught:
        load_channel_map({})
    assert caught.value.variable == CHANNEL_MAP_DIR_VAR


def test_an_empty_directory_is_refused_rather_than_read_as_nothing_declared(
    tmp_path: Path,
) -> None:
    """'No channels are declared' and 'this deployment was never configured' have the same
    shape and different meanings. Returning an empty map for the second makes every
    downstream count read zero while the call looks successful."""
    with pytest.raises(DeploymentNotDeclared, match="no \\*.json"):
        load_channel_map({CHANNEL_MAP_DIR_VAR: str(tmp_path)})


def test_one_literal_declared_twice_is_refused(tmp_path: Path) -> None:
    env = _write(
        tmp_path,
        [
            _row(raw_name="neo", channel="lung_neoplasm"),
            _row(raw_name="neo", channel="something_else"),
        ],
    )
    with pytest.raises(DeploymentNotDeclared, match="declared twice"):
        load_channel_map(env)


# --------------------------------------------------------------------------------------
# what each basis costs
# --------------------------------------------------------------------------------------


def test_directory_is_not_a_spellable_basis(tmp_path: Path) -> None:
    """'It was in the hydrothorax folder' is the reasoning behind a corpus_assertion, not a
    basis of its own. As its own basis it would sound factual and demand nothing."""
    env = _write(tmp_path, [_row(basis="directory")])
    with pytest.raises(DeploymentNotDeclared) as caught:
        load_channel_map(env)
    assert "corpus_assertion" in str(caught.value)
    assert "directory" not in BASES


def test_an_inference_must_be_attributed_and_counted(tmp_path: Path) -> None:
    """`corpus_assertion` is spellable precisely so the inference can be seen. What makes
    it safe rather than merely visible is that it costs a name and a sample size."""
    with pytest.raises(DeploymentNotDeclared, match="declared_by"):
        load_channel_map(_write(tmp_path, [_row(basis="corpus_assertion")]))

    with pytest.raises(DeploymentNotDeclared, match="inspected_n"):
        load_channel_map(
            _write(tmp_path, [_row(basis="corpus_assertion", declared_by="A Reader")])
        )


def test_an_inference_can_never_be_adjudicated(tmp_path: Path) -> None:
    """Adjudication means a reader looked at images. This basis means nobody did, so the
    combination is not a stricter claim -- it is a false one."""
    env = _write(
        tmp_path,
        [
            _row(
                basis="corpus_assertion",
                declared_by="A Reader",
                inspected_n=5,
                total_n=218,
                status="adjudicated",
            )
        ],
    )
    with pytest.raises(DeploymentNotDeclared, match="cannot be 'adjudicated'"):
        load_channel_map(env)


def test_an_inferred_channel_is_structurally_unserveable(tmp_path: Path) -> None:
    """Not a policy that something could later override -- the row produces no serveable
    channel, so there is nothing for a policy to be lenient about."""
    env = _write(
        tmp_path,
        [
            _row(
                raw_name="Segment_1",
                channel="pleural_effusion",
                basis="corpus_assertion",
                declared_by="A Reader",
                inspected_n=5,
                total_n=218,
                status="provisional",
            )
        ],
    )
    channel_map = load_channel_map(env)
    assert channel_map.serveable("pleural_effusion") is False
    assert channel_map.status_of("pleural_effusion") == "provisional"


def test_a_channel_is_as_settled_as_its_weakest_row(tmp_path: Path) -> None:
    """One inferred row folded into an otherwise adjudicated channel makes the whole
    channel provisional: the model cannot tell which of its training cases came from
    which row, so neither can a report about it."""
    env = _write(
        tmp_path,
        [
            _row(raw_name="a", channel="ch", basis="reader_adjudication",
                 declared_by="R", status="adjudicated"),
            _row(raw_name="b", channel="ch", basis="corpus_assertion",
                 declared_by="R", inspected_n=1, total_n=10, status="provisional"),
        ],
    )
    channel_map = load_channel_map(env)
    assert channel_map.status_of("ch") == "provisional"
    assert channel_map.serveable("ch") is False


def test_an_unassigned_segment_must_be_opaque(tmp_path: Path) -> None:
    """A row with no channel is a segment nobody has assigned a meaning to. Calling it
    'provisional' would imply a meaning exists and is merely unconfirmed."""
    env = _write(tmp_path, [_row(raw_name="Segment_1", channel=None, status="provisional")])
    with pytest.raises(DeploymentNotDeclared, match="only honest status"):
        load_channel_map(env)
    ok = load_channel_map(
        _write(tmp_path, [_row(raw_name="Segment_1", channel=None, status="opaque")])
    )
    assert ok.channels() == ()


# --------------------------------------------------------------------------------------
# folds keep their pre-image
# --------------------------------------------------------------------------------------


def test_a_variant_must_name_what_it_folds_onto(tmp_path: Path) -> None:
    env = _write(
        tmp_path,
        [_row(raw_name="Pulmonaty trunk", channel="pulmonary_trunk",
              basis="typographic_variant", declared_by="R")],
    )
    with pytest.raises(DeploymentNotDeclared, match="folds_onto"):
        load_channel_map(env)


def test_a_fold_target_must_exist_and_not_itself_be_a_fold(tmp_path: Path) -> None:
    """A chain of folds is a rename nobody reviewed as a whole."""
    variant = _row(
        raw_name="Pulmonaty trunk", channel="pulmonary_trunk",
        basis="typographic_variant", declared_by="R", folds_onto="Pulmorary trunk",
    )
    chained = _row(
        raw_name="Pulmorary trunk", channel="pulmonary_trunk",
        basis="typographic_variant", declared_by="R", folds_onto="Pulmonary trunk",
    )
    canonical = _row(raw_name="Pulmonary trunk", channel="pulmonary_trunk")
    with pytest.raises(DeploymentNotDeclared, match="itself a variant"):
        load_channel_map(_write(tmp_path, [variant, chained, canonical]))


def test_a_fold_cannot_change_the_channel(tmp_path: Path) -> None:
    env = _write(
        tmp_path,
        [
            _row(raw_name="Pulmonary trunk", channel="pulmonary_trunk"),
            _row(raw_name="Pulmonaty trunk", channel="aorta_arch",
                 basis="typographic_variant", declared_by="R", folds_onto="Pulmonary trunk"),
        ],
    )
    with pytest.raises(DeploymentNotDeclared, match="resolves to"):
        load_channel_map(env)


def test_the_measured_pulmonary_trunk_spellings_resolve_once_declared(tmp_path: Path) -> None:
    """The real case: five spellings, one structure, 92 cases -- but only if a person says
    so. The pre-image survives per row, so a reviewer can disagree with exactly the six
    variant cases rather than with the merge."""
    rows = [_row(corpus_id="medialisium", raw_name="Pulmonary trunk",
                 channel="pulmonary_trunk", observed_count=83)]
    for spelling, count in (("Pulmonaty trunk", 5), ("Pulmonary Trunk", 2),
                            ("Pulmorary trunk", 1), ("Pulmonary trunk ", 1)):
        rows.append(_row(corpus_id="medialisium", raw_name=spelling, observed_count=count,
                         channel="pulmonary_trunk", basis="typographic_variant",
                         declared_by="Gleb", folds_onto="Pulmonary trunk"))
    channel_map = load_channel_map(_write(tmp_path, rows))
    assert len(channel_map.rows_for_channel("pulmonary_trunk")) == 5
    assert sum(r.observed_count for r in channel_map.rows_for_channel("pulmonary_trunk")) == 92
    for spelling in ("Pulmonaty trunk", "Pulmonary trunk ", "Pulmonary Trunk"):
        row = channel_map.resolve("medialisium", spelling)
        assert row.channel == "pulmonary_trunk"
        assert row.declared_by == "Gleb", "the fold must carry who decided it"
