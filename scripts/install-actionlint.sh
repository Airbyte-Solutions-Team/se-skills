#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VERSION="1.7.7"
readonly SHA256="023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757"
readonly ARCHIVE="/tmp/actionlint_${VERSION}_linux_amd64.tar.gz"

mkdir -p "$REPO_ROOT/.tools"
curl --fail --location --silent --show-error \
  "https://github.com/rhysd/actionlint/releases/download/v${VERSION}/actionlint_${VERSION}_linux_amd64.tar.gz" \
  -o "$ARCHIVE"
printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum --check --status
tar --extract --file "$ARCHIVE" --directory "$REPO_ROOT/.tools" actionlint
chmod 0755 "$REPO_ROOT/.tools/actionlint"
printf 'installed actionlint %s at %s\n' "$VERSION" "$REPO_ROOT/.tools/actionlint"
