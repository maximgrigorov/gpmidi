# Phase 1 Acceptance Fixes

> **Для Cursor:** исправь Phase 1 в текущей ветке `feat/project-asset-storage`. Не начинай Phase 2 и не merge в `main`. Сначала прочитай `AGENTS.md`, исходное задание `docs/cursor-phase-1-project-asset-storage-task.md` и отчёт `docs/Phase_1_Project_Asset_Storage.md`.

## Цель

Закрыть найденные независимой проверкой Hermes блокеры, пересобрать и реально развернуть новый immutable image, повторить acceptance и обновить отчёт доказательствами. Существующий PVC и данные не удалять и не пересоздавать.

## Подтверждённые Hermes проблемы

### 1. Полный quality gate из исходного задания не проходит

Из корня репозитория реально выполнено:

```bash
python -m pytest services/asset_api/tests test_projects_ui.py -q
```

Результат: `68 passed, 8 errors`. `services/asset_api/app` затеняет корневой `app.py`, поэтому UI tests импортируют не Flask app, а package FastAPI. Отдельный запуск двух suites даёт 68 + 8, но исходное задание требует единый:

```bash
python -m pytest -q
```

Он обязан собирать и запускать также весь существующий regression suite, а не только 76 новых тестов.

Рекомендуемое исправление: переименовать Python package FastAPI из слишком общего `app` в `asset_api` (или другое однозначное имя), обновив Docker CMD, imports и tests. Не маскировать проблему раздельными командами и не использовать `|| true`.

### 2. Flask client по умолчанию неверно включает TLS verification

`ailab_client.py`:

```python
ASSET_API_VERIFY_TLS = os.environ.get("ASSET_API_VERIFY_TLS", "0") != "1"
```

При default `0` выражение возвращает `True`, то есть self-signed AILab TLS проверяется и client ломается. Сделай строгий parser: `1/true/yes/on` = true, `0/false/no/off` = false; default для текущего self-signed deployment — false. Добавь parameterized unit tests.

### 3. Flask upload proxy передаёт неправильный Content-Length

`app.py` передаёт `request.content_length` в `stream_proxy_upload`. Это длина всего multipart request с boundaries и form fields, а upstream body содержит только `f.stream`. Заголовок может быть больше фактического body и приводить к зависанию/ошибке протокола.

Не передавай multipart total как длину файла. Разреши `requests` определить длину seekable stream либо вычисли точный размер именно file stream без сохранения постоянной копии. Добавь integration test с реальным локальным HTTP upstream, который проверяет фактический body и Content-Length, а не mock метода.

### 4. Один upload ticket можно потребить конкурентно дважды

Hermes воспроизвёл race двумя async streaming PUT одного ticket, синхронизированными после первого chunk. Оба ответа: `201`. Оба вернули один link, но контракт one-time ticket нарушен.

Причина: status проверяется до streaming, а claim `pending -> consumed` выполняется только после полной записи без атомарного compare-and-set.

Сделай атомарный claim до чтения body, например transaction/conditional update `WHERE status='pending'`. Второй concurrent request должен получить стабильный `409` или `410`, не читать/писать blob и не создавать link. Продумай явные состояния (`pending`, `uploading`, `consumed`, `failed/expired`) и recovery после failed upload/process restart. Добавь детерминированный async concurrency test с barrier; обычный быстрый sequential test race не доказывает.

### 5. Не выполнены обязательные concurrency/rate-limit tests

Исходное задание требовало:

- два одновременных upload одинаковых bytes -> один physical blob, оба корректных project links;
- concurrent reuse одного ticket запрещён;
- rate/connection limiting на ticket creation и active uploads.

В текущих tests нет concurrency test и limiter не реализован, хотя `UPLOAD_RATE_LIMIT_PER_MIN` объявлен в config. Реализуй bounded in-process limiter, достаточный для single-replica Phase 1, с `429`, machine-readable code и `Retry-After`. Ограничь одновременно активные uploads отдельным configurable env. Тесты должны быть детерминированными и очищать limiter state между cases.

### 6. Error contract не содержит обязательный request id

Большинство `_error(...)` вызываются без `request_id`, а validation errors используют стандартный FastAPI shape. Исходное задание требует stable machine-readable `code`, `message`, `request_id` без stack trace/path.

Добавь request-id middleware/exception handlers для HTTP errors и request validation. Возвращай request id также header-ом. Не раскрывай ticket, binary body, absolute filesystem paths или traceback.

### 7. PATCH допускает blank project name

