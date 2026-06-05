
# COMPOUND paper-trading bot launcher
# Usage: .\start_bot.ps1
# Stop:  echo . > data\bot_stop.flag

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- paths ---
$PY      = "python"
$BOT     = "bot.py"
$ROUNDS_DB = "data\crash.duckdb"
$BOT_DB    = "data\bot_state.duckdb"
$LOG       = "logs\bot.log"

if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }
if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" | Out-Null }

# Remove stale stop flag if present
if (Test-Path "data\bot_stop.flag") {
    Remove-Item "data\bot_stop.flag" -Force
    Write-Host "[start_bot] removed stale stop flag"
}

Write-Host "[start_bot] rounds DB : $ROUNDS_DB"
Write-Host "[start_bot] bot DB    : $BOT_DB"
Write-Host "[start_bot] log       : $LOG"
Write-Host "[start_bot] launching bot.py ..."

# Show initial stats if bot DB already exists
if (Test-Path $BOT_DB) {
    Write-Host "`n[start_bot] --- current state ---"
    & $PY bot_stats.py --bot-db $BOT_DB
}

# Start bot in background, output to log
$proc = Start-Process -FilePath $PY `
    -ArgumentList "$BOT --db $ROUNDS_DB --bot-db $BOT_DB" `
    -RedirectStandardOutput $LOG `
    -RedirectStandardError  "$LOG.err" `
    -NoNewWindow -PassThru

Write-Host "[start_bot] bot started  PID=$($proc.Id)"
Write-Host "[start_bot] logs: $LOG"
Write-Host "[start_bot] stop: echo . > data\bot_stop.flag"
Write-Host ""
Write-Host "To watch live: Get-Content $LOG -Wait"
