"""Capacity search that scores every candidate with exact dispatch: merit order when there is no battery (already optimal),
the LP of lp_dispatch.py when there is one. Same local search as engine.search; usage:
    python lp_search.py NAME START [CARBON_PRICE] [zero_tier2] [no_reloc]"""
import json, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alt_adapter as A, engine as E, lp_dispatch as LP, export as X

gm = A.gm


def make_evaluator(start, delay=0, reloc=True, intr=True, workers=8):
    def evaluate(caps, cpr):
        caps = {k: float(v) for k, v in caps.items()}
        if caps.get('battery_4h', 0) > 0:
            try:
                m, _ = LP.lp_dispatch(start, caps, delay, reloc, intr, workers)
            except RuntimeError:
                return None
            return A.entry_from_metrics(start, caps, m)
        return A.evaluate_entry(start, caps, delay, reloc, intr, allow_infeasible=True)
    return evaluate


def lp_search(start, carbon_price=0.0, delay=0, reloc=True, intr=True, exclude=(), record=None, max_iter=40, seeds=None, verbose=False):
    seeds = seeds or [{'gas_turbine': 50, 'wind': 100, 'utility_solar': 50, 'battery_4h': 6},
                      {'gas_engine': 30, 'fuel_cell': 20, 'wind': 100, 'battery_4h': 6},
                      {'gas_turbine': 25, 'wind': 120, 'utility_solar': 60}]
    return E.search(start, carbon_price=carbon_price, exclude=exclude, record=record, max_iter=max_iter, seeds=seeds, verbose=verbose,
                    evaluate=make_evaluator(start, delay, reloc, intr))


if __name__ == '__main__':
    name, start = sys.argv[1], int(sys.argv[2])
    cp = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    intr = 'zero_tier2' not in sys.argv[4:]
    reloc = 'no_reloc' not in sys.argv[4:]
    t = time.time()
    res, j = lp_search(start, cp, intr=intr, reloc=reloc, verbose=True)
    caps = {k: float(v) for k, v in zip(E.KEYS, [res['caps'][k] for k in E.KEYS])} if isinstance(res['caps'], dict) else dict(zip(E.KEYS, res['caps']))
    m, r = LP.lp_dispatch(start, caps, 0, reloc, intr)
    ctx = gm.get_ctx(start, 0)
    out = HERE.parent / 'outputs' / 'lp' / name
    out.mkdir(parents=True, exist_ok=True)
    pf = {'initial_service_month': start, 'grid_delay_months': 0, 'capacities_mw': {k: v for k, v in caps.items()}}
    (out / 'portfolio.json').write_text(json.dumps(pf, indent=1))
    gm.to_dispatch_frame(ctx, caps, r).to_csv(out / 'dispatch.csv', index=False, float_format='%.9f')
    (out / 'engine_summary.json').write_text(json.dumps({k: v for k, v in m.items() if k not in ('ledger', 'annual')}, indent=1, default=float))
    print('DONE', name, {k: v for k, v in caps.items() if v}, 'total $%.2fM' % (m['total_decision_cost_usd'] / 1e6),
          'kg/MWh %.0f' % m['combined_electricity_co2_kg_per_mwh'], 'interrupted %.0f' % m['interrupted_mwh'], '%.0fs' % (time.time() - t))
