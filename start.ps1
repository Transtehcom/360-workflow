# Запуск «Оценка 360°». При отсутствии Python скачивает портативную версию в python\.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ManageArgs = @("run"))
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location -LiteralPath $root
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Test-Python($exe, $extra) {
    try {
        $arguments = @($extra) + @("-c", "import sqlite3, sys; print(sys.version_info >= (3, 10))")
        $output = & $exe @arguments 2>$null
        return ($LASTEXITCODE -eq 0 -and "$output".Trim() -eq "True")
    } catch { return $false }
}

$localPy = Join-Path $root "python\python.exe"
$py = $null
$pyArgs = @()
if ((Test-Path -LiteralPath $localPy) -and (Test-Python $localPy @())) { $py = $localPy }
elseif (Test-Python "py" @("-3")) { $py = "py"; $pyArgs = @("-3") }
elseif (Test-Python "python" @()) { $py = "python" }

if (-not $py) {
    $pyVersion = "3.12.10"
    $url = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
    $archive = Join-Path $root "python-download.zip"
    Write-Host "Python не найден. Скачиваю портативный Python $pyVersion в python\…"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
        Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $root "python") -Force
        $pth = Get-ChildItem -LiteralPath (Join-Path $root "python") -Filter "python*._pth" | Select-Object -First 1
        if ($pth -and -not ((Get-Content -LiteralPath $pth.FullName) -contains "..")) {
            Add-Content -LiteralPath $pth.FullName -Value ".." -Encoding ASCII
        }
        Remove-Item -LiteralPath $archive
        if (-not (Test-Python $localPy @())) { throw "Скачанный Python не запускается" }
        $py = $localPy
    } catch {
        Write-Host "Не удалось подготовить Python: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Установите Python 3.10 или новее с python.org и повторите запуск."
        exit 1
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $root "config.json"))) {
    Copy-Item -LiteralPath (Join-Path $root "config.example.json") -Destination (Join-Path $root "config.json")
    Write-Host "Создан config.json. Настройки Битрикс24 можно заполнить позже."
}
if (-not $ManageArgs.Count) { $ManageArgs = @("run") }
if ($ManageArgs[0] -eq "run") { Write-Host "Запускаю «Оценка 360°». Не закрывайте окно, пока работает приложение." }
$pyArgs += @("-u", "manage.py") + $ManageArgs
& $py @pyArgs
exit $LASTEXITCODE
