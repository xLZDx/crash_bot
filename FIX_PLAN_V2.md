# BCGame Crash ML System - Fix Plan v2

Status: NEEDS REVISION for v1; v2 accepted as the working remediation plan.

Reviewers:
- ml-engineer
- database-reviewer
- security-reviewer
- silent-failure-hunter / python-reviewer
- planner / pr-test-analyzer

External reports compared:
- Operator review
- Real-code pass against `D:\crash_collector`

## Core Contract

The remediation work must enforce this contract before any readiness or promotion claim:

```text
one prediction -> exactly one next round -> at most one idempotent settlement -> persistent audit trail
```

`SHADOW_READY` is an observation-readiness governance state only. It must never mean live execution, auto-promotion, or permission to place real-money bets.

## Confirmed Blockers

- Old metrics, virtual P&L, and readiness labels must not be used for promotion. This needs machine-enforced invalidation, not only a banner.
- `in_virtual_loss` and `VIRTUAL_LOSS_WEIGHT` must be removed from the feature path, sample weights, top features, model metadata, and prediction payloads.
- Training and inference feature builders must be split to remove the current off-by-one prediction alignment bug.
- `prediction.json` must become a strict envelope with schema/version/binding/TTL validation and a fail-closed parser.
- Virtual settlement must be atomic and idempotent, with a unique key over prediction/settled round.
- Cutoff selection must not use the test set. Test is report-only; promotion comes from a forward/shadow window.
- A `shadow_candidate` / `would_bet` mode is required, otherwise `signal=SKIP` prevents collecting post-fix virtual evidence.
- Dashboard mutating actions and network exposure must be fail-closed.
- DuckDB writer-lock policy must be explicit because collector, training/simulator, and dashboard reset can all write.

## PR0 - Freeze And Characterization

1. Set current system status to `MANUAL_REVIEW` or `DEFERRED_DEPENDENCY`.
2. Prevent old data from producing `SHADOW_READY`, `profitable`, `validated`, or similar readiness labels.
3. Mark pre-fix virtual/prediction artifacts as `valid_for_promotion=false`, or introduce a protocol/version cutoff that excludes them.
4. Add a baseline report script with:
   - rows count
   - bets coverage percent
   - virtual bets count
   - prediction age
   - current AUC/EV snapshot
5. Make test execution deterministic on Windows, for example:

```powershell
python -m pytest -q --basetemp D:\crash_collector\data\pytest-tmp
```

## PR1 - Tests Before Fixes

Add failing or characterization tests before changing core logic:

1. No-future-access test: features for index `i` must not change when `rows[i + 1]` changes, except for the label.
2. Inference alignment test: inference features must use the latest completed row `n - 1`, not training `X[-1]` for `n - 2`.
3. Prediction binding tests:
   - stale JSON
   - corrupt JSON
   - wrong schema
   - wrong feature source round
   - TTL expired
   - all fail closed with no settlement
4. Settlement tests:
   - duplicate settle is no-op
   - crash between insert/update rolls back
   - gap of 2+ rounds is audited and invalidated
5. Cutoff protocol test: validation chooses cutoff; test cannot affect selection or gate.

## PR2 - DB Contract And Settlement

1. Add schema/protocol identity fields:
   - `feature_schema_version`
   - `validation_protocol_version`
   - `prediction_id`
   - `model_id`
2. Add settlement binding fields to virtual records:
   - `prediction_id`
   - `feature_source_round_db_id`
   - `settled_round_db_id`
   - `valid_for_promotion`
   - `invalid_reason`
3. Add a unique/idempotency constraint or an equivalent table rebuild strategy.
4. Wrap virtual bet insert plus account update in an explicit transaction.
5. Add invariant checks:
   - `virtual_account.total_bets == COUNT(virtual_bets)`
   - wins match aggregate wins
   - PnL matches aggregate PnL
   - bankroll chain is continuous
   - no duplicate settled round
   - no settlement without valid binding
6. Dashboard must show `DEGRADED` on invariant mismatch.
7. Block dashboard `Reset virtual account` by default behind a local operator flag.

