"""Usage: python team_app/run_scenario.py NAME START CARBON_PRICE [MAX_INTENSITY|none] [zero_tier2]"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E, export as X

name, start, cp = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
mi = None if len(sys.argv) < 5 or sys.argv[4] == 'none' else float(sys.argv[4])
z = len(sys.argv) > 5 and sys.argv[5] == 'zero_tier2'
res, j = E.search(start, carbon_price=cp, max_intensity=mi, zero_tier2=z, verbose=True)
if res is None:
    raise SystemExit('no feasible portfolio found for this opening month')
folder = Path(__file__).resolve().parents[1] / 'outputs' / name
X.export(res, folder)
summary = {k: v for k, v in res.items() if k != '_hourly'}
summary['scenario'] = dict(start=start, carbon_price_usd_per_t=cp, max_intensity=mi, zero_tier2=z)
(folder / 'engine_summary.json').write_text(json.dumps(summary, indent=1))
print('DONE', name, json.dumps(summary['caps']), round(res['total_cost'] / 1e6, 1), round(res['intensity']), res['interrupted_mwh'])
