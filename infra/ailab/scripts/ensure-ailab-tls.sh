#!/usr/bin/env bash
set -Eeuo pipefail

# Ensure the shared self-signed `ailab-tls` certificate exists, covers every name
# the cluster is reached by, and is present in every namespace that has an
# Ingress referencing it.
#
# This supersedes generate-selfsigned-cert.sh for day-to-day use: that script
# creates the secret once and skips forever, so it cannot add a SAN after the
# fact. This one is convergent — it reissues only when a required name is
# missing, and is otherwise a no-op.
#
# An Ingress may only reference a TLS Secret in its own namespace, which is why
# the same certificate is installed into both gpmidi-ml and headlamp rather than
# issuing a second one.
#
# The private key is generated on AILab, lives only in a private temp dir for the
# duration of this script, and is NEVER written to the repository.
#
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < ensure-ailab-tls.sh
#        ssh mgrigorov@192.168.30.2 'bash -s' -- --force < ensure-ailab-tls.sh

SECRET_NAME="ailab-tls"
NAMESPACES=(gpmidi-ml headlamp)
CERT_CN="ailab.home.arpa"
CERT_DAYS=825

# Every name the cluster is legitimately reached by on 443.
#   home.arpa  — RFC 8375 name for home networks; the documented primary domain.
#   .local     — legacy aliases only. Reserved for mDNS (RFC 6762), kept so
#                existing bookmarks and /etc/hosts entries do not break.
SANS="IP:192.168.30.2"
SANS="${SANS},DNS:ailab.home.arpa"
SANS="${SANS},DNS:k8s.ailab.home.arpa"
SANS="${SANS},DNS:gpmidi.ailab.home.arpa"
SANS="${SANS},DNS:ailab.local"
SANS="${SANS},DNS:gpmidi-ml.ailab.local"
SANS="${SANS},DNS:gpmidi.ailab.local"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

K="sudo k3s kubectl"

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found in PATH" >&2; exit 1; }
}
require openssl
require sudo

# --- does the live certificate already cover everything? -------------------
needs_reissue=1
if [ "$FORCE" -eq 0 ] && $K -n "${NAMESPACES[0]}" get secret "$SECRET_NAME" >/dev/null 2>&1; then
    live_sans="$($K -n "${NAMESPACES[0]}" get secret "$SECRET_NAME" \
        -o jsonpath='{.data.tls\.crt}' | base64 -d \
        | openssl x509 -noout -ext subjectAltName 2>/dev/null || true)"
    missing=""
    IFS=',' read -r -a want <<<"$SANS"
    for entry in "${want[@]}"; do
        # "DNS:foo" -> "DNS:foo", "IP:1.2.3.4" -> "IP Address:1.2.3.4"
        needle="${entry/IP:/IP Address:}"
        case "$live_sans" in
            *"$needle"*) ;;
            *) missing="${missing} ${entry}" ;;
        esac
    done
    if [ -z "$missing" ]; then
        needs_reissue=0
        echo "Certificate ${SECRET_NAME} already covers every required name."
    else
        echo "Certificate ${SECRET_NAME} is missing:${missing}"
        echo "Reissuing."
    fi
else
    echo "Certificate ${SECRET_NAME} not present in ${NAMESPACES[0]} (or --force given). Issuing."
fi

CERT_DIR="$(mktemp -d /tmp/ailab-tls-XXXXXX)"
chmod 700 "$CERT_DIR"
trap 'rm -rf "$CERT_DIR"' EXIT

if [ "$needs_reissue" -eq 1 ]; then
    echo "Generating self-signed certificate (CN=${CERT_CN}, ${CERT_DAYS} days) ..."
    openssl req -x509 -nodes -days "$CERT_DAYS" -newkey rsa:2048 \
        -keyout "${CERT_DIR}/tls.key" \
        -out "${CERT_DIR}/tls.crt" \
        -subj "/CN=${CERT_CN}/O=ailab" \
        -addext "subjectAltName=${SANS}" \
        -addext "basicConstraints=critical,CA:FALSE" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
        -addext "extendedKeyUsage=serverAuth" 2>/dev/null
else
    # Nothing to reissue, but a namespace may still be missing the secret.
    # Recover the existing material so it can be replicated verbatim.
    $K -n "${NAMESPACES[0]}" get secret "$SECRET_NAME" \
        -o jsonpath='{.data.tls\.crt}' | base64 -d >"${CERT_DIR}/tls.crt"
    $K -n "${NAMESPACES[0]}" get secret "$SECRET_NAME" \
        -o jsonpath='{.data.tls\.key}' | base64 -d >"${CERT_DIR}/tls.key"
fi

for ns in "${NAMESPACES[@]}"; do
    if ! $K get namespace "$ns" >/dev/null 2>&1; then
        echo "Namespace ${ns} does not exist yet; skipping." >&2
        continue
    fi
    if [ "$needs_reissue" -eq 0 ] && $K -n "$ns" get secret "$SECRET_NAME" >/dev/null 2>&1; then
        echo "  ${ns}/${SECRET_NAME}: already current"
        continue
    fi
    # create --dry-run | apply, so this is an in-place update rather than a
    # delete/recreate. Traefik picks the new certificate up without a restart and
    # without dropping connections.
    $K -n "$ns" create secret tls "$SECRET_NAME" \
        --cert="${CERT_DIR}/tls.crt" \
        --key="${CERT_DIR}/tls.key" \
        --dry-run=client -o yaml | $K apply -f - >/dev/null
    echo "  ${ns}/${SECRET_NAME}: applied"
done

echo
echo "Certificate in effect:"
openssl x509 -in "${CERT_DIR}/tls.crt" -noout -subject -dates -ext subjectAltName
