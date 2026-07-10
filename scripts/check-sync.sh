#!/usr/bin/env bash
#
# check-sync.sh — guard against doc drift.
#
# Source of truth = the skill folders under skills/ (each with a SKILL.md).
# This verifies the human-maintained references list the same set:
#   - README.md skill table
#   - skills/_se-playbook.md line 3 ("All SE skills … (…)")
#
# The webapp (webapp/app.py) auto-derives its list from skills/, so it can't
# drift and isn't checked here.
#
# Also warns (non-blocking) at commit time if webapp/ or skills/ code is staged
# without a matching webapp/SESSION-LOG.md update (the doc-sync contract).
#
# Exit 0 if everything matches; exit 1 (with a diff) if not.
# Run anytime: ./scripts/check-sync.sh   (also runs as a pre-commit hook)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."   # repo root

fail=0

# --- 1. Source of truth: skill folders (exclude _playbook etc.) -------------
skills_on_disk="$(
  for d in skills/*/; do
    name="$(basename "$d")"
    [[ "$name" == _* || "$name" == .* ]] && continue
    [[ -f "$d/SKILL.md" ]] && echo "$name"
  done | sort
)"

# --- 2. Skills named in the README table ------------------------------------
# Table rows look like:  | `skill-id` | ... |
readme_skills="$(grep -oE '^\| `[a-z0-9-]+`' README.md | tr -d '|` ' | sort | uniq)"

# --- 3. Skills named in the playbook line 3 ---------------------------------
# Line 3: "...All SE skills in `~/.claude/skills/` (a, b, c, ...) reference..."
playbook_line="$(sed -n '3p' skills/_se-playbook.md)"
playbook_skills="$(
  echo "$playbook_line" \
    | grep -oE '\(([a-z0-9-]+(, )?)+\)' \
    | tr -d '()' | tr ',' '\n' | tr -d ' ' | sort | uniq
)"

compare() {
  local label="$1" list="$2"
  local missing extra
  missing="$(comm -23 <(echo "$skills_on_disk") <(echo "$list"))"
  extra="$(comm -13 <(echo "$skills_on_disk") <(echo "$list"))"
  if [[ -n "$missing" || -n "$extra" ]]; then
    echo "✗ $label is OUT OF SYNC with skills/ on disk:"
    [[ -n "$missing" ]] && echo "    missing (on disk, not in $label):" && echo "$missing" | sed 's/^/      - /'
    [[ -n "$extra"   ]] && echo "    extra   (in $label, not on disk):"  && echo "$extra"   | sed 's/^/      - /'
    fail=1
  else
    echo "✓ $label matches skills/ on disk ($(echo "$list" | grep -c . ) skills)"
  fi
}

echo "Skill-suite sync check"
echo "  skills on disk: $(echo "$skills_on_disk" | grep -c .)"
echo ""
compare "README.md table" "$readme_skills"
compare "playbook line 3" "$playbook_skills"
echo ""

# --- 3b. Objection reference exists + is readable (blocking) --------------------
# objection-handler reads this file at runtime via ~/.claude/skills/_reference/.
# Its canonical home is skills/_reference/. An accidental move/delete would
# silently break the skill, so treat a missing reference as drift.
REF_FILE="skills/_reference/airbyte-objection-reference.md"
if [[ -f "$REF_FILE" ]]; then
  echo "✓ objection reference present ($REF_FILE)"
else
  echo "✗ objection reference MISSING at $REF_FILE"
  echo "    objection-handler reads this at runtime — a move/delete breaks it."
  echo "    Restore it or update objection-handler + install.sh to the new path."
  fail=1
fi
echo ""

if [[ "$fail" -ne 0 ]]; then
  echo "DRIFT DETECTED. Update the file(s) above to match skills/ on disk."
  echo "(The webapp list is auto-derived and not checked.)"
  exit 1
fi
echo "All references in sync. ✓"

# --- 3c. Objection reference freshness (warn, non-blocking) --------------------
# The reference carries product facts (Flex/pricing/regions) that go stale.
# Its header has a "Last updated: YYYY-MM-DD" line + a quarterly refresh cadence.
# Warn if it's older than 90 days so the cadence is an actual signal, not a comment.
if [[ -f "$REF_FILE" ]]; then
  ref_date="$(grep -m1 -E '^\*\*Last updated:\*\*' "$REF_FILE" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)"
  if [[ -n "$ref_date" ]]; then
    # Convert both dates to epoch days; support BSD (macOS) and GNU date.
    if ref_epoch="$(date -j -f %Y-%m-%d "$ref_date" +%s 2>/dev/null)" \
       || ref_epoch="$(date -d "$ref_date" +%s 2>/dev/null)"; then
      now_epoch="$(date +%s)"
      age_days=$(( (now_epoch - ref_epoch) / 86400 ))
      if (( age_days > 90 )); then
        echo ""
        echo "⚠ objection reference is ${age_days} days old (Last updated: ${ref_date})."
        echo "  Product facts drift (Flex/pricing/regions/compliance). Per the file's own"
        echo "  refresh cadence, review it against current docs. (Warning only.)"
      fi
    fi
  else
    echo ""
    echo "⚠ could not find a 'Last updated: YYYY-MM-DD' line in $REF_FILE — add one so freshness can be tracked."
  fi
fi

# --- 4. SESSION-LOG staleness (staged-files only; skipped when nothing staged)
# If this commit touches webapp/ or skills/ code, webapp/SESSION-LOG.md should be
# updated too (the doc-sync contract in webapp/CLAUDE.md). Warn — don't block —
# so it's a nudge, not a wall; bypass reasons are legitimate (typo fixes, etc.).
staged="$(git diff --cached --name-only 2>/dev/null || true)"
if [[ -n "$staged" ]]; then
  touches_code="$(echo "$staged" | grep -E '^(webapp/|skills/)' | grep -vE '(SESSION-LOG\.md|\.md$)' || true)"
  logs_updated="$(echo "$staged" | grep -E '^webapp/SESSION-LOG\.md$' || true)"
  if [[ -n "$touches_code" && -z "$logs_updated" ]]; then
    echo ""
    echo "⚠ webapp/ or skills/ code is staged but webapp/SESSION-LOG.md is NOT."
    echo "  Per the doc-sync contract (webapp/CLAUDE.md), add a SESSION-LOG entry"
    echo "  and refresh its _Last updated:_ header. (Warning only — commit proceeds.)"
  fi
fi
