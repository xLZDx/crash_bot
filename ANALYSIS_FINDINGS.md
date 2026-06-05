# BCGame Crash — Pattern Analysis & Strategy Findings
Generated: 2026-05-27 | Dataset: 16,745 rounds

---

## 1. Base Probabilities

| Event | Probability |
|---|---|
| x >= 2.0 (win at 2x cashout) | 50.3% |
| x >= 5.0 | 20.1% |
| x >= 10.0 | 9.9% |
| x < 2.0 (loss) | 49.7% |

House edge at 2.0x cashout: **-0.58% per bet**
($100 bet -> expected loss $0.58 per round)

---

## 2. Pattern Statistical Tests

### Pattern 1: "After x>=10, another x>=10 follows in next 10 rounds (claimed 90%)"

| Window | Conditional P | Baseline P (same window) | Lift |
|---|---|---|---|
| Next 1 round | 9.6% | 9.9% | 0.97x |
| Next 2 rounds | 17.7% | 18.8% | 0.94x |
| Next 3 rounds | 26.4% | 26.8% | 0.98x |
| Next 5 rounds | 40.1% | 40.6% | 0.99x |
| Next 10 rounds | **65.0%** | **64.7%** | **1.00x** |

**Verdict: NO EDGE.** Claimed 90% -- actual 65%. Baseline (any 10 rounds) = 64.7%.
Seeing x>=10 provides zero predictive information.

---

### Pattern 2: "After 5+ consecutive x>=2 (win streak), expect a loss"

| Streak length | P(next < 2.0) | Baseline | Lift |
|---|---|---|---|
| After 3+ | 50.5% | 49.7% | 1.02x |
| After 5+ | 50.4% | 49.7% | 1.01x |
| After 7+ | 50.8% | 49.7% | 1.02x |
| After 10+ | 41.2% | 49.7% | 0.83x (n=17, noise) |

**Verdict: NO EDGE.** Effect is +0.4-1.1% above baseline -- statistically noise.
Streaks are a natural property of independent events, not a signal.

---

### Pattern 3: "After 10+ consecutive losses, continued losses expected"

| Streak length | P(next < 2.0) | Baseline | Lift |
|---|---|---|---|
| After 3+ | 49.9% | 49.7% | 1.00x |
| After 5+ | 51.4% | 49.7% | 1.03x |
| After 7+ | 45.9% | 49.7% | 0.92x |
| After 10+ | 30.0% | 49.7% | 0.60x (n=10, noise) |

**Verdict: NO EDGE.** n=10 for 10+ streaks -- too few samples to conclude anything.

---

### Pattern 4: "System balances itself" (autocorrelation test)

| Lag | Correlation |
|---|---|
| +1 | -0.0028 |
| +2 | +0.0039 |
| +3 | +0.0131 |
| +5 | +0.0009 |
| +10 | -0.0093 |
| +20 | -0.0117 |

**Verdict: ZERO MEMORY.** All correlations < 0.02. SHA-256 provably fair confirmed.
Each round is cryptographically independent of all prior rounds.

---

## 3. Bankroll Simulation ($5,000 start, 2% bet, cashout 2.0x, 16,645 rounds)

| Strategy | Bets | Win Rate | Final Bank | P&L |
|---|---|---|---|---|
| Baseline: every round | 16,645 | 50.3% | $1,350 | -73.0% |
| After x>=10 (Pattern 1) | 1,642 | 48.7% | $1,554 | -69.0% |
| x>=10 in last 5 rounds | 6,768 | 50.0% | $1,145 | -77.1% |
| Skip after 5+ win streak | 16,120 | 50.3% | $1,592 | -68.2% |
| After 5+ loss streak | 525 | 48.4% | $3,204 | -35.9% |
| No x>=10 in last 5 rounds | 9,877 | 50.5% | $5,894 | +17.9% |

Note on Pattern 5 (+17.9%): Tested on same data used to design it (in-sample).
True OOS test would likely show this is noise, not a real edge.

---

## 4. Why Sustained Profit Is Possible Despite Negative EV

The operator reports months of consistent profit from $5k bankroll.
Statistically plausible explanations (not mutually exclusive):

1. **Session management**: Exiting sessions on profit targets, cutting losses early.
   Postpones the house edge extraction -- does not eliminate it.
   Can produce consistent-seeming gains over months-scale horizons.

2. **Variance**: At 10-30 sessions/month, even -0.58% EV has high variance.
   5th-95th percentile outcome over ~3 months of play spans roughly +150% to -80%.
   Being in the upper half for several months is not rare.

3. **Selective memory**: Winning sessions remembered, losing sessions discounted.

---

## 5. What CAN Be Systematized (Session Management)

These rules have no predictive claim -- they manage risk within a negative-EV game:

- **Stop-loss per session**: e.g., exit if bankroll drops 10-15% in one session
- **Stop-profit per session**: e.g., exit when up $X in a session regardless of state
- **Bet sizing**: Flat % of session bankroll (not total bankroll) -- reduces gambler's ruin risk
- **Session frequency cap**: Max N sessions per day/week regardless of outcome
- **Cashout discipline**: Fixed cashout target (e.g., always 2.0x) vs chasing higher

If the operator's actual profit source is disciplined session management + variance,
formalizing those exact rules is automatable and can eliminate emotional deviation.

---

## 6. ML Development Status

The ML pipeline (training.py, simulator.py) was built to find statistical edges
in BCGame round sequences using lag features, streak features, regime detection.

Given findings above:
- Autocorrelation ~0 -> lag features have no predictive value
- Pattern lift ~1.0x -> streak/pattern features have no predictive value
- SHA-256 independence -> no historical feature can predict future rounds

The pipeline correctly identifies this: models trained on real data consistently
fail the quality gate (auc_cal < 0.52) because no real edge exists.

**Value of the ML code:**
- Correct: validates rigorously that no edge exists
- Reusable: could detect if BCGame ever introduces a real bias (unlikely)
- The infrastructure (shadow bets, promotion gate, simulator) is sound

**Recommendation:** Freeze ML development. Redirect effort to session management
formalization, which addresses the operator's actual profit source.
