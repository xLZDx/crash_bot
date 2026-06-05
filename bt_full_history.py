import duckdb, json
from datetime import datetime, timezone
from bot_strategy import STRATEGIES

con = duckdb.connect('/root/crash-collector/data/crash.duckdb', read_only=True)
rows = con.execute('SELECT multiplier, ts FROM rounds ORDER BY id ASC').fetchall()
mults = [r[0] for r in rows]
first_ts = rows[0][1]
last_ts = rows[-1][1]
total_days = (last_ts - first_ts).total_seconds() / 86400

print(f'Total rounds: {len(mults):,}')
print(f'Period: {first_ts.strftime("%Y-%m-%d")} → {last_ts.strftime("%Y-%m-%d")} ({total_days:.1f} days)')

BANK = 100.0
BASE = 0.01

def sim(mults, cfg):
    CASHOUT = cfg.cashout
    MAX_SCALE = cfg.max_scale
    LOSS_SCALE = cfg.loss_scale
    CONSEC = cfg.consec_loss_trigger or 4
    CD = cfg.consec_loss_pause or 4

    bal = BANK; scale = 1.0; consec = 0; cd = 0
    bets = wins = 0; min_bal = BANK; peak = BANK; max_dd = 0.0

    for m in mults:
        if cd > 0: cd -= 1; continue
        bet = round(BASE * min(scale, MAX_SCALE), 6)
        bets += 1
        if m >= CASHOUT:
            bal += bet*(CASHOUT-1.0); wins+=1; scale=1.0; consec=0
        else:
            bal -= bet; consec+=1
            scale = min(scale*LOSS_SCALE, MAX_SCALE)
            if CONSEC > 0 and consec >= CONSEC:
                cd=CD; scale=1.0; consec=0
        if bal > peak: peak = bal
        dd = (peak-bal)/peak*100 if peak>0 else 0
        if dd > max_dd: max_dd = dd
        min_bal = min(min_bal, bal)

    wr = wins/bets*100 if bets else 0
    return {'balance': round(bal,4), 'pnl': round(bal-BANK,4), 'bets': bets, 'wins': wins,
            'wr': round(wr,2), 'max_dd': round(max_dd,2), 'scale': scale, 'consec': consec}

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
print(f'\n{"Стратегия":<45} {"Balance":>10} {"P&L":>8} {"P&L%":>7} {"WR":>6} {"Bets":>7} {"MaxDD":>7}')
print('-'*95)
for name in strategies:
    cfg = STRATEGIES.get(name)
    if not cfg: continue
    r = sim(mults, cfg)
    sign = '+' if r['pnl']>=0 else ''
    print(f'{name:<45} ${r["balance"]:>8.2f}  {sign}${r["pnl"]:>6.2f}  {sign}{r["pnl"]/BANK*100:>5.1f}%  {r["wr"]:>5.1f}%  {r["bets"]:>6}  {r["max_dd"]:>5.1f}%')
    results[name] = r

with open('/tmp/backtest_full.json','w') as f:
    json.dump(results, f, indent=2)
print(f'\nSaved. Period: {total_days:.1f} days')
