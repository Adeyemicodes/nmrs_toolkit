@echo off
REM ---------------------------------------------------------------------------
REM NMRS Toolkit - config updater (Windows)
REM ---------------------------------------------------------------------------
REM Adds the v1.2.0 Unvoid / Reverse settings to an EXISTING config without
REM changing existing values. Run this ONCE before launching the new binary.
REM Keep update_config.ps1 in the SAME folder as this file.
REM
REM   Double-click                 -> operator machine (Reverse tab hidden)
REM   update_config.bat --admin    -> administrator machine (enables Reverse)
REM ---------------------------------------------------------------------------
setlocal

if not exist "%~dp0update_config.ps1" (
  echo ERROR: update_config.ps1 not found next to this batch file.
  echo Keep both files together in the same folder.
  echo.
  pause
  exit /b 1
)

set "ADMINFLAG="
if /I "%~1"=="--admin" set "ADMINFLAG=-Admin"
if /I "%~1"=="admin"   set "ADMINFLAG=-Admin"
if /I "%~1"=="-admin"  set "ADMINFLAG=-Admin"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_config.ps1" %ADMINFLAG%

echo.
pause
endlocal
