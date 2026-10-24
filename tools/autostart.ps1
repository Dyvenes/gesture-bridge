# Автозапуск агента при входе в Windows.
#
#   .\tools\autostart.ps1 -Broker https://server.tailnet.ts.net
#   .\tools\autostart.ps1 -Remove
#
# Ярлык кладётся в папку автозагрузки пользователя — туда, где его видно
# и откуда его можно убрать руками. Ни реестр, ни планировщик задач для
# этого не нужны, а убрать их потом сложнее.
#
# Запуск идёт через pythonw.exe: он не открывает окно консоли, и агент
# живёт только значком в трее, как и задумано.

param(
    [string]$Broker = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$startup = [Environment]::GetFolderPath("Startup")
$link = Join-Path $startup "Gesture Bridge.lnk"

if ($Remove) {
    if (Test-Path $link) {
        Remove-Item $link
        Write-Host "Автозапуск убран: $link" -ForegroundColor Cyan
    } else {
        Write-Host "Автозапуска и не было." -ForegroundColor Yellow
    }
    return
}

if (-not $Broker) {
    throw "Укажи адрес брокера: .\tools\autostart.ps1 -Broker https://server.tailnet.ts.net"
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Не найден $pythonw — сначала создай окружение и поставь зависимости"
}

$tokenFile = Join-Path $root ".token"
if (-not (Test-Path $tokenFile)) {
    Write-Host "Внимание: файла .token нет." -ForegroundColor Yellow
    Write-Host "Положи туда токен брокера, иначе агент не сможет подключиться." -ForegroundColor Yellow
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$(Join-Path $root 'agent\main.py')`" --broker `"$Broker`""
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = Join-Path $root "receiver\icons\icon-192.png"
$shortcut.Description = "Gesture Bridge — перенос содержимого жестом"
$shortcut.Save()

Write-Host "Автозапуск настроен." -ForegroundColor Green
Write-Host "  ярлык:  $link"
Write-Host "  запуск: $pythonw"
Write-Host "  брокер: $Broker"
Write-Host ""
Write-Host "Агент стартует при следующем входе в систему и будет виден"
Write-Host "значком в трее. Оттуда же его можно приостановить или закрыть."
