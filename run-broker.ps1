# Запуск брокера. Токен берётся из .token (создаётся при первом запуске).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $python broker\server.py --host 127.0.0.1 --port 8787
