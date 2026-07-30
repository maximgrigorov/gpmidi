# Reference-Guided MIDI Restoration & Enrichment

Статус: согласованный архитектурный план, реализация ещё не начата.

## 1. Цель

Система не должна сочинять power metal с нуля или заменять понравившуюся композицию каноническими паттернами. Исходным музыкальным референсом является готовая генерация Suno, с которой вручную срисован Guitar Pro-файл.

Задача системы:

1. понять, что фактически звучит в исходном аудио;
2. сопоставить аудио и распознанные Suno MIDI с Guitar Pro;
3. обнаружить потерянные, неверные и чрезмерно упрощённые места;
4. предложить или применить патчи к отдельным тактам либо ко всей партии;
5. опционально обогатить исполнение, сохраняя композицию и авторские решения;
6. передать восстановленные партии в существующий стабильный Shreddage-конвертер.

ACE-Step и ACE Studio исключены из scope. Генерация аудиоподложек не требуется.

## 2. Входные данные проекта

Один долговечный Project может содержать:

- Guitar Pro (`.gp`, `.gp5`) — несколько ревизий;
- полный mix WAV/FLAC;
- Suno stems: drums, bass, guitar, vocals и другие доступные;
- распознанные Suno MIDI для mix и/или каждого stem;
- текст песни;
- разметку структуры без времени (`[Verse]`, `[Chorus]`) или с timestamps;
- пользовательские решения по предложенным патчам.

Все большие файлы загружаются формами существующего web UI непосредственно в persistent storage на AILab. Повторно должна загружаться только обновляемая часть, обычно новая ревизия Guitar Pro.

## 3. Источники истины и временные шкалы

### 3.1. Две разные временные шкалы

- **Source-time grid:** плавающие длительности тактов оригинального WAV/Suno.
- **Destination GP grid:** выровненные пользователем такты с единым tempo.

Нельзя переносить абсолютное плавание Suno в итоговый MIDI. Восстановленное событие сначала выражается музыкальной позицией внутри source-такта, затем переносится в соответствующую позицию ровного GP-такта.

### 3.2. Приоритет источников для source-time map

1. Tempo map из распознанных Suno MIDI — основной источник.
2. Downbeats/transients drums stem — проверка фазировки и initial offset.
3. Полный mix — проверка структуры и fallback при отсутствии или повреждении MIDI.
4. Пользовательские timestamps — жёсткие anchors.
5. GP SyncPoint/tempo — вспомогательная информация, не источник времени WAV.

Если tempo maps нескольких Suno MIDI различаются, система строит отчёт и consensus; нельзя молча выбирать первый файл. Начальный offset, pickup, pre-roll и пустота в начале stems проверяются по аудио даже при наличии reference MIDI.

Результат — монотонная карта:

```text
source MIDI tick/bar <-> absolute audio time <-> GP measure/beat/tick
```

Для каждого такта сохраняются source audio interval, GP interval и confidence выравнивания.

## 4. Архитектурный pipeline

```text
GP + mix + stems + Suno MIDI + optional markup
                    |
                    v
          Audio Evidence Layer
  source-time map, structure, energy, transients
                    |
                    v
         GP <-> Audio Reconciliation
 confirmed/missing/extra/simplified events by measure
                    |
                    v
          Source Restoration Engine
   restoration of the existing Suno performance
                    |
                    v
        Optional Enrichment Engine
 selected bars/tracks only, explicit user-controlled mode
                    |
                    v
     Existing Shreddage Performance Engine
```

Существующая музыкальная семантика `gp_to_shreddage.py` остаётся стабильной. Новая подсистема должна быть аддитивной и opt-in.

## 5. Анализ аудио

### 5.1. Полный mix

Используется для:

- tempo/beat/downbeat fallback;
- границ и меток секций;
- поиска повторяющихся частей;
- общей кривой энергии;
- кульминаций и переходов;
- проверки согласованности stems.

Практические кандидаты для измерительного spike:

