import duckdb, json
from datetime import datetime, timedelta, timezone
from bot_strategy import STRATEGIES

con = duckdb.connect('/root/crash-collector/data/crash.duckdb', read_only=True)
two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14)
rows = con.execute('SELECT multiplier FROM rounds WHERE ts >= ? ORDER BY id ASC', [two_weeks_ago]).fetchall()
mults = [r[0] for r in rows]
print(f'Rounds for backtest (2 weeks): {len(mults):,}')

BANK_USD = 100.0      # $100 starting balance
BASE_BET = 0.01       # $0.01 minimum bet

def sim_strategy(mults, cfg, bank=BANK_USD, base_bet=BASE_BET):
    """Simulate strategy on historical data. Returns final stats."""
    CASHOUT = cfg.cashout
    MAX_SCALE = cfg.max_scale
    LOSS_SCALE = cfg.loss_scale
    CONSEC = cfg.consec_loss_trigger or 4
    COOLDOWN = cfg.consec_loss_pause or 4

    balance = bank
    scale = 1.0
    consec = 0
    cd = 0
    bets = wins = 0
    min_balance = bank
    running_min_from_peak = 0.0
    peak = bank

    for m in mults:
        if cd > 0:
            cd -= 1
            continue

        bet = round(base_bet * scale, 6)
        # Check hard cap
        max_bet = base_bet * MAX_SCALE
        if bet > max_bet:
            bet = max_bet

        bets += 1
        if m >= CASHOUT:
            profit = bet * (CASHOUT - 1.0)
            balance += profit
            wins += 1
            scale = 1.0
            consec = 0
        else:
            balance -= bet
            consec += 1
            new_scale = min(scale * LOSS_SCALE, MAX_SCALE)
            scale = new_scale
            if CONSEC > 0 and consec >= CONSEC:
                if COOLDOWN > 0:
                    cd = COOLDOWN
                scale = 1.0
                consec = 0

        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak * 100
        if dd > running_min_from_peak:
            running_min_from_peak = dd
        min_balance = min(min_balance, balance)

    wr = wins/bets*100 if bets else 0
    pnl = balance - bank
    return {
        'balance': round(balance, 4),
        'pnl': round(pnl, 4),
        'bets': bets,
        'wins': wins,
        'wr': round(wr, 2),
        'max_dd': round(running_min_from_peak, 2),
        'scale': scale,
        'consec': consec,
    }

# All paper bot strategies
strategies = [
    'x5','martingale','relwave_l10_h5_x3','relwave_l12_h5_21_strict','relwave_l12_h5_21',
    'elliott_impulse5_21','elliott_impulse5_x3','relwave_l15_h7_x3_d3','waveskip',
    'sniper_v2','cold_breakout_21','relwave_bad_veto_21_sticky2','relwave_bad_veto_21',
    'turtle_micro','cold_breakout_x3','martingale_21','turtle','x5_opt','martingale_30',
    'relwave_bad_veto_21_trigger10_x512','waveskip_21',
    'relwave_bad_veto_21_nr8','relwave_bad_veto_21_nr128','relwave_bad_veto_21_nr256',
    'relwave_bad_veto_21_nr512'
]

results = {}
print(f'\n{"Стратегия":<40} {"Balance":>10} {"P&L $":>8} {"P&L%":>7} {"WR%":>6} {"Bets":>6} {"MaxDD":>7}')
print('-'*90)
for name in strategies:
    cfg = STRATEGIES.get(name)
    if not cfg:
        print(f'{name}: NOT FOUND')
        continue
    r = sim_strategy(mults, cfg)
    pnl_pct = r['pnl']/BANK_USD*100
    sign = '+' if r['pnl'] >= 0 else ''
    print(f'{name:<40} ${r["balance"]:>8.2f}  {sign}${r["pnl"]:>6.2f}  {sign}{pnl_pct:>5.1f}%  {r["wr"]:>5.1f}%  {r["bets"]:>5}  {r["max_dd"]:>5.1f}%')
    results[name] = r

# Save to JSON for state initialization
with open('/tmp/backtest_2w_usd.json', 'w') as f:
    json.dump(results, f, indent=2)
print(f'\nSaved to /tmp/backtest_2w_usd.json')
