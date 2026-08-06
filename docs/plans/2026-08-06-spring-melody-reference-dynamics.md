# Spring Melody Reference Dynamics — план оценки и отчёта

**Статус:** выполнено; анализ, отчёт и воспроизводимый manifest проверены 2026-08-06.

**Цель:** восстановить из исходных stereo stems слышимую потактовую иерархию инструментов Spring Melody, отфильтровать общую динамику композиции и сформировать минимальный набор сообщений для Cryo Mix Nova только для устойчивых относительных отклонений громкости.

**Архитектура:** reference MIDI задаёт точные границы тактов в audio-time; каждый уже разделённый WAV-stem измеряется независимо. Из потактовых кривых удаляется общий для ансамбля подъём/спад, который не требует отдельной фейдерной коррекции каждого инструмента. В отчёт попадают только устойчивые относительные изменения ролей, превышающие порог.

**Технологии:** Python 3, NumPy, SoundFile, SciPy signal filters, Matplotlib, mido; self-contained HTML без внешних CDN.

---

## 1. Авторитетные входы

Корень исходного проекта:

`/home/hermes/1_Spring_Melody/`

Master:

`Spring Melody (Rus) (noce) (Remastered) Final.wav`

Stems и их роли:

- `(...)(Vocals).wav` → `Voice (Strings)_SM.wav`.
- `(...)(Guitar).wav` → reference — **единый неразделимый stem rhythm + solo guitar**; в текущем проекте его роль переносят на `Rhytm Guitar_SM.wav` + `Lead Guitar_SM.wav` одновременно. Раздельная automation из reference не выводится.
- `(...)(Synth).wav` → `Stell Guitar 1_SM.wav` + `Steel Guitar 2_SM.wav` одновременно. В Nova не использовать внутреннее название Hydra.
- `(...)(Keyboard).wav` → `Piano 1_SM.wav` + `Piano 2_SM.wav` одновременно.
- `(...)(Drums).wav` → `Drums_SM.wav`.
- `(...)(Bass).wav` → `Bass_SM.wav`.

Reference MIDI:

- рядом с каждым stem лежит синхронный одноимённый `.mid`;
- все шесть MIDI имеют одинаковую tempo map;
- MIDI используется только как audio-time grid, а не как источник loudness.

## 2. Проверенные свойства входа

- Все WAV: stereo, 48 kHz, PCM 16-bit, `302.760 s`.
- Все reference MIDI: TPB `480`, длина около `301.935 s`, `363` tempo-события.
- Темп не фиксирован ровно на 75 BPM: фактический диапазон tempo map около `68.85–77.36 BPM`, медиана около `76.67 BPM`.
- Поэтому постоянный такт `3.2 s` используется только как sanity check. Основные границы вычисляются интегрированием MIDI tempo map.
- MIDI содержит 95 полных тактов 4/4 до примерно `300.193 s`, затем неполный хвост. WAV-хвост после последнего полного такта сохраняется как outro-tail и не превращается в фиктивный 96-й такт.

## 3. Метрика громкости

Для каждого stereo stem:

1. Прочитать WAV без изменения sample rate и каналов.
2. Применить одинаковое K-weighting-приближение BS.1770 ко всему сигналу:
   - high-pass для удаления неслышимого DC/subsonic;
   - high-shelf для perceptual weighting.
3. Для каждого MIDI-такта вычислить по **всей длительности такта, включая паузы**:
   - K-weighted mean-square loudness proxy в dB;
   - unweighted RMS dBFS как независимую проверку;
   - true/sample peak proxy;
   - crest factor;
   - band-energy shares для диагностики маскировки, но не как замену loudness.
4. Не использовать EBU relative gating внутри отдельного такта: оно может выкинуть паузу и переоценить sparse stem. Для automation важна энергия, реально занимающая весь такт.

## 4. Нормализация и отделение мастер-динамики

Для каждого инструмента `i` и такта `b`:

- определить active bars относительно собственного noise/silence floor;
- вычислить активную медиану stem `M_i`;
- получить внутреннюю динамику `D_i,b = L_i,b - M_i`.

Для каждого такта:

- вычислить robust common-mode `C_b` как медиану `D_i,b` только среди активных stems;
- получить относительное отклонение роли `R_i,b = D_i,b - C_b`.

`C_b` описывает общий подъём/спад ансамбля и показывается на графике, но не порождает отдельные Nova-команды по каждому stem. Кандидаты для Nova строятся по `R_i,b`.

