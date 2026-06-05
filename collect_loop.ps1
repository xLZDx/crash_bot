$LogFile = "D:\crash_collector\logs\collector_restart.log"
$null = New-Item -ItemType Directory -Force "D:\crash_collector\logs"

while ($true) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $LogFile "$ts [START] collector"
    & python "D:\crash_collector\main.py" collect --hours 8760 2>> $LogFile
    $code = $LASTEXITCODE
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $LogFile "$ts [EXIT $code] restarting in 15s"
    Start-Sleep -Seconds 15
}
