#!/usr/bin/env bash
#
# install.sh — symlink the SE skills into ~/.claude/skills/ so Claude Code picks them up.
#
# Symlinks (not copies) mean `git pull` in this repo instantly updates your installed
# skills — no re-running needed. Run from the repo root: ./install.sh
#
# Idempotent: re-running re-points symlinks, safe to run after every pull.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_SRC="$REPO_DIR/skills"
SKILLS_DEST="$HOME/.claude/skills"

echo "SE Skills installer"
echo "  repo:   $SKILLS_SRC"
echo "  target: $SKILLS_DEST"
echo ""

if [ ! -d "$SKILLS_SRC" ]; then
  echo "ERROR: $SKILLS_SRC not found. Run this from the repo root."
  exit 1
fi

mkdir -p "$SKILLS_DEST"

linked=0
skipped=0
for item in "$SKILLS_SRC"/*; do
  name="$(basename "$item")"
  target="$SKILLS_DEST/$name"

  # If a real (non-symlink) file/dir already exists, don't clobber it — warn instead.
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "  SKIP (real file exists, not overwriting): $name"
    echo "       -> move or remove $target manually if you want the repo version"
    skipped=$((skipped+1))
    continue
  fi

  # Remove an existing symlink so we can re-point it
  [ -L "$target" ] && rm "$target"

  ln -s "$item" "$target"
  echo "  linked: $name"
  linked=$((linked+1))
done

echo ""
echo "Done. Linked $linked, skipped $skipped."
echo ""

# NOTE: the Airbyte objection reference lives in the repo at skills/_reference/ and is
# symlinked into ~/.claude/skills/_reference/ by the loop above (like _se-playbook.md).
# No special-case symlink needed — it rides the same mechanism as every other skill.
# (Prior versions symlinked it into ~/airbyte-work/04-notes/; that hop was removed on
# 2026-07-10 to keep a single source of truth and eliminate a drift surface.)

# Config check
CONFIG="$HOME/airbyte-work/.se-config.yaml"
if [ ! -f "$CONFIG" ]; then
  echo "NEXT STEP: create your SE config —"
  echo "  cp $REPO_DIR/config/se-config.example.yaml $CONFIG"
  echo "  then edit it with your name/email/org alias."
else
  echo "Found existing config at $CONFIG ✓"
fi

echo ""
echo "Optional — Salesforce CRM enrichment (see README for full steps):"
echo "  npm install -g @salesforce/cli && sf org login web --alias airbyte-prod --set-default"
echo "  npm install -g @salesforce/mcp"
echo "  then add the 'salesforce' MCP server to ~/.claude.json (see README)"
echo ""
echo "Restart Claude Code to load the skills."
