#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# NMRS Toolkit — config updater (Ubuntu/Linux/macOS)
# ---------------------------------------------------------------------------
# Adds the v1.2.0 "Unvoid Patient" / "Reverse Unvoid" settings to an EXISTING
# config without changing any existing values (DB password, backup_key,
# admin_password, profile_label, ... are all preserved). Existing comments are
# kept. No Python required.
#
# Run this ONCE on an already-deployed machine, BEFORE launching the new binary.
#
#   ./update_config.sh            # operator machine — Reverse tab stays hidden
#   ./update_config.sh --admin    # administrator machine — enables Reverse tab
#
# Safe to re-run: keys already present are left untouched.
# ---------------------------------------------------------------------------
set -euo pipefail

CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nmrs_toolkit"
CFG="$CFG_DIR/.nmrs_config.ini"
LEGACY="$CFG_DIR/nmrs_config.ini"

# Honour the legacy (non-hidden) filename if that's what's deployed.
if [ ! -f "$CFG" ] && [ -f "$LEGACY" ]; then
  CFG="$LEGACY"
fi

if [ ! -f "$CFG" ]; then
  echo "No existing config found at:"
  echo "  $CFG"
  echo "Nothing to update — a fresh install creates this from the template on first launch."
  exit 1
fi

REVERSE="false"
if [ "${1:-}" = "--admin" ]; then
  REVERSE="true"
fi

# Back up first — this file holds credentials and the backup_key.
BAK="$CFG.bak.$(date +%Y%m%d%H%M%S)"
cp -a "$CFG" "$BAK"
echo "Backed up existing config to:"
echo "  $BAK"
echo "Updating: $CFG"

ensure_key() {
  # ensure_key <section> <key> <value>
  local section="$1" key="$2" value="$3"
  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$CFG"; then
    echo "  [keep] ${key} already present — left unchanged"
    return 0
  fi
  if grep -qE "^[[:space:]]*\[${section}\][[:space:]]*$" "$CFG"; then
    awk -v sec="[$section]" -v line="$key = $value" '
      { print }
      { t=$0; gsub(/^[ \t]+|[ \t]+$/,"",t); if (t==sec && !done) { print line; done=1 } }
    ' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
    echo "  [add ] ${key} = ${value}   (under [${section}])"
  else
    printf '\n[%s]\n%s = %s\n' "$section" "$key" "$value" >> "$CFG"
    echo "  [add ] [${section}] + ${key} = ${value}"
  fi
}

ensure_key settings unvoid_accepted_reasons "Bulk void via ART/DATIM mapping, Duplicate Client"
ensure_key settings unvoid_window_seconds 120
ensure_key ui unvoid_tab_enabled true
ensure_key ui reverse_tab_enabled "$REVERSE"

# Re-tighten permissions (owner read/write only) — it holds secrets.
chmod 600 "$CFG" 2>/dev/null || true

echo "Done. Reverse Unvoid tab enabled: ${REVERSE}"
