"""Fix KeyError: 'strategy' — key in _display_rows is 'Strategy' (capital S)."""
path = "/root/crash-collector/dashboard.py"
with open(path, encoding="utf-8") as f:
    src = f.read()

fixes = [
    # error row: r["strategy"] -> r["Strategy"]
    (
        '_rows_main.append({"strategy": r["strategy"], "error": r.get("error","")})',
        '_rows_main.append({"Strategy": r["Strategy"], "error": r.get("error","")})',
    ),
    # normal row: r["strategy"] -> r["Strategy"]
    (
        '"Strategy": r["strategy"],\n                "Flags":',
        '"Strategy": r["Strategy"],\n                "Flags":',
    ),
    # det row: r["strategy"] -> r["Strategy"]
    (
        '"Strategy": r["strategy"],\n                    "P&L 1h"',
        '"Strategy": r["Strategy"],\n                    "P&L 1h"',
    ),
    # pnl_all not in _display_rows — parse from P&L All string
    (
        '"pnl_all":  r.get("pnl_all", 0),',
        '"pnl_all":  (lambda v: (float(v.replace("+","")) if v not in ("","n/a","ERROR") else 0))(r.get("P&L All","0")),',
    ),
    # _html_table error case: r["strategy"] -> r["Strategy"]
    (
        'h += f\'<tr><td>{r["strategy"]}</td><td colspan="{len(cols)-1}" style="color:#ef4444">{r["error"][:80]}</td></tr>\'',
        'h += f\'<tr><td>{r.get("Strategy", r.get("strategy","?"))}</td><td colspan="{len(cols)-1}" style="color:#ef4444">{r["error"][:80]}</td></tr>\'',
    ),
]

for old, new in fixes:
    if old in src:
        src = src.replace(old, new, 1)
        print(f"Fixed: {old[:60]}")
    else:
        print(f"NOT FOUND: {old[:60]}")

with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print("Done.")
