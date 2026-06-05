path = "/root/crash-collector/dashboard.py"
with open(path, encoding="utf-8") as f:
    src = f.read()

OLD_STYLE = """        _STYLE = \"\"\"
        <style>
        .bst { width:100%; border-collapse:collapse; font-size:18px; font-family:monospace; }
        .bst th { background:#1e293b; color:#94a3b8; font-size:14px; font-weight:600;
                  padding:8px 12px; text-align:right; border-bottom:2px solid #334155; white-space:nowrap; }
        .bst th:first-child { text-align:left; }
        .bst td { padding:7px 12px; text-align:right; border-bottom:1px solid #1e293b;
                  font-size:16px; white-space:nowrap; }
        .bst td:first-child { text-align:left; font-weight:600; }
        .bst tr:hover td { background:#1e293b55; }
        </style>\"\"\""""

NEW_STYLE = """        _STYLE = \"\"\"
        <style>
        .bst { width:100%; border-collapse:collapse; font-family:'Segoe UI',monospace; }
        .bst th { background:#1e293b; color:#94a3b8; font-size:15px; font-weight:700;
                  padding:9px 14px; text-align:right; border-bottom:2px solid #334155;
                  white-space:nowrap; letter-spacing:0.3px; }
        .bst th:first-child { text-align:left; }
        .bst td { padding:8px 14px; text-align:right; border-bottom:1px solid #1e293b;
                  font-size:17px; font-weight:700; white-space:nowrap; }
        .bst td:first-child { text-align:left; font-size:17px; font-weight:700; }
        .bst tr:hover td { background:#1e293b66; }
        </style>\"\"\""""

assert OLD_STYLE in src, "Style anchor not found"
src = src.replace(OLD_STYLE, NEW_STYLE, 1)
print("FIXED: bold CSS")

# Rename column header "Bankroll" (make sure it shows full in _MAIN list)
# Already "Bankroll" in _MAIN — just ensure the display row uses the full value
# Also rename column in _MAIN to show wider
OLD_MAIN = '_MAIN = ["Strategy","Flags","Bankroll","P&L All","%1h","%12h","%24h","%1w","WinRate","MaxDD"]'
NEW_MAIN = '_MAIN = ["Strategy","Flags","Bankroll","P&L All","%1h","%12h","%24h","%1w","WinRate","Bets","MaxDD"]'

if OLD_MAIN in src:
    src = src.replace(OLD_MAIN, NEW_MAIN, 1)
    print("FIXED: added Bets to main table")

with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print("Saved.")

# ── Test ──────────────────────────────────────────────────────────────────────
import ast
with open(path, encoding="utf-8") as f:
    code = f.read()
ast.parse(code)
print("AST parse: OK — safe to restart")