## PR3 - ML Causal Fix

1. Remove `in_virtual_loss` and all virtual-loss sample weighting.
2. Implement explicit feature builders:

```text
build_training_features(rows)
  - indices: i = max_lag .. n - 2
  - label: i + 1

build_inference_features(rows)
  - index: i = n - 1
  - no label
  - no future-row access
```

3. `_refresh_prediction()` must write a prediction envelope containing:
   - `prediction_id`
   - `created_at`
   - `schema_version`
   - `feature_schema_hash`
   - `feature_source_round_db_id`
   - `last_game_round_id`
   - `intended_next_round_after_db_id`
4. Add `shadow_candidate` / `would_bet` logging so candidate bets can be evaluated even while operator/live signal remains `SKIP`.

## PR4 - Validation Protocol

1. Temporal split remains required:
   - train: fit models
   - validation: calibration and cutoff selection
   - test: report only
2. Remove ambiguous rules like "test does not contradict" unless fully predeclared.
3. Add Wilson or bootstrap lower-bound EV gate with fixed assumptions:
   - minimum candidate bet count
   - payout/house edge formula
   - fees/slippage assumptions where relevant
   - confidence level
4. Add calibration metrics:
   - Brier
   - ECE
   - reliability bins
5. Add leakage/null diagnostics:
   - permutation or block-shuffle checks
   - baseline model diagnostics
6. Promotion decisions must come only from a post-fix forward/shadow window, not from historical test results.

## PR5 - Collector And Feature Availability

1. Update documentation and spec:
   - production path is `main.py collect -> playwright_collector.py`
   - `ws_collector.py` is experimental or deprecated
2. Either make `ws_collector.py` fail loudly as unsupported, or fix:
   - `ts=` insert bug
   - TLS verification before any trusted use
3. Add a feature coverage gate before training:
   - if bets/top1/total_bets coverage is below threshold, exclude bet/whale features from the manifest
4. Store feature manifest and feature hash in:
   - model artifact
   - prediction payload
5. Add replay tests for:
   - Playwright frame parse/store
   - duplicate rounds
   - hash update after prior round row
   - bet frames if/when used

## PR6 - Watchdog, Dashboard, Security

1. Dashboard must either:
   - bind explicitly to `127.0.0.1`, or
   - require CORS/XSRF/auth/TLS/reverse proxy for remote access
2. Add startup tests enforcing safe dashboard exposure.
3. Start/Stop training and Reset must be deny-by-default unless an explicit local operator flag is set.
4. Watchdog heartbeat must distinguish:
   - process alive
   - WebSocket frames seen
   - last successful DB commit
   - last successful training/prediction write
5. DuckDB lock is healthy only with bounded grace plus a fresh heartbeat.
6. Live adapter invariant:
   - absent or disabled by default
   - unknown/degraded/stale/fallback state always means `SKIP`
   - `SHADOW_READY` never enables real orders
7. Price feed should return typed status:
   - `LIVE`
   - `FRESH_CACHE`
   - `STALE_CACHE`
   - `UNAVAILABLE`
8. Stale or hardcoded fallback price must never increase stake.
9. Add privacy cleanup:
   - redact or retention-limit usernames
   - raw WS logs off by default
   - no raw identifiers in exports unless explicitly enabled

## Promotion Gate

Only after PR0 through PR6:

1. At least 500 post-fix valid `shadow_candidate` bets.
2. No invalid intervals counted.
3. No stale prediction windows.
4. No schema mismatch windows.
5. No duplicate settlement.
6. No DB-degraded window during the evaluation interval.
7. Bet feature coverage must match the training manifest.
8. Lower-bound EV gate must pass on the forward window.
9. Calibration must not be degraded.
10. Governance may show at most `SHADOW_READY`.

`SHADOW_READY` means observation readiness only. It is not live, promoted, canary, or active execution.

## Recommended First Step

Start with PR0 and PR1.

They are small, reduce risk immediately, and create tripwires before changing training, settlement, or dashboard behavior.
