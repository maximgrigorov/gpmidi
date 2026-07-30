# Cursor / Opus 4.6 task: Phase 1 — Project and Asset Storage

> Выполни задачу полностью: код, тесты, container image, deployment на AILab и реальные E2E smoke checks. Не ограничивайся планом или YAML.

## Goal

Реализовать первый рабочий vertical slice долговечных музыкальных Projects и content-addressed asset storage на AILab:

- Project CRUD;
- роли assets;
- SHA-256 и дедупликация;
- ревизии Guitar Pro;
- manifest/provenance и cache keys;
- upload/download API;
- формы существующего gpmidi web UI;
- физическое хранение больших файлов только в PVC AILab.

Phase 1 не анализирует аудио, не запускает модели и не меняет MIDI. Это CPU/storage-only этап.

## Git и prerequisite

1. Начни с актуального `main`, который должен содержать Phase 0 до commit `a23b5a93f8fe45220dff6aa031ebb38fa764f63f` или новее.
2. Прочитай полностью:
   - `AGENTS.md`;
   - `docs/reference-guided-midi-restoration-plan.md`;
   - `docs/Phase_0_AILab_bootstrap.md`;
   - `infra/ailab/README.md`;
   - `infra/ailab/FLUX-BOOTSTRAP.md`.
3. Создай ветку `feat/project-asset-storage`.
4. Не работай напрямую в `main`, не merge в `main`.
5. При первом SSH на AILab первым удалённым действием прочитай `/home/mgrigorov/AGENTS.md` полностью.
6. Не меняй музыкальную семантику `gp_to_shreddage.py`, humanization, timing, articulations, bends или существующий `/upload` converter flow.
7. Делай небольшие логические commits и push feature branch для review.

## Критическое правило GPU

Phase 1 не требует GPU и не должен запрашивать `nvidia.com/gpu`.

На AILab по умолчанию работает `llama-cpp`, обычно с `qwen3-coder-next`, но активным может быть другой профиль или ComfyUI. Перед deployment зафиксируй состояние через:

```text
GET http://192.168.30.2/api/stats
```

Не переключай GPU profile в `none` для этой CPU-only фазы. После deployment профиль, модель на `:8080`, VRAM workload и health должны остаться такими же. Любой будущий GPU job обязан запоминать текущий профиль, переключать через homepage/API в `none`, а затем восстанавливать именно прежний профиль в cleanup/finally path.

## Архитектура Phase 1

### AILab

Отдельный сервис `asset-api` в namespace `gpmidi-ml`:

- FastAPI;
- SQLite в WAL mode;
- один replica;
- один RWO PVC `project-assets`;
- application root `/var/lib/gpmidi`;
- blobs и БД в одном persistent filesystem;
- никакого hostPath;
- non-root, PSS restricted, read-only root filesystem;
- `/tmp` через bounded `emptyDir`;
- CPU/RAM requests и limits;
- liveness/readiness probes;
- ClusterIP Service;
- HTTPS ingress на `443` под path prefix `/asset-api`;
- без GPU request/runtimeClass.

### Текущий gpmidi host

Существующий Flask UI остаётся главным UI. Добавь отдельные страницы Projects, не ломая существующий converter UI:

- `/projects` — список и создание;
- `/projects/<project_id>` — карточка проекта, assets и GP revisions;
- формы загрузки по role;
- download links;
- понятные сообщения об ошибках AILab.

Вынеси HTTP-клиент AILab из `app.py` в отдельный модуль. Не размазывай API transport по view functions.

### Upload data path

Большие WAV/FLAC не должны сохраняться в `data/sessions` gpmidi host.

Предпочтительный путь:

1. Flask создаёт project/получает upload ticket через AILab API.
2. Браузер отправляет raw file body непосредственно на HTTPS endpoint AILab по одноразовому ticket.
3. AILab пишет поток во временный файл на том же PVC, одновременно считает SHA-256 и размер.
4. После полной валидации выполняется atomic `os.replace` в content-addressed blob path.
5. UI обновляет project page.

Upload ticket:

- cryptographically random;
- хранится в SQLite только как hash;
- одноразовый;
- TTL не более 15 минут;
- привязан к project, role, original filename и max bytes;
- повторное использование возвращает 409/410;
- не попадает в logs и git.

Если direct browser upload объективно блокируется self-signed TLS, CORS или текущей UI topology, не делай скрытый fallback с постоянным сохранением WAV на gpmidi host. Зафиксируй blocker и реализуй bounded streaming proxy без permanent file, с доказательством, что `data/sessions` не содержит загруженный audio asset после запроса. Не загружай весь файл в RAM.

