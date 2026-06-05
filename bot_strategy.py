"""
COMPOUND + backup strategies -- pure logic, no IO, no side effects.
All parameters tuned from 22k-round backtest + grid search optimization.
"""
from dataclasses import dataclass

# -- Starting bankroll (shared across all strategies) --------------------------
TOTAL_BANK_SOL = 100.0

# -- Backward-compat module-level constants (used by existing imports) ---------
SESSION_FRAC    = 0.12
BET_PCT         = 0.00015
CASHOUT         = 2.0
STOP_LOSS       = 0.08
STOP_PROFIT     = 0.25
MAX_ROUNDS      = 200
CONSEC_SL_PAUSE = 3
BET_HARD_CAP    = 0.0005


# -- Strategy config -----------------------------------------------------------

@dataclass
class StrategyConfig:
    name: str
    bet_pct: float
    cashout: float
    stop_loss: float
    stop_profit: float
    session_frac: float
    max_rounds: int       = 200
    consec_sl_pause: int  = 3
    bet_hard_cap: float   = 0.001
    scaling: float        = 1.0   # anti-martingale: multiply bet per win streak
    max_scale: float      = 1.0   # cap on scale factor
    loss_scale: float     = 1.0   # martingale: multiply bet per loss (reset on win)
    sticky_win_reset_after: int = 0  # keep martingale scale until N recovery wins; 0 = reset on first win
    filter: str           = ""    # "no_x10_last5" or "thermal_skip"
    thermal_window: int   = 0
    thermal_threshold: int = 0
    allow_patterns: tuple[str, ...] = ()
    veto_patterns: tuple[str, ...] = ()
    numeric_pattern: str = ""
    numeric_h_range: tuple[float, float] = (10.0, 100.0)
    numeric_l_range: tuple[float, float] = (1.0, 2.0)
    numeric_m_range: tuple[float, float] = (2.0, 10.0)
    numeric_delay: int = 0
    wave_low_len: int = 0
    wave_high_len: int = 0
    wave_low_max_ge2: float = 1.0
    wave_high_min_ge2: float = 0.0
    wave_high_min_ge10: float = 0.0
    wave_delay: int = 1
    rel_base_len: int = 0
    rel_low_len: int = 0
    rel_high_len: int = 0
    rel_low_margin: float = 0.0
    rel_high_margin: float = 0.0
    rel_min_delta: float = 0.0
    rel_high10_mode: str = "none"
    rel_delay: int = 1
    elliott_base_len: int = 0
    elliott_sub_len: int = 0
    elliott_min_amp: float = 0.0
    elliott_wave3_margin: float = 0.0
    elliott_wave4_floor: float = -1.0
    elliott_delay: int = 1
    cold_wave_len: int = 0
    cold_wave_min_lows: int = 0
    cold_wave_min_first_big: int = 0
    cold_wave_min_second_small: int = 0
    cold_wave_max_gap: int = 0
    cold_wave_big_min: float = 10.0
    cold_wave_small_min: float = 2.0
    cold_wave_small_max: float = 10.0
    cold_wave_delay: int = 1
    # cooldown: after N consecutive bet losses pause M rounds + reset scale
    consec_loss_trigger: int = 0  # 0 = disabled
    consec_loss_pause: int   = 0  # rounds to skip (~28.5s each)
    cooldown_resets_scale: bool = True  # False = keep scale after cooldown


