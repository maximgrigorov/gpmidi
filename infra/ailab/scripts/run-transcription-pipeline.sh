#!/usr/bin/env bash
# Build, audit, and execute the Phase 3 Basic Pitch candidate through Tekton.
# Usage:
#   KUBECONFIG=/path/to/kubeconfig run-transcription-pipeline.sh \
#     <commit-sha> <ci-image> <input-wav-url> <reference-midi-url>
set -euo pipefail

NAMESPACE=${NAMESPACE:-gpmidi-ml}
REPO_URL=${REPO_URL:-http://192.168.30.2:3300/mgrigorov/gpmidi.git}
REGISTRY=${REGISTRY:-192.168.30.2:3300}
OWNER=${OWNER:-mgrigorov}
KUBECTL_BIN=${KUBECTL_BIN:-kubectl}
SOURCE_PVC=${SOURCE_PVC:-}
EVIDENCE_PVC=${EVIDENCE_PVC:-}
INPUT_SHA256=d3e5cec64a13fc8c35050f84b9e67f30124d8bb4010295ff7524fd9ccc0cbb1f
REFERENCE_SHA256=82250c89783273ce847500a7f9a582e6df74e9e2ea8be3af296f480d2f1b8ed9

usage() {
  echo "usage: $0 <commit-sha> <ci-image> <input-wav-url> <reference-midi-url>" >&2
  exit 2
}

sha=${1:-}
ci_image=${2:-}
input_url=${3:-}
reference_url=${4:-}
[[ $sha =~ ^[0-9a-f]{40}$ ]] || usage
[[ $ci_image =~ ^[A-Za-z0-9._:/@-]+$ ]] || usage
[[ $input_url =~ ^http://[A-Za-z0-9._:/-]+$ ]] || usage
[[ $reference_url =~ ^http://[A-Za-z0-9._:/-]+$ ]] || usage
if [[ -n $SOURCE_PVC || -n $EVIDENCE_PVC ]]; then
  [[ $SOURCE_PVC =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || usage
  [[ $EVIDENCE_PVC =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || usage
  workspace_bindings=$(cat <<EOF
  - name: source
    persistentVolumeClaim:
      claimName: ${SOURCE_PVC}
  - name: evidence
    persistentVolumeClaim:
      claimName: ${EVIDENCE_PVC}
EOF
)
else
  workspace_bindings=$(cat <<'EOF'
  - name: source
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests: {storage: 4Gi}
  - name: evidence
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests: {storage: 4Gi}
EOF
)
fi

manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
cat >"$manifest" <<EOF
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: phase3-transcription-
  namespace: ${NAMESPACE}
  labels:
    gpmidi.ailab/trigger: manual-candidate
    gpmidi.ailab/purpose: phase3-transcription-spike
spec:
  pipelineSpec:
    params:
    - name: revision
      type: string
    - name: ci-image
      type: string
    - name: input-url
      type: string
    - name: reference-url
      type: string
    workspaces:
    - name: source
    - name: evidence
    - name: evidence-archive
    - name: docker-config
    results:
    - name: commit-sha
      value: \$(tasks.clone.results.commit-sha)
    - name: basic-pitch-digest
      value: \$(tasks.build-basic-pitch.results.digest)
    tasks:
    - name: clone
      taskRef:
        name: git-clone-exact
      params:
      - name: repo-url
        value: ${REPO_URL}
      - name: revision
        value: \$(params.revision)
      - name: expected-sha
        value: \$(params.revision)
      workspaces:
      - name: source
        workspace: source

    - name: transcription-tests
      runAfter: [clone]
      params:
      - name: ci-image
        value: \$(params.ci-image)
      taskSpec:
        params:
        - name: ci-image
          type: string
        workspaces:
        - name: source
        - name: evidence
        steps:
        - name: pytest
          image: \$(params.ci-image)
          workingDir: /workspace/source/services/transcription_spike
          computeResources:
            requests: {cpu: 200m, memory: 256Mi}
            limits: {cpu: "2", memory: 2Gi}
          script: |
            #!/bin/bash
            set -euo pipefail
            find /workspace/evidence -mindepth 1 -maxdepth 1 -exec rm -rf {} +
            /opt/venv-svc/bin/python -m pytest -q 2>&1 \
              | tee /workspace/evidence/pytest-transcription-spike.txt
            exit "\${PIPESTATUS[0]}"
        - name: static-checks
          image: \$(params.ci-image)
          workingDir: /workspace/source
          script: |
            #!/bin/bash
            set -euo pipefail
            /opt/venv-svc/bin/python -m ruff check services/transcription_spike 2>&1 \
              | tee /workspace/evidence/ruff-transcription-spike.txt
            git diff --check origin/main...HEAD 2>&1 \
              | tee /workspace/evidence/git-diff-check-phase3.txt
      workspaces:
      - name: source
        workspace: source
      - name: evidence
        workspace: evidence

    - name: build-basic-pitch
      runAfter: [transcription-tests]
      taskRef:
        name: kaniko-build-push
      params:
      - name: image
        value: ${REGISTRY}/${OWNER}/basic-pitch:\$(tasks.clone.results.commit-sha)
      - name: context
        value: services/transcription_spike/runtimes/basic-pitch
      - name: dockerfile
        value: services/transcription_spike/runtimes/basic-pitch/Dockerfile
      workspaces:
      - name: source
        workspace: source
      - name: docker-config
        workspace: docker-config

    - name: audit-basic-pitch
      runAfter: [build-basic-pitch]
      taskRef:
        name: image-audit
      params:
      - name: ci-image
        value: \$(params.ci-image)
      - name: image
        value: ${REGISTRY}/${OWNER}/basic-pitch:\$(tasks.clone.results.commit-sha)
      - name: digest
        value: \$(tasks.build-basic-pitch.results.digest)
      - name: commit-sha
        value: \$(tasks.clone.results.commit-sha)
      - name: name
        value: basic-pitch
      - name: expect-user
        value: "65532"
      workspaces:
      - name: evidence
        workspace: evidence
      - name: docker-config
        workspace: docker-config

    - name: real-input
      runAfter: [audit-basic-pitch]
      params:
      - name: ci-image
        value: \$(params.ci-image)
      - name: model-image
        value: ${REGISTRY}/${OWNER}/basic-pitch@\$(tasks.build-basic-pitch.results.digest)
      - name: input-url
        value: \$(params.input-url)
      - name: reference-url
        value: \$(params.reference-url)
      taskSpec:
        params:
        - name: ci-image
          type: string
        - name: model-image
          type: string
        - name: input-url
          type: string
        - name: reference-url
          type: string
        workspaces:
        - name: source
        - name: evidence
        steps:
        - name: fetch-verified-inputs
          image: \$(params.ci-image)
          computeResources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: 500m, memory: 512Mi}
          script: |
            #!/bin/bash
            set -euo pipefail
            out=/workspace/evidence/phase3-real-input
            mkdir -p "\$out"
            curl -fsSLo "\$out/input.wav" '\$(params.input-url)'
            curl -fsSLo "\$out/reference.mid" '\$(params.reference-url)'
            printf '%s  %s\n' '${INPUT_SHA256}' "\$out/input.wav" \
              | sha256sum -c -
            printf '%s  %s\n' '${REFERENCE_SHA256}' "\$out/reference.mid" \
              | sha256sum -c -
            chown -R 65532:65532 "\$out"
        - name: infer
          image: \$(params.model-image)
          command: ["/bin/sh", "-c"]
          args:
          - |
            set -eu
            python /app/run.py \
              --input /workspace/evidence/phase3-real-input/input.wav \
              --output /workspace/evidence/phase3-real-input/prediction.mid \
              > /workspace/evidence/phase3-real-input/runtime-summary.json
            cat /workspace/evidence/phase3-real-input/runtime-summary.json
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            runAsUser: 65532
            runAsGroup: 65532
          computeResources:
            requests: {cpu: "1", memory: 1Gi}
            limits: {cpu: "2", memory: 2Gi}
        - name: evaluate
          image: \$(params.ci-image)
          workingDir: /workspace/source/services/transcription_spike
          computeResources:
            requests: {cpu: 200m, memory: 256Mi}
            limits: {cpu: "1", memory: 1Gi}
          script: |
            #!/bin/bash
            set -euo pipefail
            out=/workspace/evidence/phase3-real-input
            PYTHONPATH=. /opt/venv-svc/bin/python -m transcription_spike.real_input_evaluation \
              --reference-midi "\$out/reference.mid" \
              --prediction-midi "\$out/prediction.mid" \
              --prediction-offset 195 \
              --window-start 195 \
              --window-end 215 \
              --output-json "\$out/evaluation.json"
            /opt/venv-svc/bin/python - "\$out" '\$(params.model-image)' <<'PY'
            import hashlib
            import json
            import pathlib
            import sys

            out = pathlib.Path(sys.argv[1])
            image = sys.argv[2]
            evaluation = json.loads((out / "evaluation.json").read_text())
            summary = json.loads((out / "runtime-summary.json").read_text())
            exact = evaluation["metrics"]["exact_pitch_50ms"]
            onset = evaluation["metrics"]["onset_only_50ms"]
            assert (exact["true_positives"], exact["false_positives"], exact["false_negatives"]) == (14, 64, 44)
            assert (onset["true_positives"], onset["false_positives"], onset["false_negatives"]) == (20, 58, 38)
            assert summary["model_sha256"] == "3db297d54af8e01c6e5618245c956b1d71b6a2b978cb2dedb527173186552676"
            evidence = {
                "candidate_image": image,
                "gp_grid_used": False,
                "input_sha256": "${INPUT_SHA256}",
                "reference_sha256": "${REFERENCE_SHA256}",
                "prediction_sha256": hashlib.sha256((out / "prediction.mid").read_bytes()).hexdigest(),
                "evaluation_sha256": hashlib.sha256((out / "evaluation.json").read_bytes()).hexdigest(),
                "runtime": summary,
                "verified_metrics": {"exact_pitch_50ms": exact, "onset_only_50ms": onset},
            }
            (out / "provenance.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            print(json.dumps(evidence, sort_keys=True))
            PY
            (cd "\$out" && sha256sum input.wav reference.mid prediction.mid runtime-summary.json evaluation.json provenance.json > SHA256SUMS)
      workspaces:
      - name: source
        workspace: source
      - name: evidence
        workspace: evidence

    finally:
    - name: publish-evidence
      taskRef:
        name: publish-evidence
      params:
      - name: ci-image
        value: \$(params.ci-image)
      - name: commit-sha
        value: \$(tasks.clone.results.commit-sha)
      - name: run-name
        value: phase3-transcription
      workspaces:
      - name: evidence
        workspace: evidence
      - name: evidence-archive
        workspace: evidence-archive
  params:
  - name: revision
    value: ${sha}
  - name: ci-image
    value: ${ci_image}
  - name: input-url
    value: ${input_url}
  - name: reference-url
    value: ${reference_url}
  taskRunTemplate:
    serviceAccountName: tekton-build
    podTemplate:
      hostAliases:
      - ip: 192.168.30.2
        hostnames: [server]
  workspaces:
${workspace_bindings}
  - name: evidence-archive
    persistentVolumeClaim:
      claimName: tekton-evidence
  - name: docker-config
    secret:
      secretName: gitea-registry-auth
  timeouts:
    pipeline: 2h
    tasks: 1h45m
EOF

"$KUBECTL_BIN" create -f "$manifest" -o name
