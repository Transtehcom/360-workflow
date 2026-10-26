# Офисное приложение и обратный SSH-туннель. Запуск при входе пользователя Windows.
param(
    [string]$Vps = "2.26.103.191",
    [string]$TunnelUser = "kpitunnel",
    [int]$RemotePort = 18082,
    [string]$Key = "$env:USERPROFILE\.ssh\kpi_tunnel_ed25519"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null
$log = Join-Path $root "data\office-autostart.log"
function Log($message) {
    if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 1MB) {
        Move-Item -LiteralPath $log -Destination "$log.old" -Force
    }
    Add-Content -LiteralPath $log -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $message) -Encoding UTF8
}
$mutex = New-Object System.Threading.Mutex($false, "Local\360-workflow-office")
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
$appProcess = $null
$tunnelProcess = $null
try {
    $cfg = Get-Content -LiteralPath (Join-Path $root "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $port = if ($cfg.server.port) { [int]$cfg.server.port } else { 8082 }
    if ($cfg.server.host -ne "127.0.0.1") { throw "Для туннеля задайте server.host = 127.0.0.1" }
    if (-not (Test-Path -LiteralPath $Key)) { throw "Нет SSH-ключа $Key. Настройте туннель по README." }
    $ssh = Join-Path $env:SystemRoot "System32\OpenSSH\ssh.exe"
    $py = Join-Path $root "python\python.exe"
    $pyPrefix = @()
    if (-not (Test-Path -LiteralPath $py)) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) { $py = $launcher.Source; $pyPrefix = @("-3") }
        else { $py = (Get-Command python -ErrorAction Stop).Source }
    }
    function Keep-Handle { process { $null = $_.Handle; $_ } }
    function Start-App {
        Log "запускаю приложение"
        Start-Process -FilePath $py -ArgumentList ($pyPrefix + @("-u", "manage.py", "run")) -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $root "data\app.out.log") -RedirectStandardError (Join-Path $root "data\app.err.log") | Keep-Handle
    }
    function Start-Tunnel {
        Log "открываю туннель: ${Vps}:127.0.0.1:$RemotePort -> 127.0.0.1:$port"
        $arguments = @("-N", "-i", "`"$Key`"", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
            "-R", "127.0.0.1:${RemotePort}:127.0.0.1:$port", "$TunnelUser@$Vps")
        Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru `
            -RedirectStandardError (Join-Path $root "data\tunnel.err.log") | Keep-Handle
    }
    Log "старт"
    $appProcess = Start-App
    Start-Sleep -Seconds 3
    $tunnelProcess = Start-Tunnel
    while ($true) {
        Start-Sleep -Seconds 15
        if ($appProcess.HasExited) { Log "приложение остановилось (код $($appProcess.ExitCode)), перезапуск"; $appProcess.Dispose(); $appProcess = Start-App }
        if ($tunnelProcess.HasExited) { Log "туннель закрылся (код $($tunnelProcess.ExitCode)), переподключение"; $tunnelProcess.Dispose(); $tunnelProcess = Start-Tunnel }
    }
} catch { Log "ошибка: $($_.Exception.Message)"; throw }
finally {
    foreach ($ownedProcess in @($appProcess, $tunnelProcess)) {
        if ($ownedProcess) { if (-not $ownedProcess.HasExited) { $ownedProcess.Kill() }; $ownedProcess.Dispose() }
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
