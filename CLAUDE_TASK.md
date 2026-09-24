# Claude Code entrypoint — SheetSage2 Gitea/AIlab reconciliation

Read, in order:

1. `AGENTS.md`
2. `docs/CODEX_PROJECT_GUIDE.md`
3. `docs/handoffs/CLAUDE_SHEETSAGE2_GITEA_AILAB_RECONCILE.md`

Repository: `http://192.168.30.2:3300/mgrigorov/gpmidi.git`

Branch: `feat/sheetsage2-on-demand-tool`

Verify the branch resolves to the exact 40-character handoff SHA supplied by the operator before starting. The implementation baseline immediately before this handoff is `f03b733272f5fd615b7627e462c0ffa9abfd2f2f`.

Objective: use the explicitly authorized SSH route `mgrigorov@ailab` to apply the minimum reviewed NetworkPolicy reconciliation, run exact-SHA Tekton delivery from Gitea, and prove the real gpmidi-web -> SheetSage2 API -> GPU Job -> persisted downloads vertical slice.

Hard boundaries: no GitHub push, no `main` merge, no force-push, no Gitea PR operation, no secrets in output, no guardrail bypass, no unrelated workload changes, and no AILab shutdown.
