# Cookbook: профили развёртывания Core

Три проверенных рецепта — каждый прогнан на живом стеке (2026-10-03), выводы
подлинные. Предполагается compose-стек (`docker compose -f medos/deploy/compose/docker-compose.yml up -d`)
и датасет LCTSC в `F:/WorkSpace/PulmoAI/TCIA`.

## Рецепт 1. Локальный профиль

`local.yaml` — список исследований, прогон синхронно, результат в stdout.

```bash
python -m medos.sdk run --profile medos/examples/profiles/local.yaml
```

Вывод (подлинный, LCTSC-Test-S1-104, 131 инстанс):

```json
{"study": "1.3.6.1.4.1.14519.5.2.1.7014.4598.829677454205016768063779242553", "segments": 1, "measurements": 0, "stored": []}
```

- `card: selftest` — дымовая карточка SDK; для реальной модели укажите каталог с
  `modelcard.json` тренера (относительный путь — относительно файла профиля).
- Добавьте секцию `writer: {kind: platform}` — SEG/SR уйдут в архив (путь C1b,
  проверен e2e на S1-104: модальности исследования становятся `CT·SEG·SR`).
- Отказ одного исследования — JSON-строка `{"study": ..., "refused": ...}` и
  **батч продолжается**; exit code ненулевой, если был хоть один отказ.

## Рецепт 2. Внешний профиль в стиле MosMed AI

`mosmed.yaml` — заявка из Kafka, результат в Kafka, словарь внешней системы.

Подготовка (один раз; топики общие, и старая история с чужим словарём — это
`CodecError` с диагнозом, а не молчание):

```bash
# 1) Поднимите шину, если ещё не поднята: compose-override добавляет kafka
#    (в этом стеке она уже в compose: apache/kafka на 127.0.0.1:39092).
# 2) Примите consumer-group к концу топика KAFKA-MESSAGE (иначе новая группа
#    начнёт с earliest и споткнётся о сообщения прошлых экспериментов).
```

Публикация заявки и прогон (`--once` — одно сообщение и выход; без флага — вечный
цикл воркера):

```bash
python - <<'EOF'
from confluent_kafka import Producer
p = Producer({"bootstrap.servers": "127.0.0.1:39092"})
p.produce("KAFKA-MESSAGE", b'{"messageId": "c4-cookbook-0001", "studyInstanceUid": "1.3.6.1.4.1.14519.5.2.1.7014.4598.346635067461584156068474273548"}')
p.flush()
EOF
python -m medos.sdk run --profile medos/examples/profiles/mosmed.yaml --once
```

В `DICOMREPORTNOTIFY` (подлинный вывод прогона 2026-10-03):

```json
{"type": "DICOMREPORTNOTIFY", "studyIUID": "1.3.6.1.4.1.14519.5.2.1.7014.4598.346635067461584156068474273548", "taskId": "c4-cookbook-0001", "aiResult": true}
```

Правила словаря, которые укусят один раз:

- `inbound.fields` маппит **ключ внешнего payload → каноническое поле события**
  (`studyInstanceUid: study_uid`), не наоборот. `CodecError` называет, чего не хватает.
- `outbound.template` без плейсхолдеров шлёт наружу сообщение БЕЗ идентификаторов
  исследования — внешняя система не поймёт, о ком речь. Плейсхолдеры: `{study_uid}`,
  `{task_id}`, `{ai_result}`, `{metrics...}` (см. `medos.sdk.runtime._event_document`).
- Секреты — через `token_env`, не литералами.

## Рецепт 3. От обученной модели до сервинга

Карточка тренера → Triton model repository одной командой (G-C2, путь C2):

```bash
python medos/tools/deploy_model.py --modelcard /path/to/run --repo ./model-repo
docker compose --profile inference up -d   # Triton монтирует repository на /models
```

Проверено на `artifacts/models/control-585` (`medos.chest-multipathology@0.1.0`):
реальный Triton 2.51 принял repository/config/manifest; живой инференс артефакта
бит-идентичен локальному ONNX Runtime. **Блокер последнего шага:** бэкенд
`onnxruntime` для Triton поставляется только внутри образа `nvcr.io/nvidia/tritonserver`
(на этом хосте 403 без NGC-логина) — после `docker login nvcr.io` профиль `inference`
поднимается без единого изменения кода. Evidence: `e2e-staging/g-c2/EVIDENCE.md`.

## Отладка

| Симптом | Причина | Где смотреть |
|---|---|---|
| `refused: profile: ...` | профиль против закрытой схемы | текст отказа — словарём профиля |
| `CodecError: payload carries no '...'` | внешний payload не несёт ключа маппинга | `inbound.fields` |
| 401 на POST /dicomweb | uploader-креденшел не задан | `MEDOS_GATEWAY_UPLOADER_KEY` |
| `unable to find backend library 'onnxruntime'` | нет nvcr-доступа | Рецепт 3 |
