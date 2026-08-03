# Fable 5 review handoff — OpenAI whole-song arrangement provider

You are **Fable 5** acting as an independent senior code reviewer. Review only; do not edit, commit, push, deploy, call paid APIs, or access unrelated host data.

## Reproducible Git contract

- Repository URL: `http://192.168.30.2:3300/mgrigorov/gpmidi.git`
- Review branch: `review/openai-arrangement-provider`
- Base commit: `6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987`
- Start file: `arrangement_openai.py`
- This prompt's repository path: `docs/handoffs/FABLE_5_OPENAI_ARRANGEMENT_REVIEW.md`
- Review range: `6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987...origin/review/openai-arrangement-provider`

Use a clean checkout:

```bash
git clone http://192.168.30.2:3300/mgrigorov/gpmidi.git
cd gpmidi
git fetch origin --prune
git checkout --detach origin/review/openai-arrangement-provider
git status --short
git diff --stat 6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987...HEAD
git diff 6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987...HEAD
```

Fail closed if the branch is absent, the base is not an ancestor of HEAD, the worktree is dirty, or the diff is empty. Record the exact reviewed HEAD SHA.

## Intended behavior

This change adds an opt-in server-side OpenAI Responses API provider that creates a **production-quality whole-song expression plan** from already target-library-mapped MIDI context.

"Plan" and "draft_ready" are safety/workflow terms: generated JSON must not mutate MIDI until a separate explicit approval request. They do **not** mean the musical plan may be sparse or low quality.

Required properties:

1. No API token reaches browser HTML, responses, downloadable artifacts, logs, Git, or static manifests.
2. Provider is optional; baseline conversion succeeds without configuration and without a network call.
3. OpenAI failure does not destroy or replace baseline artifacts.
4. Responses API request uses strict JSON Schema and `store=false`.
5. Preflight spend guard runs before network I/O; post-call usage and estimated cost are recorded without claiming authoritative billing.
6. Generated plans are treated as untrusted and validated before storage/apply.
7. MIDI apply requires the correct per-job confirmation token; unsafe names/paths and replay are rejected.
8. Baseline MIDI is not overwritten. Apply produces separate enriched artifacts.
9. Apply cannot add/remove/repitch notes or change note timing/rhythm.
10. Hydra service/reserved events remain protected.
11. Solo/Lead guitar remains the mapped baseline: no velocity processing, velocity shift, variance, or timing humanization.
12. Musical prompt requests every measure and every track, coherent phrase/section dynamics, evidence-based balance, restrained variance, and no arbitrary strong attenuation such as an unexplained vocal `-8`.
13. UI describes a full expression plan while clearly retaining explicit approval before MIDI mutation.
14. Kubernetes uses a Secret reference rather than embedding credentials, and egress permits only the intended public HTTPS path without opening private/link-local destinations.

## Files requiring close review

Start with:

- `arrangement_openai.py`
- `app.py`
- `arrangement_processing.py`
- `arrangement_workflow.py`
- `templates/index.html`
- `test_arrangement_openai.py`
- `test_arrangement_openai_web.py`
- `infra/ailab/apps/gpmidi-web/deployment.yaml`
- `infra/ailab/apps/gpmidi-web/networkpolicy.yaml`
- `infra/ailab/scripts/check_manifests.py`

Also inspect every file in the review diff and enough unchanged surrounding code to verify integration claims.

## Required checks

Run from repository root without setting `OPENAI_TOKEN`:

```bash
python -m pytest -q
rendered="$(mktemp)"; trap 'rm -f "$rendered"' EXIT
kubectl kustomize infra/ailab >"$rendered"
python infra/ailab/scripts/check_manifests.py "$rendered"
git diff --check 6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987...HEAD
```

Do not make a real OpenAI request. Mocked tests are expected for provider behavior. Verify that tests prove network suppression on baseline/error/spend-guard paths, secret non-disclosure, strict schema use, usage accounting, apply authorization, replay handling, path safety, baseline preservation, and Solo policy.

## Review method

For each potential blocker:

1. cite exact file and line/range at reviewed HEAD;
2. quote or precisely describe the relevant code;
3. inspect surrounding validation/caller behavior before deciding;
4. explain a concrete failure scenario;
5. classify as `security`, `correctness`, `regression`, `test_gap`, `deployment`, or `musical_contract`;
6. distinguish blockers from non-blocking suggestions.

Do not report a missing guard if it exists in a downstream validator or apply layer. Do not mistake numeric constants such as token limits for credentials. Do not construct new containment bypasses or inspect unrelated host data.

## Required output

Return **only one valid JSON object**, with no Markdown fences or prose before/after it:

```json
{
  "schema_version": 1,
  "reviewer": "Fable 5",
  "repository": "http://192.168.30.2:3300/mgrigorov/gpmidi.git",
  "branch": "review/openai-arrangement-provider",
  "base_sha": "6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987",
  "head_sha": "<40-hex reviewed HEAD>",
  "range_valid": true,
  "tests": [
    {
      "command": "python -m pytest -q",
      "exit_code": 0,
      "summary": "<actual final test summary>"
    },
    {
      "command": "kubectl kustomize infra/ailab ><temporary-rendered-file> && python infra/ailab/scripts/check_manifests.py <temporary-rendered-file>",
      "exit_code": 0,
      "summary": "<actual final summary>"
    },
    {
      "command": "git diff --check 6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987...HEAD",
      "exit_code": 0,
      "summary": "<actual result>"
    }
  ],
  "blocking_findings": [
    {
      "id": "FBL-B001",
      "severity": "critical|high|medium",
      "category": "security|correctness|regression|test_gap|deployment|musical_contract",
      "file": "path/to/file",
      "line": 1,
      "evidence": "<specific code evidence>",
      "failure_scenario": "<concrete impact>",
      "recommended_fix": "<minimal fix>"
    }
  ],
  "non_blocking_findings": [
    {
      "id": "FBL-N001",
      "category": "quality|maintainability|performance|documentation|musical_contract",
      "file": "path/to/file",
      "line": 1,
      "evidence": "<specific evidence>",
      "recommendation": "<recommendation>"
    }
  ],
  "verified_properties": ["<property backed by source/tests>"],
  "musical_prompt_assessment": {
    "complete_measure_coverage_requested": true,
    "complete_track_coverage_requested": true,
    "coherent_section_shaping_requested": true,
    "arbitrary_large_attenuation_discouraged": true,
    "solo_baseline_preserved": true,
    "notes": "<concise evidence-based assessment>"
  },
  "verdict": "pass|fail",
  "summary": "<one concise sentence>"
}
```

Verdict rules:

- `fail` if any `blocking_findings` exist, any required command fails, range validation fails, or review evidence is incomplete.
- `pass` only if `blocking_findings` is empty and all required checks pass.
- Suggestions belong only in `non_blocking_findings` and do not force failure.
