# CODEX.md

This file is the entry point for Codex and other fresh coding-agent sessions.

## Required reading

Before editing anything, read in order:

1. `AGENTS.md`
2. `LESSONS.md`
3. `docs/PROJECT_STATUS.md`
4. `docs/CODEX_PROJECT_GUIDE.md`
5. the relevant ADR/evidence/component README

## Repository contract

- Repository: `http://192.168.30.2:3300/mgrigorov/gpmidi.git`
- Code of record: `main`
- Begin from current `origin/main`; state the branch and full commit SHA.
- Do not put tokens, kubeconfigs, `.env`, uploads or generated media/MIDI/GP files
  in Git or prompts.
- Do not change musical conversion semantics without explicit user approval.
- Do not build production images on the workstation.
- Accepted delivery is AILab Tekton from an exact 40-character SHA, then digest
  proof against the live pod `imageID`.
- Never delete/recreate project, analysis or evidence PVCs during deployment.
- A configured OpenAI token is not permission for a paid call.

## Definition of done

A code change is not complete until its relevant tests pass, the exact commit is
pushed, the requested deployment path succeeds, the live result is verified, and
`main` documentation reflects any changed contract. Full commands and credential
origins are in `docs/CODEX_PROJECT_GUIDE.md`.
