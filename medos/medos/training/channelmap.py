# SPDX-License-Identifier: Apache-2.0
"""Which output channel a segment belongs to, and on whose authority.

WHY A DECLARATION AND NOT A FUNCTION
-------------------------------------
A corpus assembled from Slicer scenes names its segments in whatever the annotator typed.
Measured across this collection: `neo`, `benign`, `pn`, `vertebrae_body`, three aortic
segments, and `Pulmonary trunk` spelled five ways -- `Pulmonaty trunk`, `Pulmorary trunk`,
`Pulmonary Trunk`, and one with a trailing space. Plus 419 segments left at Slicer's
`Segment_N` default, whose meaning was never written down anywhere.

The tempting thing is a normaliser: casefold, strip, maybe an edit-distance match. This
module refuses to contain one, and the reason is a single example. `Pulmonaty` and
`Pulmonary` differ by one character and are the same structure. `S1` and `S2` differ by one
character and are two different lung segments. No string metric distinguishes those cases,
so a matcher that folds the first will eventually fold the second, silently, into a channel
a model is then trained and evaluated on. The fold has to be a judgement, and a judgement
has to have someone's name on it.

So: every raw string a corpus contains is listed, verbatim, with the channel it resolves to
and the basis for saying so. An unlisted string is a refusal naming the literal -- never a
fallback, never a guess, never a silent drop.

WHY `corpus_assertion` IS SPELLABLE
------------------------------------
The 419 unnamed segments have a meaning that can only be inferred from which folder they
sit in: `NRRD_DATASET_HYDROTHORAX/.../Segment_1` is probably an effusion. An earlier design
made that basis unspellable, so the inference could not be recorded at all.

That is the wrong shape of protection. Making it unspellable does not stop anyone
inferring; it forces whoever is unblocking a pilot to spell it `reader_adjudication`
instead, which converts a visible weak claim into an invisible false one. The requirement
is that a reader can TELL an inference from a measurement -- a visibility requirement, not
a prohibition.

So `corpus_assertion` is spellable and carries its cost in the open:
  * it MUST name a person in `declared_by`; an unattributed assertion is refused,
  * it MUST carry `inspected_n` and `total_n`, so the ratio behind the inference is on the
    record rather than in someone's memory,
  * a channel resting on it is `provisional` at best and can never be `adjudicated` without
    someone actually looking,
  * and `serveable()` is False for it, structurally -- such a channel trains, but it
    resolves to no capability, so it cannot be offered clinically no matter what policy
    anybody writes later.

Training on an unnamed auxiliary task is not the harm. Naming it is.

Spec: MOS-TRAIN-098 (annotation provenance), MOS-UI-149 (propose nothing), MOS-EVID-040.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from medos.config.devmode import refuse_dev_value_outside_dev
from medos.training.seal import DeploymentNotDeclared

__all__ = [
    "CHANNEL_MAP_DIR_VAR",
    "BASES",
    "STATUSES",
    "ChannelRow",
    "ChannelMap",
    "UnlistedSegment",
    "load_channel_map",
]

#: Where the per-corpus channel declarations live. A directory, not a variable holding
#: JSON: these tables are long (16 rows for `medialisium` alone), they are reviewed by
#: people, and they name a deployment's own corpora -- so they belong in files a
#: deployment keeps, on the pattern `MEDOS_PROVENANCE_DIR` already establishes.
CHANNEL_MAP_DIR_VAR: Final[str] = "MEDOS_CHANNEL_MAP_DIR"

#: On what authority a raw segment string resolves to a channel. CLOSED.
#:
#: `directory` is deliberately absent and must stay absent. "It was in the hydrothorax
#: folder" is the reasoning behind a `corpus_assertion`, not a basis of its own -- the
#: difference is that `corpus_assertion` demands a name and a sample size and forfeits
#: serveability, and a bare `directory` would demand nothing while sounding factual.
BASES: Final[tuple[str, ...]] = (
    # The annotator typed this name. The strongest basis available here, and still only
    # as good as the annotator.
    "author_named",
    # A named person declared this spelling to be the same structure as another. Requires
    # `folds_onto` and `declared_by`.
    "typographic_variant",
    # A named person read the corpus's own coded terminology and accepted it.
    "terminology_coded",
    # A reader looked at images and assigned the meaning. The only basis that can reach
    # `adjudicated`.
    "reader_adjudication",
    # The meaning is inferred from which corpus the segment came from. Attributed,
    # counted, and never serveable. See the module docstring.
    "corpus_assertion",
)

#: How far a channel has been established. A channel is only as settled as its weakest row.
STATUSES: Final[tuple[str, ...]] = (
    # Trains as an unnamed auxiliary task. Resolves to no clinical concept and is reported
    # by channel index, not by a name.
    "opaque",
    # Has a name, has a stated basis, has not been adjudicated. Trains; is reported with
    # its basis attached; is not serveable.
    "provisional",
    # A reader established it. Serveable, subject to everything else.
    "adjudicated",
)

_ATTRIBUTED_BASES: Final[frozenset[str]] = frozenset(
    {"typographic_variant", "reader_adjudication", "corpus_assertion"}
)
_UNSERVEABLE_BASES: Final[frozenset[str]] = frozenset({"corpus_assertion"})


class UnlistedSegment(LookupError):
    """A corpus contains a raw segment string the declaration does not list.

    Carries the literal, because the whole value of the refusal is that the operator can
    copy it into the table. Reported with `repr()` so that `'Pulmonary trunk '` is
    visibly different from `'Pulmonary trunk'` -- a trailing space that renders identically
    in every log is exactly the kind of difference this module exists to keep visible.
    """

    def __init__(self, corpus_id: str, raw_name: str, known: Sequence[str]) -> None:
        self.corpus_id = corpus_id
        self.raw_name = raw_name
        super().__init__(
            f"corpus {corpus_id!r} contains the segment name {raw_name!r}, which its "
            f"channel declaration does not list. It resolves to nothing and this is a "
            f"refusal, not a skip: a segment silently dropped is a case that trains with "
            f"one fewer supervised channel than its manifest claims.\n"
            f"  declared for this corpus: {[repr(k) for k in known]}\n"
            f"Add a row for it. Do NOT add a normaliser -- 'Pulmonaty'/'Pulmonary' is edit "
            f"distance 1 and a typo, 'S1'/'S2' is edit distance 1 and two different lung "
            f"segments, and nothing in a string tells those apart."
        )


@dataclass(frozen=True)
class ChannelRow:
    """One raw segment string, and what it means."""

    corpus_id: str
    raw_name: str
    observed_count: int
    channel: str | None
    basis: str
    status: str
    declared_by: str | None = None
    declared_on: str | None = None
    folds_onto: str | None = None
    inspected_n: int | None = None
    total_n: int | None = None
    note: str | None = None

    @property
    def serveable(self) -> bool:
        """Whether a channel resting on this row may be offered as a clinical capability.

        Structural, not advisory: `medos/medos/training/channelmap.py` is read wherever a
        capability concept is resolved, and a row that is not serveable resolves to no
        concept. A later policy cannot override it because there is nothing to override --
        the row simply does not produce the thing a capability is built from.
        """
        return (
            self.channel is not None
            and self.status == "adjudicated"
            and self.basis not in _UNSERVEABLE_BASES
        )


@dataclass(frozen=True)
class ChannelMap:
    """Every corpus's declaration, indexed for exact lookup."""

    rows: tuple[ChannelRow, ...]

    def _for(self, corpus_id: str) -> dict[str, ChannelRow]:
        return {r.raw_name: r for r in self.rows if r.corpus_id == corpus_id}

    def resolve(self, corpus_id: str, raw_name: str) -> ChannelRow:
        """The row for this exact string. NO normalisation of any kind happens here.

        Not `.strip()`, not `.casefold()`, not unicode normalisation. If this function ever
        grows one, `test_channel_map.py::test_an_unlisted_spelling_is_refused_by_its_literal`
        goes green for the wrong reason and the sixteenth spelling of a structure starts
        resolving to the fifteenth without anyone deciding that it should.
        """
        rows = self._for(corpus_id)
        try:
            return rows[raw_name]
        except KeyError:
            raise UnlistedSegment(corpus_id, raw_name, sorted(rows)) from None

    def channels(self) -> tuple[str, ...]:
        """Every declared channel name, in a stable order. Excludes unassigned rows."""
        return tuple(sorted({r.channel for r in self.rows if r.channel}))

    def rows_for_channel(self, channel: str) -> tuple[ChannelRow, ...]:
        return tuple(r for r in self.rows if r.channel == channel)

    def status_of(self, channel: str) -> str:
        """A channel is as settled as its WEAKEST contributing row.

        One `corpus_assertion` row folded into a channel whose other rows were adjudicated
        makes the whole channel provisional, because a model cannot tell which of its
        training cases came from which row.
        """
        rows = self.rows_for_channel(channel)
        if not rows:
            raise KeyError(channel)
        order = {name: i for i, name in enumerate(STATUSES)}
        return min((r.status for r in rows), key=lambda s: order[s])

    def serveable(self, channel: str) -> bool:
        rows = self.rows_for_channel(channel)
        return bool(rows) and all(r.serveable for r in rows)


