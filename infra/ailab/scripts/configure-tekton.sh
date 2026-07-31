#!/usr/bin/env bash
# Idempotent Tekton Pipelines configuration for this single-node cluster.
#
# `coschedule=workspaces` (the default) runs an affinity assistant that refuses a
# TaskRun binding more than one PersistentVolumeClaim, so a pipeline cannot have
# both a shared source workspace and a separate durable evidence workspace. On a
# single-node cluster co-scheduling buys nothing — every pod lands on the same
# node regardless — so it is disabled.
#
# Reverting: set the value back to `workspaces`.
#
# Run on the AILab host.
set -euo pipefail

KUBECTL=${KUBECTL:-"sudo k3s kubectl"}
NS=tekton-pipelines
CM=feature-flags
WANTED=disabled

current=$($KUBECTL -n "$NS" get cm "$CM" \
  -o jsonpath='{.data.coschedule}' 2>/dev/null || true)

if [ "$current" = "$WANTED" ]; then
  echo "coschedule already '$WANTED'; nothing to do"
  exit 0
fi

echo "coschedule: '${current:-<unset>}' -> '$WANTED'"
$KUBECTL -n "$NS" patch cm "$CM" --type merge \
  -p "{\"data\":{\"coschedule\":\"$WANTED\"}}"

# The controller reads the ConfigMap through an informer, but restarting it makes
# the change take effect deterministically rather than eventually.
$KUBECTL -n "$NS" rollout restart deployment/tekton-pipelines-controller
$KUBECTL -n "$NS" rollout status deployment/tekton-pipelines-controller --timeout=180s

echo "coschedule now: $($KUBECTL -n "$NS" get cm "$CM" -o jsonpath='{.data.coschedule}')"
