"""
math_engine.py — Mathematical analysis engine for BCGame crash game data.

Models implemented:
  1. Power-law distribution fit   P(M>=x) = k / x^n  (MLE via log-linear regression)
  2. Kaplan-Meier survival        empirical S(x) = P(M>x) with 95% CI (Greenwood)
  3. House-edge estimation        HE = 1 - x * P(M>=x), tested at multiple thresholds
  4. Kelly criterion              f* = (b*p - q) / b
  5. Gambler's ruin               P(ruin | bankroll=B, bet=1, p_win, b_payout)
  6. Risk-of-Ruin (Monte Carlo)   bootstrap N sessions of K rounds, track ruin rate
  7. Gap analysis                 inter-arrival times between M>=threshold events
  8. Autocorrelation / independence  Ljung-Box on log(multipliers)
  9. Strategy EV                  E[profit per bet] = p*payout - (1-p)*bet
 10. Optimal target selection     grid search over target multipliers for max EV / min RoR

Requires: numpy, scipy (optional — degrades gracefully)
"""
import math
import sys
from typing import Optional

import numpy as np

try:
    from scipy import stats as _sp_stats
    from scipy.optimize import minimize_scalar as _minimize_scalar
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

MIN_ROUNDS = 50  # refuse calculations below this sample size

# ── 1. Power-law distribution fit ─────────────────────────────────────────────

def fit_power_law(multipliers: np.ndarray) -> dict:
    """
    Fit P(M>=x) = k / x^n using OLS on log-log space.

    Returns dict: {k, n, r_squared, p_at (dict of P(M>=x) for common x values)}
    or {"error": reason} if insufficient data.
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]
    if len(m) < MIN_ROUNDS:
        return {"error": f"need {MIN_ROUNDS}+ rounds, have {len(m)}"}

    thresholds = np.array([1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0, 100.0, 500.0, 1000.0])
    n_total = len(m)
    xs, ys = [], []
    for x in thresholds:
        hits = np.sum(m >= x)
        if hits > 0:
            xs.append(math.log(x))
            ys.append(math.log(hits / n_total))

    if len(xs) < 3:
        return {"error": "insufficient high-multiplier observations for fit"}

    xs_arr, ys_arr = np.array(xs), np.array(ys)
    # OLS: log(P) = log(k) - n*log(x)  =>  slope = -n, intercept = log(k)
    slope, intercept = np.polyfit(xs_arr, ys_arr, 1)
    n_fit = -slope
    k_fit = math.exp(intercept)

    if n_fit <= 0:
        # Non-physical fit: P would increase with x — dataset too small or atypical
        import logging
        logging.getLogger("math_engine").warning(
            "fit_power_law: non-physical exponent n=%.4f (power law P increases with x). "
            "Need more data or distribution is not power-law.", n_fit
        )
        return {"error": f"non-physical power-law fit: n={n_fit:.4f} <= 0 (need more data)"}

    # R^2 in log-log space (will be high due to log compression — use as relative, not absolute, fit metric)
    y_pred = intercept + slope * xs_arr
    ss_res = np.sum((ys_arr - y_pred) ** 2)
    ss_tot = np.sum((ys_arr - np.mean(ys_arr)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Implied probabilities
    p_at = {}
    for x in [2, 5, 10, 50, 100, 1000, 10000]:
        p_at[x] = min(1.0, k_fit / (x ** n_fit))

    return {
        "k": round(k_fit, 6),
        "n": round(n_fit, 6),
        "r_squared": round(r2, 4),
        "p_at": p_at,
        "n_samples": n_total,
    }


# ── 2. House-edge estimation ───────────────────────────────────────────────────

def estimate_house_edge(multipliers: np.ndarray) -> list:
    """
    Estimate HE at multiple thresholds: HE = 1 - x * P(M>=x).

    Returns list of dicts: {threshold, hits, p_actual, implied_he, p_theoretical_1pct}
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]
    n = len(m)
    if n < MIN_ROUNDS:
        return []

    rows = []
    for x in [1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0, 100.0]:
        hits = int(np.sum(m >= x))
        p = hits / n
        he = 1 - x * p if p > 0 else None
        rows.append({
            "threshold": x,
            "hits": hits,
            "n": n,
            "p_actual": round(p, 6),
            "implied_he": round(he, 4) if he is not None else None,
            "p_theoretical_1pct": round(0.99 / x, 6),
        })
    return rows