STRATEGIES: dict[str, StrategyConfig] = {
    "compound": StrategyConfig(
        name="compound",
        bet_pct=0.00015, cashout=2.0, stop_loss=0.08, stop_profit=0.25,
        session_frac=0.12, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
        consec_loss_trigger=3, consec_loss_pause=4,   # 3 losses -> ~2 min pause
    ),
    "turtle": StrategyConfig(
        name="turtle",
        bet_pct=0.00005, cashout=2.1, stop_loss=0.40, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        consec_loss_trigger=4, consec_loss_pause=11,  # 4 losses -> ~5 min pause
    ),
    "scalper": StrategyConfig(
        name="scalper",
        bet_pct=0.0002, cashout=2.0, stop_loss=0.10, stop_profit=0.20,
        session_frac=0.10, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
        consec_loss_trigger=3, consec_loss_pause=4,   # 3 losses -> ~2 min pause
    ),
    "sniper": StrategyConfig(
        name="sniper",
        bet_pct=0.00015, cashout=3.0, stop_loss=0.15, stop_profit=0.40,
        session_frac=0.10, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
        filter="no_x10_last5",
        consec_loss_trigger=1, consec_loss_pause=42,  # 1 loss -> ~20 min pause
    ),
    "antimartingale": StrategyConfig(
        name="antimartingale",
        bet_pct=0.0001, cashout=2.0, stop_loss=0.15, stop_profit=0.35,
        session_frac=0.10, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
        scaling=1.5, max_scale=8.0,
        consec_loss_trigger=5, consec_loss_pause=17,  # 5 losses -> ~8 min pause
    ),
    "x5": StrategyConfig(
        name="x5",
        bet_pct=0.0002, cashout=5.0, stop_loss=0.15, stop_profit=0.35,
        session_frac=0.10, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
        consec_loss_trigger=5, consec_loss_pause=11,  # 5 losses -> ~5 min pause
    ),
    # x5_opt: brute-force optimised 5x -- best across 90k combos (2026-06-03)
    # WR 21.44% vs break-even 20.00% -> +1.44% edge; consec=6 pause=63r (~30min)
    "x5_opt": StrategyConfig(
        name="x5_opt",
        bet_pct=0.0001, cashout=5.0, stop_loss=0.30, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        consec_loss_trigger=6, consec_loss_pause=63,  # 6 losses -> ~30 min pause + reset
    ),
    "martingale": StrategyConfig(
        name="martingale",
        bet_pct=0.00001, cashout=2.3, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        consec_loss_trigger=4, consec_loss_pause=4,   # 4 losses -> ~2 min pause + scale reset
    ),
    "waveskip": StrategyConfig(
        name="waveskip",
        bet_pct=0.0001, cashout=1.5, stop_loss=0.08, stop_profit=0.20,
        session_frac=0.12, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
        filter="thermal_skip", thermal_window=10, thermal_threshold=-4,
        consec_loss_trigger=3, consec_loss_pause=63,  # 3 losses -> ~30 min pause
    ),
    # waveskip_21: thermal cold skip at 2.1x EV sweet spot + martingale
    "waveskip_21": StrategyConfig(
        name="waveskip_21",
        bet_pct=0.0001, cashout=2.1, stop_loss=0.30, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        filter="thermal_skip", thermal_window=10, thermal_threshold=-4,
        loss_scale=2.0, max_scale=50.0,
        consec_loss_trigger=4, consec_loss_pause=11,
    ),
    # sniper_v2: fixed cashout 2.0x (was 3.0x = COLD 75% of time), keep x10 filter
    "sniper_v2": StrategyConfig(
        name="sniper_v2",
        bet_pct=0.00015, cashout=2.0, stop_loss=0.15, stop_profit=0.40,
        session_frac=0.10, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
        filter="no_x10_last5",
        consec_loss_trigger=2, consec_loss_pause=21,  # opt: +0.07% ROI (~10 min)
    ),
    # turtle_micro: half bet of turtle, same session -- lower risk, still +0.54%
    "turtle_micro": StrategyConfig(
        name="turtle_micro",
        bet_pct=0.000025, cashout=2.1, stop_loss=0.40, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.00025,
        consec_loss_trigger=4, consec_loss_pause=11,  # opt: +0.54% ROI (~5 min)
    ),
    # tide: thermal filter (skip cold) + medium bet + 2.0x cashout
    "tide": StrategyConfig(
        name="tide",
        bet_pct=0.00008, cashout=2.0, stop_loss=0.15, stop_profit=0.35,
        session_frac=0.15, max_rounds=300, consec_sl_pause=3, bet_hard_cap=0.0006,
        filter="thermal_skip", thermal_window=5, thermal_threshold=-2,
        consec_loss_trigger=4, consec_loss_pause=42,  # opt: +0.09% ROI (~20 min)
    ),
    # martingale_21: cashout 2.1x — best ROI in backtest with 4L->5min cooldown
    "martingale_21": StrategyConfig(
        name="martingale_21",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        consec_loss_trigger=4, consec_loss_pause=11,  # 4L -> 5min
    ),
    # martingale_30: cashout 3.0x — highest ROI +1.43% in backtest
    "martingale_30": StrategyConfig(
        name="martingale_30",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        consec_loss_trigger=4, consec_loss_pause=4,   # 4L -> 2min
    ),
    # turbo: aggressive martingale -- cooldown disabled (scale reset breaks recovery math)
    "turbo": StrategyConfig(
        name="turbo",
        bet_pct=0.01, cashout=1.5, stop_loss=0.90, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=2, bet_hard_cap=0.08,
        loss_scale=2.0, max_scale=64.0,
    ),
    # pattern_x3_lhlh: shadow test of user's strongest x3 pattern candidate.
    "pattern_x3_lhlh": StrategyConfig(
        name="pattern_x3_lhlh",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="pattern_allow", allow_patterns=("LHLH",),
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # pattern_21_allow: only allow 2.1x martingale after positive pattern tails.
    "pattern_21_allow": StrategyConfig(
        name="pattern_21_allow",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="pattern_allow", allow_patterns=("LHLH", "HHLL", "HLG", "LGLL", "LHLL"),
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # pattern_21_veto: normal 2.1x martingale, but skip negative pattern tails.
    "pattern_21_veto": StrategyConfig(
        name="pattern_21_veto",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="pattern_veto", veto_patterns=("LHLLLL", "HLLLL", "HHL", "LHHL"),
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # numeric_hll_21_delay2: high then two lows, enter on the 2nd following round.
    "numeric_hll_21_delay2": StrategyConfig(
        name="numeric_hll_21_delay2",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="numeric_pattern", numeric_pattern="HLL",
        numeric_h_range=(10.0, 100.0), numeric_l_range=(1.0, 1.8), numeric_delay=2,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # numeric_hll_30_50_21_delay2: stricter version around the screenshot's 35-40x highs.
    "numeric_hll_30_50_21_delay2": StrategyConfig(
        name="numeric_hll_30_50_21_delay2",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="numeric_pattern", numeric_pattern="HLL",
        numeric_h_range=(30.0, 50.0), numeric_l_range=(1.0, 1.8), numeric_delay=2,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # numeric_hll_30_50_x3_delay2: same strict HLL timing, but test 3.0x in paper.
    "numeric_hll_30_50_x3_delay2": StrategyConfig(
        name="numeric_hll_30_50_x3_delay2",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="numeric_pattern", numeric_pattern="HLL",
        numeric_h_range=(30.0, 50.0), numeric_l_range=(1.0, 1.8), numeric_delay=2,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # wave_l12_h5_21: low-wave -> high-wave regime, strongest robust 2.1x signal.
    "wave_l12_h5_21": StrategyConfig(
        name="wave_l12_h5_21",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="wave_regime",
        wave_low_len=12, wave_high_len=5,
        wave_low_max_ge2=0.20, wave_high_min_ge2=0.60, wave_high_min_ge10=0.10,
        wave_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # wave_l12_h5_21_wide: more frequent, weaker but statistically broader variant.
    "wave_l12_h5_21_wide": StrategyConfig(
        name="wave_l12_h5_21_wide",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="wave_regime",
        wave_low_len=12, wave_high_len=5,
        wave_low_max_ge2=0.30, wave_high_min_ge2=0.60, wave_high_min_ge10=0.0,
        wave_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # wave_l12_h10_x3_shadow: high-upside x3 shadow only; low support in history.
    "wave_l12_h10_x3_shadow": StrategyConfig(
        name="wave_l12_h10_x3_shadow",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="wave_regime",
        wave_low_len=12, wave_high_len=10,
        wave_low_max_ge2=0.20, wave_high_min_ge2=0.80, wave_high_min_ge10=0.10,
        wave_delay=3,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # relwave_l12_h5_21: relative low-wave -> high-wave vs local 120-round baseline.
    "relwave_l12_h5_21": StrategyConfig(
        name="relwave_l12_h5_21",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="relative_wave",
        rel_base_len=120, rel_low_len=12, rel_high_len=5,
        rel_low_margin=0.25, rel_high_margin=0.05, rel_min_delta=0.35,
        rel_high10_mode="none", rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # relwave_l12_h5_21_strict: fewer entries, same structure, stronger high-wave requirement.
    "relwave_l12_h5_21_strict": StrategyConfig(
        name="relwave_l12_h5_21_strict",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="relative_wave",
        rel_base_len=120, rel_low_len=12, rel_high_len=5,
        rel_low_margin=0.25, rel_high_margin=0.10, rel_min_delta=0.35,
        rel_high10_mode="none", rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # relwave_l10_h5_x3: x3 shadow after relative wave with high >=10x not worse than baseline.
    "relwave_l10_h5_x3": StrategyConfig(
        name="relwave_l10_h5_x3",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="relative_wave",
        rel_base_len=120, rel_low_len=10, rel_high_len=5,
        rel_low_margin=0.25, rel_high_margin=0.05, rel_min_delta=0.35,
        rel_high10_mode="baseline", rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # relwave_l15_h7_x3_d3: stricter x3 shadow, enters on the 3rd round after the wave.
    "relwave_l15_h7_x3_d3": StrategyConfig(
        name="relwave_l15_h7_x3_d3",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="relative_wave",
        rel_base_len=120, rel_low_len=15, rel_high_len=7,
        rel_low_margin=0.25, rel_high_margin=0.05, rel_min_delta=0.45,
        rel_high10_mode="baseline", rel_delay=3,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # relwave_bad_veto_21: normal 2.1x martingale, but skip after relative hot->cold flips.
    "relwave_bad_veto_21": StrategyConfig(
        name="relwave_bad_veto_21",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=8.0,
        filter="relative_bad_veto",
        rel_base_len=180, rel_high_len=7, rel_low_len=15,
        rel_high_margin=0.10, rel_low_margin=0.15, rel_min_delta=0.25,
        rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # B: no scale reset on cooldown, cap $0.08 (same as current but scale accumulates)
    "relwave_bad_veto_21_nr8": StrategyConfig(
        name="relwave_bad_veto_21_nr8",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.008,
        loss_scale=2.0, max_scale=8.0, cooldown_resets_scale=False,
        filter="relative_bad_veto",
        rel_base_len=180, rel_high_len=7, rel_low_len=15,
        rel_high_margin=0.10, rel_low_margin=0.15, rel_min_delta=0.25, rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # D: no scale reset, cap $1.28 (scale=128)
    "relwave_bad_veto_21_nr128": StrategyConfig(
        name="relwave_bad_veto_21_nr128",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.128,
        loss_scale=2.0, max_scale=128.0, cooldown_resets_scale=False,
        filter="relative_bad_veto",
        rel_base_len=180, rel_high_len=7, rel_low_len=15,
        rel_high_margin=0.10, rel_low_margin=0.15, rel_min_delta=0.25, rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # E: no scale reset, cap $2.56 (scale=256)
    "relwave_bad_veto_21_nr256": StrategyConfig(
        name="relwave_bad_veto_21_nr256",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.256,
        loss_scale=2.0, max_scale=256.0, cooldown_resets_scale=False,
        filter="relative_bad_veto",
        rel_base_len=180, rel_high_len=7, rel_low_len=15,
        rel_high_margin=0.10, rel_low_margin=0.15, rel_min_delta=0.25, rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # F: no scale reset, cap $5.12 (scale=512)
    "relwave_bad_veto_21_nr512": StrategyConfig(
        name="relwave_bad_veto_21_nr512",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.512,
        loss_scale=2.0, max_scale=512.0, cooldown_resets_scale=False,
        filter="relative_bad_veto",
        rel_base_len=180, rel_high_len=7, rel_low_len=15,
        rel_high_margin=0.10, rel_low_margin=0.15, rel_min_delta=0.25, rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # relwave_bad_veto_21_sticky2: after a loss, keep x2 after the first recovery win;
    # reset to x1 only after the second consecutive recovery win. High-risk shadow.
    "relwave_bad_veto_21_sticky2": StrategyConfig(
        name="relwave_bad_veto_21_sticky2",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0, sticky_win_reset_after=2,
        filter="relative_bad_veto",
        rel_base_len=180, rel_high_len=7, rel_low_len=15,
        rel_high_margin=0.10, rel_low_margin=0.15, rel_min_delta=0.25,
        rel_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # relwave_bad_veto_21_trigger10_x512: high-risk paper stress test.
    # Lets martingale run up to x512 before cooldown; do not use live without more evidence.
    "relwave_bad_veto_21_trigger10_x512": StrategyConfig(
        name="relwave_bad_veto_21_trigger10_x512",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=512.0,
        filter="relative_bad_veto",
        rel_base_len=180, rel_high_len=7, rel_low_len=15,
        rel_high_margin=0.10, rel_low_margin=0.15, rel_min_delta=0.25,
        rel_delay=1,
        consec_loss_trigger=10, consec_loss_pause=4,
    ),
    # cold_breakout_21: user's observed cold wave with breakout inserts.
    # Two 7-round cold segments: each has >=5 lows (<2x); first has a big >=10x
    # breakout, second has small 2-10x breakout inserts. Paper-only until stable.
    "cold_breakout_21": StrategyConfig(
        name="cold_breakout_21",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="cold_breakout_wave",
        cold_wave_len=7, cold_wave_min_lows=5,
        cold_wave_min_first_big=1, cold_wave_min_second_small=2,
        cold_wave_max_gap=8,
        cold_wave_big_min=10.0, cold_wave_small_min=2.0, cold_wave_small_max=10.0,
        cold_wave_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # cold_breakout_x3: same detector, higher payout shadow test.
    "cold_breakout_x3": StrategyConfig(
        name="cold_breakout_x3",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="cold_breakout_wave",
        cold_wave_len=7, cold_wave_min_lows=5,
        cold_wave_min_first_big=1, cold_wave_min_second_small=2,
        cold_wave_max_gap=8,
        cold_wave_big_min=10.0, cold_wave_small_min=2.0, cold_wave_small_max=10.0,
        cold_wave_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # elliott_impulse5_21: paper-only Elliott-style heat impulse, betting the 5th subwave.
    "elliott_impulse5_21": StrategyConfig(
        name="elliott_impulse5_21",
        bet_pct=0.00001, cashout=2.1, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="elliott_impulse5",
        elliott_base_len=120, elliott_sub_len=4,
        elliott_min_amp=0.22, elliott_wave3_margin=0.08, elliott_wave4_floor=-0.05,
        elliott_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
    # elliott_impulse5_x3: stricter shadow variant; only paper until live evidence exists.
    "elliott_impulse5_x3": StrategyConfig(
        name="elliott_impulse5_x3",
        bet_pct=0.00001, cashout=3.0, stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.0005,
        loss_scale=2.0, max_scale=50.0,
        filter="elliott_impulse5",
        elliott_base_len=120, elliott_sub_len=5,
        elliott_min_amp=0.24, elliott_wave3_margin=0.10, elliott_wave4_floor=0.0,
        elliott_delay=1,
        consec_loss_trigger=4, consec_loss_pause=4,
    ),
}


# -- Pure decision functions ---------------------------------------------------

def new_session_bank(total_bank: float, cfg: StrategyConfig = None) -> float:
    frac = cfg.session_frac if cfg is not None else SESSION_FRAC
    return round(total_bank * frac, 8)


def calc_bet(session_bank: float, total_bank: float,
             cfg: StrategyConfig = None, current_scale: float = 1.0) -> float:
    bet_pct  = cfg.bet_pct      if cfg is not None else BET_PCT
    hard_cap = cfg.bet_hard_cap if cfg is not None else BET_HARD_CAP
    raw = session_bank * bet_pct * current_scale
    cap = total_bank * hard_cap
    return round(min(raw, cap), 8)


def session_pnl_pct(session_bank: float, session_start: float) -> float:
    if session_start <= 0:
        return 0.0
    return (session_bank - session_start) / session_start


def session_exit_reason(session_bank: float, session_start: float,
                        rounds: int, cfg: StrategyConfig = None) -> str:
    sl = cfg.stop_loss   if cfg is not None else STOP_LOSS
    sp = cfg.stop_profit if cfg is not None else STOP_PROFIT
    mr = cfg.max_rounds  if cfg is not None else MAX_ROUNDS
    pnl = session_pnl_pct(session_bank, session_start)
    if pnl <= -sl:   return "stop_loss"
    if pnl >= sp:    return "stop_profit"
    if rounds >= mr: return "max_rounds"
    return ""


def multiplier_class(mult: float) -> str:
    """Classify crash multipliers like the BCGame trend colors."""
    if mult < 2.0:
        return "L"
    if mult < 10.0:
        return "G"
    return "H"


def recent_pattern(recent_multipliers: list | None, max_len: int = 8) -> str:
    if not recent_multipliers:
        return ""
    return "".join(multiplier_class(float(m)) for m in recent_multipliers[-max_len:])


def pattern_matches(recent: str, patterns: tuple[str, ...]) -> bool:
    return any(recent.endswith(pat) for pat in patterns)


def numeric_class(mult: float, cfg: StrategyConfig) -> str:
    if cfg.numeric_h_range[0] <= mult <= cfg.numeric_h_range[1]:
        return "H"
    if cfg.numeric_l_range[0] <= mult <= cfg.numeric_l_range[1]:
        return "L"
    if cfg.numeric_m_range[0] <= mult <= cfg.numeric_m_range[1]:
        return "M"
    return "X"


def numeric_pattern_matches(recent_multipliers: list | None, cfg: StrategyConfig) -> bool:
    if not recent_multipliers or not cfg.numeric_pattern:
        return False
    delay = max(0, cfg.numeric_delay - 1)
    usable = recent_multipliers[:-delay] if delay else recent_multipliers
    pat = cfg.numeric_pattern
    if len(usable) < len(pat):
        return False
    recent = "".join(numeric_class(float(m), cfg) for m in usable[-len(pat):])
    return recent == pat


def wave_regime_matches(recent_multipliers: list | None, cfg: StrategyConfig) -> bool:
    """Match low-wave followed by high-wave, optionally delayed by N rounds."""
    if not recent_multipliers or cfg.wave_low_len <= 0 or cfg.wave_high_len <= 0:
        return False
    delay = max(0, cfg.wave_delay - 1)
    usable = recent_multipliers[:-delay] if delay else recent_multipliers
    need = cfg.wave_low_len + cfg.wave_high_len
    if len(usable) < need:
        return False
    low = [float(m) for m in usable[-need:-cfg.wave_high_len]]
    high = [float(m) for m in usable[-cfg.wave_high_len:]]
    low_ge2 = sum(1 for m in low if m >= 2.0) / len(low)
    high_ge2 = sum(1 for m in high if m >= 2.0) / len(high)
    high_ge10 = sum(1 for m in high if m >= 10.0) / len(high)
    return (
        low_ge2 <= cfg.wave_low_max_ge2
        and high_ge2 >= cfg.wave_high_min_ge2
        and high_ge10 >= cfg.wave_high_min_ge10
    )


def _ratio_ge(values: list[float], threshold: float) -> float:
    if not values:
        return 0.0
    return sum(1 for m in values if m >= threshold) / len(values)


def relative_wave_matches(recent_multipliers: list | None, cfg: StrategyConfig) -> bool:
    """Match low-wave -> high-wave relative to the local baseline."""
    if (
        not recent_multipliers
        or cfg.rel_base_len <= 0
        or cfg.rel_low_len <= 0
        or cfg.rel_high_len <= 0
    ):
        return False
    delay = max(0, cfg.rel_delay - 1)
    usable = recent_multipliers[:-delay] if delay else recent_multipliers
    need = cfg.rel_base_len + cfg.rel_low_len + cfg.rel_high_len
    if len(usable) < need:
        return False

    window = [float(m) for m in usable[-need:]]
    base = window[:cfg.rel_base_len]
    low = window[cfg.rel_base_len:cfg.rel_base_len + cfg.rel_low_len]
    high = window[-cfg.rel_high_len:]

    base_ge2 = _ratio_ge(base, 2.0)
    low_ge2 = _ratio_ge(low, 2.0)
    high_ge2 = _ratio_ge(high, 2.0)
    if low_ge2 > base_ge2 - cfg.rel_low_margin:
        return False
    if high_ge2 < base_ge2 + cfg.rel_high_margin:
        return False
    if high_ge2 - low_ge2 < cfg.rel_min_delta:
        return False

    if cfg.rel_high10_mode == "baseline":
        return _ratio_ge(high, 10.0) >= _ratio_ge(base, 10.0)
    if cfg.rel_high10_mode == "baseline_plus":
        return _ratio_ge(high, 10.0) >= _ratio_ge(base, 10.0) + 0.05
    return True


def relative_bad_veto_matches(recent_multipliers: list | None, cfg: StrategyConfig) -> bool:
    """Match hot-wave -> cold-wave relative to the local baseline."""
    if (
        not recent_multipliers
        or cfg.rel_base_len <= 0
        or cfg.rel_high_len <= 0
        or cfg.rel_low_len <= 0
    ):
        return False
    delay = max(0, cfg.rel_delay - 1)
    usable = recent_multipliers[:-delay] if delay else recent_multipliers
    need = cfg.rel_base_len + cfg.rel_high_len + cfg.rel_low_len
    if len(usable) < need:
        return False

    window = [float(m) for m in usable[-need:]]
    base = window[:cfg.rel_base_len]
    high = window[cfg.rel_base_len:cfg.rel_base_len + cfg.rel_high_len]
    low = window[-cfg.rel_low_len:]

    base_ge2 = _ratio_ge(base, 2.0)
    high_ge2 = _ratio_ge(high, 2.0)
    low_ge2 = _ratio_ge(low, 2.0)
    return (
        high_ge2 >= base_ge2 + cfg.rel_high_margin
        and low_ge2 <= base_ge2 - cfg.rel_low_margin
        and high_ge2 - low_ge2 >= cfg.rel_min_delta
    )


def _wave_energy(values: list[float]) -> float:
    """Heat score for a subwave: common >=2x strength plus rare >=10x impulse."""
    return _ratio_ge(values, 2.0) + 0.5 * _ratio_ge(values, 10.0)


def elliott_impulse5_matches(recent_multipliers: list | None, cfg: StrategyConfig) -> bool:
    """Approximate Elliott 1-2-3-4 setup and enter on the expected 5th heat subwave."""
    if (
        not recent_multipliers
        or cfg.elliott_base_len <= 0
        or cfg.elliott_sub_len <= 0
    ):
        return False
    delay = max(0, cfg.elliott_delay - 1)
    usable = recent_multipliers[:-delay] if delay else recent_multipliers
    need = cfg.elliott_base_len + 4 * cfg.elliott_sub_len
    if len(usable) < need:
        return False

    window = [float(m) for m in usable[-need:]]
    base = window[:cfg.elliott_base_len]
    waves = [
        window[cfg.elliott_base_len + i * cfg.elliott_sub_len:
               cfg.elliott_base_len + (i + 1) * cfg.elliott_sub_len]
        for i in range(4)
    ]
    base_e = _wave_energy(base)
    w1, w2, w3, w4 = [_wave_energy(w) for w in waves]

    wave1_impulse = w1 >= base_e + cfg.elliott_min_amp
    wave2_pullback = base_e + cfg.elliott_wave4_floor <= w2 <= w1 - cfg.elliott_min_amp * 0.5
    wave3_extension = w3 >= max(w1, w2) + cfg.elliott_wave3_margin
    wave4_pullback = base_e + cfg.elliott_wave4_floor <= w4 <= w3 - cfg.elliott_min_amp * 0.5
    no_full_collapse = w4 >= w2 - 0.10
    return wave1_impulse and wave2_pullback and wave3_extension and wave4_pullback and no_full_collapse


def cold_breakout_wave_matches(recent_multipliers: list | None, cfg: StrategyConfig) -> bool:
    """Match two cold-dominant 7-wave segments with breakout inserts inside."""
    if not recent_multipliers or cfg.cold_wave_len <= 0:
        return False
    delay = max(0, cfg.cold_wave_delay - 1)
    usable = recent_multipliers[:-delay] if delay else recent_multipliers
    need = cfg.cold_wave_len * 2
    if len(usable) < need:
        return False

    window = [float(m) for m in usable[-(need + max(0, cfg.cold_wave_max_gap)):]]

    def segment_stats(segment: list[float]) -> tuple[int, int, int]:
        lows = sum(1 for m in segment if m < 2.0)
        big = sum(1 for m in segment if m >= cfg.cold_wave_big_min)
        small = sum(
            1 for m in segment
            if cfg.cold_wave_small_min <= m < cfg.cold_wave_small_max
        )
        return lows, big, small

    n = cfg.cold_wave_len
    max_gap = max(0, cfg.cold_wave_max_gap)
    for first_start in range(0, len(window) - need + 1):
        first = window[first_start:first_start + n]
        first_lows, first_big, _ = segment_stats(first)
        if first_lows < cfg.cold_wave_min_lows or first_big < cfg.cold_wave_min_first_big:
            continue
        for gap in range(0, max_gap + 1):
            second_start = first_start + n + gap
            second_end = second_start + n
            if second_end > len(window):
                continue
            second = window[second_start:second_end]
            second_lows, _, second_small = segment_stats(second)
            if (
                second_lows >= cfg.cold_wave_min_lows
                and second_small >= cfg.cold_wave_min_second_small
            ):
                return True
    return False


def decide(session_bank: float, session_start: float, total_bank: float,
           session_rounds: int, consec_losing: int,
           cfg: StrategyConfig = None,
           current_scale: float = 1.0,
           recent_multipliers: list = None) -> dict:
    """
    Returns the bet decision for the next round.

    Return keys:
      action    -- 'bet' | 'end_session' | 'paused' | 'no_funds' | 'no_bet'
      bet_sol   -- amount to bet (0 when not betting)
      cashout   -- target multiplier (0 when not betting)
      reason    -- human-readable explanation
      new_scale -- updated scale factor (caller stores in DB)
    """
    if cfg is None:
        cfg = STRATEGIES["compound"]

    if consec_losing >= cfg.consec_sl_pause:
        return {"action": "paused", "bet_sol": 0.0, "cashout": 0.0,
                "reason": f"{consec_losing} consecutive losing sessions -- waiting",
                "new_scale": current_scale}

    exit_reason = session_exit_reason(session_bank, session_start, session_rounds, cfg)
    if exit_reason:
        return {"action": "end_session", "bet_sol": 0.0, "cashout": 0.0,
                "reason": exit_reason, "new_scale": 1.0}

    # Sniper filter: skip round if any of last 5 rounds had x >= 10
    if cfg.filter == "no_x10_last5" and recent_multipliers:
        if any(m >= 10.0 for m in recent_multipliers[-5:]):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": "sniper_filter_x10_recent", "new_scale": current_scale}

    # Thermal filter: skip when recent win/loss score is below threshold
    if cfg.filter == "thermal_skip" and recent_multipliers and cfg.thermal_window > 0:
        window = cfg.thermal_window
        recent_w = recent_multipliers[-window:]
        if len(recent_w) >= window:
            score = sum(1 if m >= cfg.cashout else -1 for m in recent_w)
            if score < cfg.thermal_threshold:
                return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                        "reason": f"thermal_cold:{score}", "new_scale": current_scale}

    # Pattern filters based on BCGame trend colors:
    #   L: <2x, G: 2-10x, H: >=10x.
    if cfg.filter in ("pattern_allow", "pattern_veto"):
        recent = recent_pattern(recent_multipliers, max_len=8)
        if cfg.filter == "pattern_allow" and not pattern_matches(recent, cfg.allow_patterns):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": f"pattern_wait:{recent}", "new_scale": current_scale}
        if cfg.filter == "pattern_veto" and pattern_matches(recent, cfg.veto_patterns):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": f"pattern_veto:{recent}", "new_scale": current_scale}

    # Numeric pattern filter from the screenshot:
    # e.g. HLL with H=30-50x, L=1.0-1.8x, delay=2 enters on the second round after HLL.
    if cfg.filter == "numeric_pattern":
        if not numeric_pattern_matches(recent_multipliers, cfg):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": "numeric_pattern_wait", "new_scale": current_scale}

    if cfg.filter == "wave_regime":
        if not wave_regime_matches(recent_multipliers, cfg):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": "wave_regime_wait", "new_scale": current_scale}

    if cfg.filter == "relative_wave":
        if not relative_wave_matches(recent_multipliers, cfg):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": "relative_wave_wait", "new_scale": current_scale}

    if cfg.filter == "relative_bad_veto":
        if relative_bad_veto_matches(recent_multipliers, cfg):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": "relative_bad_veto", "new_scale": current_scale}

    if cfg.filter == "elliott_impulse5":
        if not elliott_impulse5_matches(recent_multipliers, cfg):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": "elliott_impulse5_wait", "new_scale": current_scale}

    if cfg.filter == "cold_breakout_wave":
        if not cold_breakout_wave_matches(recent_multipliers, cfg):
            return {"action": "no_bet", "bet_sol": 0.0, "cashout": 0.0,
                    "reason": "cold_breakout_wave_wait", "new_scale": current_scale}

    bet = calc_bet(session_bank, total_bank, cfg, current_scale)
    if bet <= 0 or bet > session_bank:
        return {"action": "no_funds", "bet_sol": 0.0, "cashout": 0.0,
                "reason": "session bank too small", "new_scale": current_scale}

    return {"action": "bet", "bet_sol": bet, "cashout": cfg.cashout,
            "reason": cfg.name, "new_scale": current_scale}
