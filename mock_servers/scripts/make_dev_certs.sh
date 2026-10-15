#!/usr/bin/env bash
#
# Mint a dev CA and one server cert for localhost / 127.0.0.1 into certs/.
# The four SiLA mocks serve it with --tls. Clients trust certs/ca.crt.
#
# Usage:
#   scripts/make_dev_certs.sh            # writes certs/ next to this repo's pyproject
#   scripts/make_dev_certs.sh /some/dir  # writes there instead
#
set -euo pipefail

out="${1:-$(cd "$(dirname "$0")/.." && pwd)/certs}"
mkdir -p "$out"

openssl genrsa -out "$out/ca.key" 2048 2>/dev/null
openssl req -x509 -new -nodes -key "$out/ca.key" -sha256 -days 3650 \
    -subj "/CN=mock_servers dev CA/O=Marvel Automation Dev" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$out/ca.crt"

openssl genrsa -out "$out/server.key" 2048 2>/dev/null
openssl req -new -key "$out/server.key" \
    -subj "/CN=localhost/O=Marvel Automation Dev" \
    -out "$out/server.csr"
ext="$(mktemp)"
cat > "$ext" <<'EXT'
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,IP:127.0.0.1
EXT
openssl x509 -req -in "$out/server.csr" -CA "$out/ca.crt" -CAkey "$out/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$ext" -out "$out/server.crt" 2>/dev/null
rm -f "$ext" "$out/server.csr" "$out/ca.srl"

echo "wrote $out/ca.crt, ca.key, server.crt, server.key (SAN: localhost, 127.0.0.1)"