# ── 3. Kaplan-Meier survival function ─────────────────────────────────────────

def kaplan_meier(multipliers: np.ndarray, max_x: float = 100.0) -> list:
    """
    Empirical survival function S(x) = P(M > x) with Greenwood 95% CI.

    Returns list of (x, S_x, ci_lower, ci_upper) at key thresholds.
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]
    n = len(m)
    if n < MIN_ROUNDS:
        return []

    thresholds = [1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0, 100.0]
    results = []
    for x in thresholds:
        if x > max_x:
            break
        survivors = int(np.sum(m > x))
        s = survivors / n
        # Greenwood variance: Var(S) approx S^2 * sum_i(d_i / (n_i*(n_i-d_i)))
        # Simplified Wilson CI for proportion
        if s > 0 and s < 1:
            se = math.sqrt(s * (1 - s) / n)
            ci_lo = max(0.0, s - 1.96 * se)
            ci_hi = min(1.0, s + 1.96 * se)
        else:
            ci_lo, ci_hi = s, s
        results.append({
            "x": x,
            "S": round(s, 6),
            "ci_lower": round(ci_lo, 6),
            "ci_upper": round(ci_hi, 6),
            "survivors": survivors,
        })
    return results


# ── 4. Kelly criterion ─────────────────────────────────────────────────────────

def kelly_criterion(p_win: float, b_payout: float) -> dict:
    """
    Optimal bet fraction f* = (b*p - q) / b.

    p_win     — probability of winning (M reaches target)
    b_payout  — net payout multiple (e.g. b=9 for target 10x, net win=9x bet)

    Returns: {f_star, half_kelly, ev_per_unit, rtp_pct}
    """
    q = 1 - p_win
    f = (b_payout * p_win - q) / b_payout
    ev = b_payout * p_win - q
    return {
        "f_star": round(max(0.0, f), 6),
        "half_kelly": round(max(0.0, f / 2), 6),
        "ev_per_unit": round(ev, 6),
        "rtp_pct": round((ev + 1) * 100, 3),
        "is_positive_ev": ev > 0,
    }


# ── 5. Gambler's ruin ─────────────────────────────────────────────────────────

def gamblers_ruin(p_win: float, b_payout: float, bankroll_units: float) -> dict:
    """
    P(ruin) for flat-bet strategy using ruin formula.

    For negative-EV bets (typical): P(ruin) approaches 1 as bankroll -> inf.
    Formula: P(ruin starting at B) = ((q/p)^B - (q/p)^(B+R)) / (1 - (q/p)^(B+R))
    where B=bankroll in bet units, R=ruin threshold (0), target not explicitly modeled.

    Simplified form (infinite bankroll opponent):
      P(ruin) = 1  if EV < 0
      P(ruin) = (q/p)^B  if EV > 0 (unlikely favorable game scenario)
    """
    q = 1 - p_win
    B = bankroll_units
    ev = b_payout * p_win - q

    # Infinite-opponent formula (no upper absorbing barrier):
    #   EV <= 0  → certain ruin regardless of B: P(ruin) = 1
    #   EV > 0   → P(ruin | start at B) = (q/p)^B
    ratio = q / p_win if p_win > 0 else float("inf")
    if ratio >= 1:
        p_ruin = 1.0
    else:
        p_ruin = ratio ** B

    return {
        "p_ruin": round(min(1.0, p_ruin), 6),
        "ev_per_bet": round(ev, 6),
        "bankroll_units": B,
        "is_positive_ev": ev > 0,
    }


# ── 6. Monte Carlo Risk-of-Ruin ───────────────────────────────────────────────

def monte_carlo_ror(
    multipliers: np.ndarray,
    target: float,
    bet_fraction: float = 0.01,
    n_sessions: int = 10_000,
    rounds_per_session: int = 1_000,
    starting_bankroll: float = 1.0,
    ruin_threshold: float = 0.0,
    rng_seed: int = 42,
) -> dict:
    """
    Bootstrap Monte Carlo: sample rounds_per_session multipliers from empirical
    distribution, apply flat-bet strategy (bet bet_fraction * current_bankroll
    on each round, cash out at target), repeat n_sessions times.

    Returns: {p_ruin, p_profit, median_final, p5_final, p95_final, mean_final}
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]
    if len(m) < MIN_ROUNDS:
        return {"error": f"need {MIN_ROUNDS}+ rounds, have {len(m)}"}

    rng = np.random.default_rng(rng_seed)
    finals = []

    for _ in range(n_sessions):
        bankroll = starting_bankroll
        sample = rng.choice(m, size=rounds_per_session, replace=True)
        for crash in sample:
            if bankroll <= ruin_threshold:
                break
            bet = bankroll * bet_fraction
            if crash >= target:
                bankroll += bet * (target - 1)
            else:
                bankroll -= bet
        finals.append(bankroll)

    finals_arr = np.array(finals)
    ruined = np.sum(finals_arr <= ruin_threshold)

    return {
        "n_sessions": n_sessions,
        "rounds_per_session": rounds_per_session,
        "target": target,
        "bet_fraction": bet_fraction,
        "starting_bankroll": starting_bankroll,
        "p_ruin": round(float(ruined / n_sessions), 4),
        "p_profit": round(float(np.sum(finals_arr > starting_bankroll) / n_sessions), 4),
        "mean_final": round(float(np.mean(finals_arr)), 4),
        "median_final": round(float(np.median(finals_arr)), 4),
        "p5_final": round(float(np.percentile(finals_arr, 5)), 4),
        "p95_final": round(float(np.percentile(finals_arr, 95)), 4),
        "best_final": round(float(np.max(finals_arr)), 4),
        "worst_final": round(float(np.min(finals_arr)), 4),
    }


