# Medlange — дорожная карта доработки

> Зафиксировано 2026-10-02 по итогам пользовательского E2E и развилки продуктов.
> Umbrella: **Medlange**. Продукты: **Core** (SDK, `medos.sdk`), **Trainer**
 (nnU-Net-аналог), **Viewer** (DICOMweb-фреймворк).

## Цель

- **Medlange Core — полноценный SDK**: карточка модели → пайплайн → адаптеры
  (PACS / шина / inference) → постобработка → результаты в архиве. Устанавливается
  `pip install medos`, конфигурируется профилем развёртывания, работает в локальном и
  внешнем (bus-driven) режимах.
- **Medlange Viewer — полноценный настраиваемый фреймворк в классе OHIF**: модульное
  ядро, документированный API расширений (панели, инструменты, действия, протоколы),
  конфигурация без пересборки, брендинг, i18n. Добавление функционала разработчиком —
  через стабильные точки расширения, а не правкой ядра.

## Фаза U — UI/UX (первым делом)

- **U1. Сборка из коробки.** Ловушка `MEDOS_API_VIEWER_AUTHORIZATION` (сырой ключ молча
  даёт 401; нужен префикс `Bearer `) — либо принимать обе формы, либо отказ с
  диагнозом. Цель: `docker compose up` → один сценарий `medos doctor`-уровня, который
  сам говорит, что настроить, + опциональный сид демо-исследования.
- **U2. Навигация.** Сегодня анализ живёт на отдельной странице (`/medicalos/standalone/`),
  переход «Open the viewer» открывается в той же вкладке и теряет контекст исследования.
  Цель: единая оболочка — viewer и AI-панель как одна среда; ссылки открываются с
  сохранением контекста (study в URL); breadcrumbs.
- **U3. AI-поток внутри просмотра.** Кнопка «Анализ» на экране исследования (не на
  отдельной странице), выбор capability, индикатор прогресса, авто-перезагрузка SEG/SR
  наложением по готовности. ✅ 2026-10-03 (G-U3; заодно закрыт closure зависимостей
  capability через `GET /api/v1/capabilities` и кэш серий при перезагрузке)
- **U4. Загрузка DICOM через вебморду.** ✅ 2026-10-03 (G-U4): кнопка «Загрузить DICOM»
  + drag-drop в ворклисте → STOW-RS `{dicomweb}/studies` (multipart/related руками,
  FailedSOPSequence показывается читателю); live: LCTSC-Test-S1-201 из PulmoAI
  (118 файлов) → 200 → исследование в ворклисте. Креденшел выбирается по методу
  nginx-мапой: чтения — read-only viewer key, POST — uploader key (study.write),
  не задан — 401 (решение развёртывания). Запрошено пользователем 2026-10-03.

## Фаза V — Viewer как фреймворк

- **V1. API расширений.** Документированные точки: панель, инструмент тулбара,
  действие, протокол раскладки; стабильный JS-API поверх `core/state`; пример плагина.
- **V2. Конфигурация без сборки.** `viewer.config` (включённые панели/модули, тема,
  брендинг, роутинг) — расширение нынешних `presets.json` / `protocols.json` /
  `build.json`. ✅ 2026-10-03 (G-V2)
- **V3. Документация разработчика.** ✅ 2026-10-03: `docs/getting-started.md`
  («панель за 30 строк», шаги 1–6), `docs/plugin-template.md` (производственный
  шаблон с гейтами в комментариях), `docs/testing.md` (устройство сьюта, 5 правил,
  скелет теста); `tests/test_plugin_template.py` прогоняет гейты по коду из доков —
  гайд не может протухнуть незаметно; dogfood: панель из гайда смонтирована в
  браузере (правый рельс, «Серии») и пройдена сьютом, затем ревертнута.

## Фаза C — Core как полноценный SDK

- **C1. Замыкание результатов.** `Pipeline(..., store=...)` → SEG/SR через единый
  writer (`medos.writer`, ленивый адаптерный импорт) → `PacsAdapter.store`. External-режим
  тогда сам кладёт результаты в архив.
- **C2. Сервинг моделей.** Карточка → staging в Triton model repository; связка с
  `ConversionRun`; нормативный путь «обученная модель тренера на Triton». ✅ 2026-10-03
  (G-C2; блокер последнего шага — nvcr.io 403, зафиксирован в e2e-staging/g-c2)
- **C3. Профили развёртывания.** YAML-профиль адаптеров + `python -m medos.sdk run
  --profile local|mosmed`; публикация `medos` в PyPI. ✅ 2026-10-03 (G-C3; PyPI — ещё нет)
- **C4. Документация + examples.** ✅ 2026-10-03: cookbook
  `medos/examples/profiles/README.md` — три живых рецепта (local → JSON-результат;
  mosmed/Kafka: KAFKA-MESSAGE → `python -m medos.sdk run --once` → DICOMREPORTNOTIFY
  с подлинным выводом; карточка→Triton) + таблица отладки; гейты в
  `tests/unit/test_cookbook.py`; попутно исправлено направление `inbound.fields`
  (внешний ключ → каноническое поле) в mosmed-профиле.
