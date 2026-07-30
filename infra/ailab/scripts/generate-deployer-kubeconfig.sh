#!/usr/bin/env bash
set -Eeuo pipefail

# Generate a scoped kubeconfig for gpmidi-deployer ServiceAccount.
# Output is saved to ~/.kube/gpmidi-deployer.kubeconfig with mode 0600.
# The generated file is NEVER committed to git.
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < generate-deployer-kubeconfig.sh

NAMESPACE="gpmidi-ml"
SA_NAME="gpmidi-deployer"
DURATION="${1:-24h}"
OUTPUT="$HOME/.kube/gpmidi-deployer.kubeconfig"

API_SERVER="https://192.168.30.2:6443"
CA_CERT="/data/k3s/server/tls/server-ca.crt"

if [ ! -f "$CA_CERT" ]; then
    CA_CERT="/var/lib/rancher/k3s/server/tls/server-ca.crt"
fi

if [ ! -r "$CA_CERT" ]; then
    echo "ERROR: k3s CA certificate not readable at $CA_CERT" >&2
    exit 1
fi

# Embed the CA so the kubeconfig remains usable after it is copied to an
# approved management workstation. A server-local certificate-authority path
# would make the generated kubeconfig non-portable.
CA_DATA="$(base64 -w0 < "$CA_CERT")"

TOKEN="$(sudo k3s kubectl create token "$SA_NAME" -n "$NAMESPACE" --duration="$DURATION")"

mkdir -p "$(dirname "$OUTPUT")"

cat > "$OUTPUT" << EOF
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
  name: gpmidi-deployer@ailab
current-context: gpmidi-deployer@ailab
users:
- name: $SA_NAME
  user:
    token: $TOKEN
EOF

chmod 0600 "$OUTPUT"
echo "Kubeconfig written to $OUTPUT (valid for $DURATION)"
echo "Test: KUBECONFIG=$OUTPUT kubectl get pods -n $NAMESPACE"
