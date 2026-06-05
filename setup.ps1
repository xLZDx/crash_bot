Write-Host "Installing crash_collector dependencies..." -ForegroundColor Cyan
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

Write-Host "Installing Chromium browser..." -ForegroundColor Cyan
playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Error "playwright install failed"; exit 1 }

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Quick start:" -ForegroundColor Yellow
Write-Host "  python main.py collect              # collect for 48h (default)"
Write-Host "  python main.py collect --hours 1    # collect for 1h"
Write-Host "  python main.py analyze              # statistical report"
Write-Host "  python main.py export               # save to CSV"
Write-Host "  python main.py inspect-ws           # debug: log raw WS frames"
Write-Host "  python main.py --help               # full help"
