# Выкат брокера на Ubuntu-сервер по SSH.
#
#   .\deploy\deploy.ps1 -Target user@server              # обновить код
#   .\deploy\deploy.ps1 -Target user@server -Setup       # первый раз
#
# Едут только три каталога: broker/, receiver/ и models/. Агент остаётся
# на Windows, его на сервере нет смысла держать.

param(
    [Parameter(Mandatory = $true)][string]$Target,
    [string]$Path = "/opt/gesture-bridge",
    [switch]$Setup
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Invoke-Remote([string]$Command) {
    # -t нужен ради sudo: без псевдотерминала он не сможет спросить пароль
    # и команда молча свалится.
    & ssh -t $Target $Command
    if ($LASTEXITCODE -ne 0) { throw "сорвалось на сервере: $Command" }
}

Write-Host "==> готовлю каталог $Path" -ForegroundColor Cyan
Invoke-Remote "sudo mkdir -p '$Path' && sudo chown -R `$(id -un):`$(id -gn) '$Path'"

Write-Host "==> копирую broker, receiver, models" -ForegroundColor Cyan
& scp -r broker receiver models requirements-broker.txt "${Target}:${Path}/"
if ($LASTEXITCODE -ne 0) { throw "scp не смог скопировать файлы" }

if ($Setup) {
    Write-Host "==> ставлю venv и зависимости" -ForegroundColor Cyan
    # Намеренно тремя вызовами, а не одной цепочкой через &&: длинная
    # строка рвётся при копировании в терминал, а при сбое непонятно,
    # какое из звеньев упало.
    Invoke-Remote "cd '$Path' && python3 -m venv .venv"
    Invoke-Remote "cd '$Path' && .venv/bin/pip install --quiet --upgrade pip"
    Invoke-Remote "cd '$Path' && .venv/bin/pip install --quiet -r requirements-broker.txt"

    Write-Host "==> создаю служебного пользователя и файл с токеном" -ForegroundColor Cyan
    # Токен генерируем на сервере: так он не проходит через буфер обмена
    # и не оседает в истории команд.
    Invoke-Remote @"
sudo useradd --system --no-create-home --shell /usr/sbin/nologin gesturebridge 2>/dev/null || true
if [ ! -f /etc/gesture-bridge.env ]; then
  printf 'BRIDGE_TOKEN=%s\n' "`$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')" | sudo tee /etc/gesture-bridge.env >/dev/null
  sudo chmod 600 /etc/gesture-bridge.env
  sudo chown gesturebridge:gesturebridge /etc/gesture-bridge.env
fi
sudo chown -R gesturebridge:gesturebridge '$Path'
"@

    Write-Host "==> ставлю systemd-юнит" -ForegroundColor Cyan
    & scp deploy/gesture-bridge.service "${Target}:/tmp/gesture-bridge.service"
    if ($LASTEXITCODE -ne 0) { throw "не удалось скопировать юнит" }
    Invoke-Remote "sudo mv /tmp/gesture-bridge.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now gesture-bridge"
}
else {
    Invoke-Remote "sudo chown -R gesturebridge:gesturebridge '$Path'"

    # Юнит везём каждый раз. Иначе его правки молча не доезжают, и служба
    # падает по причине, которой в выкаченном коде уже нет.
    Write-Host "==> обновляю systemd-юнит" -ForegroundColor Cyan
    & scp deploy/gesture-bridge.service "${Target}:/tmp/gesture-bridge.service"
    if ($LASTEXITCODE -ne 0) { throw "не удалось скопировать юнит" }
    Invoke-Remote "sudo mv /tmp/gesture-bridge.service /etc/systemd/system/ && sudo systemctl daemon-reload"

    Write-Host "==> перезапускаю службу" -ForegroundColor Cyan
    Invoke-Remote "sudo systemctl restart gesture-bridge"
}

Write-Host "==> состояние службы" -ForegroundColor Cyan
Invoke-Remote "systemctl --no-pager --lines=5 status gesture-bridge || true"

if ($Setup) {
    Write-Host ""
    Write-Host "Осталось два шага на сервере:" -ForegroundColor Yellow
    Write-Host "  1) tailscale serve --bg 8787"
    Write-Host "  2) sudo cat /etc/gesture-bridge.env   # отсюда возьми токен"
}
