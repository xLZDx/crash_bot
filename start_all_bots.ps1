
# All-strategy paper-trading bot launcher
# Usage: .\start_all_bots.ps1
# Stop one:  echo . > data\bot_stop_<strategy>.flag
# Stop all:  echo . > data\bot_stop.flag

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$PY        = "python"
$BOT       = "bot.py"
$ROUNDS_DB = "data\crash.duckdb"
$LOG_DIR   = "logs"

if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }
if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR | Out-Null }

# --- stop any running bots via global stop flag ---
$globalFlag = "data\bot_stop.flag"
if (Test-Path $globalFlag) { Remove-Item $globalFlag -Force }

$strategies = @("compound", "turtle", "scalper", "sniper", "antimartingale", "x5", "martingale")

# Remove per-strategy stop flags
foreach ($s in $strategies) {
    $sf = "data\bot_stop_$s.flag"
    if (Test-Path $sf) {
        Remove-Item $sf -Force
        Write-Host "[start_all] removed stale stop flag for $s"
    }
}

# --- migrate legacy DB if present ---
$legacyDb = "data\bot_state.duckdb"
$compoundDb = "data\bot_state_compound.duckdb"
if ((Test-Path $legacyDb) -and (-not (Test-Path $compoundDb))) {
    Write-Host "[start_all] migrating bot_state.duckdb -> bot_state_compound.duckdb"
    Copy-Item $legacyDb $compoundDb
    Write-Host "[start_all] migration done -- original kept as backup"
}

# --- show current stats if DBs exist ---
Write-Host ""
Write-Host "[start_all] --- current state ---"
& $PY bot_stats_all.py --data-dir data

# --- launch all strategy bots ---
Write-Host ""
Write-Host "[start_all] launching all $($strategies.Count) strategy bots ..."
Write-Host ""

foreach ($s in $strategies) {
    $botDb  = "data\bot_state_$s.duckdb"
    $logOut = "$LOG_DIR\bot_$s.log"
    $logErr = "$LOG_DIR\bot_$s.err"

    $proc = Start-Process -FilePath $PY `
        -ArgumentList "-u $BOT --db $ROUNDS_DB --bot-db $botDb --strategy $s" `
        -RedirectStandardOutput $logOut `
        -RedirectStandardError  $logErr `
        -NoNewWindow -PassThru

    Write-Host "[start_all] $s  PID=$($proc.Id)  log=$logOut"
}

Write-Host ""
Write-Host "[start_all] all bots started"
Write-Host "[start_all] stop one:  echo . > data\bot_stop_<strategy>.flag"
Write-Host "[start_all] stop all:  echo . > data\bot_stop.flag"
Write-Host ""
Write-Host "Watch compound:       Get-Content $LOG_DIR\bot_compound.log -Wait"
Write-Host "Watch all (tail):     .\watch_all_bots.ps1"
Write-Host "Stats comparison:     python bot_stats_all.py"
