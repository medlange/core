# Cookbook: Core deployment profiles

Three proven recipes — each run against the live stack (2026-10-03); the outputs
are genuine. Assumes the compose stack (`docker compose -f medos/deploy/compose/docker-compose.yml up -d`)
and the LCTSC dataset at `F:/WorkSpace/PulmoAI/TCIA`.

## Recipe 1. Local profile

`local.yaml` — a list of studies, a synchronous run, the result to stdout.

```bash
python -m medos.sdk run --profile medos/examples/profiles/local.yaml
```

Output (genuine, LCTSC-Test-S1-104, 131 instances):

```json
{"study": "1.3.6.1.4.1.14519.5.2.1.7014.4598.829677454205016768063779242553", "segments": 1, "measurements": 0, "stored": []}
```

- `card: selftest` — the SDK's smoke-test card; for a real model, point it at the directory
  with the trainer's `modelcard.json` (a relative path is resolved against the profile file).
- Add a `writer: {kind: platform}` section — SEG/SR go to the archive (path C1b,
  verified e2e on S1-104: the study's modalities become `CT·SEG·SR`).
- A refusal of one study — a JSON line `{"study": ..., "refused": ...}` — and
  **the batch continues**; the exit code is non-zero if there was at least one refusal.

## Recipe 2. External profile in the MosMed AI style

`mosmed.yaml` — a task from Kafka, a result to Kafka, the external system's dictionary.

Preparation (once; the topics are shared, and old history with someone else's dictionary
means a `CodecError` with a diagnosis, not silence):

```bash
# 1) Bring the bus up if it is not up yet: the compose override adds kafka
#    (in this stack it is already in compose: apache/kafka on 127.0.0.1:39092).
# 2) Advance the consumer group to the end of the KAFKA-MESSAGE topic (otherwise a new
#    group starts at earliest and trips over messages from past experiments).
```

Publishing a task and running (`--once` — one message and exit; without the flag — the
worker's endless loop):

```bash
python - <<'EOF'
from confluent_kafka import Producer
p = Producer({"bootstrap.servers": "127.0.0.1:39092"})
p.produce("KAFKA-MESSAGE", b'{"messageId": "c4-cookbook-0001", "studyInstanceUid": "1.3.6.1.4.1.14519.5.2.1.7014.4598.346635067461584156068474273548"}')
p.flush()
EOF
python -m medos.sdk run --profile medos/examples/profiles/mosmed.yaml --once
```

In `DICOMREPORTNOTIFY` (genuine output of the 2026-10-03 run):

```json
{"type": "DICOMREPORTNOTIFY", "studyIUID": "1.3.6.1.4.1.14519.5.2.1.7014.4598.346635067461584156068474273548", "taskId": "c4-cookbook-0001", "aiResult": true}
```

Dictionary rules that will bite exactly once:

- `inbound.fields` maps the **external payload key → the event's canonical field**
  (`studyInstanceUid: study_uid`), not the other way round. A `CodecError` names what is missing.
- An `outbound.template` without placeholders sends out a message WITHOUT the study's
  identifiers — the external system will not know whom it is about. Placeholders: `{study_uid}`,
  `{task_id}`, `{ai_result}`, `{metrics...}` (see `medos.sdk.runtime._event_document`).
- Secrets — via `token_env`, not literals.

## Recipe 3. From a trained model to serving

Trainer card → Triton model repository in one command (G-C2, path C2):

```bash
python medos/tools/deploy_model.py --modelcard /path/to/run --repo ./model-repo
docker compose --profile inference up -d   # Triton mounts the repository at /models
```

Verified on `artifacts/models/control-585` (`medos.chest-multipathology@0.1.0`):
a real Triton 2.51 accepted the repository/config/manifest; live inference of the artifact
is bit-identical to the local ONNX Runtime. **Last-step blocker:** the `onnxruntime`
backend for Triton ships only inside the `nvcr.io/nvidia/tritonserver` image
(on this host, 403 without an NGC login) — after `docker login nvcr.io` the `inference`
profile comes up without a single code change. Evidence: `e2e-staging/g-c2/EVIDENCE.md`.

## Debugging

| Symptom | Cause | Where to look |
|---|---|---|
| `refused: profile: ...` | profile violates the closed schema | the refusal text, in the profile's vocabulary |
| `CodecError: payload carries no '...'` | the external payload lacks a mapping key | `inbound.fields` |
| 401 on POST /dicomweb | the uploader credential is not set | `MEDOS_GATEWAY_UPLOADER_KEY` |
| `unable to find backend library 'onnxruntime'` | no nvcr access | Recipe 3 |
