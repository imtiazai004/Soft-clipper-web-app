@echo off
REM Double-click this to run the Soft Clipper network diagnostic.
REM Windows blocks .ps1 files by default, so we launch PowerShell ourselves
REM with the policy bypassed for this one script only (nothing is installed
REM and no system setting is changed).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0network-diagnostic.ps1"