## Filesystem layout

Внутри PVC:

```text
/var/lib/gpmidi/
├── db/
│   └── projects.sqlite3
├── blobs/
│   └── sha256/
│       └── ab/
│           └── cd/
│               └── <full_sha256>
└── tmp/
    └── uploads/
```

Правила:

- blob path строится только из проверенного lowercase SHA-256;
- original filename является metadata и никогда не участвует в physical path;
- запрет `..`, absolute paths, slash/backslash/NUL/control characters;
- temp и final blob находятся на одном filesystem для atomic rename;
- temp удаляется при disconnect, ошибке, превышении limit и startup cleanup;
- symlink traversal запрещён;
- физическое удаление blobs и garbage collection не входят в Phase 1;
- удаление Project удаляет логические ссылки, но не blob.

## Data model

Используй SQLite migrations/version table, foreign keys и transactions. Не создавай ORM-магии без необходимости; SQLAlchemy допустим, но schema должна быть явной и протестированной.

Минимальные сущности:

### projects

- `id`: UUID;
- `name`;
- `description` optional;
- `created_at` UTC;
- `updated_at` UTC;
- optimistic version/revision integer.

### assets

- `sha256` primary key;
- `size_bytes`;
- `media_type`;
- `original_name` только как первое наблюдавшееся имя либо вынеси имена в links;
- `blob_relpath`;
- `created_at` UTC;
- integrity status.

### project_assets

- UUID link id;
- project id;
- asset SHA-256;
- role;
- label optional;
- original filename;
- created_at;
- provenance JSON;
- unique constraints, исключающие случайные дубли одного link.

### gp_revisions

- project id;
- monotonic revision number per project;
- asset SHA-256;
- original filename;
- created_at;
- optional user note;
- unique `(project_id, revision)`;
- повторная загрузка того же GP hash не должна молча создавать новую ревизию: вернуть существующую либо потребовать явный параметр; выбери и документируй одно поведение.

### upload_tickets

- ticket hash;
- project id;
- role;
- original filename;
- max bytes;
- expiry;
- consumed timestamp/status.

## Asset roles

Храни role как проверяемую строку с центральным registry/enum. Минимум:

- `mix`;
- `stem.drums`;
- `stem.bass`;
- `stem.guitar`;
- `stem.vocals`;
- `stem.other`;
- `suno-midi.mix`;
- `suno-midi.drums`;
- `suno-midi.bass`;
- `suno-midi.guitar`;
- `suno-midi.other`;
- `lyrics`;
- `structure`;
- `guitar-pro`.

GP загружается через dedicated revisions endpoint, даже если в storage это обычный content-addressed asset.

## Validation and limits

Настраиваемые env limits, с безопасными defaults:

- audio mix/stem: до 1 GiB на файл;
- MIDI: до 100 MiB;
- Guitar Pro: до 100 MiB;
- lyrics/structure: до 5 MiB;
- имя Project: 1–120 Unicode characters после trim;
- description: до 4000 characters.

Разрешённые расширения:

- audio: `.wav`, `.flac`;
- MIDI: `.mid`, `.midi`;
- GP: `.gp`, `.gp3`, `.gp4`, `.gp5`, `.gpx`;
- text/structure: `.txt`, `.md`, `.json`.

Не доверяй только Content-Type или extension. Добавь дешёвую signature/header validation, не декодируя полный audio. Несовпадение role/type возвращает 415/422. Ограничение размера применяется во время streaming, а не после записи всего файла.

## API contract

Версионированный prefix:

```text
/asset-api/v1
```

Минимум:

- `GET /healthz`;
- `GET /readyz` с реальной проверкой SQLite/PVC;
- `POST /projects`;
- `GET /projects`;
- `GET /projects/{project_id}`;
- `PATCH /projects/{project_id}`;
- `DELETE /projects/{project_id}`;
- `POST /projects/{project_id}/upload-tickets`;
- `PUT /uploads/{ticket}` — raw streaming body;
- `POST /projects/{project_id}/gp-revisions` либо ticket flow с role `guitar-pro`;
- `GET /projects/{project_id}/assets`;
- `GET /projects/{project_id}/manifest`;
- `GET /projects/{project_id}/assets/{link_id}/download`;
- `DELETE /projects/{project_id}/assets/{link_id}`.

Response objects должны содержать stable IDs, SHA-256, size, role, revision и timestamps, но никогда physical absolute path.

Download:

- authorizes by project link, а не только по известному SHA;
- безопасный `Content-Disposition`;
- streaming response;
- корректный Content-Length;
- хотя бы basic HTTP Range support для больших audio assets либо честно документированный blocker с тестом текущего поведения.

