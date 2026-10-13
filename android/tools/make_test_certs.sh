#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Test certificates for K1000TrustTest (app/src/test/resources/certs/). Public certificates only:
# every private key is made in a temporary directory and deleted on exit. Throwaway CAs, never
# the K1000 CA (its key stays on mgr-ran). Re-run to regenerate; the tests pin "now" to
# 2026-10-12, inside these validity windows.
#
#   wrongca-leaf.pem      a CA with the SAME name and constraints as the K1000 CA (another key):
#                         a perfect look-alike leaf, must be refused
#   testca.pem            a name-constrained test CA (10.42.0.0/24 + k1000.home.arpa), and leaves:
#     testca-good.pem       IP:10.42.0.1 + DNS:k1000.home.arpa (accepted with testca as anchor)
#     testca-extra-ip.pem   + IP:10.43.0.1 (outside the constraints)
#     testca-extra-dns.pem  + DNS:evil.example (outside the constraints)
#     testca-cn-outside.pem CN=evil.example, names inside
#     testca-other-ip.pem   IP:10.42.0.5 only (inside the constraints, not a K1000 host)
#     testca-wildcard.pem   DNS:*.home.arpa only
#     testca-is-ca.pem      CA:TRUE
#     testca-clientauth.pem extendedKeyUsage clientAuth only
#   unconstrained-ca.pem  a CA without name constraints (the app must refuse to use it)
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
out=$here/app/src/test/resources/certs
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$out"
NB=20261001000000Z
NA=20361001000000Z

key() { openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tmp/$1.key" 2>/dev/null; }

ca() {  # name CN O constrained(0/1)   (a config file, so "+" in the CN stays one RDN)
  key "$1"
  {
    printf '[req]\ndistinguished_name = dn\nprompt = no\nx509_extensions = v3\n[dn]\nCN = %s\nO = %s\n[v3]\n' "$2" "$3"
    printf 'basicConstraints = critical, CA:TRUE, pathlen:0\nkeyUsage = critical, keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\n'
    if [ "$4" = 1 ]; then printf 'nameConstraints = critical, permitted;IP:10.42.0.0/255.255.255.0, permitted;DNS:k1000.home.arpa\n'; fi
  } > "$tmp/$1.cnf"
  openssl req -new -x509 -key "$tmp/$1.key" -config "$tmp/$1.cnf" -not_before $NB -not_after $NA -out "$tmp/$1.pem" 2>/dev/null
}

leaf() {  # ca out subject extfile-lines...
  local cakey=$1 out_name=$2 subj=$3; shift 3
  key "$out_name"
  openssl req -new -key "$tmp/$out_name.key" -subj "$subj" -out "$tmp/$out_name.csr" 2>/dev/null
  printf '%s\n' "$@" > "$tmp/$out_name.ext"
  openssl x509 -req -in "$tmp/$out_name.csr" -CA "$tmp/$cakey.pem" -CAkey "$tmp/$cakey.key" -set_serial "0x$(openssl rand -hex 8)" \
    -not_before $NB -not_after $NA -extfile "$tmp/$out_name.ext" -out "$out/$out_name.pem" 2>/dev/null
}

SERVER=("basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth")

# The look-alike: same subject DN and constraints as the real K1000 CA, different key.
ca wrongca "K1000 Local CA (10.42.0.0/24 + k1000.home.arpa only)" "Trente-Sept K1000" 1
leaf wrongca wrongca-leaf "/CN=k1000.home.arpa" "${SERVER[@]}" "subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa"

ca testca "K1000 Test CA (tests only)" "K1000 tests" 1
cp "$tmp/testca.pem" "$out/testca.pem"
leaf testca testca-good "/CN=k1000.home.arpa" "${SERVER[@]}" "subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa"
leaf testca testca-extra-ip "/CN=k1000.home.arpa" "${SERVER[@]}" "subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa,IP:10.43.0.1"
leaf testca testca-extra-dns "/CN=k1000.home.arpa" "${SERVER[@]}" "subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa,DNS:evil.example"
leaf testca testca-cn-outside "/CN=evil.example" "${SERVER[@]}" "subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa"
leaf testca testca-other-ip "/CN=k1000.home.arpa" "${SERVER[@]}" "subjectAltName=IP:10.42.0.5"
leaf testca testca-wildcard "/CN=k1000.home.arpa" "${SERVER[@]}" "subjectAltName=DNS:*.home.arpa"
leaf testca testca-is-ca "/CN=k1000.home.arpa" "basicConstraints=critical,CA:TRUE" "keyUsage=critical,digitalSignature,keyCertSign" "extendedKeyUsage=serverAuth" "subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa"
leaf testca testca-clientauth "/CN=k1000.home.arpa" "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" "extendedKeyUsage=clientAuth" "subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa"

ca unconstrained "Unconstrained Test CA" "K1000 tests" 0
cp "$tmp/unconstrained.pem" "$out/unconstrained-ca.pem"

ls "$out"
