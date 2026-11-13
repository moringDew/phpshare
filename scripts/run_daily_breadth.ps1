$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = "C:\Program Files\python\python.exe"
$LogDir = Join-Path $Root "output\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("daily_breadth_" + (Get-Date -Format "yyyy-MM-dd") + ".log")

Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
& $Python -I "scripts\daily_breadth_report.py" --send --signals-only *>> $Log
exit $LASTEXITCODE
