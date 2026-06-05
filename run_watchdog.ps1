# run_watchdog.ps1 -- self-healing loop for watchdog.py
# Restarts watchdog.py automatically if it exits for any reason.
while ($true) {
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [watchdog-runner] Starting watchdog.py"
    & C:\Python314\python.exe D:\crash_collector\watchdog.py
    $code = $LASTEXITCODE
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [watchdog-runner] Exited (code=$code) -- restarting in 10s"
    Start-Sleep -Seconds 10
}