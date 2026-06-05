import json, duckdb
from pathlib import Path

results = json.load(open('/tmp/backtest_2w_usd.json'))

BANK_USD = 100.0
BASE_BET = 0.01
DATA_DIR = Path('/root/crash-collector/data')

# Get the round_id from 2 weeks ago (for last_decision_round_id)
con = duckdb.connect(str(DATA_DIR / 'crash.duckdb'), read_only=True)
from datetime import datetime, timedelta, timezone
two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14)
first_row = con.execute('SELECT MIN(id) FROM rounds WHERE ts >= ?', [two_weeks_ago]).fetchone()
start_round_id = first_row[0] or 0
latest_row = con.execute('SELECT MAX(id) FROM rounds').fetchone()
latest_round_id = latest_row[0] or 0
print(f'Start round_id (2w ago): {start_round_id}')
print(f'Latest round_id: {latest_round_id}')

# Update each paper bot's DuckDB state
strategy_to_db = {
    'x5': 'bot_state_x5',
    'martingale': 'bot_state_martingale',
    'relwave_l10_h5_x3': 'bot_state_relwave_l10_h5_x3',
    'relwave_l12_h5_21_strict': 'bot_state_relwave_l12_h5_21_strict',
    'relwave_l12_h5_21': 'bot_state_relwave_l12_h5_21',
    'elliott_impulse5_21': 'bot_state_elliott_impulse5_21',
    'elliott_impulse5_x3': 'bot_state_elliott_impulse5_x3',
    'relwave_l15_h7_x3_d3': 'bot_state_relwave_l15_h7_x3_d3',
    'waveskip': 'bot_state_waveskip',
    'sniper_v2': 'bot_state_sniper_v2',
    'cold_breakout_21': 'bot_state_cold_breakout_21',
    'relwave_bad_veto_21_sticky2': 'bot_state_relwave_bad_veto_21_sticky2',
    'relwave_bad_veto_21': 'bot_state_relwave_bad_veto_21',
    'turtle_micro': 'bot_state_turtle_micro',
    'cold_breakout_x3': 'bot_state_cold_breakout_x3',
    'martingale_21': 'bot_state_martingale_21',
    'turtle': 'bot_state_turtle',
    'x5_opt': 'bot_state_x5_opt',
    'martingale_30': 'bot_state_martingale_30',
    'relwave_bad_veto_21_trigger10_x512': 'bot_state_relwave_bad_veto_21_trigger10_x512',
    'waveskip_21': 'bot_state_waveskip_21',
    'relwave_bad_veto_21_nr8': 'bot_state_relwave_bad_veto_21_nr8',
    'relwave_bad_veto_21_nr128': 'bot_state_relwave_bad_veto_21_nr128',
    'relwave_bad_veto_21_nr256': 'bot_state_relwave_bad_veto_21_nr256',
    'relwave_bad_veto_21_nr512': 'bot_state_relwave_bad_veto_21_nr512',
}

for strat, db_name in strategy_to_db.items():
    if strat not in results:
        print(f'SKIP {strat}: no backtest data')
        continue
    r = results[strat]
    db_path = DATA_DIR / f'{db_name}.duckdb'
    if not db_path.exists():
        print(f'SKIP {strat}: db not found')
        continue
    try:
        with duckdb.connect(str(db_path)) as db:
            # Get current schema
            tables = [t[0] for t in db.execute("SHOW TABLES").fetchall()]
            if 'virtual_account' in tables:
                # Update balance to $100 + backtest P&L
                new_balance = r['balance']  # e.g. 100.57 for x5
                db.execute("UPDATE virtual_account SET balance_sol = ? WHERE account_id = 1", [new_balance])
                # Update last_session_round_id
                if 'system_health' in tables:
                    db.execute("UPDATE system_health SET last_round_id = ? WHERE key = 'last_round_id'",
                               [latest_round_id])
            print(f'OK {strat}: balance={r["balance"]:.2f} pnl={r["pnl"]:+.2f} bets={r["bets"]}')
    except Exception as e:
        print(f'ERR {strat}: {e}')

print('\nDone')
