import json
from pathlib import Path

pred_file = Path('data/prediction.json')
if pred_file.exists():
    data = json.loads(pred_file.read_text(encoding='utf-8'))
    print("=== CURRENT PREDICTION ===")
    print(json.dumps(data.get('prediction', {}), indent=2))
    print()
    print("=== MODEL METRICS ===")
    print(json.dumps(data.get('metrics', {}), indent=2))
    print()
    print("=== SELECTIVE BETTING TABLE ===")
    sel = data.get('selective', {})
    for cutoff, info in sorted(sel.items()):
        print(f"  cutoff={cutoff}: {info}")
    print()
    print("Best cutoff:", data.get('best_cutoff'))
    print("Top features:", data.get('top_features'))
    print("Rows used:", data.get('rows_used'))
    print("Updated:", data.get('ts'))
else:
    print("No prediction file found at data/prediction.json")
