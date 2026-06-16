#!/usr/bin/env bash
# Install the repo's git hooks (pre-commit drift check) into .git/hooks.
# Run once after cloning:  ./scripts/install-hooks.sh
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hook_src="$repo_root/scripts/pre-commit"
hook_dst="$repo_root/.git/hooks/pre-commit"

if [[ ! -d "$repo_root/.git" ]]; then
  echo "Not a git repo (no .git dir). Run from a clone of se-skills."
  exit 1
fi

ln -sf "../../scripts/pre-commit" "$hook_dst"
chmod +x "$hook_src"
echo "Installed pre-commit hook → $hook_dst"
echo "It runs scripts/check-sync.sh before each commit. Bypass with: git commit --no-verify"
