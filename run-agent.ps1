# Запуск агента на ПК.
# Укажи адрес брокера: .\run-agent.ps1 https://mashina.tailnet.ts.net
param([string]$Broker = "http://127.0.0.1:8787")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $python agent\main.py --broker $Broker
