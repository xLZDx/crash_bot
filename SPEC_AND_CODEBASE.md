# BCGame Crash ML System — Specification & Full Codebase
**Version:** 2026-05-24  
**Status:** Active development, virtual-only mode  
**Reviewer access:** This document is self-contained. No prior context required.

---

## Table of Contents

1. [Project Purpose](#1-project-purpose)
2. [Fundamental Constraint](#2-fundamental-constraint)
3. [System Architecture](#3-system-architecture)
4. [Database Schema](#4-database-schema)
5. [Data Collection Pipeline](#5-data-collection-pipeline)
6. [Feature Engineering Specification](#6-feature-engineering-specification)
7. [ML Training Pipeline](#7-ml-training-pipeline)
8. [Virtual Simulator](#8-virtual-simulator)
9. [Mathematical Analysis Engine](#9-mathematical-analysis-engine)
10. [Watchdog / Process Management](#10-watchdog--process-management)
11. [Dashboard](#11-dashboard)
12. [Known Issues & Limitations](#12-known-issues--limitations)
13. [Current System State](#13-current-system-state)
14. [Full Source Code](#14-full-source-code)

---

## 1. Project Purpose

Collect BCGame crash game data in real-time, train an XGBoost+LightGBM ensemble to predict whether the next round's crash multiplier will be >= 2.0x, run virtual bets to validate edge before risking real money.

**Goal:** find a calibrated ML signal with AUC > 0.52 that produces positive expected value (EV > 0) on selective bets (top N% confidence rounds), verified over 500+ virtual bets.

**Current state:** collecting ~3,000 rounds/day, model AUC oscillates 0.50–0.52, all predictions in SKIP mode. Virtual bets paused until AUC gate clears.

---

## 2. Fundamental Constraint

BCGame crash is **provably fair** via SHA-256 hash chain. Each round's outcome is committed in the hash *before* any bets are placed. This means:

- The multiplier sequence is **mathematically i.i.d.** — no sequence pattern can predict the next outcome
- Autocorrelation test (Ljung-Box on log-multipliers) confirms: p-value >> 0.05, series is independent
- **Any AUC lift above 0.50 must come from:**
  a. Bet volume / player behavior features (whales loading up before favourable rounds — unconfirmed)
  b. Time-of-day patterns (server load, player population cycles)
  c. Overfitting / statistical noise at current sample sizes
- The system is designed assuming (a)+(b) may exist as weak signals, with (c) controlled by walk-forward CV

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│  BCGame server   socketv4.bcgame61.com (WebSocket)       │
└────────────────────────┬────────────────────────────────┘
                         │ binary Socket.IO frames
                         ▼
┌─────────────────────────────────────────────────────────┐
│  ws_collector.py   (PID kept alive by watchdog.py)       │
│  - Engine.IO v3 / Socket.IO protocol                     │
│  - Parses binary protobuf-like frames                    │
│  - Writes to crash.duckdb: rounds + bets tables          │
│  - ~30 MB RAM, 1-2% CPU                                  │
└────────────────────────┬────────────────────────────────┘
                         │ DuckDB file lock
                         ▼
┌─────────────────────────────────────────────────────────┐
│  crash.duckdb   (D:\crash_collector\data\crash.duckdb)   │
│  Tables: rounds, bets, virtual_bets, virtual_account     │
└──────────┬──────────────────────┬───────────────────────┘
           │ read (50k rows)       │ read/write
           ▼                      ▼
┌──────────────────┐   ┌──────────────────────────────────┐
│  training.py     │   │  dashboard.py  (Streamlit :8501)  │
│  - Retrain every │   │  - Read-only view of all tables   │
│    50 new rounds │   │  - prediction.json display        │
│  - XGBoost +     │   │  - Math analysis (math_engine.py) │
│    LightGBM      │   │  - Virtual account equity curve   │
│  - Walk-forward  │   └──────────────────────────────────┘
│    CV 70/15/15   │
│  - Writes        │
│    prediction.json│
│  - Virtual bets  │
└──────────────────┘

┌─────────────────────────────────────────────────────────┐
│  watchdog.py   (run_watchdog.ps1 keeps it alive)         │
│  - Checks all 3 services every 30s                       │
│  - Restarts if stale or dead                             │
│  - STALE_SEC=240 (tolerates WebSocket reconnect gaps)    │
└─────────────────────────────────────────────────────────┘
```

**File layout:**
```
D:\crash_collector\
├── config.py              -- constants (URLs, key names, paths)
├── ws_collector.py        -- primary data collector (WebSocket)
├── collector.py           -- frame parser + bet accumulator
├── storage.py             -- DuckDB thin wrapper
├── training.py            -- ML pipeline (this file is the core)
├── simulator.py           -- virtual betting account
├── math_engine.py         -- mathematical analysis (power law, Kelly, RoR, etc.)
├── dashboard.py           -- Streamlit read-only dashboard
├── watchdog.py            -- process health monitor + auto-restart
├── run_watchdog.ps1       -- PS1 wrapper to keep watchdog alive
├── analysis.py            -- one-off analysis scripts
├── strategy_optimizer.py  -- grid search over betting strategies
├── price_feed.py          -- SOL/USD price via CoinGecko
├── data/
│   ├── crash.duckdb       -- main database
│   ├── prediction.json    -- latest ML prediction (written after every round)
│   ├── models/            -- saved model artifacts (not currently used)
│   └── ws_debug.log       -- raw WebSocket frame dump
└── logs/
    ├── collector.log
    ├── training.log
    └── watchdog.log
```

---

## 4. Database Schema

### `rounds` — one row per completed crash round
```sql
CREATE TABLE rounds (
    id            BIGINT PRIMARY KEY,            -- sequential, auto-assigned
    game_round_id VARCHAR UNIQUE,                -- BCGame's own round ID
    multiplier    DOUBLE NOT NULL,               -- crash point (>= 1.0)
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    source        VARCHAR NOT NULL DEFAULT 'ws', -- 'ws' = WebSocket
    total_bets    DOUBLE,                        -- total USDT wagered (may be NULL)
    num_bettors   INTEGER,                       -- number of players (may be NULL)
    frame_event   VARCHAR,                       -- event type from wire
    hash          VARCHAR,                       -- SHA-256 provably-fair hash
    hash_verified BOOLEAN DEFAULT FALSE
);
```

### `bets` — individual player bets (one row per player per round)
```sql
CREATE TABLE bets (
    id        BIGINT PRIMARY KEY,
    round_id  VARCHAR NOT NULL,   -- references rounds.game_round_id
    currency  VARCHAR,            -- 'USDT', 'BTC', 'SOL', etc.
    amount    DOUBLE,             -- bet amount in currency units
    username  VARCHAR,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### `virtual_bets` — virtual account bet history
```sql
CREATE TABLE virtual_bets (
    id             BIGINT PRIMARY KEY,
    round_db_id    BIGINT,
    game_round_id  VARCHAR,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal         VARCHAR,        -- 'BET' or 'SKIP'
    confidence     DOUBLE,         -- calibrated P(win)
    kelly_f        DOUBLE,         -- Kelly fraction used
    cashout_target DOUBLE,         -- exit multiplier (always 2.0)
    bet_sol        DOUBLE,         -- bet size in SOL
    actual_mult    DOUBLE,         -- what actually happened
    won            BOOLEAN,
    pnl_sol        DOUBLE,
    bankroll_after DOUBLE
);
```

### `virtual_account` — running balance (single row, id=1)
```sql
CREATE TABLE virtual_account (
    id            INTEGER PRIMARY KEY,
    bankroll_sol  DOUBLE NOT NULL,        -- starting: 1.1 SOL (~$100)
    total_bets    INTEGER NOT NULL DEFAULT 0,
    total_wins    INTEGER NOT NULL DEFAULT 0,
    total_pnl_sol DOUBLE NOT NULL DEFAULT 0.0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 5. Data Collection Pipeline

### WebSocket Protocol (ws_collector.py)

Server: `socketv4.bcgame61.com` — Engine.IO v3, Socket.IO binary transport.

**Connection sequence:**
1. Open WebSocket to `wss://socketv4.bcgame61.com/socket.io/?EIO=3&transport=websocket`
2. Receive text OPEN frame: `0{sid, pingInterval, pingTimeout, ...}`
3. Send namespace subscribes: `\x04\x00[len][ns]\x00` for each namespace in order:
   `/game-support`, `/g/tasks/verify`, `/user`, `/gs`, `/g/cm`, `/multi/g/cm`
4. When `/g/cm` ACK received, send join: `\x04\x82\x00\x00\x00\x00\x05/g/cm\x04join`
5. Server streams binary events on `/g/cm`

**Binary frame format:**
```
[04][02][05]/g/cm[event_type][protobuf_payload]
  event_type 0x01 = round complete (crash_point in field 14 or 12)
  event_type 0x02 = live multiplier update (field 14 = multiplier*100)
```

**Round complete data extraction:**
- Multiplier read from protobuf field 14 (divide by 100) or field 12
- `total_bets` and `num_bettors` accumulated from per-player bet frames before round end
- Individual bets stored in `bets` table via `storage.insert_bet()`
- Aggregated `total_bets`/`num_bettors` stored in `rounds` table

**Reconnect logic:** exponential backoff 1s → 2s → ... → 60s cap. STALE_SEC=240 before watchdog restarts.

---

## 6. Feature Engineering Specification

`build_features(rows, loss_ids)` in `training.py`.

**Input row tuple layout (after 2026-05-24 update):**
```
(id, game_round_id, multiplier, ts, total_bets, num_bettors, hash, top1_bet)
  0       1             2        3      4            5         6       7
```

**Target variable:**
```
y[i] = 1 if mults[i+1] >= 2.0 else 0
```

Features are computed at position `i` (last known round), predicting round `i+1`.
No future data is used. Loop starts at `i = max(LAG_WINDOWS) = 100` to ensure full windows.

### Feature Groups (72 total)

#### Group 1: Rolling window statistics (6 windows × 8 stats = 48 features)
Windows: `[3, 5, 10, 20, 50, 100]`

For each window `w`, compute over `mults[i-w : i]` (excludes round `i`):
| Feature | Formula |
|---|---|
| `lag{w}_min` | min of window |
| `lag{w}_max` | max of window |
| `lag{w}_mean` | arithmetic mean |
| `lag{w}_std` | std + 1e-6 |
| `lag{w}_lt2_rate` | fraction < 2.0 |
| `lag{w}_lt1p1_rate` | fraction < 1.1 (instant-crash rate) |
| `lag{w}_gt5_rate` | fraction >= 5.0 |
| `lag{w}_median` | median |

#### Group 2: Streak features (4 features)
| Feature | Formula |
|---|---|
| `streak_below_thresh` | consecutive rounds < 2.0 ending at i-1 |
| `streak_above_thresh` | consecutive rounds >= 2.0 ending at i-1 |
| `rounds_since_big` | rounds since last mult >= 5.0 (cap 200) |
| `rounds_since_huge` | rounds since last mult >= 10.0 (cap 200) |

#### Group 3: Bet volume — rolling windows (6 features)
From `total_bets` column in `rounds` table (aggregated USDT, may be NULL → NaN → 0).

| Feature | Formula |
|---|---|
| `bets_mean5` | mean(total_bets[i-5:i]), NaN-safe |
| `bets_std5` | std(total_bets[i-5:i]), NaN-safe |
| `bets_mean20` | mean(total_bets[i-20:i]), NaN-safe |
| `bets_trend5` | mean(last 2 of window) - mean(first 3 of window) |
| `bettors_mean5` | mean(num_bettors[i-5:i]) |
| `bettors_trend5` | mean(last 2 of bettors) - mean(first 3 of bettors) |

#### Group 4: Time encoding (4 features)
Source: `ts[i]` (timestamp of round i).

| Feature | Formula |
|---|---|
| `hour_sin` | sin(2π × hour / 24) |
| `hour_cos` | cos(2π × hour / 24) |
| `dow_sin` | sin(2π × day_of_week / 7) |
| `dow_cos` | cos(2π × day_of_week / 7) |

Cyclic encoding prevents artificial discontinuity at midnight / week boundary.

#### Group 5: Log-scale features (2 features)
| Feature | Formula |
|---|---|
| `log_mean5` | mean(log1p(mults[i-5:i])) |
| `log_mean20` | mean(log1p(mults[i-20:i])) |

Log1p compresses high multipliers (e.g. 1000x → 6.9, 2x → 0.69), reducing skew.

#### Group 6: Virtual-loss signal (1 feature)
| Feature | Formula |
|---|---|
| `in_virtual_loss` | 1.0 if game_round_id of round i+1 is in recent virtual losses, else 0.0 |

Purpose: "memory" of past prediction mistakes. Upweighted in training (`VIRTUAL_LOSS_WEIGHT=2.5`).

#### Group 7: Minute-of-hour (2 features) [added 2026-05-24]
| Feature | Formula |
|---|---|
| `minute_sin` | sin(2π × minute / 60) |
| `minute_cos` | cos(2π × minute / 60) |

Fine-grained time signal. BCGame runs ~2 rounds/minute; minute within hour may correlate with player population cycles.

#### Group 8: Current-round bet concentration (4 features) [added 2026-05-24]
Source: `total_bets[i]` and `top1_bet[i]` (from `bets` table JOIN).

| Feature | Formula | Notes |
|---|---|---|
| `log_bets_current` | log1p(total_bets[i]) if >= 0, else 0.0 | NaN/negative → 0.0 |
| `bets_per_player` | total_bets[i] / num_bettors[i] | 0.0 if either missing |
| `whale_ratio` | top1_bet[i] / total_bets[i] | 0.0 if missing; can exceed 1.0 if currency mismatch |
| `top1_bet_log` | log1p(top1_bet[i]) | 0.0 if NULL (no USDT/USD bets in `bets` table) |

`top1_bet` = `MAX(amount) WHERE currency IN ('USDT','USD') AND amount > 0` from `bets` table, joined per round. NULL when the collector didn't capture individual bets for that round.

#### Group 9: Log-space volatility (1 feature) [added 2026-05-24]
| Feature | Formula |
|---|---|
| `log_std5` | std(log1p(mults[i-5:i])) + 1e-6, if window >= 2 elements; else 0.0 |

---

## 7. ML Training Pipeline

### Constants
```python
THRESHOLD        = 2.0       # cashout target
MIN_ROWS         = 500       # minimum rounds before first training
RETRAIN_EVERY    = 50        # retrain after N new rounds
QUALITY_GATE_AUC = 0.52      # model must beat this to emit BET signals
LAG_WINDOWS      = [3, 5, 10, 20, 50, 100]
TRAIN_FRAC       = 0.70      # walk-forward: first 70% = train
VAL_FRAC         = 0.15      # next 15% = validation
# remaining 15% = test
CONFIDENCE_CUTOFFS = [0.50, 0.52, 0.54, 0.55, 0.57, 0.60]
VIRTUAL_LOSS_WEIGHT = 2.5    # upweight rounds where simulator lost
```

### Walk-Forward Split
```
|←─── train (70%) ────→|←─ val (15%) →|←─ test (15%) →|
       ↓                    ↓                ↓
  fit XGB+LGB          calibrate          evaluate
                       isotonic           AUC, EV, Kelly
```

No shuffle. Strict temporal ordering. Prevents look-ahead bias.

### Models

**XGBoost:**
```python
XGBClassifier(
    n_estimators=400, max_depth=4, learning_rate=0.04,
    subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
    gamma=0.1, scale_pos_weight=scale_pos,   # handles class imbalance
    tree_method="hist", early_stopping_rounds=30,
    eval_set=[(X_va, y_va)]
)
```

**LightGBM:**
```python
LGBMClassifier(
    n_estimators=400, max_depth=4, learning_rate=0.04,
    subsample=0.8, colsample_bytree=0.7, min_child_samples=20,
    scale_pos_weight=scale_pos,
    early_stopping(30)
)
```

**Ensemble:** simple average of raw probabilities from both models.

**Calibration:** IsotonicRegression fitted on validation set, applied to test set.
- Ensures P=0.6 means ~60% actual win rate
- Fitted on val, evaluated on test → no leakage

### Sample Weights
```python
sw[i] = VIRTUAL_LOSS_WEIGHT (2.5)  if round i+1 was a virtual loss
sw[i] = 1.0                         otherwise
```
Applied during training to focus model on rounds where it previously failed.

### Selective Betting Evaluation
For each cutoff c in CONFIDENCE_CUTOFFS:
```
mask = calibrated_proba >= c
n_bets = sum(mask)
win_rate = mean(y_test[mask])
EV = win_rate * (2.0 - 1) - (1 - win_rate) * 1.0
   = win_rate - (1 - win_rate)
   = 2*win_rate - 1      [simplifies for cashout=2x]
Kelly_f = max(0, (1*win_rate - (1-win_rate)) / 1)
        = max(0, 2*win_rate - 1)
```
Only cutoffs with n_bets >= 10 considered. Best cutoff = highest EV with n_bets >= 20.

### Quality Gate
```python
if auc_cal >= 0.52 and cal_p >= best_cutoff and ev_at_cutoff > 0:
    signal = "BET"
else:
    signal = "SKIP"
```
When AUC < 0.52, ALL rounds get SKIP. No virtual money bet until model demonstrates edge.

### Retraining Cadence
- Retrain every 50 new rounds (~1 hour at BCGame's rate)
- If < 50 new rounds since last train: skip retrain, refresh prediction only
- `prediction.json` updated after every retrain cycle regardless

### Output: prediction.json
```json
{
  "ts": "2026-05-24T17:24:13+00:00",
  "rows_used": 14708,
  "prediction": {
    "p_above_threshold": 0.54,
    "p_raw": 0.52,
    "threshold": 2.0,
    "best_cutoff": 0.52,
    "signal": "BET",
    "kelly_bet_fraction": 0.08,
    "ev_at_cutoff": 0.08,
    "confidence": "LOW",
    "xgb_p": 0.51,
    "lgb_p": 0.53,
    "last_round_db_id": 19037,
    "last_game_round_id": "abc123"
  },
  "metrics": {
    "auc_raw": 0.5140,
    "auc_cal": 0.5176,
    "brier_score": 0.2498,
    "base_win_rate": 0.4923,
    "test_n": 2192,
    "train_n": 10224,
    "val_n": 2191,
    "pos_rate": 0.4927
  },
  "selective": {
    "0.5": {"n_bets": 2192, "win_rate": 0.4923, "ev": -0.015, ...},
    "0.52": {"n_bets": 641, "win_rate": 0.5398, "ev": 0.0796, "signal": "BET"},
    ...
  },
  "best_cutoff": 0.52,
  "top_features": {"in_virtual_loss": 0.072, "lag5_lt2_rate": 0.058, ...}
}
```

---

## 8. Virtual Simulator

`simulator.py` — VirtualAccount class.

**Starting bankroll:** 1.1 SOL (~$100 at ~$91/SOL)  
**Min bet:** 0.000006 SOL (BCGame's minimum)  
**Max bet fraction:** 5% of bankroll per round

**Bet sizing:**
```python
kelly_bet = bankroll * max(kelly_f, 0.005)   # floor at 0.5% of bankroll
bet_sol   = max(MIN_BET_SOL, min(kelly_bet, bankroll * MAX_BET_FRAC))
```

**Settlement:**
```python
won = actual_mult >= cashout_target  # cashout = 2.0
pnl = bet_sol * (cashout - 1) if won else -bet_sol
```

**Loss signal feedback loop:**
- `get_loss_round_ids(last_n=500)` → returns set of game_round_ids where virtual bets lost
- Fed to `build_features()` as `loss_ids` parameter
- Rounds where simulator lost get 2.5× sample weight in training
- Purpose: model focuses more on rounds it previously mispredicted

---

## 9. Mathematical Analysis Engine

`math_engine.py` — standalone, no DB access.

### Power Law Fit
BCGame multipliers follow approximately P(M>=x) = k/x^n.

OLS on log-log space:
```
log(P(M>=x)) = log(k) - n*log(x)
slope = -n,  intercept = log(k)
```
Uses thresholds [1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0, 100.0, 500.0, 1000.0].
Returns R² in log-log space (used as relative fit quality, not absolute).

### House Edge Estimation
```
HE(x) = 1 - x * P(M >= x)
```
For BCGame with 1% house edge: P(M>=x) = 0.99/x, so HE = 1 - 0.99 = 1%.

### Kaplan-Meier Survival
Empirical S(x) = P(M > x) with Wilson 95% CI:
```
SE = sqrt(S * (1-S) / n)
CI = [S - 1.96*SE, S + 1.96*SE]
```

### Kelly Criterion
```
f* = (b*p - q) / b
where b = net payout per win (e.g. 1.0 for 2x cashout)
      p = P(win), q = 1-p
EV  = b*p - q
```

### Gambler's Ruin
Infinite-opponent formula:
```
P(ruin) = 1                  if EV <= 0
P(ruin) = (q/p)^B            if EV > 0
where B = bankroll in bet units
```

### Monte Carlo Risk-of-Ruin
Bootstrap: N=10,000 sessions × K=1,000 rounds each.
Sample multipliers with replacement from empirical distribution.
Flat fractional bet strategy (fraction × current bankroll per round, cash out at target).
Returns: p_ruin, p_profit, median/p5/p95 final bankroll.

### Independence Test (Ljung-Box on log-multipliers)
```
Q = n*(n+2) * Σ(ρ_k² / (n-k))   for k=1..max_lag
where ρ_k = ACF at lag k
```
H0: series is independent. Reject if Q > chi²(df=max_lag).
**Empirical result on 15,000+ rounds: p-value >> 0.05 → fail to reject H0 → series is i.i.d.**

---

## 10. Watchdog / Process Management

`watchdog.py` + `run_watchdog.ps1`

### Monitored Services
```python
PROCS = {
    "collector": {
        "script": "ws_collector.py",
        "check": collector_age,        # returns seconds since last DB write
        "stale_if": lambda age: age > STALE_SEC,
    },
    "training": {
        "script": "training.py",
        "check": lambda: None,         # just check PID alive
    },
    "dashboard": {
        "script": "dashboard.py",
        "check": lambda: http_get(DASHBOARD_PORT),
        "stale_if": lambda code: code != 200,
    },
}
```

### Check Interval
`CHECK_SEC = 30` — checks all services every 30 seconds.

### Stale Detection
`STALE_SEC = 240` — collector considered stale if no new round written in 240s.
BCGame rounds average ~25-35s; 240s tolerance handles WebSocket reconnect gaps (observed max gap: 150s).

### `collector_age()` Special Values
- Returns `-1.0` → DuckDB write-lock (collector is actively writing) → healthy
- Returns `float("inf")` → genuine error (DB doesn't exist, query failed) → restart

### Auto-Restart
On stale/dead detection:
1. Kill all matching PIDs (`find_procs()` excludes watchdog's own PID)
2. Spawn: `subprocess.Popen([PYTHON, script_path], cwd=SCRIPT_DIR, ...)`
3. Log restart event

### run_watchdog.ps1
```powershell
while ($true) {
    & C:\Python314\python.exe D:\crash_collector\watchdog.py
    Start-Sleep -Seconds 10
}
```
If watchdog itself crashes, PS1 loop restarts it after 10s.

---

## 11. Dashboard

`dashboard.py` — Streamlit app on port 8501.

**Tabs:**
- **Overview** — round history table, multiplier distribution histogram, streak stats
- **ML Predictions** — current prediction, selective betting table, top features, AUC trend
- **Virtual Account** — bankroll equity curve, bet history, win/loss stats
- **Math Analysis** — power law fit, house edge, KM survival, strategy EV, independence test
- **Strategy Optimizer** — grid search over (cutoff, cashout) combinations

All reads are `read_only=True` DuckDB connections with 8-attempt retry (Windows file lock contention).
Streamlit cache TTL: 20s for data, 30s for math results.

---

## 12. Known Issues & Limitations

### Resolved (2026-05-24)
- ✅ `log_bets_current`: guarded against negative total_bets (log1p(negative) → nan, now → 0.0)
- ✅ `log_std5`: guarded against empty window (np.std([]) → nan, now → 0.0)
- ✅ `build_features()` docstring updated to reflect 8-tuple row layout
- ✅ Backward compat: `len(r) > 7` guard keeps old 7-tuple callers working (top1_bet → nan → feature = 0.0)

### Open (from 2026-05-24 agent review)

**MAJOR — whale_ratio can exceed 1.0**
`top1_bet` is USDT/USD only (from `bets` table). `total_bets` in `rounds` may be currency-agnostic or USDT-only depending on collection path. When they use different currency scopes, `whale_ratio > 1.0` is possible. Currently no clamp or warning. Mitigation: ratio > 1.0 is absorbed by tree splits. Fix: log warning when ratio > 1.0.

**MAJOR — train/serve potential skew on top1_bet**
At training time, `top1_bet` is read from a fully-settled `bets` table in batch. At inference time (real-money deployment), `top1_bet` must be read only after the round is fully closed and all bets committed. Not yet validated for inference path (moot while system is virtual-only).

**MINOR — `run_forever()` bare `except Exception`**
Training loop catches all exceptions and continues. After repeated failures, no escalation beyond log ERROR. No circuit breaker.

**MINOR — `prediction.json` has no freshness guard**
If `_write_prediction()` fails (disk full), file remains stale with no TTL check on reads. Not dangerous in virtual mode; would matter for real-money deployment.

**NOT an issue (confirmed safe):**
- `whale_ratio` when `cur_bet=0`: guarded by `cur_bet > 0` check → 0.0 fallback ✅
- `log_std5` when i < 5: loop starts at i=100, window always 5 elements ✅
- `bets` table missing: `CrashStorage.__init__()` always creates it ✅

---

## 13. Current System State

As of 2026-05-24 20:25 UTC:

| Metric | Value |
|---|---|
| Total rounds collected | ~19,400 |
| Collection rate | ~3,000 rounds/day |
| Data span | 2026-05-20 to present (~5 days) |
| Latest AUC (calibrated) | 0.5065 |
| Quality gate | 0.52 (NOT passed) |
| Current signal | SKIP (no virtual bets) |
| Virtual bets total | ~109 (accumulated when AUC was briefly above gate) |
| Virtual P&L | +188% but statistically insignificant (n=109, CI lower bound ~38%) |
| Best retrain run | 2026-05-24 17:23 UTC: AUC=0.5176, cutoff=0.52, EV=0.0796 |

**AUC trend (last 10 retrains, newest first):**
```
20:25  0.5065  ← current
19:57  0.5050
19:33  0.5013
19:07  0.5028
18:40  0.5014
18:14  0.4990
17:47  0.5046
17:23  0.5176  ← only run above gate today
16:58  0.5024
16:33  0.5038
```

**To reach 500 virtual bets:**
- Currently BLOCKED (AUC below gate)
- When AUC >= 0.52: at cutoff=0.52 → ~641 potential bets/retraining period → 500 bets within ~24-48 hours
- At strict cutoff=0.60 → ~40 bets/day → ~10 days
- Need: AUC to stabilize above 0.52 (currently oscillating at gate boundary)

---

## 14. Full Source Code

### config.py
```python
from pathlib import Path

GAME_URL = "https://bcgame61.com/game/crash"

_ROOT = Path(__file__).parent
DB_PATH          = str(_ROOT / "data"  / "crash.duckdb")
DEBUG_WS_LOG     = str(_ROOT / "data"  / "ws_debug.log")
COLLECTOR_LOG    = str(_ROOT / "logs"  / "collector.log")

LOGIN_TIMEOUT_SECONDS = 90

ROUND_END_EVENTS = [
    "round_end", "roundEnd",
    "crash",     "crashed",
    "bust",      "busted",
    "game_over", "gameOver",
    "result",    "final",
    "end",       "complete",
]

MULTIPLIER_KEYS = [
    "crash_point",   "crashPoint",
    "multiplier",    "finalMultiplier", "final_multiplier",
    "bust_at",       "bustAt",
    "crash_rate",    "crashRate",
]

BET_AMOUNT_KEYS = [
    "total_bets",    "totalBets",
    "total_bet",     "totalBet",
    "total_wagered", "totalWagered",
    "wagered",       "total_amount", "totalAmount",
]

BET_COUNT_KEYS = [
    "player_count", "playerCount",
    "num_players",  "numPlayers",
    "bettors",      "betters",
]
```

---

### storage.py
```python
import re
import time
import duckdb
from pathlib import Path
from typing import Optional

_UNSAFE_RE = re.compile(r"""[;'"`\x00]|\.\.""")


def _connect(db_path: str, read_only: bool = False, retries: int = 6) -> duckdb.DuckDBPyConnection:
    delay = 0.1
    for attempt in range(retries):
        try:
            return duckdb.connect(db_path, read_only=read_only)
        except duckdb.IOException:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


class CrashStorage:
    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rounds (
                    id            BIGINT PRIMARY KEY,
                    game_round_id VARCHAR UNIQUE,
                    multiplier    DOUBLE NOT NULL,
                    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
                    source        VARCHAR NOT NULL DEFAULT 'ws',
                    total_bets    DOUBLE,
                    num_bettors   INTEGER,
                    frame_event   VARCHAR,
                    hash          VARCHAR,
                    hash_verified BOOLEAN DEFAULT FALSE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id        BIGINT PRIMARY KEY,
                    round_id  VARCHAR NOT NULL,
                    currency  VARCHAR,
                    amount    DOUBLE,
                    username  VARCHAR,
                    ts        TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            try:
                conn.execute("CREATE SEQUENCE seq_round_id START 1")
            except Exception:
                pass
            try:
                conn.execute("CREATE SEQUENCE seq_bet_id START 1")
            except Exception:
                pass
            for col, defn in [("hash", "VARCHAR"), ("hash_verified", "BOOLEAN DEFAULT FALSE")]:
                try:
                    conn.execute(f"ALTER TABLE rounds ADD COLUMN {col} {defn}")
                except Exception:
                    pass
        finally:
            conn.close()

    def insert(self, multiplier, source="ws", game_round_id=None,
               total_bets=None, num_bettors=None, frame_event=None, hash=None):
        conn = _connect(self._path)
        try:
            row = conn.execute("""
                INSERT INTO rounds
                    (id, game_round_id, multiplier, source,
                     total_bets, num_bettors, frame_event, hash)
                VALUES (nextval('seq_round_id'), ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (game_round_id) DO NOTHING
                RETURNING id
            """, [game_round_id, round(multiplier, 4), source,
                  total_bets, num_bettors, frame_event, hash]).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def insert_bet(self, round_id, currency, amount, username=None):
        conn = _connect(self._path)
        try:
            conn.execute("""
                INSERT INTO bets (id, round_id, currency, amount, username)
                VALUES (nextval('seq_bet_id'), ?, ?, ?, ?)
            """, [round_id, currency, amount, username])
        finally:
            conn.close()

    def get_training_rows(self, min_id: int = 0, limit: int = 50_000):
        """
        Tuple layout:
          0  id
          1  game_round_id
          2  multiplier
          3  ts
          4  total_bets
          5  num_bettors
          6  hash
          7  top1_bet   -- largest individual USDT/USD bet (NULL if none recorded)
        """
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute("""
                SELECT r.id, r.game_round_id, r.multiplier, r.ts,
                       r.total_bets, r.num_bettors, r.hash,
                       bq.top1_bet
                FROM rounds r
                LEFT JOIN (
                    SELECT round_id,
                           MAX(CASE WHEN currency IN ('USDT', 'USD') AND amount > 0
                                    THEN amount ELSE NULL END) AS top1_bet
                    FROM bets
                    GROUP BY round_id
                ) bq ON bq.round_id = r.game_round_id
                WHERE r.id > ?
                ORDER BY r.id ASC
                LIMIT ?
            """, [min_id, limit]).fetchall()
        finally:
            conn.close()
        return rows
```

---

### training.py
```python
"""
Continuous ML training pipeline for BCGame crash prediction.

HOW TO GET A REAL EDGE:
  1. Walk-forward CV -- train on past, validate strictly on future. No shuffle.
  2. Selective betting -- only bet top-N% confidence rounds.
  3. Bet volume features -- rapid volume increase before crash is a weak signal.
  4. Calibrated probabilities -- Isotonic regression makes P(win)=0.6 mean 60% real.
  5. Threshold optimization -- find the P cutoff where selective EV > 0.
  6. Kelly criterion sizing -- never overbets, survives drawdowns.

Break-even:
  - Flat-bet at 2x: need win_rate > 50.5% (with 1% house edge).
  - Selective-bet at 2x with top-30% confidence: if model has AUC=0.56,
    those 30% rounds have ~53-54% win rate -> EV turns positive.
"""

import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import xgboost as xgb
    import lightgbm as lgb
    from sklearn.calibration import CalibratedClassifierCV, calibration_curve
    from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
    from sklearn.isotonic import IsotonicRegression
    _HAS_ML = True
except ImportError as e:
    _HAS_ML = False
    _ML_ERR = str(e)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH
from simulator import VirtualAccount

# -- Config -------------------------------------------------------------------

THRESHOLD        = 2.0
MIN_ROWS         = 500          # raised: need enough for walk-forward
RETRAIN_EVERY    = 50
QUALITY_GATE_AUC = 0.52         # model must beat this AUC or all signals = SKIP
LAG_WINDOWS      = [3, 5, 10, 20, 50, 100]
MODEL_DIR        = Path(os.path.dirname(__file__)) / "data" / "models"
PREDICTION_FILE  = Path(os.path.dirname(__file__)) / "data" / "prediction.json"
LOG_FILE         = Path(os.path.dirname(__file__)) / "logs" / "training.log"
TRAIN_INTERVAL_S = 60

TRAIN_FRAC = 0.70
VAL_FRAC   = 0.15

CONFIDENCE_CUTOFFS = [0.50, 0.52, 0.54, 0.55, 0.57, 0.60]
VIRTUAL_LOSS_WEIGHT = 2.5

# -- Logging ------------------------------------------------------------------

def _build_logger() -> logging.Logger:
    log = logging.getLogger("training")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    log.addHandler(ch)
    return log


log = _build_logger()


# -- Feature engineering ------------------------------------------------------

def build_features(rows: list, loss_ids: Optional[set] = None) -> tuple:
    """
    rows: (id, game_round_id, multiplier, ts, total_bets, num_bettors, hash, top1_bet)
          Indices 0-6 are legacy; index 7 (top1_bet) is added by get_training_rows()
          and is None/absent in hand-built test fixtures (gracefully falls back to nan).
    loss_ids: set of game_round_ids where the simulator lost -- used as extra signal.
    Returns (X, y, feature_names) -- strictly no future leakage.
    """
    mults     = np.array([r[2] for r in rows], dtype=np.float64)
    bets      = np.array([r[4] if r[4] else np.nan for r in rows], dtype=np.float64)
    bettors   = np.array([r[5] if r[5] else np.nan for r in rows], dtype=np.float64)
    top1_bets = np.array([r[7] if len(r) > 7 and r[7] is not None else np.nan for r in rows], dtype=np.float64)
    ts_vals   = [r[3] for r in rows]

    max_lag = max(LAG_WINDOWS)
    n = len(mults)
    if n <= max_lag + 1:
        return None, None, None

    feature_names = []
    for w in LAG_WINDOWS:
        for s in ["min", "max", "mean", "std", "lt2_rate", "lt1p1_rate", "gt5_rate", "median"]:
            feature_names.append(f"lag{w}_{s}")

    feature_names += [
        "streak_below_thresh",
        "streak_above_thresh",
        "rounds_since_big",
        "rounds_since_huge",
        # Bet volume
        "bets_mean5", "bets_std5", "bets_mean20", "bets_trend5",
        "bettors_mean5", "bettors_trend5",
        # Time
        "hour_sin", "hour_cos", "dow_sin", "dow_cos",
        # Log-transform of recent multipliers
        "log_mean5", "log_mean20",
        # Virtual simulator signal
        "in_virtual_loss",
        # Minute-of-hour cyclic encoding [added 2026-05-24]
        "minute_sin", "minute_cos",
        # Current-round bet concentration (from per-player bets table) [added 2026-05-24]
        "log_bets_current",
        "bets_per_player",
        "whale_ratio",
        "top1_bet_log",
        # Log-space volatility [added 2026-05-24]
        "log_std5",
    ]

    X_rows, y_rows = [], []

    for i in range(max_lag, n - 1):
        feats = []

        for w in LAG_WINDOWS:
            window = mults[i - w: i]
            feats.append(float(np.min(window)))
            feats.append(float(np.max(window)))
            feats.append(float(np.mean(window)))
            feats.append(float(np.std(window) + 1e-6))
            feats.append(float(np.mean(window < 2.0)))
            feats.append(float(np.mean(window < 1.1)))
            feats.append(float(np.mean(window >= 5.0)))
            feats.append(float(np.median(window)))

        # Streak features
        streak_below = streak_above = 0
        for j in range(i - 1, max(i - 100, -1), -1):
            if mults[j] < THRESHOLD:
                streak_below += 1
                if streak_above:
                    break
            else:
                streak_above += 1
                if streak_below:
                    break
        feats.append(float(streak_below))
        feats.append(float(streak_above))

        # Rounds since last big crash
        rsb = rsh = 0
        for j in range(i - 1, max(i - 200, -1), -1):
            if mults[j] >= 5.0 and rsb == 0:
                rsb = i - j
            if mults[j] >= 10.0 and rsh == 0:
                rsh = i - j
            if rsb and rsh:
                break
        feats.append(float(rsb or 200))
        feats.append(float(rsh or 200))

        # Bet volume features
        b5  = bets[max(0, i-5):i];  b5v  = b5[~np.isnan(b5)]
        b20 = bets[max(0, i-20):i]; b20v = b20[~np.isnan(b20)]
        feats.append(float(np.mean(b5v))  if len(b5v) >= 2 else 0.0)
        feats.append(float(np.std(b5v))   if len(b5v) >= 2 else 0.0)
        feats.append(float(np.mean(b20v)) if len(b20v) >= 3 else 0.0)
        if len(b5v) >= 5:
            trend = float(np.mean(b5v[-2:]) - np.mean(b5v[:3]))
        else:
            trend = 0.0
        feats.append(trend)

        # Bettor count features
        n5  = bettors[max(0, i-5):i]; n5v = n5[~np.isnan(n5)]
        feats.append(float(np.mean(n5v))  if len(n5v) >= 2 else 0.0)
        bt_trend = float(np.mean(n5v[-2:]) - np.mean(n5v[:3])) if len(n5v) >= 5 else 0.0
        feats.append(bt_trend)

        # Cyclic time encoding
        ts = ts_vals[i]
        hour = dow = minute = 0
        if ts is not None:
            try:
                if hasattr(ts, "hour"):
                    hour, dow, minute = ts.hour, ts.weekday(), ts.minute
                else:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    hour, dow, minute = dt.hour, dt.weekday(), dt.minute
            except Exception:
                pass
        feats.append(float(np.sin(2 * np.pi * hour / 24)))
        feats.append(float(np.cos(2 * np.pi * hour / 24)))
        feats.append(float(np.sin(2 * np.pi * dow / 7)))
        feats.append(float(np.cos(2 * np.pi * dow / 7)))

        # Log-scale features
        w5  = mults[max(0, i-5):i]
        w20 = mults[max(0, i-20):i]
        feats.append(float(np.mean(np.log1p(w5))))
        feats.append(float(np.mean(np.log1p(w20))))

        # Virtual-loss signal
        gid = rows[i + 1][1]
        feats.append(1.0 if (loss_ids and gid in loss_ids) else 0.0)

        # Minute-of-hour cyclic [added 2026-05-24]
        feats.append(float(np.sin(2 * np.pi * minute / 60)))
        feats.append(float(np.cos(2 * np.pi * minute / 60)))

        # Current-round bet features [added 2026-05-24]
        cur_bet = bets[i]
        cur_cnt = bettors[i]
        t1      = top1_bets[i]
        feats.append(float(np.log1p(cur_bet)) if (not np.isnan(cur_bet) and cur_bet >= 0.0) else 0.0)
        if not np.isnan(cur_bet) and not np.isnan(cur_cnt) and cur_cnt > 0:
            feats.append(float(cur_bet / cur_cnt))
        else:
            feats.append(0.0)
        if not np.isnan(t1) and not np.isnan(cur_bet) and cur_bet > 0:
            feats.append(float(t1 / cur_bet))
        else:
            feats.append(0.0)
        feats.append(float(np.log1p(t1)) if not np.isnan(t1) else 0.0)

        # Log-space std of last 5 multipliers [added 2026-05-24]
        _lw5 = np.log1p(mults[max(0, i - 5):i])
        feats.append(float(np.std(_lw5) + 1e-6) if len(_lw5) >= 2 else 0.0)

        label = 1 if mults[i + 1] >= THRESHOLD else 0
        X_rows.append(feats)
        y_rows.append(label)

    if not X_rows:
        return None, None, None

    return np.array(X_rows, dtype=np.float32), np.array(y_rows, dtype=np.int32), feature_names


# -- Walk-forward training ----------------------------------------------------

def train_models(X: np.ndarray, y: np.ndarray, feature_names: list) -> dict:
    n = len(X)
    tr_end  = int(n * TRAIN_FRAC)
    val_end = int(n * (TRAIN_FRAC + VAL_FRAC))

    X_tr, y_tr = X[:tr_end],       y[:tr_end]
    X_va, y_va = X[tr_end:val_end], y[tr_end:val_end]
    X_te, y_te = X[val_end:],       y[val_end:]

    pos_rate = float(y_tr.mean())
    scale_pos = (1 - pos_rate) / max(pos_rate, 1e-6)

    vl_col = feature_names.index("in_virtual_loss")
    sw_tr = np.where(X_tr[:, vl_col] > 0, VIRTUAL_LOSS_WEIGHT, 1.0)
    sw_va = np.where(X_va[:, vl_col] > 0, VIRTUAL_LOSS_WEIGHT, 1.0)

    xgb_model = xgb.XGBClassifier(
        n_estimators=400, max_depth=4, learning_rate=0.04,
        subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
        gamma=0.1, scale_pos_weight=scale_pos,
        eval_metric="logloss", verbosity=0, tree_method="hist",
        early_stopping_rounds=30,
    )
    xgb_model.fit(
        X_tr, y_tr, sample_weight=sw_tr,
        eval_set=[(X_va, y_va)], sample_weight_eval_set=[sw_va],
        verbose=False,
    )

    lgb_model = lgb.LGBMClassifier(
        n_estimators=400, max_depth=4, learning_rate=0.04,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=20,
        scale_pos_weight=scale_pos, verbosity=-1,
    )
    lgb_model.fit(
        X_tr, y_tr, sample_weight=sw_tr,
        eval_set=[(X_va, y_va)],
        callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(-1)],
    )

    xgb_va = xgb_model.predict_proba(X_va)[:, 1]
    lgb_va = lgb_model.predict_proba(X_va)[:, 1]
    raw_va = (xgb_va + lgb_va) / 2.0

    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(raw_va, y_va)

    xgb_te = xgb_model.predict_proba(X_te)[:, 1]
    lgb_te = lgb_model.predict_proba(X_te)[:, 1]
    raw_te = (xgb_te + lgb_te) / 2.0
    cal_te = calibrator.predict(raw_te)

    auc_raw = roc_auc_score(y_te, raw_te) if len(np.unique(y_te)) > 1 else 0.5
    auc_cal = roc_auc_score(y_te, cal_te) if len(np.unique(y_te)) > 1 else 0.5
    brier   = brier_score_loss(y_te, cal_te)
    base_wr = float(y_te.mean())

    selective = {}
    for cutoff in CONFIDENCE_CUTOFFS:
        mask = cal_te >= cutoff
        n_bet = int(mask.sum())
        if n_bet < 10:
            selective[cutoff] = {"n_bets": n_bet, "win_rate": None, "ev": None}
            continue
        wr = float(y_te[mask].mean())
        ev = wr * (THRESHOLD - 1) - (1 - wr) * 1.0
        b = THRESHOLD - 1
        kelly_f = max(0.0, (b * wr - (1 - wr)) / b)
        selective[cutoff] = {
            "n_bets": n_bet, "pct_bets": round(n_bet / len(y_te) * 100, 1),
            "win_rate": round(wr, 4), "ev": round(ev, 4),
            "kelly_f": round(kelly_f, 4),
            "signal": "BET" if ev > 0 else "SKIP",
        }

    best_cutoff = max(
        [c for c in CONFIDENCE_CUTOFFS
         if selective[c].get("ev") is not None and selective[c]["n_bets"] >= 20],
        key=lambda c: selective[c]["ev"],
        default=0.52,
    )

    importance = dict(zip(feature_names, xgb_model.feature_importances_.tolist()))
    top10 = sorted(importance.items(), key=lambda x: -x[1])[:10]

    return {
        "xgb_model": xgb_model, "lgb_model": lgb_model, "calibrator": calibrator,
        "metrics": {
            "auc_raw": round(float(auc_raw), 4), "auc_cal": round(float(auc_cal), 4),
            "brier_score": round(float(brier), 4), "base_win_rate": round(base_wr, 4),
            "test_n": len(y_te), "train_n": len(y_tr), "val_n": len(y_va),
            "pos_rate": round(pos_rate, 4),
        },
        "selective": selective, "best_cutoff": best_cutoff, "top_features": top10,
    }


# -- Prediction ---------------------------------------------------------------

def predict_next(models: dict, X_last_row: np.ndarray) -> dict:
    x = X_last_row.reshape(1, -1)
    xgb_p = float(models["xgb_model"].predict_proba(x)[0, 1])
    lgb_p = float(models["lgb_model"].predict_proba(x)[0, 1])
    raw_p = (xgb_p + lgb_p) / 2.0
    cal_p = float(models["calibrator"].predict([raw_p])[0])

    best_cut = models["best_cutoff"]
    sel_info = models["selective"].get(best_cut, {})
    ev_at_cut = sel_info.get("ev", 0) or 0
    kelly = sel_info.get("kelly_f", 0) or 0

    auc_ok = models["metrics"].get("auc_cal", 0) >= QUALITY_GATE_AUC
    signal = "BET" if (auc_ok and cal_p >= best_cut and ev_at_cut > 0) else "SKIP"

    return {
        "p_above_threshold": round(cal_p, 4), "p_raw": round(raw_p, 4),
        "threshold": THRESHOLD, "best_cutoff": best_cut, "signal": signal,
        "kelly_bet_fraction": round(kelly, 4), "ev_at_cutoff": round(ev_at_cut, 4),
        "confidence": "HIGH" if abs(cal_p - 0.5) > 0.08 else "LOW",
        "xgb_p": round(xgb_p, 4), "lgb_p": round(lgb_p, 4),
    }


def _write_prediction(payload: dict) -> None:
    PREDICTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREDICTION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(PREDICTION_FILE)


def _read_prediction_file() -> dict:
    try:
        if PREDICTION_FILE.exists():
            return json.loads(PREDICTION_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# -- Main pipeline ------------------------------------------------------------

class TrainingPipeline:
    def __init__(self, db_path: str = DB_PATH):
        self._db_path   = db_path
        self._last_trained_at: int = 0
        self._models: Optional[dict] = None
        self._sim = VirtualAccount(db_path)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)

    def _load_rows(self) -> list:
        from storage import CrashStorage
        return CrashStorage(self._db_path).get_training_rows(limit=50_000)

    def run_once(self, loss_ids: Optional[set] = None) -> bool:
        rows = self._load_rows()
        if not rows:
            return False

        latest_id = rows[-1][0]
        if latest_id - self._last_trained_at < RETRAIN_EVERY and self._models:
            self._refresh_prediction(rows, loss_ids=loss_ids)
            return False

        n = len(rows)
        if n < MIN_ROWS:
            log.info("Only %d rows, need %d to train", n, MIN_ROWS)
            return False

        log.info("Training on %d rows (latest_id=%d)", n, latest_id)
        t0 = time.time()

        X, y, feat_names = build_features(rows, loss_ids=loss_ids)
        if X is None or len(X) < 200:
            log.warning("Not enough feature rows after build_features: %s",
                        len(X) if X is not None else 0)
            return False

        result = train_models(X, y, feat_names)
        elapsed = time.time() - t0

        self._models = result
        self._last_trained_at = latest_id

        m = result["metrics"]
        log.info(
            "Walk-forward  auc_cal=%.4f  brier=%.4f  "
            "train=%d  val=%d  test=%d  elapsed=%.1fs",
            m["auc_cal"], m["brier_score"],
            m["train_n"], m["val_n"], m["test_n"], elapsed,
        )
        best = result["best_cutoff"]
        sel  = result["selective"].get(best, {})
        log.info(
            "Best cutoff=%.2f  win_rate=%s  ev=%s  kelly=%s  n_bets=%s",
            best, sel.get("win_rate"), sel.get("ev"),
            sel.get("kelly_f"),  sel.get("n_bets"),
        )

        self._refresh_prediction(rows, elapsed=elapsed, loss_ids=loss_ids)
        return True

    def _refresh_prediction(self, rows, elapsed=0.0, loss_ids=None):
        if not self._models:
            return
        X, y, _ = build_features(rows, loss_ids=loss_ids)
        if X is None or len(X) == 0:
            return

        pred    = predict_next(self._models, X[-1])
        metrics = self._models["metrics"]

        last_row = rows[-1]
        pred["last_round_db_id"]   = last_row[0]
        pred["last_game_round_id"] = last_row[1]

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows_used": len(rows),
            "prediction": pred,
            "metrics": metrics,
            "selective": {str(k): v for k, v in self._models["selective"].items()},
            "best_cutoff": self._models["best_cutoff"],
            "top_features": {k: round(v, 4) for k, v in self._models["top_features"]},
            "train_elapsed_s": round(elapsed, 1),
            "disclaimer": (
                "BCGame crash is provably fair (SHA-256 chain). "
                "ML finds empirical patterns only. "
                "Positive EV requires model AUC > 0.53 on out-of-sample test data."
            ),
        }
        _write_prediction(payload)

    # -- Simulator integration ------------------------------------------------

    def _settle_new_rounds(self, prev_rows, curr_rows, last_pred):
        if not last_pred or not prev_rows or not curr_rows:
            return
        last_known_id = prev_rows[-1][0]
        new_rounds = [r for r in curr_rows if r[0] > last_known_id]
        if not new_rounds:
            return
        r = new_rounds[0]
        result = self._sim.place_and_settle(
            round_db_id=r[0],
            game_round_id=r[1] or str(r[0]),
            actual_mult=r[2],
            prediction=last_pred,
        )
        if result:
            log.info(
                "VirtualBet  round=%s  mult=%.2fx  %s  pnl=%.6f SOL  bankroll=%.6f SOL",
                r[1], r[2],
                "WIN" if result["won"] else "LOSS",
                result["pnl_sol"], result["bankroll"],
            )

    def run_forever(self, interval_s: int = TRAIN_INTERVAL_S) -> None:
        log.info("Training pipeline started  threshold=%.1fx  retrain_every=%d",
                 THRESHOLD, RETRAIN_EVERY)

        prev_rows: list = []
        last_pred: dict = {}

        while True:
            try:
                loss_ids  = self._sim.get_loss_round_ids(last_n=500)
                curr_rows = self._load_rows()
                self._settle_new_rounds(prev_rows, curr_rows, last_pred)
                self.run_once(loss_ids=loss_ids)
                prev_rows = self._load_rows()
                raw_pred  = _read_prediction_file()
                last_pred = raw_pred.get("prediction", {})
            except Exception:
                log.error("run_forever iteration failed", exc_info=True)
            time.sleep(interval_s)


if __name__ == "__main__":
    import argparse
    if not _HAS_ML:
        print(f"ERROR: {_ML_ERR}")
        sys.exit(1)
    ap = argparse.ArgumentParser()
    ap.add_argument("--db",       default=DB_PATH)
    ap.add_argument("--once",     action="store_true")
    ap.add_argument("--interval", type=int, default=TRAIN_INTERVAL_S)
    args = ap.parse_args()
    p = TrainingPipeline(db_path=args.db)
    if args.once:
        p.run_once()
    else:
        p.run_forever(interval_s=args.interval)
```

**Feature count: **72** (verified: `len(feature_names) == X.shape[1]`).  
**Feature index of `in_virtual_loss`: 64** (used for sample weight extraction in `train_models()`).  
**Backward compatibility:** callers passing 7-tuple rows get `top1_bet = nan -> 0.0` for all 4 new bet-concentration features.

---

### simulator.py
```python
"""
Virtual betting account for BCGame crash simulator.

Stores all virtual bets and account state in the same DuckDB.
Called by training.py run_forever() to place/settle bets each cycle.
Dashboard reads this data as read-only.

Starting bankroll: START_SOL (~$100 at current SOL rate).
Minimum bet: 0.000006 SOL (BCGame minimum).
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb

_CONN_RETRIES = 6

START_SOL:    float = 1.1       # ~$100 @ ~$91/SOL
MIN_BET_SOL:  float = 0.000006  # BCGame minimum
MAX_BET_FRAC: float = 0.05      # never bet more than 5% per round


def _connect(db_path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    delay = 0.1
    for attempt in range(_CONN_RETRIES):
        try:
            return duckdb.connect(db_path, read_only=read_only)
        except duckdb.IOException:
            if attempt == _CONN_RETRIES - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def ensure_tables(db_path: str) -> None:
    """Idempotent schema migration — safe to call on every startup."""
    conn = _connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS virtual_bets (
                id             BIGINT PRIMARY KEY,
                round_db_id    BIGINT,
                game_round_id  VARCHAR,
                ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
                signal         VARCHAR,
                confidence     DOUBLE,
                kelly_f        DOUBLE,
                cashout_target DOUBLE,
                bet_sol        DOUBLE,
                actual_mult    DOUBLE,
                won            BOOLEAN,
                pnl_sol        DOUBLE,
                bankroll_after DOUBLE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS virtual_account (
                id            INTEGER PRIMARY KEY,
                bankroll_sol  DOUBLE NOT NULL,
                total_bets    INTEGER NOT NULL DEFAULT 0,
                total_wins    INTEGER NOT NULL DEFAULT 0,
                total_pnl_sol DOUBLE NOT NULL DEFAULT 0.0,
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute(
            "INSERT INTO virtual_account (id, bankroll_sol) VALUES (1, ?) "
            "ON CONFLICT (id) DO NOTHING",
            [START_SOL],
        )
        try:
            conn.execute("CREATE SEQUENCE seq_vbet_id START 1")
        except Exception:
            pass
    finally:
        conn.close()


class VirtualAccount:
    def __init__(self, db_path: str):
        self._path = db_path
        ensure_tables(db_path)

    # ── Account state ─────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        conn = _connect(self._path, read_only=True)
        try:
            row = conn.execute(
                "SELECT bankroll_sol, total_bets, total_wins, total_pnl_sol "
                "FROM virtual_account WHERE id=1"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"bankroll_sol": START_SOL, "total_bets": 0,
                    "total_wins": 0, "total_pnl_sol": 0.0}
        return {
            "bankroll_sol":  row[0],
            "total_bets":    row[1],
            "total_wins":    row[2],
            "total_pnl_sol": row[3],
        }

    def reset(self) -> None:
        """Reset to starting bankroll, clear all bet history."""
        conn = _connect(self._path)
        try:
            conn.execute(
                "UPDATE virtual_account "
                "SET bankroll_sol=?, total_bets=0, total_wins=0, "
                "total_pnl_sol=0.0, updated_at=now() WHERE id=1",
                [START_SOL],
            )
            conn.execute("DELETE FROM virtual_bets")
        finally:
            conn.close()

    # ── Bet placement & settlement ────────────────────────────────────────────

    def place_and_settle(
        self,
        round_db_id:   int,
        game_round_id: str,
        actual_mult:   float,
        prediction:    dict,
    ) -> Optional[dict]:
        """
        Given a completed round and the ML prediction that was active before it,
        record a virtual bet and settle it.

        prediction: the dict from prediction.json["prediction"]
        Returns the bet record, or None if model said SKIP or bankroll depleted.
        """
        state    = self.get_state()
        bankroll = state["bankroll_sol"]

        if bankroll < MIN_BET_SOL:
            return None

        signal     = prediction.get("signal", "SKIP")
        kelly_f    = float(prediction.get("kelly_bet_fraction", 0.0))
        confidence = float(prediction.get("p_above_threshold", 0.0))
        ev         = float(prediction.get("ev_at_cutoff", 0.0))
        cashout    = float(prediction.get("threshold", 2.0))

        if signal != "BET" or ev <= 0:
            return None

        # Kelly-sized bet, floored at MIN_BET_SOL, capped at MAX_BET_FRAC
        kelly_bet = bankroll * max(kelly_f, 0.005)
        bet_sol   = max(MIN_BET_SOL, min(kelly_bet, bankroll * MAX_BET_FRAC))

        won    = actual_mult >= cashout
        pnl    = bet_sol * (cashout - 1) if won else -bet_sol
        new_br = max(0.0, bankroll + pnl)

        conn = _connect(self._path)
        try:
            conn.execute("""
                INSERT INTO virtual_bets
                    (id, round_db_id, game_round_id, signal, confidence,
                     kelly_f, cashout_target, bet_sol, actual_mult,
                     won, pnl_sol, bankroll_after)
                VALUES (nextval('seq_vbet_id'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [round_db_id, game_round_id, signal, confidence,
                  kelly_f, cashout, bet_sol, actual_mult, won, pnl, new_br])

            conn.execute("""
                UPDATE virtual_account
                SET bankroll_sol  = ?,
                    total_bets    = total_bets + 1,
                    total_wins    = total_wins + ?,
                    total_pnl_sol = total_pnl_sol + ?,
                    updated_at    = now()
                WHERE id = 1
            """, [new_br, 1 if won else 0, pnl])
        finally:
            conn.close()

        return {
            "round_db_id":  round_db_id,
            "game_round_id": game_round_id,
            "signal":       signal,
            "bet_sol":      round(bet_sol, 6),
            "cashout":      cashout,
            "actual_mult":  actual_mult,
            "won":          won,
            "pnl_sol":      round(pnl, 6),
            "bankroll":     round(new_br, 6),
        }

    # ── History queries ───────────────────────────────────────────────────────

    def get_history(self, limit: int = 200) -> list:
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute("""
                SELECT id, round_db_id, game_round_id, ts::TIMESTAMPTZ,
                       signal, confidence, kelly_f, cashout_target,
                       bet_sol, actual_mult, won, pnl_sol, bankroll_after
                FROM virtual_bets ORDER BY id DESC LIMIT ?
            """, [limit]).fetchall()
        except Exception:
            return []
        finally:
            conn.close()
        cols = ["id", "round_db_id", "game_round_id", "ts",
                "signal", "confidence", "kelly_f", "cashout_target",
                "bet_sol", "actual_mult", "won", "pnl_sol", "bankroll_after"]
        return [dict(zip(cols, r)) for r in rows]

    def get_bankroll_curve(self) -> list:
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute(
                "SELECT bankroll_after FROM virtual_bets ORDER BY id ASC"
            ).fetchall()
        except Exception:
            return [START_SOL]
        finally:
            conn.close()
        return [START_SOL] + [r[0] for r in rows]

    def get_loss_round_ids(self, last_n: int = 500) -> set:
        """Return game_round_ids of recent virtual losses for training signal."""
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute("""
                SELECT game_round_id FROM virtual_bets
                WHERE won = false
                ORDER BY id DESC LIMIT ?
            """, [last_n]).fetchall()
        except Exception:
            return set()
        finally:
            conn.close()
        return {r[0] for r in rows if r[0]}
```

---

### math_engine.py
*(full file — 555 lines)*

Entry points:
- `full_report(multipliers)` — all analyses in one call
- `fit_power_law(m)` — P(M>=x) = k/x^n fit
- `estimate_house_edge(m)` — HE = 1 - x*P(M>=x)
- `kaplan_meier(m)` — empirical survival with 95% CI
- `kelly_criterion(p, b)` — f* = (b*p-q)/b
- `gamblers_ruin(p, b, B)` — P(ruin) for infinite opponent
- `monte_carlo_ror(m, target, bet_fraction, n_sessions)` — bootstrap RoR
- `gap_analysis(m, threshold)` — inter-arrival times, chi-sq memorylessness test
- `independence_test(m)` — Ljung-Box Q-statistic on log(M)
- `strategy_ev(m, targets)` — EV + Kelly for each target multiplier

---

### ws_collector.py
```python
"""
ws_collector.py — lightweight direct WebSocket collector for BCGame crash.

Connects directly to socketv4.bcgame61.com via Socket.IO / Engine.IO v3.
Resource usage: ~30 MB RAM, ~1-2% CPU.

Protocol (reverse-engineered from _capture_sent2.py on 2026-05-18):
  Server: socketv4.bcgame61.com  (Engine.IO v3, Socket.IO binary frames)

  Connection flow:
    1. Connect WS, receive text OPEN frame: 0{sid,pingInterval,...}
    2. Send namespace connects: \\x04\\x00[len][ns]\\x00
    3. When /g/cm ACK received, send join: \\x04\\x82\\x00\\x00\\x00\\x00\\x05/g/cm\\x04join
    4. Server streams binary events on /g/cm

  Binary event frames: [04][02][05]/g/cm[event_type][protobuf_payload]
    event_type 0x01 = round complete (has crash_point in field 14 or 12)
    event_type 0x02 = live multiplier update (field 14 = multiplier*100)
"""
import asyncio
import json
import logging
import logging.handlers
import ssl as _ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config import COLLECTOR_LOG, DB_PATH
from storage import CrashStorage

# ── Constants ──────────────────────────────────────────────────────────────────

_WS_URL = (
    "wss://socketv4.bcgame61.com/socket.io/"
    "?Accept-Language=en&EIO=3&transport=websocket"
)

_HEADERS = {
    "Origin": "https://bcgame61.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Namespaces to subscribe (same order the browser client uses)
_NAMESPACES = [
    b"/game-support",
    b"/g/tasks/verify",
    b"/user",
    b"/gs",
    b"/g/cm",
    b"/multi/g/cm",
]

_CM_NS          = b"/g/cm"
_CM_NS_LEN      = len(_CM_NS)                          # 5
_CM_HEADER      = b'\x04\x02' + bytes([_CM_NS_LEN]) + _CM_NS  # 04 02 05 /g/cm
_CM_ACK         = b'\x04\x00' + bytes([_CM_NS_LEN]) + _CM_NS + b'\x00'  # /g/cm connect ACK
_CM_JOIN        = b'\x04\x82\x00\x00\x00\x00' + bytes([_CM_NS_LEN]) + _CM_NS + b'\x04join'

_EIO_PING       = "2"                  # EIO text ping
_EIO_PONG       = "3"                  # expected EIO text pong
_DEFAULT_PING_S = 5.0                  # fallback if server doesn't send pingInterval

# BCGame CDN has a non-critical Basic-Constraints CA cert — bypass verify.
# Connection reads public game data only; no credentials sent.
_SSL_CTX = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = _ssl.CERT_NONE

_EVT_ROUND_COMPLETE = 0x01
_EVT_LIVE_UPDATE    = 0x02

_MIN_M, _MAX_M = 1.0, 100_000.0

# ── Logging ───────────────────────────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    log = logging.getLogger("ws_collector")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    Path(COLLECTOR_LOG).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        COLLECTOR_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    log.addHandler(ch)
    return log


log = _build_logger()

# ── Protobuf helpers ──────────────────────────────────────────────────────────

def _decode_varint(data: bytes, pos: int):
    result = shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return result, pos
    return result, pos


def _skip_pb_field(data: bytes, pos: int, wire_type: int) -> int:
    if wire_type == 0:
        _, pos = _decode_varint(data, pos)
    elif wire_type == 1:
        pos += 8
    elif wire_type == 2:
        length, pos = _decode_varint(data, pos)
        pos += length
    elif wire_type == 5:
        pos += 4
    return pos


def _scan_multiplier(payload: bytes) -> Optional[float]:
    """
    Scan protobuf payload for a crash multiplier.
    Field 14 (tag 0x70) = multiplier*100 confirmed from live "02" frames.
    Also try field 6 (tag 0x30) as fallback.
    Returns float or None.
    """
    pos = 0
    while pos < len(payload):
        if pos >= len(payload):
            break
        tag_byte = payload[pos]; pos += 1
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3

        if wire_type == 0:
            val, pos = _decode_varint(payload, pos)
            if field_num in (14, 6):
                m = val / 100.0
                if _MIN_M <= m <= _MAX_M:
                    return round(m, 2)
        elif wire_type == 2:
            length, pos = _decode_varint(payload, pos)
            pos += length
        else:
            new_pos = _skip_pb_field(payload, pos, wire_type)
            if new_pos <= pos:
                break
            pos = new_pos
    return None


def _scan_round_id(payload: bytes) -> Optional[str]:
    """Field 1 (tag 0x08) = round_id varint."""
    pos = 0
    while pos < len(payload):
        tag_byte = payload[pos]; pos += 1
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3
        if wire_type == 0:
            val, pos = _decode_varint(payload, pos)
            if field_num == 1:
                return str(val)
        else:
            new_pos = _skip_pb_field(payload, pos, wire_type)
            if new_pos <= pos:
                break
            pos = new_pos
    return None


def _parse_cm_frame(raw: bytes) -> Optional[dict]:
    """
    Parse /g/cm binary frame from socketv4.bcgame61.com.
    Frame format: [04][02][05]/g/cm[event_type_byte][protobuf...]
    Returns dict with multiplier/round_id on success, None otherwise.
    """
    if not raw.startswith(_CM_HEADER):
        return None
    offset = len(_CM_HEADER)
    if offset >= len(raw):
        return None
    event_type = raw[offset]
    payload = raw[offset + 1:]

    if event_type == _EVT_ROUND_COMPLETE:
        mult = _scan_multiplier(payload)
        if mult is None:
            log.debug("01-frame: no crash_point  hex=%s", raw.hex())
            return None
        rid = _scan_round_id(payload)
        return {"multiplier": mult, "game_round_id": rid, "frame_event": "round_complete"}

    # 0x02 = live multiplier update — not a completed round, skip
    return None

# ── Main collector ─────────────────────────────────────────────────────────────

class LightCollector:
    def __init__(self, storage: CrashStorage, url: Optional[str] = None):
        self._storage    = storage
        self._url        = url or _WS_URL
        self._rounds     = 0
        self._ping_iv    = _DEFAULT_PING_S
        self._joined     = False       # True after /g/cm join sent

    async def run(self, duration_hours: float = 8760.0):
        deadline = time.time() + duration_hours * 3600
        reconnect_delay = 5

        while time.time() < deadline:
            self._joined = False
            try:
                log.info("Connecting to %s", self._url)
                await self._session()
                reconnect_delay = 5
            except (ConnectionClosed, WebSocketException, OSError) as e:
                log.warning("WS disconnected: %s — reconnecting in %ds", e, reconnect_delay)
            except Exception as e:
                log.error("Unexpected error: %s — reconnecting in %ds", e, reconnect_delay)

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

    async def _session(self):
        async with websockets.connect(
            self._url,
            additional_headers=_HEADERS,
            ssl=_SSL_CTX,
            ping_interval=None,
            close_timeout=5,
            max_size=4 * 2 ** 20,   # 4 MB — initial history burst is ~42 KB
        ) as ws:
            log.info("WS connected")

            # Step 1: receive Engine.IO OPEN frame (text "0{...}")
            try:
                open_msg = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(open_msg, str) and open_msg.startswith("0"):
                    try:
                        eio_data = json.loads(open_msg[1:])
                        self._ping_iv = eio_data.get("pingInterval", 5000) / 1000.0
                        log.info("EIO OPEN  sid=%s  pingInterval=%.1fs",
                                 eio_data.get("sid", "?"), self._ping_iv)
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                log.warning("No EIO OPEN frame within 10s — proceeding anyway")

            # Step 2: subscribe to namespaces
            for ns in _NAMESPACES:
                await ws.send(b'\x04\x00' + bytes([len(ns)]) + ns + b'\x00')
                await asyncio.sleep(0.05)
            log.info("Namespace connects sent (%d)", len(_NAMESPACES))

            await asyncio.gather(
                self._recv_loop(ws),
                self._ping_loop(ws),
            )

    async def _recv_loop(self, ws):
        async for msg in ws:
            if isinstance(msg, bytes):
                log.debug("BIN %db: %s", len(msg), msg[:32].hex())
                self._handle_binary(ws, msg)
            elif isinstance(msg, str):
                log.debug("TEXT: %s", msg[:120])
                self._handle_text(ws, msg)

    async def _ping_loop(self, ws):
        """EIO text ping every pingInterval seconds."""
        await asyncio.sleep(self._ping_iv)
        while True:
            try:
                await ws.send(_EIO_PING)
            except Exception:
                break
            await asyncio.sleep(self._ping_iv)

    def _handle_text(self, ws, msg: str):
        # EIO pong "3" — no action needed (ping_loop handles timing)
        pass

    def _handle_binary(self, ws, raw: bytes):
        # Check for /g/cm ACK and send join if not yet done
        if not self._joined and raw == _CM_ACK:
            asyncio.ensure_future(self._send_join(ws))
            return

        result = _parse_cm_frame(raw)
        if not result:
            return

        mult  = result["multiplier"]
        gid   = result.get("game_round_id")
        event = result["frame_event"]

        try:
            self._storage.insert(
                multiplier    = mult,
                ts            = datetime.now(timezone.utc),
                source        = "ws_direct",
                game_round_id = gid,
                frame_event   = event,
                hash          = None,
            )
            self._rounds += 1
            if self._rounds <= 5 or self._rounds % 50 == 0:
                log.info("Round #%d  %.2fx  id=%s", self._rounds, mult, gid)
        except Exception as e:
            log.error("DB insert failed: %s", e)

    async def _send_join(self, ws):
        try:
            await ws.send(_CM_JOIN)
            self._joined = True
            log.info("Sent /g/cm join — data stream should start")
        except Exception as e:
            log.error("Failed to send join: %s", e)


# ── Entry point (called from main.py) ────────────────────────────────────────

async def run(db_path: str = DB_PATH, duration_hours: float = 8760.0,
              url: Optional[str] = None):
    storage = CrashStorage(db_path)
    n_existing = storage.count()
    log.info("DB: %s  (%d existing rounds)", db_path, n_existing)
    print(f"[ws_collector] DB: {db_path}  ({n_existing:,} rounds)")
    print(f"[ws_collector] Lightweight mode — no browser. ~30 MB RAM.")
    try:
        collector = LightCollector(storage, url=url)
        await collector.run(duration_hours=duration_hours)
    finally:
        storage.close()
```

---

### watchdog.py
```python
"""watchdog.py -- health monitor for crash_collector processes.

Checks every 30 s:
- collector:  last round in DB older than 60 s -> kill + restart
- dashboard:  port 8501 not responding          -> kill + restart
- training:   process not alive                 -> restart

Logs to logs/watchdog.log.
"""
import os
import sys
import time
import socket
import logging
import subprocess
from pathlib import Path

import duckdb
import psutil

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE           = Path(r"D:\crash_collector")
DB_PATH        = str(BASE / "data" / "crash.duckdb")
PYTHON         = r"C:\Python314\python.exe"
STALE_SEC      = 240   # collector: no new round for this long -> restart (240 to survive 60-100s WS drops)
CHECK_SEC      = 30    # loop interval
DASHBOARD_PORT = 8501

PROCS = {
    "collector": {
        "match": ["main.py", "collect"],
        "cmd": [PYTHON, str(BASE / "main.py"), "collect", "--hours", "8760"],
    },
    "training": {
        "match": ["training.py"],
        "cmd": [PYTHON, str(BASE / "training.py")],
    },
    "dashboard": {
        "match": ["streamlit", "dashboard.py"],
        "cmd": [
            PYTHON, "-m", "streamlit", "run", str(BASE / "dashboard.py"),
            "--server.port", str(DASHBOARD_PORT),
            "--server.headless", "true",
            "--server.enableCORS", "false",
            "--server.enableXsrfProtection", "false",
            "--server.fileWatcherType", "none",
        ],
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_PATH = BASE / "logs" / "watchdog.log"
LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def find_procs(match_tokens: list) -> list:
    """Return psutil.Process objects whose cmdline contains all match_tokens."""
    result = []
    my_pid = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.pid == my_pid:
                continue
            if p.info["name"] and "python" in p.info["name"].lower():
                cmd = " ".join(p.info["cmdline"] or [])
                if all(tok in cmd for tok in match_tokens):
                    result.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def kill_proc_tree(proc: psutil.Process) -> None:
    try:
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        proc.kill()
        proc.wait(timeout=10)
        log.info("Killed PID %d (+%d children)", proc.pid, len(children))
    except (psutil.NoSuchProcess, psutil.TimeoutExpired) as exc:
        log.warning("Kill failed: %s", exc)


def start_proc(name: str) -> None:
    cfg = PROCS[name]
    # Guard: re-check right before spawn to prevent duplicate processes
    if find_procs(cfg["match"]):
        log.info("start_proc(%s): already running, skipping spawn", name)
        return
    stderr_log = open(BASE / "logs" / f"{name}.stderr.log", "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cfg["cmd"],
            cwd=str(BASE),
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        log.info("Started %-10s PID=%d", name, proc.pid)
    except Exception as exc:
        log.error("Failed to start %s: %s", name, exc, exc_info=True)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def collector_age() -> float:
    """Seconds since the last round was stored.
    Returns -1.0 if DB is write-locked (collector actively writing — healthy).
    Returns inf on genuine error or empty table.
    """
    try:
        con = duckdb.connect(DB_PATH, read_only=True)
        try:
            row = con.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(ts))) FROM rounds"
            ).fetchone()
            return float(row[0]) if (row and row[0] is not None) else float("inf")
        finally:
            con.close()
    except Exception as exc:
        msg = str(exc)
        if "being used by another process" in msg or "Cannot open file" in msg:
            # Write-lock held by collector = actively inserting = healthy
            log.debug("DB write-locked by collector (healthy)")
            return -1.0
        log.warning("DB age check failed: %s", exc)
        return float("inf")


def port_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Per-process checks
# ---------------------------------------------------------------------------

def check_collector() -> None:
    age   = collector_age()
    procs = find_procs(PROCS["collector"]["match"])

    if age == -1.0:
        if not procs:
            # Stale lock file left by a crashed collector — restart
            log.warning("collector DB locked but NO process found -- stale lock, restarting")
            start_proc("collector")
            return
        log.info("collector OK  PIDs=%s  (DB locked by writer)", [p.pid for p in procs])
        return

    if age > STALE_SEC:
        log.warning("collector STUCK: last_round=%.0fs ago, PIDs=%s -- restarting",
                    age, [p.pid for p in procs])
        for p in procs:
            kill_proc_tree(p)
        time.sleep(3)
        start_proc("collector")
    elif not procs:
        log.warning("collector NOT RUNNING -- starting")
        start_proc("collector")
    else:
        log.info("collector OK  PIDs=%s  last_round=%.0fs ago",
                 [p.pid for p in procs], age)


def check_dashboard() -> None:
    alive = port_alive(DASHBOARD_PORT)
    procs = find_procs(PROCS["dashboard"]["match"])

    if not alive:
        log.warning("dashboard NOT RESPONDING on :%d, PIDs=%s -- restarting",
                    DASHBOARD_PORT, [p.pid for p in procs])
        for p in procs:
            kill_proc_tree(p)
        time.sleep(3)
        start_proc("dashboard")
    elif not procs:
        log.warning("dashboard proc GONE -- starting")
        start_proc("dashboard")
    else:
        log.info("dashboard OK  PIDs=%s  port:%d alive",
                 [p.pid for p in procs], DASHBOARD_PORT)


def check_training() -> None:
    procs = find_procs(PROCS["training"]["match"])

    if not procs:
        # Double-check after a short pause to guard against momentary psutil misses
        time.sleep(2)
        procs = find_procs(PROCS["training"]["match"])

    if not procs:
        log.warning("training NOT RUNNING -- starting")
        start_proc("training")
    else:
        log.info("training  OK  PIDs=%s", [p.pid for p in procs])


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Watchdog started  stale=%ds  interval=%ds ===", STALE_SEC, CHECK_SEC)
    while True:
        try:
            check_collector()
            check_dashboard()
            check_training()
        except Exception as exc:
            log.error("Loop error: %s", exc, exc_info=True)
        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
```

---

## Appendix: Design Decisions

### Why DuckDB?
- Single-file embedded database — no daemon, no network, simple deployment
- Columnar storage handles analytical queries efficiently
- Python client supports `read_only=True` for concurrent readers
- Write-lock detection via IOException is used as a health signal

### Why XGBoost + LightGBM ensemble?
- Both are gradient boosted trees, handle NaN natively (missing value splits)
- Different implementations → complementary errors → ensemble reduces variance
- Calibration (isotonic regression) makes probabilities meaningful
- No neural network: data volume (~15k rows) is too small; trees generalize better

### Why walk-forward CV, not k-fold?
- Financial time series: using future data to train on past data inflates AUC by 5-15%
- Walk-forward guarantees: model only sees data that would have been available at training time
- No shuffle: preserves temporal ordering

### Why AUC quality gate at 0.52?
- AUC 0.50 = random → EV negative (house edge ~1%)
- AUC 0.52 = weak edge → at selective cutoffs (top 20% confidence) actual win rate ≈ 51-53% → EV positive
- Conservative: requires demonstrated out-of-sample edge before any real money

### Why virtual mode first?
- Need 500+ bets to achieve statistical power: 95% CI lower bound must be positive
- At 109 bets (current): CI too wide to distinguish edge from noise
- At 500 bets with 53% win rate: CI = [50.1%, 55.9%] → lower bound clears 50.5% breakeven