`ProjectCreate` trim/проверяет blank, но `ProjectUpdate` этого validator не имеет. `"name": "   "` проходит Pydantic и записывается как пустая строка после `.strip()`. Используй общий validator и добавь regression test.

### 8. Delete Project оставляет живые upload tickets

`upload_tickets.project_id` не имеет FK/cascade. Ticket, выпущенный до удаления Project, остаётся pending. Последующий upload может записать physical blob, затем упасть на FK project link, оставив orphan.

Сделай безопасную schema migration для уже существующей SQLite v1 без потери данных: invalidates/cascades tickets при удалении Project либо атомарно перепроверяет Project до commit и гарантированно не оставляет новый orphan blob. Обязательно test: issue ticket -> delete project -> PUT -> controlled 404/410, no blob/link/temp. Не удаляй существующий PVC/DB ради миграции.

### 9. Repository hygiene не проходит

`git diff --check origin/main...HEAD` на проверенном tip `ba1fe99` сообщил:

- trailing whitespace в `docs/Phase_1_Project_Asset_Storage.md:3-4`;
- лишний blank line EOF в `static/style.css`.

Исправь. Также убери новые unused imports/variables в Phase 1 коде. Не обязательно чистить pre-existing unrelated lint debt, но новые файлы должны проходить configured lint.

### 10. Live Flask acceptance не доказан

Отчёт показывает создание Project только через API. В обязательном acceptance были:

- создать Project через реальный Flask UI;
- загрузить через реальный Flask streaming proxy;
- проверить существующий converter `/upload` на fixture.

UI unit tests с mocked client этого не доказывают. На доступном Hermes host `/projects` сейчас не отдаётся новым UI, а URL в отчёте оставлен placeholder `http://<gpmidi-host>:8082/projects`.

Разверни/перезапусти фактический Flask service на его реальном host, зафиксируй точный LAN URL и выполни browser/curl E2E. Не сохраняй WAV постоянно на Flask host; допустимы только bounded framework temp/spool semantics, после request временный файл должен исчезнуть.

## Дополнительные обязательные проверки

1. Сохрани текущий GPU profile до работы; Phase 1 CPU-only и не должен его переключать.
2. Не удаляй и не пересоздавай `project-assets` PVC. Выполни schema migration in place.
3. Собери новый image с tag, основанным на новом git SHA; старый `asset-api:17448310e361` после fixes недействителен.
4. Выполни `kubectl diff`, apply, rollout status и проверь реальные pod image/probes/logs.
5. Повтори dedup, GP revisions, download SHA, manifest determinism, pod delete и rollout persistence.
6. Проверь Gitea, homepage, smoke-app и GPU profile до/после.
7. Проверь, что `/asset-api/healthz`, `/readyz` и `/v1/projects` доступны извне по HTTPS.
8. Не включай auth и HTTP Range сверх Phase 1 scope; эти boundaries могут остаться документированными.

## Quality gates — запускать именно так

Из корня репозитория:

```bash
python -m pytest -q
python -m ruff check <все изменённые Phase-1 Python files>
git diff --check origin/main...HEAD
bash -n infra/ailab/scripts/*.sh
shellcheck infra/ailab/scripts/*.sh
kubectl kustomize infra/ailab > /tmp/ailab-rendered.yaml
kubeconform -strict -summary -ignore-missing-schemas /tmp/ailab-rendered.yaml
```

Нельзя заменять полный `python -m pytest -q` суммой раздельных suites. Нельзя глотать failure через `|| true`.

## Обновить отчёт

Обнови `docs/Phase_1_Project_Asset_Storage.md`:

- новые commits и image tag;
- schema migration version и доказательство сохранности старых данных;
- полный test count из одного root pytest run;
- concurrency same-ticket и same-blob evidence;
- limiter evidence;
- точный Flask UI URL и реальный UI create/upload/converter smoke;
- corrected TLS behavior;
- live rollout/persistence/GPU before-after;
- устранённые findings Hermes;
- оставшиеся честные boundaries.

## Stop conditions

Остановись и спроси пользователя, если требуется удалить/recreate PVC, потерять данные, остановить GPU workload, reboot, открыть новые credentials или ломать существующие ingress routes.

## Финальный ответ

Верни ветку, новые commits, новый deployed image tag, полный root pytest output summary, migration result, concurrency/limiter evidence, точный UI URL, live API/UI/persistence evidence, GPU profile before/after и путь обновлённого отчёта. Не merge в `main`.