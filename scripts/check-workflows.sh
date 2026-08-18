#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ACTIONLINT_BIN="${ACTIONLINT_BIN:-${REPO_ROOT}/.tools/actionlint}"

if [[ ! -x "$ACTIONLINT_BIN" ]]; then
  printf 'actionlint unavailable; set ACTIONLINT_BIN to the pinned binary\n'
  exit 0
fi

mapfile -t workflows < <(find "$REPO_ROOT/.github/workflows" -maxdepth 1 -type f -name '*.yml' -print | sort)
[[ "${#workflows[@]}" -gt 0 ]] || { printf 'no workflow files found\n' >&2; exit 1; }
"$ACTIONLINT_BIN" "${workflows[@]}"
