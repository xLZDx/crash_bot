import duckdb, json
from pathlib import Path

results = json.load(open('/tmp/backtest_2w_usd.json'))
DATA_DIR = Path('/root/crash-collector/data')
BANK_USD = 100.0

strategy_to_db = {
    'x5':'x5','martingale':'martingale','relwave_l10_h5_x3':'relwave_l10_h5_x3',
    'relwave_l12_h5_21_strict':'relwave_l12_h5_21_strict','relwave_l12_h5_21':'relwave_l12_h5_21',
    'elliott_impulse5_21':'elliott_impulse5_21','elliott_impulse5_x3':'elliott_impulse5_x3',
    'relwave_l15_h7_x3_d3':'relwave_l15_h7_x3_d3','waveskip':'waveskip','sniper_v2':'sniper_v2',
    'cold_breakout_21':'cold_breakout_21','relwave_bad_veto_21_sticky2':'relwave_bad_veto_21_sticky2',
    'relwave_bad_veto_21':'relwave_bad_veto_21','turtle_micro':'turtle_micro',
    'cold_breakout_x3':'cold_breakout_x3','martingale_21':'martingale_21','turtle':'turtle',
    'x5_opt':'x5_opt','martingale_30':'martingale_30',
    'relwave_bad_veto_21_trigger10_x512':'relwave_bad_veto_21_trigger10_x512',
    'waveskip_21':'waveskip_21',
    'relwave_bad_veto_21_nr8':'relwave_bad_veto_21_nr8',
    'relwave_bad_veto_21_nr128':'relwave_bad_veto_21_nr128',
    'relwave_bad_veto_21_nr256':'relwave_bad_veto_21_nr256',
    'relwave_bad_veto_21_nr512':'relwave_bad_veto_21_nr512',
}

for strat, db_suf in strategy_to_db.items():
    r = results.get(strat)
    if not r:
        continue
    db_path = DATA_DIR / f'bot_state_{db_suf}.duckdb'
    if not db_path.exists():
        print(f'SKIP {strat}: no db')
        continue
    try:
        with duckdb.connect(str(db_path)) as db:
            new_bal = r['balance']  # backtest result (e.g. 100.57 for x5)
            # Set total_bank_sol = backtest balance, session_bank_sol = backtest balance
            db.execute("""UPDATE strategy_state SET
                total_bank_sol = ?,
                session_bank_sol = ?,
                session_start_sol = ?,
                current_scale = 1.0,
                consec_losing_sessions = 0
                WHERE id = 1""", [new_bal, new_bal, new_bal])
            # Clear bet history for clean P&L going forward
            db.execute("DELETE FROM strategy_bets")
            db.execute("DELETE FROM strategy_snapshots")
            try:
                db.execute("DELETE FROM strategy_sessions")
            except:
                pass
            row = db.execute("SELECT total_bank_sol FROM strategy_state WHERE id=1").fetchone()
            print(f'OK {strat:<45} ${row[0]:.2f} (pnl_vs_100={r["pnl"]:+.2f})')
    except Exception as e:
        print(f'ERR {strat}: {e}')
print('Done')
