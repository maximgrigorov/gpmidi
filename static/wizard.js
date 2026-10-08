(() => {
  'use strict';
  const $ = (selector, root = document) => root?.querySelector(selector) || null;
  const $$ = (selector, root = document) => root ? Array.from(root.querySelectorAll(selector)) : [];

  const drawer = $('[data-history-drawer]');
  const setDrawer = open => {
    if (!drawer) return;
    drawer.classList.toggle('open', open);
    drawer.setAttribute('aria-hidden', String(!open));
    document.body.classList.toggle('drawer-open', open);
  };
  $$('[data-history-open]').forEach(button => button.addEventListener('click', () => setDrawer(true)));
  $$('[data-history-close]').forEach(button => button.addEventListener('click', () => setDrawer(false)));

  const input = $('#fileInput');
  const dropzone = $('#dropzone');
  const filename = $('#fileName');
  const fileError = $('#fileError');
  const uploadForm = $('#uploadForm');
  const validateUpload = file => {
    if (!file) return false;
    const max = Number(input?.dataset.maxBytes || 0);
    const tooLarge = max && file.size > max;
    if (filename) filename.textContent = file.name;
    if (fileError) {
      fileError.hidden = !tooLarge;
      fileError.textContent = tooLarge ? `Файл больше ${Math.round(max / 1024 / 1024)} МБ.` : '';
    }
    return !tooLarge;
  };
  input?.addEventListener('change', () => validateUpload(input.files[0]));
  ['dragenter', 'dragover'].forEach(name => dropzone?.addEventListener(name, event => {
    event.preventDefault(); dropzone.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(name => dropzone?.addEventListener(name, event => {
    event.preventDefault(); dropzone.classList.remove('dragover');
  }));
  dropzone?.addEventListener('drop', event => {
    if (event.dataTransfer.files.length) { input.files = event.dataTransfer.files; validateUpload(input.files[0]); }
  });
  uploadForm?.addEventListener('submit', event => {
    if (!validateUpload(input.files[0])) event.preventDefault();
  });

  const wizard = $('#wizardForm');
  const panels = $$('[data-step]');
  const validSteps = ['tracks', 'sound', 'extra', 'result'];
  const currentStep = () => new URL(location.href).searchParams.get('step') || (wizard ? 'tracks' : 'result');
  const showStep = (step, push = true) => {
    if (!validSteps.includes(step)) return;
    document.body.classList.add('js-ready');
    panels.forEach(panel => panel.hidden = panel.dataset.step !== step);
    $$('.stepper li').forEach((item, index) => item.classList.toggle('active', index === validSteps.indexOf(step) + 1));
    if (push) { const url = new URL(location.href); url.searchParams.set('step', step); history.pushState({step}, '', url); }
    window.scrollTo({top: 0, behavior: 'smooth'});
  };
  if (wizard) showStep(currentStep(), false);
  window.addEventListener('popstate', () => wizard && showStep(currentStep(), false));

  const selectedTracks = () => $$('input[name="track_indices"]', wizard).filter(input => input.checked);
  const syncTrackSelection = () => {
    const selected = new Set(selectedTracks().map(input => input.value));
    $$('[data-effect-row]', wizard).forEach(row => row.dataset.selected = selected.has(row.dataset.track) ? '1' : '0');
    const warning = $('[data-no-tracks]', wizard); if (warning) warning.hidden = selected.size > 0;
    updateDependencies();
  };
  $$('input[name="track_indices"]', wizard).forEach(input => input.addEventListener('change', syncTrackSelection));

  const parents = {ghost_notes:'humanize', lock_to_drums:'humanize', double_track:'humanize', palm_mute_motion:'humanize', fret_hand_cost:'humanize', humanize_timing_over_gp_offsets:'humanize', keep_gp_played_overlaps:'preserve_gp_played_offsets'};
  const updateDependencies = () => {
    $$('[data-effect-row]', wizard).forEach(row => {
      const selected = row.dataset.selected !== '0';
      $$('input[data-effect]', row).forEach(cell => {
        const effect = cell.dataset.effect;
        const parent = parents[effect];
        let enabled = selected && (!parent || $(`input[data-effect="${parent}"]`, row)?.checked);
        if (effect === 'humanize_timing_over_gp_offsets') enabled = enabled && $(`input[data-effect="preserve_gp_played_offsets"]`, row)?.checked;
        cell.disabled = !enabled;
        cell.closest('td')?.classList.toggle('disabled', !enabled);
      });
    });
    updateSummary();
  };

  $$('[data-role-select]', wizard).forEach(select => select.addEventListener('change', () => {
    const card = select.closest('[data-track-card]');
    const row = $(`[data-effect-row][data-track="${card.dataset.track}"]`, wizard);
    card.dataset.role = select.value; if (row) row.dataset.role = select.value; updateSummary();
  }));
  $$('[data-role][data-effect]', wizard).forEach(group => group.addEventListener('change', () => {
    $$(`[data-effect-row][data-role="${group.dataset.role}"] input[data-effect="${group.dataset.effect}"]`, wizard).forEach(cell => {
      if (!cell.dataset.exception) cell.checked = group.checked;
    }); updateDependencies();
  }));
  $$('[data-effect-row] input[data-effect]', wizard).forEach(cell => cell.addEventListener('change', () => {
    cell.dataset.exception = '1'; updateDependencies();
  }));
  $$('[data-clear-exceptions]', wizard).forEach(button => button.addEventListener('click', () => {
    const role = button.dataset.clearExceptions;
    $$(`[data-effect-row][data-role="${role}"] input[data-effect]`, wizard).forEach(cell => delete cell.dataset.exception);
    $$(`[data-role="${role}"][data-effect]`, wizard).forEach(group => group.dispatchEvent(new Event('change')));
  }));
  $('[data-reset-defaults]', wizard)?.addEventListener('click', () => {
    $$('[data-effect-row] input[data-effect]', wizard).forEach(cell => { cell.checked = cell.dataset.default === '1'; delete cell.dataset.exception; });
    updateDependencies();
  });
  $('[data-humanize-drums]', wizard)?.addEventListener('click', () => {
    $$('[data-effect-row][data-role="drums"] input[data-effect="humanize"]', wizard).forEach(cell => { cell.checked = true; delete cell.dataset.exception; }); updateDependencies();
  });

  function updateSummary() {
    if (!wizard) return;
    const active = $$('[data-effect-row] input[data-effect]:checked', wizard).filter(cell => !cell.disabled && cell.closest('tr').dataset.selected !== '0');
    const changes = $$('[data-effect-row] input[data-effect]', wizard).filter(cell => cell.checked !== (cell.dataset.default === '1'));
    const count = $('[data-change-count]', wizard); if (count) count.textContent = changes.length ? `изменено · ${changes.length}` : 'по умолчанию';
    const list = $('[data-outcome-list]', wizard); if (list) {
      const names = [...new Set(active.map(cell => cell.closest('td').previousElementSibling ? cell.dataset.effect : cell.dataset.effect))];
      list.innerHTML = names.length ? names.map(name => `<li>${name}</li>`).join('') : '<li>Точный перенос нот без дополнительного оживления</li>';
    }
    const drumOn = $$('[data-effect-row][data-role="drums"] input[data-effect="humanize"]', wizard).some(cell => cell.checked && !cell.disabled);
    const lockOn = $$('[data-effect-row] input[data-effect="lock_to_drums"]', wizard).some(cell => cell.checked && !cell.disabled);
    const drumSelected = $$('[data-effect-row][data-role="drums"]', wizard).some(row => row.dataset.selected !== '0');
    const warning = $('[data-drum-warning]', wizard); if (warning) warning.hidden = !(lockOn && (!drumOn || !drumSelected));
  }

  $$('[data-next]', wizard).forEach(button => button.addEventListener('click', () => {
    if (!selectedTracks().length) { syncTrackSelection(); showStep('tracks'); return; }
    showStep(button.dataset.next);
  }));
  $$('[data-back]', wizard).forEach(button => button.addEventListener('click', () => showStep(button.dataset.back)));
  wizard?.addEventListener('submit', event => {
    if (!selectedTracks().length) { event.preventDefault(); syncTrackSelection(); showStep('tracks'); return; }
    const overlay = $('[data-building]'); if (overlay) overlay.hidden = false;
  });
  syncTrackSelection();

  $$('[data-daw-checklist]').forEach(list => {
    const key = `gpmidi:daw:${list.dataset.dawChecklist}`;
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(key) || '{}'); } catch (_) {}
    $$('[data-check]', list).forEach(box => { box.checked = Boolean(saved[box.dataset.check]); box.addEventListener('change', () => { saved[box.dataset.check] = box.checked; localStorage.setItem(key, JSON.stringify(saved)); }); });
  });

    function formatMs(ms) {
      const total = Math.max(0, Math.floor(ms / 1000));
      const minutes = Math.floor(total / 60);
      const seconds = String(total % 60).padStart(2, '0');
      return `${minutes}:${seconds}`;
    }

    const PRESET_MAP = {
      acoustic: { instrument: 'acoustic_guitar_steel', gain: 0.85, attack: 0.005, release: 0.18 },
      rhythm: { instrument: 'overdriven_guitar', gain: 0.92, attack: 0.003, release: 0.16 },
      solo: { instrument: 'distortion_guitar', gain: 0.96, attack: 0.002, release: 0.22 },
      bass: { instrument: 'electric_bass_finger', gain: 0.95, attack: 0.006, release: 0.22 },
      strings: { instrument: 'string_ensemble_1', gain: 0.82, attack: 0.03, release: 0.35 },
      voice: { instrument: 'choir_aahs', gain: 0.8, attack: 0.02, release: 0.28 },
      keys: { instrument: 'acoustic_grand_piano', gain: 0.88, attack: 0.004, release: 0.18 },
      drums: { instrument: 'synth_drum', gain: 0.82, attack: 0.001, release: 0.08 },
      default: { instrument: 'electric_piano_1', gain: 0.84, attack: 0.005, release: 0.18 },
    };
    const SOUNDFONT_BASE = 'https://cdn.jsdelivr.net/gh/gleitz/midi-js-soundfonts@gh-pages/MusyngKite/';
    const soundfontCache = new Map();
    let sharedAudioCtx = null;

    function getAudioContext() {
      if (!sharedAudioCtx) {
        sharedAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      return sharedAudioCtx;
    }

    async function loadInstrument(player, presetName = player.instrumentPreset) {
      if (!window.Soundfont) throw new Error('soundfont-player not loaded');
      const preset = PRESET_MAP[presetName];
      const instrumentName = preset ? preset.instrument : (presetName || PRESET_MAP.default.instrument);
      const cacheKey = instrumentName;
      if (!soundfontCache.has(cacheKey)) {
        soundfontCache.set(cacheKey, window.Soundfont.instrument(player.audioCtx, instrumentName, {
          format: 'mp3',
          soundfont: 'MusyngKite',
          nameToUrl: (name, soundfont, format) => `${SOUNDFONT_BASE}${name}-${format}.js`,
        }).catch(error => {
          soundfontCache.delete(cacheKey);
          throw error;
        }));
      }
      return soundfontCache.get(cacheKey);
    }

    function notePreset(player, note) {
      const base = PRESET_MAP[player.instrumentPreset] || PRESET_MAP.default;
      const preset = { ...base, durationScale: 1 };
      if (player.instrumentPreset === 'drums') {
        if (note.pitch <= 36) preset.instrument = 'taiko_drum';
        else if ([38, 40].includes(note.pitch)) preset.instrument = 'orchestra_hit';
        else if ([41, 43, 45, 47, 48, 50].includes(note.pitch)) preset.instrument = 'melodic_tom';
        else if ([42, 44, 46, 49, 51, 52, 55, 57, 59].includes(note.pitch)) preset.instrument = 'reverse_cymbal';
        else preset.instrument = 'synth_drum';
      }
      if (note.articulation_key === 'mute') {
        preset.gain *= 0.65;
        preset.release = Math.min(preset.release, 0.1);
        preset.durationScale = 0.55;
      }
      if (note.articulation_key === 'harmonics') {
        preset.instrument = player.instrumentPreset === 'bass' ? 'electric_bass_finger' : 'electric_guitar_clean';
        preset.gain *= 0.8;
      }
      if (note.articulation_key === 'tremolo') {
        preset.durationScale = 0.75;
      }
      if (note.modifiers?.includes('vibrato')) {
        preset.gain *= 1.03;
      }
      if (note.modifiers?.includes('legato')) {
        preset.attack = Math.max(0.001, preset.attack * 0.5);
      }
      if (note.modifiers?.includes('pinch')) {
        preset.instrument = player.instrumentPreset === 'bass' ? 'slap_bass_1' : 'overdriven_guitar';
        preset.gain *= 1.06;
      }
      return preset;
    }

    function buildBendSteps(note) {
      if (!note.modifiers?.includes('bend/slide')) return [note.pitch];
      const target = Number.isFinite(note.bend_target_pitch) ? note.bend_target_pitch : (note.pitch + (Number(note.bend_semitones) || 2));
      const bendAmount = Math.abs(target - note.pitch);
      const durationMs = Math.max(1, Number(note.duration_ms) || 0);
      const steps = Math.max(8, Math.min(24, Math.round((durationMs / 28) + (bendAmount * 2))));
      return Array.from({ length: steps }, (_, idx) => {
        const ratio = steps === 1 ? 1 : idx / (steps - 1);
        return note.pitch + ((target - note.pitch) * ratio);
      });
    }

    function collectRequiredInstruments(player) {
      const names = new Set();
      const basePreset = PRESET_MAP[player.instrumentPreset] || PRESET_MAP.default;
      names.add(basePreset.instrument);
      player.notes.slice(0, 160).forEach(note => {
        const preset = notePreset(player, note);
        if (preset.instrument) names.add(preset.instrument);
      });
      return Array.from(names);
    }

    function clampMs(player, value) {
      return Math.max(0, Math.min(player.durationMs, Number(value) || 0));
    }

    function updatePlayhead(player) {
      if (!player.playhead) return;
      const ratio = player.durationMs ? (player.currentMs / player.durationMs) : 0;
      player.playhead.style.left = `${Math.max(0, Math.min(100, ratio * 100))}%`;
    }

    function syncPlayerUi(player) {
      player.seek.value = clampMs(player, player.currentMs);
      player.timeLabel.textContent = `${formatMs(player.currentMs)} / ${formatMs(player.durationMs)}`;
      updatePlayhead(player);
    }

    function stopPlayer(player, preservePosition = true) {
      if (!player) return;
      if (player.isPlaying && preservePosition) {
        player.currentMs = Math.min(
          player.durationMs,
          player.playbackOffsetMs + (performance.now() - player.startedAtPerfMs)
        );
      }
      player.isPlaying = false;
      player.timeouts.forEach(id => clearTimeout(id));
      player.timeouts = [];
      player.oscillators.forEach(item => {
        try { item.stop?.(); } catch (e) {}
        try { item.disconnect?.(); } catch (e) {}
        try { item.osc.stop(); } catch (e) {}
        try { item.osc.disconnect(); } catch (e) {}
        try { item.gain.disconnect(); } catch (e) {}
        try { item.filter.disconnect(); } catch (e) {}
      });
      player.oscillators = [];
      if (player.rafId) cancelAnimationFrame(player.rafId);
      player.rafId = null;
      player.playBtn.textContent = 'Play';
      syncPlayerUi(player);
    }

    function scheduleNote(player, note) {
      const preset = notePreset(player, note);
      const delay = Math.max(0, note.start_ms - player.playbackOffsetMs);
      const timeoutId = setTimeout(async () => {
        if (!player.isPlaying) return;
        const instrument = await loadInstrument(player, preset.instrument);
        if (!player.isPlaying) return;
        const noteDurationSec = Math.max(0.02, (Number(note.duration_ms) || 0) / 1000);
        const isShortNote = noteDurationSec <= 0.16;
        const effectiveScale = isShortNote ? Math.min(preset.durationScale, 0.92) : preset.durationScale;
        const durationSec = Math.max(0.02, noteDurationSec * effectiveScale);
        const targetGain = Math.max(0.05, Math.min(1.2, preset.gain * (0.55 + (note.velocity / 127))));
        const bendSteps = buildBendSteps(note);
        const segmentSec = durationSec / bendSteps.length;
        const shortRelease = Math.min(preset.release, Math.max(0.012, noteDurationSec * 0.18));
        bendSteps.forEach((pitch, idx) => {
          const when = player.audioCtx.currentTime + (segmentSec * idx);
          const isLast = idx === bendSteps.length - 1;
          const voice = instrument.play(pitch, when, {
            gain: idx === 0 ? targetGain : targetGain * 0.92,
            attack: idx === 0 ? preset.attack : 0.001,
            release: isLast ? (isShortNote ? shortRelease : preset.release) : 0.01,
            duration: Math.max(0.02, segmentSec + (isLast ? (isShortNote ? shortRelease : Math.min(preset.release, noteDurationSec * 0.35 + 0.02)) : 0)),
          });
          player.oscillators.push(voice);
        });
      }, delay);
      player.timeouts.push(timeoutId);
    }

    async function startPlayer(player) {
      if (activePlayer && activePlayer !== player) stopPlayer(activePlayer, true);
      activePlayer = player;
      if (!player.audioCtx) player.audioCtx = getAudioContext();
      if (player.audioCtx.state === 'suspended') await player.audioCtx.resume();

      player.playBtn.disabled = true;
      player.playBtn.textContent = 'Loading…';
      try {
        await Promise.all(collectRequiredInstruments(player).map(name => loadInstrument(player, name)));
      } catch (error) {
        player.playBtn.disabled = false;
        player.playBtn.textContent = 'Play';
        console.error('soundfont load failed', error);
        throw error;
      }
      player.playBtn.disabled = false;

      stopPlayer(player, true);
      player.currentMs = clampMs(player, player.currentMs);
      player.playbackOffsetMs = player.currentMs;
      player.startedAtCtxTime = player.audioCtx.currentTime;
      player.startedAtPerfMs = performance.now();
      player.isPlaying = true;
      player.playBtn.textContent = 'Pause';
      syncPlayerUi(player);

      player.notes.filter(note => note.end_ms > player.playbackOffsetMs).forEach(note => scheduleNote(player, note));

      const tick = () => {
        if (!player.isPlaying) return;
        player.currentMs = Math.min(
          player.durationMs,
          player.playbackOffsetMs + (performance.now() - player.startedAtPerfMs)
        );
        syncPlayerUi(player);
        if (player.currentMs >= player.durationMs) {
          player.currentMs = 0;
          player.playbackOffsetMs = 0;
          stopPlayer(player, true);
          syncPlayerUi(player);
          return;
        }
        player.rafId = requestAnimationFrame(tick);
      };
      player.rafId = requestAnimationFrame(tick);
    }

    async function seekTo(player, nextMs, shouldResume = false) {
      const resume = shouldResume ?? player.isPlaying;
      stopPlayer(player, true);
      player.currentMs = clampMs(player, nextMs);
      player.playbackOffsetMs = player.currentMs;
      syncPlayerUi(player);
      if (resume) await startPlayer(player);
    }

    function buildPreviewMarkup(data) {
      const total = Math.max(1, data.duration_ms || 1);
      const minPitch = data.pitch_min;
      const maxPitch = data.pitch_max;
      const pitchSpan = Math.max(1, maxPitch - minPitch + 1);
      const instrumentLabel = data.instrument_preset || 'default';
      const presetOptions = Object.keys(PRESET_MAP).map(name => `
        <option value="${name}" ${name === instrumentLabel ? 'selected' : ''}>${name}</option>
      `).join('');

      const bars = data.notes.map(note => {
        const left = (note.start_ms / total) * 100;
        const width = Math.max(0.6, (note.duration_ms / total) * 100);
        const top = ((maxPitch - note.pitch) / pitchSpan) * 100;
        const mods = (note.modifiers || []).map(mod => `mod-${mod.replace(/[^a-z0-9]+/gi, '-')}`).join(' ');
        return `<div class="roll-note art-${note.articulation_key} ${mods}" style="left:${left}%;width:${width}%;top:${top}%;" title="${note.note_name} · ${note.articulation}"></div>`;
      }).join('');

      return `
        <div class="preview-shell" data-player-root>
          <div class="player-bar sticky-player-bar">
            <div class="player-controls">
              <button type="button" class="btn btn-sm primary" data-play-btn>Play</button>
              <button type="button" class="btn btn-sm" data-restart-btn>С начала</button>
            </div>
            <input type="range" min="0" max="${Math.max(1, data.duration_ms)}" value="0" step="10" data-seek>
            <div class="time-label mono" data-time-label>0:00 / ${formatMs(data.duration_ms)}</div>
          </div>
          <div class="preview-summary">
            <span class="badge accent plain">${data.note_count} нот</span>
            <label class="preset-select-wrap">
              <span class="muted">Preset</span>
              <select data-preset-select>
                ${presetOptions}
              </select>
            </label>
            <span class="badge plain">диапазон ${data.pitch_min}–${data.pitch_max}</span>
            <span class="badge plain">длина ${formatMs(data.duration_ms)}</span>
          </div>
          <div class="roll-frame" data-roll-frame>
            <div class="roll-grid"></div>
            <div class="roll-playhead" data-playhead></div>
            <div class="roll-notes">${bars}</div>
          </div>
        </div>
      `;
    }

    function wirePlayer(root, data) {
      const playBtn = root.querySelector('[data-play-btn]');
      const restartBtn = root.querySelector('[data-restart-btn]');
      const seek = root.querySelector('[data-seek]');
      const timeLabel = root.querySelector('[data-time-label]');
      const presetSelect = root.querySelector('[data-preset-select]');
      const playhead = root.querySelector('[data-playhead]');
      const rollFrame = root.querySelector('[data-roll-frame]');
      const state = {
        notes: data.notes.map((note, idx, arr) => {
          const next = arr[idx + 1];
          let previewTargetPitch = note.pitch + 2;
          if (next && Math.abs((next.start_ms ?? 0) - (note.end_ms ?? 0)) <= 260) {
            previewTargetPitch = next.pitch;
          }
          return { ...note, previewTargetPitch };
        }),
        instrumentPreset: data.instrument_preset || 'default',
        durationMs: Math.max(1, data.duration_ms || 1),
        currentMs: 0,
        playbackOffsetMs: 0,
        startedAtCtxTime: 0,
        startedAtPerfMs: 0,
        audioCtx: null,
        isPlaying: false,
        timeouts: [],
        oscillators: [],
        rafId: null,
        playBtn,
        seek,
        timeLabel,
        presetSelect,
        playhead,
        rollFrame,
        resumeAfterSeek: false,
      };

      syncPlayerUi(state);

      playBtn.addEventListener('click', async (event) => {
        event.stopPropagation();
        if (state.isPlaying) {
          stopPlayer(state, true);
        } else {
          try {
            await startPlayer(state);
          } catch (error) {
            console.error('preview playback failed', error);
          }
        }
      });

      restartBtn.addEventListener('click', async (event) => {
        event.stopPropagation();
        await seekTo(state, 0, true);
      });

      seek.addEventListener('click', event => event.stopPropagation());
      seek.addEventListener('pointerdown', event => {
        event.stopPropagation();
        state.resumeAfterSeek = state.isPlaying;
        if (state.isPlaying) stopPlayer(state, true);
      });

      seek.addEventListener('input', event => {
        event.stopPropagation();
        state.currentMs = clampMs(state, seek.value);
        state.playbackOffsetMs = state.currentMs;
        syncPlayerUi(state);
      });

      seek.addEventListener('change', async event => {
        event.stopPropagation();
        await seekTo(state, seek.value, state.resumeAfterSeek);
        state.resumeAfterSeek = false;
      });

      presetSelect.addEventListener('click', event => event.stopPropagation());
      presetSelect.addEventListener('change', async event => {
        event.stopPropagation();
        state.instrumentPreset = presetSelect.value || 'default';
        try {
          if (!state.audioCtx) state.audioCtx = getAudioContext();
          await loadInstrument(state, state.instrumentPreset);
        } catch (error) {
          console.error('soundfont load failed', error);
        }
      });

      rollFrame.addEventListener('click', async event => {
        event.stopPropagation();
        const rect = rollFrame.getBoundingClientRect();
        const ratio = rect.width ? ((event.clientX - rect.left) / rect.width) : 0;
        await seekTo(state, state.durationMs * Math.max(0, Math.min(1, ratio)), state.isPlaying);
      });
    }

    async function ensurePreview(url, row) {
      if (!previewCache.has(url)) {
        const response = await fetch(url, { credentials: 'same-origin' });
        if (!response.ok) throw new Error(`preview ${response.status}`);
        previewCache.set(url, await response.json());
      }
      const data = previewCache.get(url);
      const cell = row.querySelector('td') || row;
      if (!row.dataset.loaded) {
        cell.innerHTML = buildPreviewMarkup(data);
        wirePlayer(cell.querySelector('[data-player-root]'), data);
        row.dataset.loaded = '1';
      }
    }

    async function togglePreview(summaryRow) {
      const targetId = summaryRow.dataset.previewRow;
      const url = summaryRow.dataset.previewUrl;
      const row = document.getElementById(targetId);
      const isHidden = row.classList.contains('is-hidden');

      document.querySelectorAll('.preview-row').forEach(other => {
        if (other !== row) other.classList.add('is-hidden');
      });
      document.querySelectorAll('.track-summary-row').forEach(other => {
        if (other !== summaryRow) other.classList.remove('is-open');
      });

      if (!isHidden) {
        row.classList.add('is-hidden');
        summaryRow.classList.remove('is-open');
        return;
      }

      row.classList.remove('is-hidden');
      summaryRow.classList.add('is-open');
      try {
        await ensurePreview(url, row);
      } catch (error) {
        const cell = row.querySelector('td') || row;
        cell.innerHTML = `<div class="preview-error">Не удалось загрузить preview: ${error.message}</div>`;
      }
    }

    document.querySelectorAll('.track-summary-row').forEach(row => {
      row.addEventListener('click', async (event) => {
        if (event.target !== row && event.target.closest('a, button, input, label, select, form, details, summary')) return;
        await togglePreview(row);
      });
    });

})();
