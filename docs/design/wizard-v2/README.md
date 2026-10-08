# Макет мастера GP → MIDI, версия 2

Выгрузка холста Claude Design «gpmidi — мастер GP → MIDI v2»
(https://claude.ai/artifact/8eJjWiEPDvnZqw1jGGiKdN, приватный) от 2026-10-08.
Макет сделан по брифу [`docs/CLAUDE_DESIGN_PROMPT.md`](../../CLAUDE_DESIGN_PROMPT.md)
на коде `main@f4dd78a`; задание на реализацию —
[`docs/codex-ui-wizard-task.md`](../../codex-ui-wizard-task.md).

## Как смотреть

Каждый `*.dc.html` — обычная HTML-страница со стилями из `wiz.css`. Откройте
через любой статический сервер, чтобы подхватился относительный `wiz.css`:

```bash
python3 -m http.server 8766 --directory docs/design/wizard-v2
```

Теги `<x-dc>` и `<helmet>`, строка `<script src="./support.js">` и блок
`<script type="text/x-dc">` относятся к среде холста. Браузер их игнорирует,
переносить в приложение не нужно.

## Экраны

| Файл | Что это |
|---|---|
| `Main.dc.html` | ① Файл · 1440 |
| `D2-Tracks.dc.html` | ② Дорожки · 1440 |
| `D3-Sound.dc.html` | ③ Звучание · 1440. Интерактивен только на холсте; без него на месте сводки «Что получится» видны `{{…}}` |
| `D3b-Sound-open.dc.html` | ③ Звучание: раскрыты «Точность нот» и «По дорожкам». **Эталон разметки шага ③** |
| `D4-Extra.dc.html` | ④ Дополнительно · 1440 |
| `D5-Result.dc.html` | ⑤ Результат · 1440, две версии |
| `M1-File` … `M5-Result` | те же шаги · 390 |
| `S-Empty`, `S-ParseError`, `S-TooBig`, `S-NoTracks`, `S-Building`, `S-BuildError`, `S-OldSession`, `S-History` | состояния |
| `Components.dc.html` | лист компонентов |
| `Spec.dc.html` | **спецификация**: устройство страницы, контрол → поле формы, токены |
| `Decisions.dc.html` | 8 спорных решений с рекомендациями — приняты все |
| `wiz.css` | стили макета, токены — в `:root` |
| `tokens.json` | токены, извлечённые из `static/style.css`, с правками контраста |

Подписи вроде `[ТЕКСТ ОШИБКИ ПАРСЕРА]` и `[ДОРОЖКА 1]` — места для настоящих
данных.