def _fault(detail: str) -> DeploymentNotDeclared:
    return DeploymentNotDeclared(CHANNEL_MAP_DIR_VAR, detail)


def _row(document: Mapping[str, object], source: Path, index: int) -> ChannelRow:
    where = f"{source.name} row {index}"

    def need(key: str) -> str:
        value = document.get(key)
        if not isinstance(value, str) or not value.strip():
            raise _fault(f"{where}: {key!r} is required and must be a non-empty string")
        return value

    corpus_id = need("corpus_id")
    # `raw_name` is read WITHOUT .strip(). A trailing space is part of the literal the
    # corpus contains, and a declaration that quietly trims it would not match.
    raw_name = document.get("raw_name")
    if not isinstance(raw_name, str) or not raw_name:
        raise _fault(f"{where}: 'raw_name' is required and must be a non-empty string")

    basis = need("basis")
    if basis not in BASES:
        raise _fault(
            f"{where}: basis {basis!r} is not one of {list(BASES)}. If you are reaching "
            f"for 'directory', the value you want is 'corpus_assertion' -- it says the "
            f"same thing and costs what that claim actually costs: a named person, a "
            f"sample size, and no clinical serving."
        )
    status = need("status")
    if status not in STATUSES:
        raise _fault(f"{where}: status {status!r} is not one of {list(STATUSES)}")

    channel = document.get("channel")
    if channel is not None and (not isinstance(channel, str) or not channel.strip()):
        raise _fault(f"{where}: 'channel' must be a non-empty string or null")
    if channel is None and status != "opaque":
        raise _fault(
            f"{where}: has no channel but status {status!r}. A row with no channel is a "
            f"segment nobody has assigned a meaning to; the only honest status for it is "
            f"'opaque'."
        )

    declared_by = document.get("declared_by")
    if basis in _ATTRIBUTED_BASES and not (
        isinstance(declared_by, str) and declared_by.strip()
    ):
        raise _fault(
            f"{where}: basis {basis!r} requires 'declared_by'. This basis records a "
            f"judgement rather than something the file said, and an unattributed "
            f"judgement is indistinguishable from a measurement to everyone downstream."
        )

    if basis == "typographic_variant" and not document.get("folds_onto"):
        raise _fault(
            f"{where}: basis 'typographic_variant' requires 'folds_onto' naming the "
            f"spelling this one folds into, so the pre-image survives and a reviewer can "
            f"disagree with exactly these cases rather than with a merge."
        )

    if basis == "corpus_assertion":
        inspected, total = document.get("inspected_n"), document.get("total_n")
        if not isinstance(inspected, int) or not isinstance(total, int) or total <= 0:
            raise _fault(
                f"{where}: basis 'corpus_assertion' requires integer 'inspected_n' and "
                f"'total_n' (total > 0). The inference may be right; what makes it "
                f"reportable rather than misleading is that the ratio behind it travels "
                f"with it."
            )
        if inspected > total:
            raise _fault(f"{where}: inspected_n {inspected} exceeds total_n {total}")
        if status == "adjudicated":
            raise _fault(
                f"{where}: a 'corpus_assertion' cannot be 'adjudicated'. Adjudication "
                f"means a reader looked at images; this basis means nobody did."
            )

    observed = document.get("observed_count", 0)
    if not isinstance(observed, int) or observed < 0:
        raise _fault(f"{where}: 'observed_count' must be a non-negative integer")

    return ChannelRow(
        corpus_id=corpus_id,
        raw_name=raw_name,
        observed_count=observed,
        channel=channel,
        basis=basis,
        status=status,
        declared_by=str(declared_by) if isinstance(declared_by, str) else None,
        declared_on=str(document["declared_on"]) if document.get("declared_on") else None,
        folds_onto=str(document["folds_onto"]) if document.get("folds_onto") else None,
        inspected_n=(
            document.get("inspected_n")
            if isinstance(document.get("inspected_n"), int)
            else None
        ),
        total_n=document.get("total_n") if isinstance(document.get("total_n"), int) else None,
        note=str(document["note"]) if document.get("note") else None,
    )