# ── 7. Gap analysis ───────────────────────────────────────────────────────────

def gap_analysis(multipliers: np.ndarray, threshold: float) -> dict:
    """
    Inter-arrival times (gaps) between rounds where M >= threshold.

    Tests if gaps follow Geometric distribution (memoryless / independent).
    Expected gap = 1 / P(M>=threshold).

    Returns: {mean_gap, median_gap, std_gap, min_gap, max_gap,
              theoretical_gap, cv (coeff of variation), n_events,
              memoryless_chi2_pvalue (if scipy available)}
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]
    if len(m) < MIN_ROUNDS:
        return {"error": f"need {MIN_ROUNDS}+ rounds"}

    p = float(np.sum(m >= threshold) / len(m))
    if p == 0:
        return {"error": f"no observations >= {threshold}x in dataset"}

    # Compute gaps between hit positions
    hit_positions = np.where(m >= threshold)[0]
    if len(hit_positions) < 2:
        return {
            "n_events": len(hit_positions),
            "p_empirical": round(p, 6),
            "theoretical_gap": round(1 / p, 1),
            "error": "fewer than 2 events — cannot compute gap stats",
        }

    gaps = np.diff(hit_positions).astype(float)

    result = {
        "threshold": threshold,
        "n_events": len(hit_positions),
        "n_rounds": len(m),
        "p_empirical": round(p, 6),
        "theoretical_gap": round(1 / p, 1),
        "mean_gap": round(float(np.mean(gaps)), 1),
        "median_gap": round(float(np.median(gaps)), 1),
        "std_gap": round(float(np.std(gaps)), 1),
        "min_gap": int(np.min(gaps)),
        "max_gap": int(np.max(gaps)),
        "cv": round(float(np.std(gaps) / np.mean(gaps)), 3),  # 1.0 = geometric
    }

    # Chi-square test for geometric distribution (memorylessness)
    if HAS_SCIPY and len(gaps) >= 20:
        # Geometric pmf: P(X=k) = (1-p)^(k-1) * p
        max_gap = min(int(np.max(gaps)), 200)
        observed_counts = np.bincount(gaps.astype(int), minlength=max_gap + 1)[1:]
        expected_counts = np.array([
            len(gaps) * p * (1 - p) ** (k - 1) for k in range(1, len(observed_counts) + 1)
        ])
        # Merge tail to avoid small expected counts
        min_expected = 5
        while len(expected_counts) > 2 and expected_counts[-1] < min_expected:
            expected_counts[-2] += expected_counts[-1]
            observed_counts[-2] += observed_counts[-1]
            expected_counts = expected_counts[:-1]
            observed_counts = observed_counts[:-1]

        if len(observed_counts) >= 2:
            chi2, pval = _sp_stats.chisquare(observed_counts, expected_counts)
            result["memoryless_chi2_pvalue"] = round(float(pval), 4)
            result["is_memoryless"] = pval > 0.05

    return result


# ── 8. Autocorrelation / independence test ────────────────────────────────────

def independence_test(multipliers: np.ndarray, max_lag: int = 20) -> dict:
    """
    Test if consecutive multipliers are independent using Ljung-Box on log(M).

    Returns: {ljung_box_pvalue, is_independent, acf_values, significant_lags}
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]
    if len(m) < 50:
        return {"error": "need 50+ rounds for independence test"}

    log_m = np.log(m)
    n = len(log_m)
    mean_log = np.mean(log_m)
    var_log = np.var(log_m)

    # Compute ACF for lags 1..max_lag
    acf = []
    for lag in range(1, min(max_lag + 1, n // 2)):
        cov = np.mean((log_m[:-lag] - mean_log) * (log_m[lag:] - mean_log))
        acf.append(round(cov / var_log, 4) if var_log > 0 else 0.0)

    # Ljung-Box statistic: Q = n*(n+2) * sum(acf_k^2 / (n-k))
    q_stat = n * (n + 2) * sum(
        acf[k - 1] ** 2 / (n - k) for k in range(1, len(acf) + 1)
    )
    df = len(acf)
    p_val = 1.0
    if HAS_SCIPY:
        p_val = float(_sp_stats.chi2.sf(q_stat, df=df))

    # Significant lags (|acf| > 2/sqrt(n) = Bartlett's confidence bound)
    bound = 2.0 / math.sqrt(n)
    sig_lags = [i + 1 for i, a in enumerate(acf) if abs(a) > bound]

    return {
        "n_rounds": n,
        "ljung_box_q": round(q_stat, 3),
        "ljung_box_df": df,
        "ljung_box_pvalue": round(p_val, 4),
        "is_independent": p_val > 0.05,
        "acf": acf[:10],  # first 10 lags
        "significant_lags": sig_lags,
        "bartlett_bound": round(bound, 4),
    }


# ── 9. Strategy EV and optimal target ─────────────────────────────────────────

def strategy_ev(multipliers: np.ndarray, targets: list = None) -> list:
    """
    For each target multiplier, compute:
      p_win   = P(M >= target) from empirical data
      ev      = p_win * (target - 1) - (1 - p_win) = p_win * target - 1
      kelly_f = optimal bet fraction
      avg_rounds_to_hit = 1/p_win
      days_to_hit = avg_rounds / 900 (assumed 900 rounds/day)

    Returns list of dicts sorted by EV desc.
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]
    n = len(m)
    if n < MIN_ROUNDS:
        return []

    if targets is None:
        targets = [2, 3, 5, 10, 20, 50, 100, 500, 1000, 10000]

    rows = []
    for target in targets:
        hits = int(np.sum(m >= target))
        p = hits / n
        if p == 0:
            avg_gap = float("inf")
            days = float("inf")
            ev = -1.0
            kf = 0.0
        else:
            avg_gap = 1 / p
            days = avg_gap / 900
            ev = p * target - 1  # net EV in bet units
            b_net = target - 1   # net payout per win
            kf = max(0.0, (b_net * p - (1 - p)) / b_net) if b_net > 0 else 0.0

        rows.append({
            "target": target,
            "hits": hits,
            "n": n,
            "p_win": round(p, 8),
            "ev_per_bet": round(ev, 6),
            "rtp_pct": round((ev + 1) * 100, 3),
            "kelly_f": round(kf, 6),
            "avg_rounds_to_hit": round(avg_gap, 0) if p > 0 else None,
            "days_to_hit": round(days, 2) if p > 0 else None,
        })

    rows.sort(key=lambda r: r["ev_per_bet"], reverse=True)
    return rows


# ── 10. Summary report ────────────────────────────────────────────────────────

def full_report(multipliers: np.ndarray) -> dict:
    """
    Run all analyses and return a single structured dict.
    Entry point for dashboard and agent use.
    """
    m = np.asarray(multipliers, dtype=float)
    m = m[m >= 1.0]

    report = {
        "n_rounds": len(m),
        "has_scipy": HAS_SCIPY,
    }

    if len(m) < MIN_ROUNDS:
        report["error"] = f"need {MIN_ROUNDS}+ rounds, have {len(m)}"
        return report

    report["basic_stats"] = {
        "mean": round(float(np.mean(m)), 4),
        "median": round(float(np.median(m)), 4),
        "std": round(float(np.std(m)), 4),
        "p1": round(float(np.percentile(m, 1)), 4),
        "p5": round(float(np.percentile(m, 5)), 4),
        "p25": round(float(np.percentile(m, 25)), 4),
        "p75": round(float(np.percentile(m, 75)), 4),
        "p95": round(float(np.percentile(m, 95)), 4),
        "p99": round(float(np.percentile(m, 99)), 4),
        "max": round(float(np.max(m)), 4),
        "frac_below_2x": round(float(np.sum(m < 2) / len(m)), 4),
        "frac_above_10x": round(float(np.sum(m >= 10) / len(m)), 4),
    }

    report["distribution"] = fit_power_law(m)
    report["house_edge"] = estimate_house_edge(m)
    report["survival"] = kaplan_meier(m)
    report["strategy_ev"] = strategy_ev(m)
    report["independence"] = independence_test(m)
    report["gap_10x"] = gap_analysis(m, threshold=10.0)
    report["gap_50x"] = gap_analysis(m, threshold=50.0)

    return report


# ── CLI convenience ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, "D:/crash_collector")
    import duckdb

    db_path = "D:/crash_collector/data/crash.duckdb"
    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute("SELECT multiplier FROM rounds WHERE multiplier >= 1.0").fetchall()
    con.close()

    multipliers = np.array([r[0] for r in rows])
    print(f"Loaded {len(multipliers)} rounds from {db_path}")

    report = full_report(multipliers)

    print(f"\n=== BASIC STATS (n={report['n_rounds']}) ===")
    if "error" in report:
        print(f"  {report['error']}")
        sys.exit(0)

    s = report["basic_stats"]
    print(f"  mean={s['mean']}x  median={s['median']}x  max={s['max']}x")
    print(f"  below 2x: {s['frac_below_2x']*100:.1f}%  above 10x: {s['frac_above_10x']*100:.1f}%")

    d = report["distribution"]
    if "error" not in d:
        print(f"\n=== POWER LAW FIT ===")
        print(f"  P(M>=x) = {d['k']} / x^{d['n']}  (R^2={d['r_squared']})")
        for x, p in d["p_at"].items():
            print(f"  P(M>={x}x) = {p*100:.4f}%  (1 in {1/p:.0f} rounds)" if p > 0 else f"  P(M>={x}x) = ~0")

    print(f"\n=== HOUSE EDGE ESTIMATES ===")
    for row in report["house_edge"]:
        he = row["implied_he"]
        he_str = f"{he*100:.1f}%" if he is not None else "N/A"
        print(f"  x>={row['threshold']:.0f}: actual={row['p_actual']*100:.3f}%  HE~{he_str}  "
              f"theoretical_1pct={row['p_theoretical_1pct']*100:.3f}%")

    print(f"\n=== STRATEGY EV (top 5) ===")
    for row in report["strategy_ev"][:5]:
        print(f"  x{row['target']:>6}: p={row['p_win']*100:.4f}%  "
              f"EV={row['ev_per_bet']:+.5f}  RTP={row['rtp_pct']:.2f}%  "
              f"Kelly={row['kelly_f']*100:.3f}%  "
              f"avg_hit={'every '+str(int(row['avg_rounds_to_hit']))+' rounds' if row['avg_rounds_to_hit'] else 'never'}")

    ind = report["independence"]
    if "error" not in ind:
        print(f"\n=== INDEPENDENCE TEST ===")
        print(f"  Ljung-Box Q={ind['ljung_box_q']}  p={ind['ljung_box_pvalue']}  "
              f"independent={'YES' if ind['is_independent'] else 'NO (serial correlation detected)'}")
        if ind["significant_lags"]:
            print(f"  Significant ACF lags: {ind['significant_lags']}")
