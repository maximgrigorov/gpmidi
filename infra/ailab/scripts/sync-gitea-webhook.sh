#!/usr/bin/env bash
set -Eeuo pipefail

# Keep the Gitea webhook target aligned with the EventListener-owned Service.
# Tekton Triggers owns/recreates Service/el-gitea-listener, so its ClusterIP may
# change after reinstall/reconciliation. Gitea runs on the AILab host, which can
# route to the k3s Service CIDR; this script resolves the current IP and updates
# the repository hook without exposing either credential.
#
# Run on AILab as an authorized operator:
#   bash infra/ailab/scripts/sync-gitea-webhook.sh [hook-id]

NAMESPACE="gpmidi-ml"
SERVICE="el-gitea-listener"
WEBHOOK_SECRET="gitea-webhook-secret"
GITEA_URL="http://192.168.30.2:3300"
REPOSITORY="mgrigorov/gpmidi"
GITEA_USER="${GITEA_USER:-mgrigorov}"
HOOK_ID="${1:-1}"
TMP="$(mktemp -d)"
TOKEN=""
if [ -n "${KUBECTL:-}" ]; then
    read -r -a KUBE_CMD <<<"$KUBECTL"
else
    KUBE_CMD=(sudo k3s kubectl)
fi

cleanup() {
    unset TOKEN
    rm -rf "$TMP"
}
trap cleanup EXIT
chmod 700 "$TMP"

for cmd in curl python3 base64; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: required command not found: $cmd" >&2
        exit 1
    }
done

CLUSTER_IP="$("${KUBE_CMD[@]}" -n "$NAMESPACE" get service "$SERVICE" \
    -o jsonpath='{.spec.clusterIP}')"
if [[ ! "$CLUSTER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: invalid ClusterIP returned for $NAMESPACE/$SERVICE" >&2
    exit 1
fi
printf 'http://%s:8080' "$CLUSTER_IP" > "$TMP/url"

"${KUBE_CMD[@]}" -n "$NAMESPACE" get secret "$WEBHOOK_SECRET" \
    -o jsonpath='{.data.secret}' | base64 -d > "$TMP/webhook-secret"
chmod 600 "$TMP/webhook-secret"
if [ ! -s "$TMP/webhook-secret" ]; then
    echo "ERROR: $NAMESPACE/$WEBHOOK_SECRET key 'secret' is empty" >&2
    exit 1
fi

if [ -n "${GITEA_TOKEN_FILE:-}" ]; then
    TOKEN="$(<"$GITEA_TOKEN_FILE")"
else
    read -rsp 'Gitea repository token: ' TOKEN
    printf '\n'
fi
if [ -z "$TOKEN" ]; then
    echo "ERROR: empty Gitea token" >&2
    exit 1
fi

printf 'machine 192.168.30.2 login %s password %s\n' "$GITEA_USER" "$TOKEN" \
    > "$TMP/netrc"
unset TOKEN
chmod 600 "$TMP/netrc"

python3 - "$TMP" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
payload = {
    "active": True,
    "branch_filter": "*",
    "events": ["push"],
    "config": {
        "url": (root / "url").read_text(encoding="utf-8"),
        "content_type": "json",
        "secret": (root / "webhook-secret").read_text(encoding="utf-8"),
    },
}
(root / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
PY
chmod 600 "$TMP/payload.json"

curl -fsS --netrc-file "$TMP/netrc" -X PATCH \
    -H 'Content-Type: application/json' \
    --data-binary @"$TMP/payload.json" \
    "$GITEA_URL/api/v1/repos/$REPOSITORY/hooks/$HOOK_ID" \
    > "$TMP/response.json"

python3 - "$TMP/response.json" <<'PY'
import json
import sys

hook = json.load(open(sys.argv[1], encoding="utf-8"))
print(
    f"hook={hook['id']} active={hook['active']} events={hook['events']} "
    f"url={hook['config']['url']} "
    f"content_type={hook['config'].get('content_type')}"
)
PY