- Beat This! — beat/downbeat tracking;
- All-In-One Music Structure Analyzer — tempo, beats, downbeats, sections;
- librosa — onsets, chroma, recurrence, energy;
- Essentia — только после отдельной проверки AGPL-лицензирования.

### 5.2. Drums stem

Нужно извлекать как минимум:

- kick;
- snare;
- closed/open hi-hat;
- ride;
- crash;
- tom groups;
- onset time;
- относительную интенсивность;
- confidence.

Кандидаты сравниваются на реальном power-metal stem, а не по README: ADTOF/ADTOF-pytorch, DrumScript и другие проверяемые ADT-модели.

Сырой результат модели не заменяет GP автоматически. Для каждого такта формируется разница GP vs audio evidence. Аналогичные такты повторяющихся секций используются как дополнительное свидетельство.

### 5.3. Bass stem

Начальный кандидат — Basic Pitch плюс GP-constrained matching. Более тяжёлые multi-instrument модели используются только если покажут измеримое преимущество.

Из stem переносятся либо предлагаются:

- подтверждённые атаки;
- повторные ноты, потерянные при ручной транскрипции;
- длительности;
- относительная velocity;
- slides/pitch contour;
- staccato/sustain;
- дополнительные ноты с гармонической проверкой;
- микровременное отношение к kick.

Ошибки octave/harmonic splitting, bleed и объединение повторных нот должны ограничиваться контекстом GP и confidence.

### 5.4. Rhythm guitar

Два независимых режима:

- **Restore from stem:** пропущенные атаки, gallop/reverse gallop, mute lengths, паузы, открытие аккордов, плотность и динамика.
- **Controlled variation:** только по явной опции; вариации повторов и L/R дублей без изменения гармонии и разрушения tightness.

## 6. Пользовательские режимы по тактам

Для каждого инструмента и такта:

- оставить GP;
- восстановить по stem;
- восстановить и применить performance pass;
- осторожно обогатить;
- выбрать предложенный вариант вручную.

Confidence-индикация:

- зелёный — GP и evidence совпадают;
- жёлтый — вероятное упрощение/пропуск;
- красный — существенное расхождение;
- серый — недостаточно данных, автоматически не менять.

Выходы должны сохраняться раздельно:

- Original adapted;
- Source-restored;
- Source-restored + performance;
- Source-restored + conservative enrichment;
- общий Type-1 `_ALL.mid`;
- отчёт точных изменений по тактам.

## 7. Долговечные Projects и storage

HTTP cookie не является владельцем данных. Нужна постоянная сущность Project с `project_id`, manifest и ревизиями.

Пример логической структуры:

```text
Project
|-- source assets
|   |-- mix
|   |-- stems
|   |-- Suno MIDI
|   `-- structure/lyrics
|-- GP revisions
|-- cached analyses
|-- reconciliation revisions
`-- output revisions
```

### 7.1. Content-addressed assets

Каждый загруженный файл идентифицируется SHA-256. Это обеспечивает:

- дедупликацию;
- повторное использование одного asset в нескольких ревизиях;
- точную provenance;
- безопасную cache invalidation;
- отсутствие зависимости от имени файла.

Удаление физического asset разрешено только после удаления всех ссылок и согласно явной retention policy.

### 7.2. Cache keys

Тяжёлые результаты зависят от hash входа, имени и версии модели, checkpoint hash и параметров. GP reconciliation отдельно зависит от GP hash, source-time-map hash и event-analysis hashes.

При загрузке новой GP-ревизии не пересчитываются:

- source-time map;
- structure analysis;
- stem transcription;
- energy/transient analysis.

Пересчитываются:

- GP parse;
- GP/audio reconciliation;
- confidence и candidate patches;
- Shreddage exports;
- отчёты.

## 8. Размещение компонентов

### 8.1. Текущий gpmidi host

Остаётся web/UI и стабильным Shreddage-конвертером. Он не должен хранить полный набор больших WAV: локальный диск ограничен.

### 8.2. AILab

Характеристики:

- `192.168.30.2`;
- RTX 5060 Ti 16 GB;
- 32 GB RAM;
- Intel i5-13400F, 10 cores / 16 threads;
- `/data` используется для моделей и persistent storage.

