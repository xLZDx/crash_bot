import duckdb, json
from pathlib import Path

results = json.load(open('/tmp/backtest_2w_usd.json'))
DATA_DIR = Path('/root/crash-collector/data')
BANK_USD = 100.0

strategy_to_db = {
    'x5': 'bot_state_x5', 'martingale': 'bot_state_martingale',
    'relwave_l10_h5_x3': 'bot_state_relwave_l10_h5_x3',
    'relwave_l12_h5_21_strict': 'bot_state_relwave_l12_h5_21_strict',
    'relwave_l12_h5_21': 'bot_state_relwave_l12_h5_21',
    'elliott_impulse5_21': 'bot_state_elliott_impulse5_21',
    'elliott_impulse5_x3': 'bot_state_elliott_impulse5_x3',
    'relwave_l15_h7_x3_d3': 'bot_state_relwave_l15_h7_x3_d3',
    'waveskip': 'bot_state_waveskip', 'sniper_v2': 'bot_state_sniper_v2',
    'cold_breakout_21': 'bot_state_cold_breakout_21',
    'relwave_bad_veto_21_sticky2': 'bot_state_relwave_bad_veto_21_sticky2',
    'relwave_bad_veto_21': 'bot_state_relwave_bad_veto_21',
    'turtle_micro': 'bot_state_turtle_micro', 'cold_breakout_x3': 'bot_state_cold_breakout_x3',
    'martingale_21': 'bot_state_martingale_21', 'turtle': 'bot_state_turtle',
    'x5_opt': 'bot_state_x5_opt', 'martingale_30': 'bot_state_martingale_30',
    'relwave_bad_veto_21_trigger10_x512': 'bot_state_relwave_bad_veto_21_trigger10_x512',
    'waveskip_21': 'bot_state_waveskip_21',
    'relwave_bad_veto_21_nr8': 'bot_state_relwave_bad_veto_21_nr8',
    'relwave_bad_veto_21_nr128': 'bot_state_relwave_bad_veto_21_nr128',
    'relwave_bad_veto_21_nr256': 'bot_state_relwave_bad_veto_21_nr256',
    'relwave_bad_veto_21_nr512': 'bot_state_relwave_bad_veto_21_nr512',
}

for strat, db_name in strategy_to_db.items():
    r = results.get(strat)
    if not r:
        print(f'SKIP {strat}')
        continue
    db_path = DATA_DIR / f'{db_name}.duckdb'
    if not db_path.exists():
        print(f'SKIP {strat}: no db')
        continue
    try:
        with duckdb.connect(str(db_path)) as db:
            # Set initial bank = 100 USD, current balance = backtest result
            new_balance = r['balance']
            db.execute("UPDATE virtual_account SET balance_sol = ?, initial_bank_sol = ? WHERE account_id = 1",
                       [new_balance, BANK_USD])
            # Clear bet history so P&L is clean from this point
            db.execute("DELETE FROM bets")
            db.execute("DELETE FROM strategy_snapshots")
            # Reset virtual_bets if exists
            try:
                db.execute("DELETE FROM virtual_bets")
            except:
                pass
            # Verify
            row = db.execute("SELECT balance_sol, initial_bank_sol FROM virtual_account WHERE account_id=1").fetchone()
            print(f'OK {strat:<45} balance=${row[0]:.2f}  init=${row[1]:.2f}  pnl={r["pnl"]:+.2f}')
    except Exception as e:
        print(f'ERR {strat}: {e}')

print('\nAll done')
