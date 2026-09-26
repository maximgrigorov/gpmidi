# CLAUDE.md

@AGENTS.md

## Where the code lives and what you can reach

- Primary remote: Gitea `http://server:3300/mgrigorov/gpmidi` (home LAN only).
  Tekton on AILab builds and deploys exact SHAs from Gitea `main`.
- GitHub `maximgrigorov/gpmidi` is a **public** copy of Gitea `main`, so that
  cloud sessions and design tools can read the code. Sync is manual; there is no
  mirror job.
- From a cloud or remote session, Gitea, AILab, Tekton and the real `.gp`
  sample files are unreachable. Do not try to deploy, run CI or touch live
  infrastructure. Work on a branch, open a PR on GitHub, never push to `main`.
- The repository is public: never commit secrets, tokens, personal data or
  generated MIDI/audio/GP listening artifacts.
- `CLAUDE_TASK.md` is a finished SheetSage2 handoff (2026-09-24), not a
  current task.

## Setup (mirrors the Docker image)

Python 3.11 — `numpy==1.23.2` has no wheels for newer Pythons. `tuttut`
declares obsolete GUI dependencies, so install it without them:

```bash
python3.11 -m venv .venv
sed '/^tuttut==/d' requirements.txt > /tmp/requirements-headless.txt
.venv/bin/pip install -r /tmp/requirements-headless.txt 'matplotlib==3.7.5' ruff
.venv/bin/pip install --no-deps tuttut==0.0.6
```

Checks (the same gates Tekton runs first):

```bash
.venv/bin/python -m pytest -q   # 344 passed / 27 skipped on 677eadb; sample-dependent tests skip
.venv/bin/ruff check .
```

Web app: `PORT=8765 SECRET_KEY=dev GPMIDI_DATA_ROOT=/tmp/gpmidi-data .venv/bin/python app.py`.

## Current work

- Web UI as a step-by-step wizard: plan `docs/UI_WIZARD_PLAN.md`, design brief
  `docs/CLAUDE_DESIGN_PROMPT.md`. Conversion semantics must not change.