## 5. Минимальная интерполяция и пороги

Начальные правила (могут быть уточнены после просмотра реальных кривых; изменение фиксируется ниже):

- основной порог: `|R| >= 2.0 dB`;
- слабые отклонения `< 2.0 dB` не описывать;
- сглаживание: median filter шириной 3 такта только для подавления одиночного выброса; raw-кривая остаётся в JSON/графике;
- сохранять диапазон от 2 последовательных тактов одного знака;
- одиночный такт сохранять только при `|R| >= 3.0 dB`;
- разрешено склеить один промежуточный такт ниже порога, только если соседние диапазоны имеют тот же знак и этот такт не меняет роль;
- не интерполировать через паузу/неактивность инструмента;
- уровни в инструкции округлять до `0.5 dB` и трактовать как starting point, не как восстановленное положение исходного фейдера.

## 6. Группировка Nova-инструкций

- Пересекающиеся диапазоны разных инструментов группировать в одно сообщение по тактам.
- Не писать отдельную команду для common-mode подъёма всего ансамбля.
- `Guitar` переводить в одинаковое одновременное изменение `Rhytm Guitar_SM.wav` + `Lead Guitar_SM.wav`.
- `Synth` переводить в совместное изменение `Stell Guitar 1_SM.wav` + `Steel Guitar 2_SM.wav`.
- `Keyboard` переводить в совместное изменение `Piano 1_SM.wav` + `Piano 2_SM.wav`; внутри пары не изобретать раздельную automation, которой нет в reference stem.
- `Vocals` переводить на `Voice (Strings)_SM.wav` только как динамическую роль. Тембральную обработку вокала на strings не переносить.
- Формулировать коротко: диапазон тактов, что поднять/опустить, ориентировочная величина и музыкальная роль.
- В каждом диапазоне указывать точные project timestamps `MM:SS.mmm–MM:SS.mmm`, вычисленные из MIDI tempo map. Номер такта без тайминга недостаточен для Nova.

## 7. Артефакты

Рабочая директория отчёта:

`/home/hermes/reports/spring-melody-reference-dynamics/`

Обязательные файлы:

- `reference_dynamics.json` — входы, hashes, tempo/bar grid, raw и processed metrics, thresholds, detected ranges;
- `reference_dynamics.csv` — одна строка на инструмент/такт;
- `reference_dynamics.html` — self-contained отчёт;
- `reference_dynamics_prompts.txt` — только отобранные сообщения Nova;
- `plots/` — PNG/SVG-графики для независимого просмотра;
- `run_manifest.json` — версии библиотек, команда запуска и SHA-256 результатов.

Публикация:

`/srv/shared/http/spring-melody-reference-dynamics/`

Фактический результат:

- 95 полных тактов по общей MIDI tempo map;
- 37 устойчивых диапазонов выше финального порога;
- 23 сгруппированных сообщения Nova;
- self-contained HTML: `/home/hermes/reports/spring-melody-reference-dynamics/reference_dynamics.html`;
- опубликованный URL: `http://192.168.40.254/shared/spring-melody-reference-dynamics/reference_dynamics.html`;
- SHA-256 финального HTML v2: `191ad0b18b7c33e9d23fe5628d275647350c0d7438bebd843d2ff96e83183836`.

## 8. Содержание HTML

1. Краткий verdict и ограничения.
2. Интерактивно читаемая легенда mapping инструментов.
3. Общая потактовая динамика stems.
4. График common-mode — что оставляем общему mix/master stage.
5. График относительных отклонений `R_i,b` с линиями `±2.0 dB`.
6. Heatmap «инструмент × такт».
7. Отобранные диапазоны и готовые сообщения Nova с Copy-кнопками.
8. Полная таблица `такт → точное начало → точный конец`.
9. Полная таблица метрик и collapsed technical details.
10. Явное ограничение: измеряется слышимый результат stem, а не скрытая исходная plugin/fader automation.

## 9. Проверка

- SHA-256 всех WAV/MIDI входов записаны.
- Границы тактов монотонны, не выходят за WAV и совпадают между всеми reference MIDI.
- K-weighted и RMS-кривые качественно согласуются; сильные расхождения отмечаются как спектрально обусловленные.
- Silence не превращается в команду «опустить ещё сильнее».
- Каждая Guitar-инструкция называет `Rhytm Guitar_SM.wav` и `Lead Guitar_SM.wav` вместе и требует одинакового изменения обеих дорожек.
- Слабые изменения ниже порога не попадают в prompts.
- JSON/CSV и числа в HTML генерируются одним кодом.
- HTML открывается без внешней сети, графики и Copy-кнопки работают.
- Served HTML совпадает с локальным файлом побайтово.
- Отчёт визуально проверяется на desktop viewport около `1920×1080`; iPhone-оптимизация не является требованием.

