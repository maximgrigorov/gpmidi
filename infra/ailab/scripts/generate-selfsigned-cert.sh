#!/usr/bin/env bash
set -Eeuo pipefail

# Generate self-signed TLS certificate and create Kubernetes secret.
# The private key is NEVER committed to git.
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < generate-selfsigned-cert.sh

NAMESPACE="gpmidi-ml"
SECRET_NAME="ailab-tls"
CERT_DIR="/tmp/ailab-tls-$$"

trap 'rm -rf "$CERT_DIR"' EXIT
mkdir -p "$CERT_DIR"

if sudo k3s kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" &>/dev/null; then
    echo "TLS secret $SECRET_NAME already exists in $NAMESPACE. Skipping."
    exit 0
fi

echo "Generating self-signed certificate ..."

openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$CERT_DIR/tls.key" \
    -out "$CERT_DIR/tls.crt" \
    -subj "/CN=ailab.local/O=gpmidi-ml" \
    -addext "subjectAltName=IP:192.168.30.2,DNS:ailab.local,DNS:gpmidi-ml.ailab.local"

echo "Creating Kubernetes TLS secret ..."

sudo k3s kubectl create secret tls "$SECRET_NAME" \
    --cert="$CERT_DIR/tls.crt" \
    --key="$CERT_DIR/tls.key" \
    -n "$NAMESPACE"

echo "TLS secret $SECRET_NAME created in $NAMESPACE."
echo "Certificate details:"
openssl x509 -in "$CERT_DIR/tls.crt" -noout -text | grep -A1 "Subject Alternative Name"
