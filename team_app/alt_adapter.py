"""Adapter around the alternative evaluator in ../model_alt (gridmodel.py, unchanged).

Gives the app the same entry/hourly shapes as engine.py, but with the alternative dispatch (which also buys
negative-price merchant power ahead of already-paid contracts). Supports grid delay, Tier-2 policy and Tier-1 cap.
Price scenarios are NOT supported by the alternative evaluator (use engine.py for those, labelled as extensions).
"""
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ALT = HERE.parent / 'model_alt'
sys.path.insert(0, str(ALT))
import gridmodel as gm  # noqa: E402  (alternative evaluator; imports the supplied checker unchanged)
import engine as E  # noqa: E402
import casepatch  # noqa: E402,F401  (per-thread case isolation: always installed, so concurrent cases cannot interfere)


def scenario(delay=0, reloc=True, intr=True, carbon=0.0):
    return gm.Scenario(grid_delay_months=int(delay), tier2_max_fraction=1.0 if intr else 0.0, tier1_max_mw=None if reloc else 0.0,
                       carbon_shadow_usd_per_t=float(carbon))


def entry_from_metrics(start, caps, m):
    return dict(start=int(start), caps=[float(caps.get(k, 0.0)) for k in E.KEYS],
                total_cost=m['total_decision_cost_usd'], local_energy_cost=m['local_energy_cost_usd'], relocation_cost=m['relocation_cost_usd'],
                interruption_cost=m['interruption_consequence_usd'], co2_t=m['combined_operational_co2_t'], served_mwh=m['local_served_mwh'],
                requested_mwh=m['requested_mwh'], relocated_mwh=m['relocated_local_equivalent_mwh'], interrupted_mwh=m['interrupted_mwh'],
                merchant_mwh=m['merchant_mwh'], clean_mwh=m['clean_energy_mwh'], intensity=m['combined_electricity_co2_kg_per_mwh'])


def entry_from_cache_row(r):
    """One row of the alternative model's cached CSV -> our pool entry."""
    served = r['local_served_mwh']
    return dict(start=int(r['initial_service_month']), caps=[float(r[k]) for k in E.KEYS],
                total_cost=r['total_decision_cost_usd'], local_energy_cost=r['local_energy_cost_usd'], relocation_cost=r['relocation_cost_usd'],
                interruption_cost=r['interruption_consequence_usd'], co2_t=r['combined_operational_co2_t'], served_mwh=served,
                requested_mwh=r['requested_mwh'], relocated_mwh=r['relocated_local_equivalent_mwh'], interrupted_mwh=r['interrupted_mwh'],
                merchant_mwh=r['merchant_mwh'], clean_mwh=r['clean_energy_share'] * served, intensity=r['combined_electricity_co2_kg_per_mwh'])


def evaluate_entry(start, caps, delay=0, reloc=True, intr=True, carbon=0.0, allow_infeasible=False):
    """Dispatch + accounting. Returns a pool entry, or None when the portfolio is not acceptable:
      * it breaks the catalog / land / physical rules (violations): NEVER returned, so it cannot be recorded or cached;
      * it cannot serve the demand: None, unless allow_infeasible=True (searches use this to see HOW MUCH demand is unserved and move the
        right way); that entry carries 'unserved_mwh' > 0 and callers must not record it."""
    m, res, _ = gm.evaluate(int(start), {k: float(v) for k, v in caps.items()}, scenario(delay, reloc, intr, carbon))
    if res.get('violations'):
        return None
    if res['feasible']:
        return entry_from_metrics(start, caps, m)
    if not allow_infeasible:
        return None
    e = entry_from_metrics(start, caps, m)
    e['unserved_mwh'] = float(np.asarray(res['unmet_mw']).sum())
    return e


def _job(args):
    return evaluate_entry(*args)


def simulate(caps, start, delay=0, reloc=True, intr=True):
    """Hourly arrays in the same layout as engine.dispatch()['_hourly'] (for charts, export and the checker)."""
    caps = {k: float(v) for k, v in caps.items()}
    sc = scenario(delay, reloc, intr)
    m, res, ctx = gm.evaluate(int(start), caps, sc)
    if res.get('violations'):
        raise ValueError('invalid portfolio or schedule: ' + '; '.join(res['violations']))
    frame = gm.to_dispatch_frame(ctx, caps, res)
    hourly = {c: frame[c].to_numpy() for c in frame.columns if c != 'model_hour'}
    return dict(caps=caps, start=int(start), delay=int(delay), _hourly=hourly, feasible=res['feasible'])
