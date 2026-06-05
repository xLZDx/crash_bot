
# Tail all bot logs in parallel (one line per update)
# Press Ctrl+C to stop

$strategies = @("compound", "turtle", "scalper", "sniper", "antimartingale", "x5", "martingale")
$jobs = @()

foreach ($s in $strategies) {
    $logFile = "logs\bot_$s.log"
    if (Test-Path $logFile) {
        $job = Start-Job -ScriptBlock {
            param($path)
            Get-Content $path -Wait -Tail 0
        } -ArgumentList (Resolve-Path $logFile)
        $jobs += @{ name = $s; job = $job }
        Write-Host "Watching $s ($logFile)"
    } else {
        Write-Host "SKIP $s -- log not found: $logFile"
    }
}

if ($jobs.Count -eq 0) {
    Write-Host "No bot logs found. Start bots with .\start_all_bots.ps1 first."
    exit 1
}

Write-Host "--- live output (Ctrl+C to stop) ---"

try {
    while ($true) {
        foreach ($entry in $jobs) {
            $output = Receive-Job -Job $entry.job
            foreach ($line in $output) {
                Write-Host $line
            }
        }
        Start-Sleep -Milliseconds 500
    }
} finally {
    foreach ($entry in $jobs) {
        Stop-Job -Job $entry.job
        Remove-Job -Job $entry.job
    }
}
