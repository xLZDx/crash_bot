"""
Strategy optimizer for BCGame crash.
Evaluates Flat / Martingale / Anti-Martingale across a parameter grid.
All amounts in SOL. Returns results sorted by Sharpe ratio.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List

import numpy as np

MIN_BET_SOL   = 0.000006
MAX_ROUNDS    = 5_000   # cap for performance inside dashboard


@dataclass
class StrategyResult:
    name:                  str
    base_bet_sol:          float
    cashout:               float
    loss_mult:             float
    win_mult:              float
    start_sol:             float

    final_sol:             float = 0.0
    roi_pct:               float = 0.0
    max_drawdown_pct:      float = 0.0
    win_rate:              float = 0.0
    ev_per_bet:            float = 0.0
    total_bets:            int   = 0
    busts:                 int   = 0
    sharpe:                float = 0.0
    bankroll_curve:        List[float] = field(default_factory=list)
    pnl_per_round:         List[float] = field(default_factory=list)


# ── Sharpe helper ─────────────────────────────────────────────────────────────

def _sharpe(pnl: list) -> float:
    if len(pnl) < 5:
        return 0.0
    arr = np.asarray(pnl, dtype=float)
    std = arr.std()
    return float(arr.mean() / std * math.sqrt(len(arr))) if std > 1e-10 else 0.0


# ── Simulation kernels ────────────────────────────────────────────────────────

def _sim_flat(mults, base_bet, cashout, start) -> StrategyResult:
    bankroll = start
    wins = losses = busts = 0
    curve = [bankroll]
    pnl_list = []
    peak = start
    max_dd = 0.0

    for m in mults:
        if bankroll < MIN_BET_SOL:
            busts += 1
            break
        bet = min(base_bet, bankroll)
        if m >= cashout:
            pnl = bet * (cashout - 1)
            wins += 1
        else:
            pnl = -bet
            losses += 1
        bankroll = max(0.0, bankroll + pnl)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        curve.append(bankroll)
        pnl_list.append(pnl)

    total = wins + losses
    wr = wins / total if total else 0.0
    return StrategyResult(
        name=f"Flat {cashout}x base={base_bet:.6f}",
        base_bet_sol=base_bet, cashout=cashout, loss_mult=1.0, win_mult=1.0,
        start_sol=start, final_sol=bankroll,
        roi_pct=(bankroll - start) / start * 100,
        max_drawdown_pct=max_dd * 100,
        win_rate=wr, ev_per_bet=wr * (cashout - 1) - (1 - wr),
        total_bets=total, busts=busts,
        sharpe=_sharpe(pnl_list),
        bankroll_curve=curve, pnl_per_round=pnl_list,
    )


def _sim_martingale(mults, base_bet, cashout, loss_mult, stop_limit, start) -> StrategyResult:
    bankroll = start
    cur_bet  = base_bet
    wins = losses = busts = 0
    curve = [bankroll]
    pnl_list = []
    peak = start
    max_dd = 0.0

    for m in mults:
        if bankroll < MIN_BET_SOL:
            busts += 1
            break
        bet = min(cur_bet, bankroll)
        if m >= cashout:
            pnl = bet * (cashout - 1)
            wins += 1
            cur_bet = base_bet
        else:
            pnl = -bet
            losses += 1
            nxt = cur_bet * loss_mult
            cur_bet = nxt if nxt <= stop_limit and nxt <= bankroll else base_bet

        bankroll = max(0.0, bankroll + pnl)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        curve.append(bankroll)
        pnl_list.append(pnl)

    total = wins + losses
    wr = wins / total if total else 0.0
    return StrategyResult(
        name=f"Martingale {cashout}x x{loss_mult}",
        base_bet_sol=base_bet, cashout=cashout, loss_mult=loss_mult, win_mult=1.0,
        start_sol=start, final_sol=bankroll,
        roi_pct=(bankroll - start) / start * 100,
        max_drawdown_pct=max_dd * 100,
        win_rate=wr, ev_per_bet=wr * (cashout - 1) - (1 - wr),
        total_bets=total, busts=busts,
        sharpe=_sharpe(pnl_list),
        bankroll_curve=curve, pnl_per_round=pnl_list,
    )


def _sim_anti_martingale(mults, base_bet, cashout, win_mult, stop_limit, start) -> StrategyResult:
    bankroll = start
    cur_bet  = base_bet
    wins = losses = busts = 0
    curve = [bankroll]
    pnl_list = []
    peak = start
    max_dd = 0.0

    for m in mults:
        if bankroll < MIN_BET_SOL:
            busts += 1
            break
        bet = min(cur_bet, bankroll)
        if m >= cashout:
            pnl = bet * (cashout - 1)
            wins += 1
            nxt = cur_bet * win_mult
            cur_bet = nxt if nxt <= stop_limit else base_bet
        else:
            pnl = -bet
            losses += 1
            cur_bet = base_bet

        bankroll = max(0.0, bankroll + pnl)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        curve.append(bankroll)
        pnl_list.append(pnl)

    total = wins + losses
    wr = wins / total if total else 0.0
    return StrategyResult(
        name=f"Anti-Mart {cashout}x x{win_mult}",
        base_bet_sol=base_bet, cashout=cashout, loss_mult=1.0, win_mult=win_mult,
        start_sol=start, final_sol=bankroll,
        roi_pct=(bankroll - start) / start * 100,
        max_drawdown_pct=max_dd * 100,
        win_rate=wr, ev_per_bet=wr * (cashout - 1) - (1 - wr),
        total_bets=total, busts=busts,
        sharpe=_sharpe(pnl_list),
        bankroll_curve=curve, pnl_per_round=pnl_list,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def run_optimizer(
    mults:     list,
    start_sol: float = 1.1,
    sol_usd:   float = 150.0,
) -> list:
    """
    Run full parameter grid. Returns list[StrategyResult] sorted by Sharpe desc.
    Capped at MAX_ROUNDS rounds for performance.
    """
    if not mults:
        return []

    sample = mults[-MAX_ROUNDS:]   # use most recent rounds

    BASE_BETS  = [0.000006, 0.00001, 0.00005]
    CASHOUTS   = [1.5, 2.0, 2.5, 3.0]
    LOSS_MULTS = [1.5, 2.0, 2.5]
    WIN_MULTS  = [1.5, 2.0, 2.5]
    stop_limit = start_sol * 0.10  # stop_if_next_bet > 10% of starting bankroll

    results: list[StrategyResult] = []

    for co in CASHOUTS:
        for bb in BASE_BETS:
            results.append(_sim_flat(sample, bb, co, start_sol))

    for co in CASHOUTS:
        for bb in BASE_BETS:
            for lm in LOSS_MULTS:
                results.append(_sim_martingale(sample, bb, co, lm, stop_limit, start_sol))

    for co in CASHOUTS:
        for bb in BASE_BETS:
            for wm in WIN_MULTS:
                results.append(_sim_anti_martingale(sample, bb, co, wm, stop_limit, start_sol))

    results.sort(key=lambda r: r.sharpe, reverse=True)
    return results
