# ---------------------------------------------------------------------------
# NMRS Toolkit - config updater (Windows, PowerShell)
# ---------------------------------------------------------------------------
# Adds the v1.2.0 "Unvoid Patient" / "Reverse Unvoid" settings to an EXISTING
# config without changing any existing values (DB password, backup_key,
# admin_password, profile_label, ... are all preserved). Existing comments are
# kept. No Python required.
#
# This is invoked by update_config.bat - run that. To run directly:
#   powershell -ExecutionPolicy Bypass -File update_config.ps1
#   powershell -ExecutionPolicy Bypass -File update_config.ps1 -Admin
#
# Safe to re-run: keys already present are left untouched.
# ---------------------------------------------------------------------------
param([switch]$Admin)

$ErrorActionPreference = "Stop"

$dir    = Join-Path $env:APPDATA "NMRS_Toolkit"
$cfg    = Join-Path $dir ".nmrs_config.ini"
$legacy = Join-Path $dir "nmrs_config.ini"

if ((-not (Test-Path -LiteralPath $cfg)) -and (Test-Path -LiteralPath $legacy)) {
    $cfg = $legacy
}
if (-not (Test-Path -LiteralPath $cfg)) {
    Write-Host "No existing config found at:"
    Write-Host "  $cfg"
    Write-Host "Nothing to update - a fresh install creates this from the template on first launch."
    exit 1
}

$reverse = "false"
if ($Admin) { $reverse = "true" }

# Back up first - this file holds credentials and the backup_key.
$bak = "$cfg.bak." + (Get-Date -Format "yyyyMMddHHmmss")
Copy-Item -LiteralPath $cfg -Destination $bak
Write-Host "Backed up existing config to:"
Write-Host "  $bak"
Write-Host "Updating: $cfg"

# Read as UTF-8 explicitly. Windows PowerShell 5.1's default Get-Content uses
# the ANSI code page (cp1252); on a UTF-8 file that misreads multibyte chars
# (e.g. the em-dash in comments) and a later UTF-8 rewrite produces bytes like
# 0x9D that crash the app. -Encoding UTF8 reads UTF-8 correctly (BOM tolerated).
$lines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $cfg -Encoding UTF8)

function Ensure-Key([string]$section, [string]$key, [string]$value) {
    $keyPattern = "^\s*" + [regex]::Escape($key) + "\s*="
    foreach ($l in $lines) {
        if ($l -match $keyPattern) {
            Write-Host "  [keep] $key already present - left unchanged"
            return
        }
    }
    $secHeader = "[$section]"
    $idx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq $secHeader) { $idx = $i; break }
    }
    if ($idx -ge 0) {
        $lines.Insert($idx + 1, "$key = $value")
        Write-Host "  [add ] $key = $value   (under $secHeader)"
    } else {
        $lines.Add("")
        $lines.Add($secHeader)
        $lines.Add("$key = $value")
        Write-Host "  [add ] $secHeader + $key = $value"
    }
}

Ensure-Key "settings" "unvoid_accepted_reasons" "Bulk void via ART/DATIM mapping, Duplicate Client"
Ensure-Key "settings" "unvoid_window_seconds"   "120"
Ensure-Key "ui"       "unvoid_tab_enabled"      "true"
Ensure-Key "ui"       "reverse_tab_enabled"     $reverse

# Write UTF-8 WITHOUT a BOM. (PS 5.1's "Set-Content -Encoding UTF8" emits a BOM;
# the app tolerates one, but BOM-free keeps the file byte-clean for any tool.)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($cfg, $lines, $utf8NoBom)

Write-Host "Done. Reverse Unvoid tab enabled: $reverse"
