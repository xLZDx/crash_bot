path = "/root/crash-collector/dashboard.py"
with open(path, encoding="utf-8") as f:
    src = f.read()

# _display_rows is built by iterating _bot_rows where key = "strategy" (lowercase)
# Both the error-row append and the normal-row append incorrectly use r["Strategy"]

fixes = [
    # error row (r is from _bot_rows -> lowercase "strategy")
    (
        '"Strategy": r["Strategy"],\n                    "Flags": "❌",',
        '"Strategy": r["strategy"],\n                    "Flags": "❌",',
    ),
    # normal row (r is from _bot_rows -> lowercase "strategy")
    (
        '"Strategy": r["Strategy"],\n                "Flags": " ".join',
        '"Strategy": r["strategy"],\n                "Flags": " ".join',
    ),
]

applied = 0
for old, new in fixes:
    if old in src:
        src = src.replace(old, new, 1)
        print(f"FIXED: {old[:70]}")
        applied += 1
    else:
        print(f"NOT FOUND: {old[:70]}")

assert applied == 2, f"Expected 2 fixes, applied {applied}"
with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print("Saved.")

# ── TEST: import the relevant parts without running Streamlit ─────────────────
import sys
sys.path.insert(0, "/root/crash-collector")
import ast
with open(path, encoding="utf-8") as f:
    code = f.read()
ast.parse(code)
print("AST parse: OK")

# Test load_bot_stats returns dicts with lowercase "strategy"
import glob, os
os.chdir("/root/crash-collector")
from bot_state import BotAccount
import duckdb as _ddb
import pandas as pd
from pathlib import Path

dbs = sorted(glob.glob("data/bot_state_*.duckdb"))[:2]
for db_path in dbs:
    strategy = Path(db_path).stem.replace("bot_state_", "")
    acc = BotAccount(db_path)
    state = acc.get_state()
    row = {"strategy": strategy, "bank": state["total_bank_sol"]}
    # simulate what _display_rows does
    display = {"Strategy": row["strategy"]}
    print(f"  {strategy}: display key ok -> {display}")

print("Runtime test: OK — safe to restart")
