# SPDX-License-Identifier: Apache-2.0
"""The deployment profile: one YAML file that turns the SDK into a running pipeline.

A profile is DATA, and the schema is CLOSED. It says where studies come from (PACS),
where the model runs (inference), what happens to results (writer), and what triggers
the work (local list vs external bus) — the four seams `medos.sdk.pipeline` already has,
named in one place an operator can edit without reading Python.

    version: 1
    card: /srv/models/lung-segmentation            # a modelcard.json directory
    pacs:   {kind: dicomweb, base_url: "...", token_env: MEDOS_PACS_TOKEN}
    inference: {kind: kserve_v2, url: "http://127.0.0.1:8000"}
    writer: {kind: platform}                       # optional; absent = results not stored
    mode: local
    local: {studies: ["1.2.276..."]}

`mode: external` replaces `local:` with `external:` (bus + inbound/outbound/error
mappings in the EXTERNAL system's vocabulary — the same shapes `medos.sdk.adapters.bus`
already speaks).

WHY A LOADER MODULE AND NOT CONSTRUCTOR FLAGS. The two modes used to be assembled by
hand in every e2e script (see `medos/sdk/README.md`'s history): a dozen constructor
calls, copy-pasted between the local harness and the mosmed example, drifting one
parameter at a time. The profile is the deduplication: one normaliser, one CLI
(`python -m medos.sdk run --profile ...`), and the e2e scripts become flags, not code.

Errors are REFUSALS with the profile's own vocabulary, because the reader of the error
is the operator who wrote the file: `profile: pacs.kind is 'http'`, not
`KeyError: 'base_url'`.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from medos.sdk.adapters.bus import MessageMapping
from medos.sdk.adapters.inference import EmbeddedInference, KServeV2Inference
from medos.sdk.adapters.pacs import DicomWebPacs
from medos.sdk.adapters.results import PlatformWriter
from medos.sdk.modelcard import ModelCard
from medos.sdk.pipeline import Pipeline
from medos.sdk.runtime import ExternalWorker, LocalWorker

__all__ = ["ProfileError", "Profile", "load_profile", "build_pipeline", "build_worker"]

_TOP_KEYS = (
    "version", "card", "pacs", "inference", "select_series", "writer",
    "mode", "local", "external",
)


class ProfileError(RuntimeError):
    """The profile is absent, malformed, or names something this SDK cannot build.

    A refusal, not a traceback: the reader is the operator who wrote the file.
    """


def _req(table: Mapping[str, Any], key: str, where: str) -> Any:
    value = table.get(key)
    if value in (None, "", [], {}):
        raise ProfileError(f"profile: {where}.{key} is missing or empty")
    return value


def _reject_unknown(table: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = [k for k in table if k not in allowed]
    if unknown:
        raise ProfileError(
            f"profile: {where} carries unknown key(s) {unknown}; allowed: {list(allowed)}"
        )


@dataclass(frozen=True)
class Profile:
    """The normalised profile. Members are the built objects, not the raw YAML — after
    `load_profile` the CLI never touches the document again."""

    card: ModelCard
    pacs: DicomWebPacs
    inference: Any
    writer: Any
    mode: str
    local_studies: tuple[str, ...]
    external: Mapping[str, Any] | None
    select_kind: str
    raw: Mapping[str, Any]


def _load_card(card_spec: Any, profile_dir: Path) -> ModelCard:
    """`card:` is either a directory holding modelcard.json, or the string 'selftest'
    for the SDK's built-in smoke card (the one `medos.sdk.fixtures` documents)."""
    if card_spec == "selftest":
        from medos.sdk.fixtures import selftest_spec_document
        from medos.sdk.modelcard import document_for

        spec_document = selftest_spec_document()
        root = Path(tempfile.mkdtemp(prefix="medos-sdk-selftest-card-"))
        document = document_for(
            model_id=spec_document["model_id"],
            model_version=spec_document["model_version"],
            spec_document=spec_document,
            weights={
                "path": ".",
                "weights_file": "inprocess",
                "format": "inprocess",
                "digest": "sha256:" + hashlib.sha256(b"selftest").hexdigest(),
            },
            frameworks={"numpy": "inprocess"},
            outputs=({"kind": "segmentation", "value": 1, "name": "selftest"},),
            stamp={"producer": "medos.sdk.profiles"},
        )
        (root / "modelcard.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
        return ModelCard.load(root)
    if isinstance(card_spec, str):
        # Relative card paths resolve against the profile's own directory, which is
        # where an operator naturally puts the model next to the file that names it.
        card_dir = (profile_dir / card_spec).resolve()
        if not card_dir.is_dir():
            raise ProfileError(f"profile: card directory {card_dir} does not exist")
        try:
            return ModelCard.load(card_dir)
        except Exception as exc:  # ModelCardError carries the specific refusal
            raise ProfileError(f"profile: card: {exc}") from exc
    raise ProfileError("profile: card must be a directory path or the string 'selftest'")


def _build_pacs(table: Mapping[str, Any]) -> DicomWebPacs:
    _reject_unknown(table, ("kind", "base_url", "token", "token_env", "timeout_s"), "pacs")
    if table.get("kind", "dicomweb") != "dicomweb":
        raise ProfileError(
            f"profile: pacs.kind is {table.get('kind')!r}; only 'dicomweb' is served"
        )
    base_url = str(_req(table, "base_url", "pacs"))
    token = table.get("token")
    if token is None and table.get("token_env"):
        import os

        token = os.environ.get(str(table["token_env"]), "")
    timeout = float(table.get("timeout_s", 60.0))
    return DicomWebPacs(base_url, token=str(token or ""), timeout_s=timeout)


def _build_inference(table: Mapping[str, Any], profile_dir: Path) -> Any:
    _reject_unknown(
        table, ("kind", "url", "model_version", "timeout_s", "script", "callable"),
        "inference",
    )
    kind = _req(table, "kind", "inference")
    if kind == "kserve_v2":
        return KServeV2Inference(
            url=str(_req(table, "url", "inference")),
            timeout_s=float(table.get("timeout_s", 300.0)),
            model_version=table.get("model_version"),
        )
    if kind == "embedded":
        # Embedded inference is the operator's own code, named BY PATH — the same
        # explicit shape as the trainer's provider modules: the SDK loads what the
        # profile points at, and nothing else.
        import importlib.util

        script = (profile_dir / str(_req(table, "script", "inference"))).resolve()
        callable_name = str(table.get("callable", "run"))
        if not script.is_file():
            raise ProfileError(f"profile: inference.script {script} does not exist")
        spec = importlib.util.spec_from_file_location("medos_sdk_embedded", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, callable_name, None)
        if not callable(fn):
            raise ProfileError(
                f"profile: inference.script has no callable {callable_name!r}"
            )
        return EmbeddedInference(fn)
    raise ProfileError(f"profile: inference.kind is {kind!r}; one of 'kserve_v2', 'embedded'")


def _build_writer(table: Mapping[str, Any] | None) -> Any:
    if table is None:
        return None
    _reject_unknown(
        table, ("kind", "tenant_id", "clinical_use_mode", "service_id", "service_version"),
        "writer",
    )
    if table.get("kind", "platform") != "platform":
        raise ProfileError(
            f"profile: writer.kind is {table.get('kind')!r}; only 'platform' is served"
        )
    return PlatformWriter(
        tenant_id=str(table.get("tenant_id", "00000000-0000-0000-0000-000000000000")),
        clinical_use_mode=str(table.get("clinical_use_mode", "RESEARCH_ONLY")),
        service_id=str(table.get("service_id", "medos.sdk")),
        service_version=str(table.get("service_version", "0.1.0")),
    )


def _selector(kind: str):
    """The named series selectors, beside the pipeline's CT-only default (C5).

    `image_only` is an EXPLICIT opt-in for the permissive selector — since C5 the
    pipeline default is CT-only, so "image_only" must not quietly degrade into it.
    """
    if kind in ("image_only", ""):
        from medos.sdk.pipeline import _image_series_only  # noqa: PLC0415

        return _image_series_only
    if kind == "ct_only":
        return None  # the pipeline default; None keeps the default's exclusions
    raise ProfileError(f"profile: select_series is {kind!r}; one of 'image_only', 'ct_only'")


def load_profile(path: str | Path) -> Profile:
    profile_path = Path(path)
    if not profile_path.is_file():
        raise ProfileError(f"profile: {profile_path} does not exist")
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"profile: {profile_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError("profile: the document must be a mapping at the top level")
    _reject_unknown(raw, _TOP_KEYS, "profile")

    if raw.get("version") != 1:
        raise ProfileError(
            f"profile: version is {raw.get('version')!r}; this reader knows version 1"
        )
    profile_dir = profile_path.resolve().parent

    card = _load_card(_req(raw, "card", "profile"), profile_dir)
    pacs = _build_pacs(_req(raw, "pacs", "profile"))
    inference = _build_inference(_req(raw, "inference", "profile"), profile_dir)
    writer = _build_writer(raw.get("writer"))
    select_kind = str(raw.get("select_series", "ct_only"))

    mode = _req(raw, "mode", "profile")
    local_studies: tuple[str, ...] = ()
    external: Mapping[str, Any] | None = None
    if mode == "local":
        local = _req(raw, "local", "profile")
        _reject_unknown(local, ("studies",), "local")
        studies = _req(local, "studies", "local")
        if not isinstance(studies, list) or not all(isinstance(s, str) for s in studies):
            raise ProfileError("profile: local.studies must be a list of study UIDs")
        local_studies = tuple(studies)
    elif mode == "external":
        external = _req(raw, "external", "profile")
        _reject_unknown(external, ("bus", "inbound", "outbound", "error"), "external")
        for key in ("bus", "inbound", "outbound"):
            _req(external, key, "external")
    else:
        raise ProfileError(f"profile: mode is {mode!r}; one of 'local', 'external'")

    return Profile(
        card=card,
        pacs=pacs,
        inference=inference,
        writer=writer,
        mode=mode,
        local_studies=local_studies,
        external=external,
        select_kind=select_kind,
        raw=raw,
    )


def build_pipeline(profile: Profile) -> Pipeline:
    """The pipeline the profile describes. `select_series=None` is the pipeline's own
    image-only default — the profile says so explicitly so the operator sees it."""
    return Pipeline(
        profile.card,
        pacs=profile.pacs,
        inference=profile.inference,
        writer=profile.writer,
        select_series=_selector(profile.select_kind),
    )


def build_bus(table: Mapping[str, Any]) -> Any:
    kind = _req(table, "kind", "external.bus")
    if kind == "kafka":
        from medos.sdk.adapters.bus import KafkaBus

        _reject_unknown(
            table, ("kind", "bootstrap_servers", "group_id", "poll_timeout_s"),
            "external.bus",
        )
        return KafkaBus(
            str(_req(table, "bootstrap_servers", "external.bus")),
            group_id=str(table.get("group_id", "medos-sdk")),
            poll_timeout_s=float(table.get("poll_timeout_s", 1.0)),
        )
    if kind == "rabbit":
        from medos.sdk.adapters.bus import RabbitBus

        _reject_unknown(table, ("kind", "url", "poll_timeout_s"), "external.bus")
        return RabbitBus(
            str(_req(table, "url", "external.bus")),
            poll_timeout_s=float(table.get("poll_timeout_s", 1.0)),
        )
    raise ProfileError(f"profile: external.bus.kind is {kind!r}; one of 'kafka', 'rabbit'")


def _mapping(table: Mapping[str, Any], where: str) -> MessageMapping:
    _reject_unknown(table, ("topic", "fields", "template"), where)
    return MessageMapping(
        template=dict(table.get("template") or {}),
        fields=dict(table.get("fields") or {}),
    )


def build_worker(profile: Profile):
    """The worker the mode names: local studies through a LocalWorker, or the bus
    through an ExternalWorker with the external system's vocabulary as data."""
    pipeline = build_pipeline(profile)
    if profile.mode == "local":
        return LocalWorker(pipeline)
    assert profile.external is not None
    ext = profile.external
    inbound = _req(ext, "inbound", "external")
    outbound = _req(ext, "outbound", "external")
    error = ext.get("error")
    return ExternalWorker(
        pipeline,
        build_bus(_req(ext, "bus", "external")),
        inbound_topic=str(_req(inbound, "topic", "external.inbound")),
        inbound=_mapping(inbound, "external.inbound"),
        outbound_topic=str(_req(outbound, "topic", "external.outbound")),
        outbound=_mapping(outbound, "external.outbound"),
        error_topic=str(_req(error, "topic", "external.error")) if error else "",
        error_mapping=_mapping(error, "external.error") if error else None,
    )