Errors — JSON с stable machine-readable `code`, message и request id. Не возвращай stack traces или filesystem paths.

## Manifest, provenance and cache keys

`GET /manifest` возвращает deterministic JSON с:

- schema version;
- project metadata;
- GP revisions;
- project asset links;
- sha256, size, role, media type;
- provenance upload timestamp/original name;
- без host paths и ticket/token.

Порядок списков стабильный. Добавь функцию cache key:

```text
sha256(canonical_json({
  input_sha256,
  processor_name,
  processor_version,
  checkpoint_sha256,
  parameters
}))
```

Canonical JSON: UTF-8, sorted keys, compact separators, запрещены NaN/Infinity. В Phase 1 достаточно unit tests и API utility; никакой model execution.

Ключевой regression test: загрузка новой GP revision не изменяет SHA/assets/cache identities существующих mix/stems/Suno MIDI.

## Concurrency and consistency

Проверить минимум:

- два одновременных upload одинакового файла создают один physical blob;
- оба project links корректны;
- SQLite busy timeout/WAL;
- transaction rollback не оставляет dangling DB link;
- crash между temp write и DB commit не повреждает existing blob;
- повтор ticket не создаёт link;
- failed upload удаляет temp;
- startup cleanup удаляет только просроченные temp, не blobs.

Один replica обязателен, пока SQLite и RWO PVC. Зафиксируй это в manifest и документации.

## Security boundary

- PSS restricted;
- no hostPath, privileged, hostNetwork, hostPID;
- ServiceAccount token automount disabled, если Kubernetes API не нужен приложению;
- read-only root filesystem;
- dropped capabilities;
- seccomp RuntimeDefault;
- exact CORS origin list из env; никаких `*` для write endpoints;
- upload ticket не логируется;
- request body/original binary content не логируется;
- не хранить credentials/TLS key/kubeconfig в git;
- rate/connection limiting хотя бы на upload ticket creation и active uploads;
- API пока LAN-only и без user authentication — это явно пометить как Phase 1 boundary, не называть production-secure.

Помни: пользователь с правом создавать arbitrary Deployment/Job в namespace потенциально может mount известный Secret этого namespace. PSS это не предотвращает. Не добавляй чувствительные Secrets в `gpmidi-ml` без отдельного решения.

## Container image and deployment

Gitea v1.25.5 container registry отвечает на `http://192.168.30.2:3300/v2/` с 401, то есть registry endpoint существует, но auth не подготовлена в рамках Phase 0.

Предпочтение:

1. Если approved registry credentials уже безопасно доступны — build pinned image, push без печати token и deploy immutable digest.
2. Если credentials отсутствуют — не угадывай и не коммить token. Для real single-node smoke допустим воспроизводимый fallback:
   - `docker build` с tag, включающим git SHA;
   - `docker save`;
   - import через `sudo k3s ctr images import`;
   - manifest с exact tag и `imagePullPolicy: Never`;
   - зафиксировать, что это не полноценный GitOps image delivery.

Не использовать `latest`. Добавь `.dockerignore`. Image должен запускать production ASGI server, не `uvicorn --reload`.

Manifests:

```text
services/asset_api/
infra/ailab/apps/asset-api/
infra/ailab/scripts/build-import-asset-api.sh
```

Обнови `infra/ailab/kustomization.yaml`, но не добавляй one-shot smoke Jobs в durable root.

Перед изменением live deployment сделай `kubectl diff`. После — rollout status, logs, probes и endpoint tests.

## UI requirements

В существующем UI:

- отдельный navigation entry `Projects`;
- project create form;
- project list с количеством assets и последней GP revision;
- project details;
- отдельная загрузка GP revision;
- role selector для mix/stems/MIDI/text;
- upload progress и понятный success/error;
- отображение SHA prefix, полного размера, role, original filename, timestamp;
- download link;
- удаление link только после confirmation;
- никакого directory listing или physical path;
- существующий Guitar Pro converter upload и его byte-for-byte/default-off поведение не меняются.

JS должен быть минимальным и протестированным настолько, насколько позволяет текущий stack. API client/network failures не должны приводить к 500 Flask stack trace.

## Tests — обязательный TDD

Создавай тесты до реализации. Минимум:

### Unit

- role/extension/signature validation;
- path traversal filenames;
- streaming SHA and size limit;
- blob path derivation;
- canonical manifest ordering;
- cache key determinism/change sensitivity;
- GP revision numbering/idempotency;
- expired/used ticket;
- safe Content-Disposition.