- **C5. Живучесть (из E2E 2026-10-02).** Исправлено: QIDO DICOM JSON Model в PACS-адаптере.
  ✅ 2026-10-04: дефолтный селектор pipeline — CT-only, каждое исключение записано
  (`PipelineResult.exclusions`: series_uid, modality, причина — derived/не-CT/кастомный
  селектор); отказы pipeline — словарь (`PipelineError.code/study_uid/as_dict()`:
  `no_eligible_series`, `store_refused`), CLI печатает словарь; профильный
  `image_only` — явный opt-in, не тихий дефолт.

## Всплывшее по ходу (дефекты и долги)

- **OHIF-расширение** (`medos/web/ohif-extension`) шлёт одиночный `target` — выбор
  `emphysema_laa` там всё ещё падает; нужен тот же descriptor-call, что в нативном
  viewer'е (core).
- **Дефект трейсера:** `instance_norm` в shipped-бандлах запечён с `train=True`
  (конвертации в ONNX верны, флаг достаётся исходному трейсу; влияет на сервинг-числа).
  Завести в trainer как баг-пункт.
- **Triton-разблокировка:** `docker login nvcr.io` (или mirror образа
  `nvcr.io/nvidia/tritonserver`) — после чего профиль `inference` поднимается без кода.

## Фаза T-vanilla — Trainer без MONAI и nnU-Net (решение владельца 2026-10-04)

Трейнер переписывается на собственный стек (чистый PyTorch): цель — самостоятельный
фреймворк, к которому приходят сами по себе, а не производная чужих. Основание —
аудит `docs/audits/trainer-2026-10-04.md`. Шаги:

- **T1. Сети с нуля:** собственный 3D UNet (stem-strides из плана, instance norm,
  deep supervision) вместо обёрток над MONAI-архитектурами; overlays больше не
  «написаны, но не прибиты» — это единственный путь.
- **T2. Данные с нуля:** загрузчик кейсов (NIfTI/нативный формат), weighted patch
  sampling, аугментации (mirror/rotate/scale/intensity) — свои, без nnU-Net pipeline.
- **T3. Обучение:** свой цикл (masked region loss сохраняется — фирменная
  подсистема), LR-плато, чекпоинты, детерминизм (существующий environment/stamp
  переезжают как есть).
- **T4. Inference с нуля:** sliding window с гауссовским блендингом — сразу закрывает
  и внешний predict CLI, и известный разрыв kserve_v2 в core (нет обратного
  маппинга patch→source).
- **T5. Планирование:** fingerprint→plan (патч/спейсинг/батч по VRAM) — своя логика
  вместо nnU-Net plans.
- **T6. Автономный вход:** генератор run-dir из nnU-Net-структуры датасета +
  `trainer/examples/` — сторонний исследователь доходит до модели без платформы.
- **T7. Срез старого:** nnunetv2/monai выводятся из requirements и импортов;
  README/CI/доки приводятся к реальности (4 модуля/54 теста → факт).

Порядок: T1→T2→T3 (вертикальный срез на синтетике, ✅ 2026-10-04) → T4 ✅ (пуш
7c296a3, 2026-10-05: sliding window + served-twin + predict CLI) → T5 ✅ (пуш
8c0209d: fingerprint→plan с reasoned-решениями) → T6 ✅ (пуш 99d68af+2e7c2ee:
vanilla-plan/vanilla-fit/vanilla-import-nnunet + examples/toy_pipeline.py) →
T7 ✅ (пуш bb6d6ac: −14 373 строки, nnunetv2/monai/medos.sdk выведены из
трейнера целиком; сьюты: trainer 127 тестов CPU, монорепо 1542 passed).
Платформенный run-dir контракт (plan/fit/execute через medos.sdk) сознательно
срезан — интеграция vanilla-бэкенда с core (autoconfig-маппинг, modelcard) —
отдельная фаза после C-next, если владелец решит возвращать платформенный
путь обучения.

- **T8 (предложение владельца 2026-10-04): Triton-ядра** (OpenAI Triton, GPU) —
  опциональный ускоритель архитектур: слитые свёртка+norm, fused softmax-Dice.
  Эталоном остаётся чистый PyTorch-путь: CPU и машины без GPU обязаны обучать
  и инференсить без Triton; ядра — за детектом возможностей и флагом плана.
  Внимание на омоним: это НЕ NVIDIA Triton Inference Server (тот — в core/C2).

## Фаза V-next — Viewer как платформа (по аудиту docs/audits/viewer-2026-10-04.md)

1. OVERLAY: реализовать потребителя или удалить вид из реестра.
2. Сетевой seam: экспорт сконфигурированного DicomWebClient (фасад в src/core),
   проброс authHeaderProvider из viewer-config.js — secured PACS без proxy-инъекции.
3. Манифест плагинов (viewer.plugins.js) — регистрация без правки app.js;
   гейт на коллизии клавиш инструментов.
4. Починить docs/extensions.md (реальный TOOL API) + API-референс (state, вклады,
   ctx, CSS-токены, схемы JSON).
5. Честная история про transfer syntax (uncompressed-only в README/getting-started;
   опциональный WASM-декодек за флагом — отдельным решением).