Фактически выполнено:

- semantic validation: PASS — 570 CSV-строк (`95 × 6`), 37 диапазонов, 23 prompts;
- `python -m py_compile tools/analyze_reference_dynamics.py`: PASS;
- `git diff --check`: PASS;
- все 6 reference MIDI имеют одинаковую tempo map; 363 tempo-события;
- K-weighted/RMS correlation по ролям: `0.884–0.994`;
- master/stem-sum sanity check: correlation `0.7916`, residual после оптимального scalar gain `−4.28 dB` относительно master; master не считается побитно равной суммой stems из-за bus/master processing;
- локальные и опубликованные HTML/JSON/CSV/prompts/manifest совпали byte-for-byte;
- Chromium desktop render 1920×1080: PASS; 4/4 images loaded, horizontal overflow отсутствует, 23/23 Copy-кнопки найдены, JS errors отсутствуют;
- первоначальный дефект графика с отображением неактивного noise floor до `−100 dB` исправлен: неактивные участки теперь показаны разрывами линий.
- HTML v2 semantic validation: PASS — точные девять Nova track names, 96 границ для 95 тактов, timestamps во всех 23 prompts и 37 диапазонах, 95 строк полной timing-grid; старые `Hydra`/`Synth 1`/`Synth 2` в пользовательских артефактах отсутствуют.
- HTML v2 browser validation: PASS — 4/4 графика загружены, 23/23 Copy-кнопки, горизонтальный overflow отсутствует, JS errors отсутствуют; served-файлы совпадают с локальными byte-for-byte.

## 10. Воспроизведение и handoff

Планируемый versioned скрипт:

`tools/analyze_reference_dynamics.py`

Команда:

```bash
/home/hermes/.cache/spring-melody-reference-dynamics-venv/bin/python \
  tools/analyze_reference_dynamics.py \
  --project-root "/home/hermes/1_Spring_Melody" \
  --output "/home/hermes/reports/spring-melody-reference-dynamics" \
  --threshold-db 2.0 \
  --single-bar-threshold-db 3.0
```

Продолжение в Claude/Cursor должно начинаться с этого документа и `run_manifest.json`, а не с повторного угадывания mapping или порогов.

## 11. Журнал уточнений

- `2026-08-06`: первоначальная идея фиксированных 75 BPM / 3.2 s заменена на точные границы из reference MIDI tempo map после проверки 363 tempo-событий. Постоянный BPM оставлен только как sanity check.
- `2026-08-06`: зафиксировано, что Guitar reference stem объединяет rhythm + solo и регулируется только целиком.
- `2026-08-06`: после sensitivity-check порог повышен с `1.5` до `2.0 dB`, а single-bar gate с `2.5` до `3.0 dB`. Это уменьшило результат с 46 диапазонов / 28 prompts до 37 / 23, сохранив 13 длинных диапазонов от четырёх тактов; отброшены преимущественно мелкие Voice/Synth-флуктуации.
- `2026-08-06`: добавлена независимая RMS-проверка K-weighted кривых. Фактическая корреляция по ролям составила `0.884–0.994`, поэтому выбранные изменения не объясняются одним perceptual weighting.
- `2026-08-06`: из-за несовместимости системных NumPy/SciPy анализ запускается в отдельном venv `/home/hermes/.cache/spring-melody-reference-dynamics-venv` с NumPy 1.26.4, SciPy 1.13.1, SoundFile 0.13.1, Matplotlib 3.8.4 и mido 1.3.3; рабочее окружение gpmidi не изменялось.
- `2026-08-06`: после ответа Nova заменены внутренние/условные названия на точные имена девяти текущих дорожек. Reference Guitar теперь явно управляет `Rhytm Guitar_SM.wav` + `Lead Guitar_SM.wav` совместно; reference Synth — `Stell Guitar 1_SM.wav` + `Steel Guitar 2_SM.wav` совместно.
- `2026-08-06`: каждый Nova-prompt и каждый detected range дополнен точными start/end timestamps; в HTML добавлена полная таблица границ всех 95 тактов. Причина: Nova не может найти такты без временных координат waveform.
