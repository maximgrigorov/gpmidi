#!/usr/bin/env bash
set -Eeuo pipefail

# Mint a short-lived, namespaced kubeconfig for the installed hermes-operator
# ServiceAccount. This identity can monitor/start gpmidi Tekton runs and perform
# bounded live verification; it is not cluster-admin.
#
# Run on the AILab host from a trusted repository checkout:
#   bash infra/ailab/scripts/generate-operator-kubeconfig.sh [duration]
#
# Output contains a bearer token, is mode 0600, and must never be committed.

NAMESPACE="gpmidi-ml"
SA_NAME="hermes-operator"
DURATION="${1:-24h}"
OUTPUT="${OUTPUT:-$HOME/.kube/gpmidi-operator.kubeconfig}"
API_SERVER="https://192.168.30.2:6443"
CA_CERT="/data/k3s/server/tls/server-ca.crt"

if [ ! -f "$CA_CERT" ]; then
    CA_CERT="/var/lib/rancher/k3s/server/tls/server-ca.crt"
fi

if ! sudo test -r "$CA_CERT"; then
    echo "ERROR: k3s CA certificate not readable at $CA_CERT" >&2
    exit 1
fi

if ! sudo k3s kubectl -n "$NAMESPACE" get serviceaccount "$SA_NAME" >/dev/null 2>&1; then
    echo "ERROR: ServiceAccount $NAMESPACE/$SA_NAME is not installed." >&2
    echo "Apply infra/ailab/base/rbac.yaml as an AILab administrator first." >&2
    exit 1
fi

CA_DATA="$(sudo base64 -w0 "$CA_CERT")"
TOKEN="$(sudo k3s kubectl -n "$NAMESPACE" create token "$SA_NAME" --duration="$DURATION")"
trap 'unset TOKEN CA_DATA' EXIT

mkdir -p "$(dirname "$OUTPUT")"
umask 077
cat > "$OUTPUT" <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: $CA_DATA
    server: $API_SERVER
  name: ailab
contexts:
- context:
    cluster: ailab
    namespace: $NAMESPACE
    user: $SA_NAME
  name: gpmidi-operator@ailab
current-context: gpmidi-operator@ailab
users:
- name: $SA_NAME
  user:
    token: $TOKEN
EOF
chmod 0600 "$OUTPUT"

printf 'Kubeconfig written to %s (valid for %s)\n' "$OUTPUT" "$DURATION"
printf 'Test: KUBECONFIG=%q kubectl auth can-i create pipelineruns.tekton.dev -n %q\n' \
    "$OUTPUT" "$NAMESPACE"