## Фаза C-next — Core как production SDK (по аудиту docs/audits/core-2026-10-04.md)

1. Живучесть bus: ExternalWorker не коммитит offset при CodecError/падении без
   error-топика; backoff в run_forever; poison → error-топик или DLQ.
2. Идемпотентность записи: провести expected_idempotency_key до PlatformWriter
   (сейчас всегда None — дубли SEG при повторном прогоне).
3. Карточка 1.1: трейнер пишет structure/concept_key; публичная JSON-схема
   карточки в medos/schemas/; верификация digest весов в ModelCard.load.
4. Реальный kserve_v2-путь: клиентский sliding window + обратный маппинг в source
   grid (трейнер T4 даёт эталон реализации); e2e против Triton-репозитория из
   deploy_model.
5. Многомодельность (маршрутизация по model_id заявки или отказ при mismatch),
   наблюдаемость (logging/метрики), публикация в PyPI, SDK-only упаковка.

## Миграция в медленж-репозитории

Статус 2026-10-04: организация `github.com/medlange` собрана — созданы и запушены
`medlange/{core,trainer,viewer}` (public, `main`, автор коммитов ATMZR) и профиль
`medlange/.github`; чистые клоны проверены (viewer: 252 теста; core/trainer: импорты).
Аудиты продуктов зафиксированы в `docs/audits/`. Монорепо `MedOS` остаётся
источником истины для разработки; синк в сплиты → пуш — рабочий цикл до разбора
припаркованной работы peer-сессии.

## Цели (измеримые, проверяемые)

- **G-U3 (главная боль пользователя 2026-10-03):** радиолог открывает исследование во
  вьювере и запускает анализ, не покидая просмотрщика: кнопка «Анализ» → диалог с
  выбором модели из конфигурации → прогресс → результат наложением. Срок: 2026-10.
- **G-V1:** сторонний разработчик добавляет панель/действие одним модулем + одной
  регистрацией, без правки `app.js`; пример плагина и документация ≤30 строк на
  контрибуцию. Срок: 2026-10.
- **G-C1b ✅ (2026-10-03):** `Pipeline(writer=)` кладёт SEG/SR в архив через единый `medos.writer`
  (схема карточки 1.1 с кодами понятий); e2e внешнего режима без ручных шагов.
- **G-U1 ✅** сырой ключ креденшела работает из коробки (проверено 200).
- **G-V2 ✅ (2026-10-03):** `viewer.config.json` — панели/тема/брендинг/роутинг
  (`?study=` deep link) без пересборки; дефолт шипуется в дереве, деплой монтирует
  поверх (Medlange-палитра живьём в браузере).
- **G-C2 ✅ (2026-10-03, с зафиксированным внешним блокером последнего шага):**  `tools/deploy_model.py --modelcard <run> --repo <repo>` — карточка тренера
  (`medlange.modelcard/1`) → Triton model repository одной командой: ONNX-экспорт
  (`packaging.onnx_bytes`), digest-проверка, гейты MOS-OPS-071-075 по построению,
  warmup из golden-фикстуры. Прогон на обученной модели (control-585): реальный
  Triton 2.51 принял repository/config/manifest, живой инференс артефакта бит-идентичен
  локальному ORT. Блокер: бэкенд `onnxruntime` для Triton живёт только внутри образа
  nvcr.io (403 без NGC-логина) — `unable to find backend library for backend
  'onnxruntime'`. Evidence: `D:/PycharmProjects/e2e-staging/g-c2/EVIDENCE.md`.
- **G-C3 ✅ (2026-10-03):** `python -m medos.sdk run --profile <yaml>` — YAML-профиль
  (закрытая схема: card/pacs/inference/writer/mode, секреты из env, отказы словарём
  профиля) + CLI с batch-дисциплиной; профили `medos/examples/profiles/{local,mosmed}.yaml`;
  e2e на живом стеке: LCTSC S1-104 из `F:/WorkSpace/PulmoAI` → 1 сегмент, exit 0.
- **G-U4 ✅ (2026-10-03):** загрузка DICOM через UI viewer'а: S1-201 (118 файлов из
  PulmoAI) → STOW-RS → 200 → ворклист 23→24; креденшел по методу (чтения read-only,
  POST — uploader); 8 gate-тестов, viewer-сьют 244 зелёных.

## Порядок

U1 → C1 → V1 → (U2, U3) → V2 → C2 → C3 → U4 → V3 → C4 → C5 — ✅ всё закрыто
(2026-10-03/04). Дальше: долги (OHIF-closure, instance_norm в трейсере, nvcr-login
для Triton, PyPI-публикация medos) — по мере касания их области; следующая фаза
планирования — за пользователем.

## Вопрос «сложнее ли, чем в OHIF?» — ответ

Внутри репозитория viewer пишется проще, чем OHIF (ванильные ES-модули, нет сборки,
217 исполняемых архитектурных тестов). Но у OHIF есть то, чего у viewer нет: стабильный
сторонний extension API (modes/extensions) и экосистема. Разрыв закрывает Фаза V —
это и есть «фреймворк как OHIF».