На AILab размещаются:

- k3s single-node;
- GPU audio-analysis worker;
- project/asset API;
- persistent project storage под отдельным каталогом `/data`, без изменения существующих моделей;
- очередь/статусы jobs;
- health/readiness/metrics;
- позже GitOps deployment через Flux.

Файлы загружаются из web UI в persistent volume AILab через backend API. Для первого этапа достаточно filesystem-backed content-addressed store; S3/MinIO можно добавить позднее без изменения публичного storage API.

## 9. Изоляция и deployment

Предпочтительный вариант:

- k3s вместо minikube;
- host NVIDIA driver + NVIDIA Container Toolkit;
- NVIDIA Kubernetes device plugin;
- namespace `gpmidi-ml`;
- Pod Security `restricted` для application workloads;
- NetworkPolicy, ResourceQuota, LimitRange;
- отдельный StorageClass на `/data/k3s-storage`;
- TLS ingress на `443`;
- Flux GitOps после безопасного bootstrap credentials;
- без SSH-доступа для Hermes;
- при необходимости namespace-scoped kubeconfig без cluster/node/hostPath/privileged прав.

Portainer не используется как основной API, поскольку обычно даёт слишком широкие host-level права. Tekton не нужен на первом этапе; build + registry + Flux проще и лучше изолированы.

## 10. План реализации

### Phase 0 — AILab bootstrap

- k3s;
- GPU runtime/device plugin;
- `/data` StorageClass/PVC;
- ingress TLS 443;
- test deployment;
- GPU Job/Pod smoke;
- PVC persistence smoke;
- namespace/RBAC/security boundaries;
- воспроизводимые scripts/manifests и отчёт.

### Phase 1 — Project and Asset Storage

- Project CRUD;
- asset roles;
- SHA-256 and deduplication;
- GP revisions;
- manifests/provenance;
- cache keys;
- upload/download API;
- UI forms uploading directly to AILab-backed persistent storage.

### Phase 2 — Reference-time vertical slice

- ingest GP + mix + drums/bass stems + Suno MIDI;
- extract and validate source-time map;
- map source measures to GP measures;
- produce per-measure JSON/HTML report without changing MIDI.

### Phase 3 — Transcription spike

- compare 2–3 drum transcribers;
- Basic Pitch baseline for bass;
- measure precision/recall manually on representative sections;
- record GPU time, VRAM, RAM and model licenses;
- select models only from real evidence.

### Phase 4 — Restoration

- candidate patch schema;
- confidence thresholds;
- per-measure approval;
- drums and bass restoration;
- unchanged/default-off regression guarantees;
- independent MIDI verification.

### Phase 5 — Enrichment

- optional conservative enrichment;
- section/repetition aware variations;
- rhythm-guitar restore/variation;
- A/B/C outputs;
- audible validation in Logic/Kontakt.

## 11. Разделение работы

Opus 4.6 через Cursor может реализовать инфраструктурный bootstrap и vertical slice в feature branches. Задачи должны быть узкими, с fixtures, acceptance tests и реальными smoke runs.

Hermes выполняет архитектурный/code review и E2E-проверку:

- diff and security review;
- dependency/model licensing;
- cache invalidation;
- upload safety/path traversal;
- image reproducibility;
- live GPU/storage/ingress checks;
- повторную загрузку только GP без пересчёта stems;
- независимый разбор итоговых MIDI;
- гарантию отсутствия изменений при выключенной функции.

## 12. Неподвижные ограничения

- Не менять существующую музыкальную семантику конвертера молча.
- Все restoration/enrichment функции opt-in до отдельного одобрения.
- Не считать статистическую «живость» доказательством музыкальности.
- Не переносить плавающий абсолютный tempo Suno в ровный GP output.
- Не доверять сырой транскрипции без GP/audio reconciliation и confidence.
- Не хранить credentials, TLS private keys или kubeconfigs в git.
- Не модифицировать и не удалять существующие данные/модели в `/data`.