### API integration

- Project CRUD;
- upload unique asset;
- same bytes twice -> one blob;
- same blob linked to two projects;
- failed/oversized upload -> no blob/link/temp;
- GP revision sequence;
- manifest excludes absolute paths/secrets;
- authorized download by project link;
- nonexistent/mismatched project link -> 404;
- delete link keeps blob;
- concurrency duplicate upload;
- restart with same temp SQLite/PVC directory preserves projects/assets.

### Existing Flask UI

- Projects pages render;
- create project forwards correctly;
- upload ticket/client failure is user-visible;
- existing `/upload` tests remain green;
- no audio asset appears under `data/sessions` after Project upload flow.

### Deployment/E2E

Используй маленькие synthetic fixtures, не коммить copyrighted music или большой binary:

- tiny valid WAV;
- tiny FLAC, MIDI and GP fixture if existing repo fixture is licensed/appropriate;
- invalid signature;
- filename traversal payload;
- duplicate bytes under different names.

## Real acceptance on AILab

До теста зафиксируй:

- current `/api/stats` profile;
- `GET :8080/v1/models` current model;
- Gitea `:3300` health;
- ingress smoke health;
- PVC state.

Затем реально:

1. deploy `asset-api`;
2. создать Project через API;
3. создать Project через Flask UI;
4. загрузить tiny WAV как mix;
5. загрузить те же bytes под другим именем и доказать dedup;
6. загрузить GP revision 1 и revision 2;
7. скачать asset и сравнить SHA-256;
8. получить deterministic manifest;
9. удалить/recreate asset-api pod;
10. повторить GET/download и сравнить SHA;
11. rollout restart и повторная проверка;
12. убедиться, что `project-assets` PVC Bound и physical blobs находятся только под provisioned `/data/k3s-storage/...`, без раскрытия/изменения соседних каталогов;
13. доказать, что existing converter `/upload` всё ещё работает на fixture;
14. проверить, что Gitea, homepage, ingress и LLM health не повреждены;
15. доказать, что GPU profile/model не переключался и Phase 1 pod не имеет GPU request.

Не загружай пользовательские реальные stems без явного указания. Для smoke достаточно tiny generated fixture.

## Quality gates

Обязательно выполнить и сохранить реальные результаты:

```text
python -m pytest -q
ruff/format check, если добавлен и зафиксирован tool config
bash -n infra/ailab/scripts/*.sh
shellcheck infra/ailab/scripts/*.sh
kubectl kustomize infra/ailab
kubeconform rendered manifests
kubectl diff
kubectl apply
kubectl rollout status
curl/API E2E
```

Также:

- `git diff --check`;
- secret scan;
- no generated DB, blob, kubeconfig, TLS key, token, image tar in git;
- dependency versions pinned;
- container runs by digest or exact git-SHA tag;
- reports contain no credentials or absolute sensitive paths.

## Required docs

Создай/обнови:

```text
docs/Phase_1_Project_Asset_Storage.md
services/asset_api/README.md
infra/ailab/apps/asset-api/README.md
infra/ailab/README.md
```

Итоговый report должен содержать:

1. architecture и отклонения от задания;
2. schema/migration version;
3. API endpoints;
4. filesystem layout;
5. image tag/digest и delivery method;
6. manifests/resources/limits;
7. фактические test counts;
8. реальные E2E request/response summaries;
9. dedup evidence: два links, один blob SHA/path identity без раскрытия host path;
10. persistence evidence после pod recreation;
11. подтверждение сохранности существующих `/data`, Gitea, homepage, LLM;
12. GPU profile/model before/after;
13. known boundaries/blockers;
14. rollback steps;
15. branch и commit SHAs.

## Stop conditions

Остановись и спроси пользователя, если:

- требуется удалить/перенести существующие `/data` assets;
- PVC нужно пересоздать с потерей данных;
- требуется reboot;
- registry требует создания/показа нового credential;
- ingress 443 или существующие routes надо ломать;
- live GPU workload нужно остановить для CPU-only Phase 1;
- self-signed TLS делает direct upload невозможным и нужен topology decision;
- миграция schema может удалить данные;
- обнаружено противоречие с remote `AGENTS.md`.

## Финальный ответ Cursor

Верни:

- ветку и commits;
- что реально deployed;
- URL UI/API;
- test output summary;
- E2E evidence;
- GPU profile/model before/after;
- сохранность existing services/data;
- blockers и security boundaries;
- точный путь `docs/Phase_1_Project_Asset_Storage.md`.

Не merge в `main`. После завершения ветка будет независимо проверена Hermes.