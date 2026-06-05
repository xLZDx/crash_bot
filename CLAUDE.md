# Crash Collector — Project CLAUDE.md

## Project Overview

BCGame crash-game data collector + ML training pipeline + Streamlit dashboard.
All data stays on `D:\crash_collector\`.

## Stack

- Python 3.x, `playwright`, `duckdb`, `scikit-learn`, `lightgbm`, `streamlit`
- **Single persistent Playwright/Chromium session** — direct WS auth blocked by Cloudflare
- Storage: `data/crash.duckdb` (DuckDB)
- Logs: `logs/` directory

## File Map

| File | Purpose |
|---|---|
| `playwright_collector.py` | **Active collector** — single persistent Chromium session (~200 MB RAM), intercepts `socketv4.bcgame61.com` WS frames |
| `ws_collector.py` | Legacy direct-WS collector (blocked by server auth `p`/`t` tokens) — kept for reference |
| `training.py` | ML pipeline: walk-forward CV, LightGBM, quality gate AUC ≥ 0.52 |
| `dashboard.py` | Streamlit dashboard (port 8501): stats, projections, virtual simulator |
| `storage.py` | DuckDB read/write wrapper (`CrashStorage`) |
| `config.py` | Constants: `DB_PATH`, `COLLECTOR_LOG`, `GAME_URL` |
| `main.py` | CLI entry point (typer): `collect`, `train`, `dashboard`, `analyze`, `export` |
| `collect_loop.ps1` | PowerShell watchdog — restarts `main.py collect` on crash (15 s delay) |
| `train_loop.ps1` | PowerShell watchdog — restarts `main.py train` on crash (15 s delay) |

## Current System State (2026-05-19)

- **Collector:** `playwright_collector.py` — single persistent Playwright session, PID varies (watchdog via `collect_loop.ps1`)
  - Parser FIXED 2026-05-19: replaced broken max-scan with `_parse_cm_round_end` from `collector.py`
  - Old parser bug: scanned ALL `/g/cm` frames for `0x30` byte → hit cashout frames → returned 1.09x for every round
  - Fixed parser: filters only `\x02ed` (round-end) and `\x02st` (round-stats) frames → correct crash points
  - Verified: rounds 9258940=1.58x, 9258941=1.91x captured correctly (no more stuck 1.09x)
  - DB wiped and restarted clean at 09:24 on 2026-05-19 (all 2866 corrupt rounds deleted)
- **Training:** `training.py` with `QUALITY_GATE_AUC = 0.52` — signals forced to SKIP if model AUC < 0.52
  - Min rounds for training: 500 (walk-forward CV, `MIN_ROWS=500`) — need ~8h collection to reach threshold
- **Math engine:** `math_engine.py` created 2026-05-19 — 10 models: power law fit, KM survival, HE estimation, Kelly, gambler's ruin, Monte Carlo RoR, gap analysis, independence test, strategy EV, full_report()
- **Math agent:** `crash-math-agent` installed to `~/.claude/agents/` and `agents-skills-repo/agents/` — probability/statistics specialist
- **Dashboard:** `dashboard.py` — Virtual Simulator has Auto (ML) + Manual (play mode) tabs
- **Auto-start (Windows Registry HKCU\Run):**
  - `CrashCollector` → `collect_loop.ps1`
  - `CrashTraining`  → `train_loop.ps1`
- **DB:** `data/crash.duckdb` — clean, collecting from round 9258940 (started 09:24 2026-05-19)

## Frame Protocol (reverse-engineered 2026-05-19, confirmed)

- Server: `wss://socketv4.bcgame61.com/socket.io/?EIO=3&transport=websocket`
- Auth tokens `p` and `t` are Cloudflare-session-bound — cannot be replicated without the browser stack
- Frame format: `\x04\x02\x05/g/cm` (8-byte header) + event suffix + protobuf payload
- **Frame types:**
  - `\x01e`  (`01 65`) — player cashout: field3=cashout_mult×100 (IGNORE — not crash point)
  - `\x02pg` (`02 70 67`) — progress ping / tick counter (IGNORE)
  - `\x02ed` (`02 65 64`) — round end: field1=round_id, field6=crash_point×100 (CAPTURE)
  - `\x02st` (`02 73 74`) — round stats: same as ed + field7=provably-fair hash (CAPTURE)
- **Parser:** `_parse_cm_round_end` in `collector.py` — proper protobuf walk, strips 2-byte suffix,
  handles stray `0x64` wire_type=4 byte, reads field1 (round_id) and field6 (crash×100)
- Keep-alive: Playwright page reload if no round data for >300 s

## Running Manually

```powershell
# Start collector (foreground, for debugging)
python D:\crash_collector\main.py collect --hours 1

# Start training
python D:\crash_collector\main.py train

# Start dashboard
python D:\crash_collector\main.py dashboard

# Check round count
python -c "from storage import CrashStorage; s=CrashStorage('D:/crash_collector/data/crash.duckdb'); print(s.count()); s.close()"
```

## Install Dependencies

```powershell
pip install playwright duckdb scikit-learn lightgbm streamlit typer
python -m playwright install chromium
```

## Resource Budget

| Process | Expected RAM | Expected CPU |
|---|---|---|
| `collect` (playwright_collector) | ~200 MB | ~5-10% idle |
| `train` | ~200-500 MB | ~10-30% (during fit) |
| `dashboard` (Streamlit) | ~100-200 MB | ~2-5% |