def load_channel_map(env: Mapping[str, str] | None = None) -> ChannelMap:
    """Read every declaration. Raises `DeploymentNotDeclared`; never returns a default.

    An empty map is NOT a valid answer and is refused. "No channels are declared" and "this
    deployment has not been configured" are different states with the same shape, and
    returning an empty map for the second makes every downstream count read zero while
    looking successful -- which is the failure this codebase keeps finding.
    """
    source = dict(env if env is not None else os.environ)
    raw = (source.get(CHANNEL_MAP_DIR_VAR) or "").strip()
    # A development default must not reach a deployment that has not declared itself
    # one. First statement after the read, before any interpretation: this makes the
    # loader strictly stricter and leaves its "never returns a default" promise intact.
    refuse_dev_value_outside_dev(CHANNEL_MAP_DIR_VAR, raw, source)

    if not raw:
        raise _fault(
            "no channel declaration directory is set. A multi-channel model needs to know "
            "which raw segment name in which corpus becomes which output channel, and on "
            "whose authority. That cannot be inferred from the data -- 419 segments in "
            "this collection are named 'Segment_N' and one structure is spelled five ways "
            "-- so it is declared. Point it at a directory of *.json channel tables; "
            "medos/tools/ingest/channel_map_stub.py writes a starting one."
        )
    directory = Path(raw)
    if not directory.is_dir():
        raise _fault(f"{directory} is not a directory")

    rows: list[ChannelRow] = []
    seen: dict[tuple[str, str], Path] = {}
    files = sorted(directory.glob("*.json"))
    if not files:
        raise _fault(
            f"{directory} contains no *.json channel table. An empty directory is refused "
            f"rather than read as 'nothing is declared': the two are indistinguishable "
            f"downstream and only one of them is a configuration."
        )
    for path in files:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _fault(f"{path.name}: cannot be read as JSON: {exc}") from exc
        entries = document.get("rows") if isinstance(document, dict) else document
        if not isinstance(entries, list) or not entries:
            raise _fault(f"{path.name}: expected a non-empty 'rows' array")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise _fault(f"{path.name} row {index}: not an object")
            row = _row(entry, path, index)
            key = (row.corpus_id, row.raw_name)
            if key in seen:
                raise _fault(
                    f"{path.name} row {index}: {row.raw_name!r} is declared twice for "
                    f"corpus {row.corpus_id!r} (also in {seen[key].name}). Two rows for "
                    f"one literal means two answers and no rule for choosing."
                )
            seen[key] = path
            rows.append(row)

    _check_folds(rows)
    return ChannelMap(rows=tuple(rows))


def _check_folds(rows: Iterable[ChannelRow]) -> None:
    """A `typographic_variant` must fold onto a spelling that exists and is not itself a
    variant. A chain of folds is a rename nobody reviewed as a whole."""
    by_corpus: dict[str, dict[str, ChannelRow]] = {}
    for row in rows:
        by_corpus.setdefault(row.corpus_id, {})[row.raw_name] = row
    for row in rows:
        if row.basis != "typographic_variant":
            continue
        target = by_corpus.get(row.corpus_id, {}).get(row.folds_onto or "")
        if target is None:
            raise _fault(
                f"{row.corpus_id}: {row.raw_name!r} folds onto {row.folds_onto!r}, which "
                f"is not a declared spelling in that corpus."
            )
        if target.basis == "typographic_variant":
            raise _fault(
                f"{row.corpus_id}: {row.raw_name!r} folds onto {row.folds_onto!r}, which "
                f"is itself a variant. Fold onto the canonical spelling directly, so that "
                f"one review sees the whole merge."
            )
        if target.channel != row.channel:
            raise _fault(
                f"{row.corpus_id}: {row.raw_name!r} resolves to channel {row.channel!r} "
                f"but folds onto {row.folds_onto!r}, which resolves to {target.channel!r}."
            )
