$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$env:PYTHONIOENCODING = 'utf-8'
$env:YSP_CONFIG = Join-Path $PSScriptRoot 'config.json'
Set-Location -LiteralPath $PSScriptRoot
try {
    $runtime = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'data/runtime.json') -Raw | ConvertFrom-Json
    $pythonPath = $runtime.python
    if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = (Get-Command python.exe -ErrorAction Stop).Source }
    $manage = Join-Path $PSScriptRoot 'manage.py'
    & $pythonPath $manage migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw '数据库初始化失败，请检查依赖和日志。' }
    $url = 'http://127.0.0.1:8787'
    $running = $false
    try { $h = Invoke-RestMethod ($url + '/api/health') -TimeoutSec 2; $running = $h.service -eq 'electricity-portal' } catch { }
    if (-not $running) {
        if (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue) { throw '8787 端口被占用，请先关闭旧服务。' }
        $proc = Start-Process -FilePath $pythonPath -ArgumentList ('-u "' + $manage + '" serve_portal') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'data/server.log') -RedirectStandardError (Join-Path $PSScriptRoot 'data/server-errors.log') -PassThru
        $proc.Id | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'data/server.pid')
        for ($i=0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 300
            try { $h = Invoke-RestMethod ($url + '/api/health') -TimeoutSec 1; if ($h.service -eq 'electricity-portal') { $running=$true; break } } catch { }
        }
        if (-not $running) { throw '服务启动失败，请查看 data/server-errors.log。' }
    }
    $workerRunning = $false
    $workerPidPath = Join-Path $PSScriptRoot 'data/worker.pid'
    if (Test-Path -LiteralPath $workerPidPath) {
        $workerId = [int](Get-Content -LiteralPath $workerPidPath)
        $workerInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$workerId"
        $workerRunning = $workerInfo -and $workerInfo.CommandLine.Contains($manage) -and $workerInfo.CommandLine.Contains('collect_accounts')
    }
    if (-not $workerRunning) {
        $worker = Start-Process -FilePath $pythonPath -ArgumentList ('-u "' + $manage + '" collect_accounts') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'data/worker.log') -RedirectStandardError (Join-Path $PSScriptRoot 'data/worker-errors.log') -PassThru
        $worker.Id | Set-Content -LiteralPath $workerPidPath
    }
    Start-Process ($url + '/eschool')
    Write-Host '多用户版本已启动：http://127.0.0.1:8787' -ForegroundColor Green
    Write-Host '易校园登录：http://127.0.0.1:8787/eschool'
    Write-Host '后台每10分钟采集，可关闭此窗口。关机后请重新双击启动。'
} catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
