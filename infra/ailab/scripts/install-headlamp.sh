#!/usr/bin/env bash
set -Eeuo pipefail

# Install / upgrade the Headlamp cluster dashboard on AILab.
#
# Idempotent: `helm upgrade --install` plus `kubectl apply`, so running it twice
# converges instead of failing. The chart version is pinned in versions.env and
# passed with --version; the chart is never resolved as "latest".
#
# Ownership boundary:
#   Helm owns  — the Headlamp Deployment, Service, ServiceAccount, ConfigMap.
#   This repo owns — the namespace, the headlamp-admin login identity (rbac.yaml)
#                    and the Ingress (ingress.yaml). `ingress.enabled` is false
#                    in values.yaml precisely so nothing is declared twice.
#
# No secret material is created or printed. The admin login token is minted on
# demand through the TokenRequest API; the command is printed at the end.
#
# Run from a checkout on AILab (the script reads sibling manifests):
#   ssh mgrigorov@192.168.30.2
#   cd /path/to/gpmidi && bash infra/ailab/scripts/install-headlamp.sh

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AILAB_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${AILAB_DIR}/apps/headlamp"
VERSIONS_FILE="${AILAB_DIR}/versions.env"

NAMESPACE="headlamp"
RELEASE="headlamp"
REPO_NAME="headlamp"
REPO_URL="https://kubernetes-sigs.github.io/headlamp/"
ROLLOUT_TIMEOUT="180s"

K="sudo k3s kubectl"
# k3s keeps its admin kubeconfig root-readable only, so Helm runs under sudo with
# that file pointed at explicitly rather than relying on an ambient KUBECONFIG.
HELM="sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm"

log() { printf '\n=== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- prerequisites ---------------------------------------------------------
log "Validating prerequisites"

for f in "${VERSIONS_FILE}" \
         "${APP_DIR}/namespace.yaml" \
         "${APP_DIR}/values.yaml" \
         "${APP_DIR}/rbac.yaml" \
         "${APP_DIR}/ingress.yaml"; do
    [ -f "$f" ] || die "missing required file: $f"
done

command -v sudo >/dev/null 2>&1 || die "sudo not found"
sudo -n true 2>/dev/null || die "sudo requires a password in this session; run interactively"
command -v helm >/dev/null 2>&1 || die "helm not found — run scripts/install-helm.sh first"
$K version --output=json >/dev/null 2>&1 || die "cannot reach the k3s API server via 'sudo k3s kubectl'"

# shellcheck source=/dev/null
. "${VERSIONS_FILE}"
: "${HEADLAMP_CHART_VERSION:?HEADLAMP_CHART_VERSION not set in versions.env}"
[ "$HEADLAMP_CHART_VERSION" = "latest" ] && die "HEADLAMP_CHART_VERSION must be an explicit version, not 'latest'"

# Headlamp is reached over the same Traefik entrypoint as everything else. If
# Traefik does not own 443 the routing model in ingress.yaml cannot hold, so fail
# loudly here rather than half-installing.
tls_port="$($K -n kube-system get svc traefik \
    -o jsonpath='{.spec.ports[?(@.name=="websecure")].port}' 2>/dev/null || true)"
[ "$tls_port" = "443" ] || die "Traefik does not expose websecure on 443 (got '${tls_port:-none}')"

echo "helm:            $(helm version --short)"
echo "k3s:             $($K version -o json | python3 -c 'import sys,json;print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
echo "chart version:   ${HEADLAMP_CHART_VERSION}"
echo "traefik 443:     ok"

# --- namespace -------------------------------------------------------------
log "Applying namespace"
$K apply -f "${APP_DIR}/namespace.yaml"

# --- TLS -------------------------------------------------------------------
# The Ingress references ailab-tls, which must exist in this namespace too.
log "Ensuring shared TLS certificate covers the dashboard hostname"
bash "${SCRIPT_DIR}/ensure-ailab-tls.sh"

# --- chart -----------------------------------------------------------------
log "Adding/refreshing the official Headlamp chart repository"
$HELM repo add "$REPO_NAME" "$REPO_URL" --force-update >/dev/null
$HELM repo update "$REPO_NAME" >/dev/null
echo "resolved: $($HELM search repo "${REPO_NAME}/headlamp" --version "$HEADLAMP_CHART_VERSION" \
    --output json | python3 -c 'import sys,json;c=json.load(sys.stdin)[0];print("chart",c["version"],"app",c["app_version"])')"

log "helm upgrade --install ${RELEASE} (chart ${HEADLAMP_CHART_VERSION})"
$HELM upgrade --install "$RELEASE" "${REPO_NAME}/headlamp" \
    --version "$HEADLAMP_CHART_VERSION" \
    --namespace "$NAMESPACE" \
    --values "${APP_DIR}/values.yaml" \
    --wait --timeout "$ROLLOUT_TIMEOUT"

# --- repository-owned supplements -----------------------------------------
log "Applying login identity (headlamp-admin) and Ingress"
$K apply -f "${APP_DIR}/rbac.yaml"
$K apply -f "${APP_DIR}/ingress.yaml"

# --- verify ----------------------------------------------------------------
log "Waiting for rollout"
$K -n "$NAMESPACE" rollout status deploy/"$RELEASE" --timeout="$ROLLOUT_TIMEOUT"

log "Result"
$HELM list --namespace "$NAMESPACE"
$K -n "$NAMESPACE" get deploy,svc,ingress,sa
echo
$K -n "$NAMESPACE" get pods \
    -o custom-columns='POD:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,IMAGE:.status.containerStatuses[0].imageID'

cat <<'EOF'

=== Done.

Open the dashboard at:
  https://192.168.30.2/            (default route, no DNS needed)
  https://k8s.ailab.home.arpa/     (once the MikroTik A record exists)

Headlamp will ask for a token. Mint a fresh 24h one with:

  sudo k3s kubectl -n headlamp create token headlamp-admin --duration=24h

That token is short-lived and is not stored anywhere — re-run the command
whenever the session expires. Never paste it into the repository.
EOF
