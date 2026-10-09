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
      fileError.textContent = tooLarge ? `The file is larger than ${Math.round(max / 1024 / 1024)} MB.` : '';
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
  const formSteps = ['tracks', 'sound', 'extra'];
  const validSteps = [...formSteps, 'result'];
  const currentStep = () => {
    const step = new URL(location.href).searchParams.get('step');
    if (wizard) return formSteps.includes(step) ? step : 'tracks';
    return 'result';
  };
  const showStep = (step, push = true) => {
    if (!validSteps.includes(step)) return;
    document.body.classList.add('js-ready');
    panels.forEach(panel => { panel.hidden = panel.dataset.step !== step; });
    $$('.stepper li').forEach((item, index) => item.classList.toggle('active', index === validSteps.indexOf(step) + 1));
    if (push) { const url = new URL(location.href); url.searchParams.set('step', step); history.pushState({step}, '', url); }
    window.scrollTo({top: 0});
  };
  if (wizard) showStep(currentStep(), false);
  // Without JS the group switches do nothing, so the table ships open; with JS
  // the groups drive it and it starts collapsed, as in the mockup.
  $('[data-matrix]', wizard)?.removeAttribute('open');
  window.addEventListener('popstate', () => { if (wizard) showStep(currentStep(), false); });

  // The table cells are the only inputs with names: group controls (data-role +
  // data-effect, role "*" = every track) set cells and read their state back.
  const parents = {
    ghost_notes: 'humanize', lock_to_drums: 'humanize', double_track: 'humanize',
    palm_mute_motion: 'humanize', fret_hand_cost: 'humanize',
    humanize_timing_over_gp_offsets: 'humanize', keep_gp_played_overlaps: 'preserve_gp_played_offsets',
  };
  const precisionKeys = ['expand_gp_hidden_32nds', 'preserve_gp_played_offsets', 'keep_gp_played_overlaps', 'humanize_timing_over_gp_offsets'];
  const rows = () => $$('[data-effect-row]', wizard);
  const rowSelected = row => row.dataset.selected !== '0';
  const rowsOfRole = role => rows().filter(row => role === '*' || row.dataset.role === role);
  const cellsFor = (role, effect, onlySelected = false) => rowsOfRole(role)
    .filter(row => !onlySelected || rowSelected(row))
    .map(row => $(`input[data-effect="${effect}"]`, row)).filter(Boolean);
  const cellOf = (row, effect) => $(`input[data-effect="${effect}"]`, row);
  const cellEnabled = cell => {
    const row = cell.closest('[data-effect-row]');
    if (!rowSelected(row)) return false;
    const parent = parents[cell.dataset.effect];
    if (parent && !cellOf(row, parent)?.checked) return false;
    if (cell.dataset.effect === 'humanize_timing_over_gp_offsets' && !cellOf(row, 'preserve_gp_played_offsets')?.checked) return false;
    return true;
  };
  const selectedTracks = () => $$('input[name="track_indices"]', wizard).filter(input => input.checked);

  function refresh() {
    if (!wizard) return;
    const selected = new Set(selectedTracks().map(input => input.value));
    rows().forEach(row => { row.dataset.selected = selected.has(row.dataset.track) ? '1' : '0'; });
    const noTracks = $('[data-no-tracks]', wizard); if (noTracks) noTracks.hidden = selected.size > 0;
    const count = $('[data-selected-count]', wizard); if (count) count.textContent = `${selected.size} of ${rows().length} selected`;
    rows().forEach(row => $$('input[data-effect]', row).forEach(cell => {
      const on = cellEnabled(cell);
      cell.disabled = !on;
      cell.closest('td')?.classList.toggle('disabled', !on);
      cell.closest('td')?.classList.toggle('exception', Boolean(cell.dataset.exception));
    }));

    $$('[data-role][data-effect]', wizard).forEach(control => {
      const cells = cellsFor(control.dataset.role, control.dataset.effect, true);
      const option = control.closest('[data-group-option]');
      if (option) option.hidden = cells.length === 0;
      const on = cells.filter(cell => cell.checked).length;
      control.checked = cells.length > 0 && on === cells.length;
      control.indeterminate = on > 0 && on < cells.length;
      control.disabled = cells.length === 0 || cells.every(cell => cell.disabled);
      control.closest('label')?.classList.toggle('is-disabled', control.disabled);
    });

    $$('[data-role-group]', wizard).forEach(card => {
      const role = card.dataset.roleGroup;
      const roleRows = rowsOfRole(role).filter(rowSelected);
      $('[data-role-empty]', card).hidden = roleRows.length > 0;
      $('[data-role-body]', card).hidden = roleRows.length === 0;
      card.classList.toggle('is-empty', roleRows.length === 0);
      $('[data-role-tracks]', card).textContent = roleRows.map(row => row.dataset.trackName).join(', ');
      const humanize = $('input[data-effect="humanize"]', card);
      const humanizeOn = Boolean(humanize && (humanize.checked || humanize.indeterminate));
      const nested = $('[data-nested]', card);
      const nestedCount = $$('[data-group-option]', nested).filter(option => !option.hidden).length;
      nested.hidden = !humanizeOn || nestedCount === 0;
      const hint = $('[data-nested-hint]', card);
      hint.hidden = humanizeOn || nestedCount === 0;
      hint.textContent = `${nestedCount} more option${nestedCount === 1 ? '' : 's'} once humanize is on`;
      const exceptions = rowsOfRole(role).some(row => $$('input[data-exception]', row).length > 0);
      $('[data-clear-exceptions]', card).hidden = !exceptions;
    });
    const others = rowsOfRole('other').filter(rowSelected);
    const otherLine = $('[data-other-line]', wizard);
    if (otherLine) {
      otherLine.hidden = others.length === 0;
      $('[data-role-tracks-other]', otherLine).textContent = others.map(row => row.dataset.trackName).join(', ');
    }

    const preserve = cellsFor('*', 'preserve_gp_played_offsets', true);
    const soloRule = cell => cell.checked === (cell.closest('[data-effect-row]').dataset.role === 'solo');
    const mode = preserve.every(soloRule) ? 'solo'
      : preserve.every(cell => cell.checked) ? 'all'
        : preserve.every(cell => !cell.checked) ? 'none' : 'custom';
    $$('[data-offsets-mode]', wizard).forEach(button => button.setAttribute('aria-pressed', String(button.dataset.offsetsMode === mode)));
    const precisionChanges = precisionKeys.flatMap(key => cellsFor('*', key, true))
      .filter(cell => cell.checked !== (cell.dataset.default === '1')).length;
    const precisionCount = $('[data-precision-count]', wizard);
    if (precisionCount) {
      precisionCount.textContent = precisionChanges ? `changed · ${precisionChanges}` : 'default';
      precisionCount.classList.toggle('changed', precisionChanges > 0);
    }
    const exceptionCount = rows().filter(rowSelected).flatMap(row => $$('input[data-exception]', row)).length;
    const changeCount = $('[data-change-count]', wizard);
    if (changeCount) {
      changeCount.textContent = exceptionCount ? `overrides: ${exceptionCount}` : 'same as groups';
      changeCount.classList.toggle('changed', exceptionCount > 0);
    }

    const list = $('[data-outcome-list]', wizard);
    if (list) {
      const active = rows().filter(rowSelected);
      // Column order of the table = the order of EFFECT_LABELS on the server.
      const keys = $$('.effect-table thead th[data-effect-key]', wizard).map(th => th.dataset.effectKey);
      const lines = [];
      if (!active.some(row => cellOf(row, 'humanize')?.checked)) lines.push('Without humanize — MIDI as written');
      keys.forEach(key => {
        const on = active.filter(row => { const cell = cellOf(row, key); return cell && cell.checked && !cell.disabled; });
        if (on.length) lines.push(`${cellOf(on[0], key).dataset.label}: ${on.map(row => row.dataset.trackName).join(', ')}`);
      });
      list.replaceChildren(...lines.map(text => { const item = document.createElement('li'); item.textContent = text; return item; }));
    }

    const drumRows = rowsOfRole('drums').filter(rowSelected);
    const drumOn = drumRows.some(row => cellOf(row, 'humanize')?.checked);
    const lockOn = cellsFor('*', 'lock_to_drums', true).some(cell => cell.checked && !cell.disabled);
    const warning = $('[data-drum-warning]', wizard);
    if (warning) {
      warning.hidden = !(lockOn && !drumOn);
      $('[data-drum-warning-text]', warning).textContent = drumRows.length
        ? 'The rhythm section will lock to a straight grid — you won’t hear a difference.'
        : 'No drum track is selected — there is nothing to lock to.';
      $('[data-humanize-drums]', warning).hidden = drumRows.length === 0;
    }
  }

  $$('input[name="track_indices"]', wizard).forEach(input => input.addEventListener('change', refresh));
  $$('[data-role-select]', wizard).forEach(select => select.addEventListener('change', () => {
    const card = select.closest('[data-track-card]');
    card.dataset.role = select.value;
    const row = $(`[data-effect-row][data-track="${card.dataset.track}"]`, wizard);
    if (row) row.dataset.role = select.value;
    refresh();
  }));
  $$('[data-role][data-effect]', wizard).forEach(control => control.addEventListener('change', () => {
    cellsFor(control.dataset.role, control.dataset.effect).forEach(cell => { if (!cell.dataset.exception) cell.checked = control.checked; });
    refresh();
  }));
  rows().forEach(row => $$('input[data-effect]', row).forEach(cell => cell.addEventListener('change', () => {
    cell.dataset.exception = '1';
    refresh();
  })));
  $$('[data-clear-exceptions]', wizard).forEach(button => button.addEventListener('click', () => {
    const roleRows = rowsOfRole(button.dataset.clearExceptions);
    const effects = new Set(roleRows.flatMap(row => $$('input[data-exception]', row).map(cell => cell.dataset.effect)));
    effects.forEach(effect => {
      const cells = roleRows.map(row => cellOf(row, effect)).filter(Boolean);
      const normal = cells.find(cell => !cell.dataset.exception);
      cells.forEach(cell => {
        if (cell.dataset.exception) cell.checked = normal ? normal.checked : cell.dataset.default === '1';
        delete cell.dataset.exception;
      });
    });
    refresh();
  }));
  $$('[data-offsets-mode]', wizard).forEach(button => button.addEventListener('click', () => {
    const mode = button.dataset.offsetsMode;
    rows().forEach(row => {
      const cell = cellOf(row, 'preserve_gp_played_offsets');
      if (!cell) return;
      cell.checked = mode === 'all' || (mode === 'solo' && row.dataset.role === 'solo');
      delete cell.dataset.exception;
    });
    refresh();
  }));
  $('[data-reset-precision]', wizard)?.addEventListener('click', () => {
    precisionKeys.forEach(key => cellsFor('*', key).forEach(cell => { cell.checked = cell.dataset.default === '1'; delete cell.dataset.exception; }));
    refresh();
  });
  $('[data-reset-defaults]', wizard)?.addEventListener('click', () => {
    rows().forEach(row => $$('input[data-effect]', row).forEach(cell => { cell.checked = cell.dataset.default === '1'; delete cell.dataset.exception; }));
    refresh();
  });
  $('[data-humanize-drums]', wizard)?.addEventListener('click', () => {
    rowsOfRole('drums').forEach(row => { const cell = cellOf(row, 'humanize'); if (cell) { cell.checked = true; delete cell.dataset.exception; } });
    refresh();
  });

  $$('[data-next]', wizard).forEach(button => button.addEventListener('click', () => {
    if (!selectedTracks().length) { refresh(); showStep('tracks'); return; }
    showStep(button.dataset.next);
  }));
  $$('[data-back]', wizard).forEach(button => button.addEventListener('click', () => showStep(button.dataset.back)));
  wizard?.addEventListener('submit', event => {
    if (!selectedTracks().length) { event.preventDefault(); refresh(); showStep('tracks'); return; }
    const overlay = $('[data-building]'); if (overlay) overlay.hidden = false;
    // A second click during a synchronous build would start a second version.
    setTimeout(() => $$('button[type="submit"]', wizard).forEach(button => { button.disabled = true; }), 0);
  });
  window.addEventListener('pageshow', () => {
    $$('button[type="submit"]', wizard).forEach(button => { button.disabled = false; });
    const overlay = $('[data-building]'); if (overlay) overlay.hidden = true;
  });
  refresh();

  $$('[data-daw-checklist]').forEach(list => {
    const key = `gpmidi:daw:${list.dataset.dawChecklist}`;
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(key) || '{}'); } catch (_) {}
    $$('[data-check]', list).forEach(box => { box.checked = Boolean(saved[box.dataset.check]); box.addEventListener('change', () => { saved[box.dataset.check] = box.checked; localStorage.setItem(key, JSON.stringify(saved)); }); });
  });

    // Piano-roll preview and player (moved from the old index.html unchanged).
    const previewCache = new Map();
    let activePlayer = null;

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
              <button type="button" class="btn btn-sm" data-restart-btn>Restart</button>
            </div>
            <input type="range" min="0" max="${Math.max(1, data.duration_ms)}" value="0" step="10" data-seek>
            <div class="time-label mono" data-time-label>0:00 / ${formatMs(data.duration_ms)}</div>
          </div>
          <div class="preview-summary">
            <span class="badge accent plain">${data.note_count} notes</span>
            <label class="preset-select-wrap">
              <span class="muted">Preset</span>
              <select data-preset-select>
                ${presetOptions}
              </select>
            </label>
            <span class="badge plain">range ${data.pitch_min}–${data.pitch_max}</span>
            <span class="badge plain">length ${formatMs(data.duration_ms)}</span>
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
        cell.innerHTML = `<div class="preview-error">Could not load the preview: ${error.message}</div>`;
      }
    }

    document.querySelectorAll('.track-summary-row').forEach(row => {
      row.addEventListener('click', async (event) => {
        if (event.target !== row && event.target.closest('a, button, input, label, select, form, details, summary')) return;
        await togglePreview(row);
      });
    });

})();
