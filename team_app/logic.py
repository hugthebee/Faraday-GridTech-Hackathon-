"""Decision logic for Team 11 GridTech: candidate pools, hard filters, preference ranking, relaxation advice.

Required inputs (hard filters) remove portfolios. Preferences only rank the survivors.
"""
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

import engine as E

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / 'scenario_cache'
LABELS = {'utility_solar': 'Utility solar', 'wind': 'Wind', 'battery_4h': 'Battery (4h)', 'gas_turbine': 'Gas turbine',
          'gas_engine': 'Gas engine', 'fuel_cell': 'Fuel cell', 'nuclear': 'Nuclear PPA'}
STARTS = [14, 16, 18, 20, 22, 24, 26, 28, 30, 34, 40]


# ----------------------------------------------------------------------------- pools
def _frame(rows, offset=10):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    caps = pd.DataFrame(df.pop('caps').tolist(), columns=E.KEYS)
    df = pd.concat([df.reset_index(drop=True), caps], axis=1)
    df['full_month'] = df.start + offset                # offset = months from opening to full demand (10 in the packet)
    df['clean_share'] = df.clean_mwh / df.served_mwh
    df['cost_per_mwh'] = df.total_cost / df.requested_mwh
    return df


USE_ALT = (HERE / 'pool2_base.json').exists()   # pools scored with the alternative evaluator (model_alt) when built


def load_pool(name, alt=None):
    alt = USE_ALT if alt is None else alt
    if alt:
        for prefix in ('pool3', 'pool2'):          # pool3 = pool2 + anchors and trade-off sweep (build_pareto.py)
            f = HERE / f'{prefix}_{name}.json'
            if f.exists():
                return _frame(json.loads(f.read_text()))
    return _frame(json.loads((HERE / f'pool_{name}.json').read_text()))


def policy_key(delay, price, reloc, intr):
    return f'd{delay}_{price}_r{int(reloc)}_i{int(intr)}'


def _scenario_file(policy):
    return CACHE_DIR / f'{policy_key(*policy)}.json'


def base_pool_for(policy):
    """Pre-built pool for the two standard policies, else None."""
    delay, price, reloc, intr = policy
    if price == 'Base' and reloc:
        if delay == 0:
            return load_pool('base' if intr else 'no_t2')
        if delay == 6 and intr and USE_ALT:
            return load_pool('d6')
    return None


def pool_for(policy, starts_max, exclude=()):
    """Candidate pool for a policy: a prebuilt table (plus any re-optimized rows) or a live re-simulation."""
    base = base_pool_for(policy)
    if base is None:
        return evaluate_scenario(policy, starts_max, exclude)
    f = _scenario_file(policy)
    if f.exists():
        base = pd.concat([base, _frame(json.loads(f.read_text()))]).drop_duplicates(subset=['start'] + E.KEYS).reset_index(drop=True)
    return base


def frontier_candidates(starts_max, exclude, per_start=8):
    """Pick a manageable set of promising (start, capacities) from the union of the pre-built pools."""
    df = pd.concat([load_pool('base'), load_pool('no_t2')]).drop_duplicates(subset=['start'] + E.KEYS)
    df = df[df.start <= starts_max]
    for k in exclude:
        df = df[df[k] == 0]
    picks = []
    for s, g in df.groupby('start'):
        g = g.sort_values('cost_per_mwh')
        front, best_i = [], np.inf
        for _, r in g.iterrows():  # lowest-cost-first sweep keeps points that improve carbon intensity
            if r.intensity < best_i - 1e-9:
                front.append(r); best_i = r.intensity
        idx = np.unique(np.linspace(0, len(front) - 1, min(per_start, len(front))).astype(int)) if front else []
        picks += [front[i] for i in idx]
    return picks


def evaluate_scenario(policy, starts_max, exclude=()):
    """Re-simulate promising pre-built portfolios under a non-standard scenario, plus any re-optimized extras."""
    delay, price, reloc, intr = policy
    rows = []
    for r in frontier_candidates(starts_max, exclude):
        caps = {k: float(r[k]) for k in E.KEYS}
        if USE_ALT and price == 'Base':
            import alt_adapter as A
            e = A.evaluate_entry(int(r.start), caps, delay, reloc, intr)
            if e:
                rows.append(e)
            continue
        res = E.dispatch(caps, int(r.start), delay, 0.0, 60.0, price, reloc, intr)
        if res['unserved_mwh'] <= 1e-6:
            rows.append(E.entry(res))
    f = _scenario_file(policy)
    if f.exists():
        rows += [e for e in json.loads(f.read_text()) if e['start'] <= starts_max and all(e['caps'][E.KEYS.index(k)] == 0 for k in exclude)]
    df = _frame(rows)
    return df.drop_duplicates(subset=['start'] + E.KEYS).reset_index(drop=True) if len(df) else df


def _opt_job(args):
    policy, start, cp, exclude = args
    delay, price, reloc, intr = policy
    rec = []
    if USE_ALT and price == 'Base':   # same evaluator as the pools and the exported dispatch
        import alt_adapter as A
        # The carbon price only steers the search objective. Every recorded row uses the plain cost-ordered dispatch
        # (carbon shadow 0), i.e. exactly what the app exports, so pool metrics always match the checked dispatch.
        E.search(start, carbon_price=cp, exclude=exclude, record=rec, max_iter=40,
                 evaluate=lambda caps, cpr: A.evaluate_entry(start, caps, delay, reloc, intr, 0.0, allow_infeasible=True))
    else:
        E.search(start, carbon_price=cp, delay=delay, price=price, allow_reloc=reloc, allow_intr=intr, exclude=exclude, record=rec, max_iter=40)
    return rec


def reoptimize(policy, starts, exclude=()):
    """Run fresh local searches for this scenario in parallel; results are cached on disk."""
    jobs = [(policy, s, cp, tuple(exclude)) for s in starts for cp in (0, 100)]
    out = {}
    with ProcessPoolExecutor(max_workers=8) as ex:
        for rec in ex.map(_opt_job, jobs):
            for e in rec:
                out[(e['start'], tuple(e['caps']))] = e
    CACHE_DIR.mkdir(exist_ok=True)
    f = _scenario_file(policy)
    old = {(e['start'], tuple(e['caps'])): e for e in (json.loads(f.read_text()) if f.exists() else [])}
    old.update(out)
    f.write_text(json.dumps(list(old.values())))
    return len(out)


# ----------------------------------------------------------------------------- filters
def apply_requirements(df, req, exclude, baseline_cost=None):
    """Return (survivors, filters) where filters = {name: (boolean mask, series, direction, label)}."""
    f = {}
    if req.get('deadline_full') is not None:
        f['Deadline for full demand'] = (df.full_month <= req['deadline_full'], df.full_month, 'min', 'full-demand month')
    if req.get('latest_full') is not None:
        f['Latest acceptable full-demand month'] = (df.full_month <= req['latest_full'], df.full_month, 'min', 'full-demand month')
    if req.get('cost_ceiling_m') is not None:
        f['Cost ceiling'] = (df.total_cost <= req['cost_ceiling_m'] * 1e6, df.total_cost / 1e6, 'min', 'total decision cost ($M)')
    if req.get('premium_pct') is not None:
        if baseline_cost is None or not np.isfinite(baseline_cost):
            # A premium cap that cannot be measured must NOT be dropped: it blocks everything and says why (see premium_baseline).
            f['Premium cap vs baseline (no baseline found)'] = (pd.Series(False, index=df.index), pd.Series(np.nan, index=df.index), 'min', 'premium over baseline (%)')
        else:
            ceiling = baseline_cost * (1 + req['premium_pct'] / 100)
            f['Premium cap vs baseline'] = (df.total_cost <= ceiling, (df.total_cost / baseline_cost - 1) * 100, 'min', 'premium over baseline (%)')
    if req.get('carbon_limit') is not None:
        f['Carbon limit'] = (df.intensity <= req['carbon_limit'], df.intensity, 'min', 'combined kg CO2/MWh')
    if req.get('clean_floor_pct') is not None:
        f['Clean share floor'] = (df.clean_share * 100 >= req['clean_floor_pct'], df.clean_share * 100, 'max', 'clean share (%)')
    if req.get('fixed_start') is not None:
        f['Opening month fixed by the developer'] = (df.start == req['fixed_start'], df.start, 'min', 'opening month')
    ok = pd.Series(True, index=df.index)
    for m, *_ in f.values():
        ok &= m
    for k in exclude:
        ok &= df[k] == 0
    return df[ok], f


def premium_baseline(pool, exclude, baseline_full, ramp=10):
    """Cheapest portfolio reaching full demand in EXACTLY month `baseline_full` (opening baseline_full - ramp), under the exclusions.
    Returns (cost, None) or (None, reason). No nearest-month substitution: a premium measured against the wrong month is a wrong answer."""
    s0 = int(baseline_full) - int(ramp)
    if s0 < 14:
        return None, f'the baseline month {baseline_full} needs an opening in month {s0}, before the earliest possible month 14'
    p = pool[pool.apply(lambda r: all(r[k] == 0 for k in exclude), axis=1)] if exclude and len(pool) else pool
    g = p[p.start == s0] if len(p) else p
    if g is None or len(g) == 0:
        return None, f'this pool has no portfolio opening in month {s0} (full demand month {baseline_full}), so the baseline for the premium cap does not exist'
    return float(g.total_cost.min()), None


def explain_empty(df, f, exclude):
    """Name the binding requirement and suggest a relaxation (based on the portfolios actually tested)."""
    base = pd.Series(True, index=df.index)
    for k in exclude:
        base &= df[k] == 0
    rows = []
    for name in f:
        ok = base.copy()
        for other, (m, *_ ) in f.items():
            if other != name:
                ok &= m
        n = int(ok.sum())
        if n:
            _, series, direction, label = f[name]
            best = series[ok].min() if direction == 'min' else series[ok].max()
            rows.append((n, name, label, best, direction))
    rows.sort(key=lambda r: -r[0])
    return rows


# ----------------------------------------------------------------------------- ranking
def _axes(d, latest, month_value_m, carbon_price):
    d = d.copy()
    d['delay_credit'] = month_value_m * 1e6 * (latest - d.start)             # customer valuation (not a common cost)
    d['carbon_cost'] = carbon_price * d.co2_t                                # customer valuation (not a common cost)
    d['adjusted_cost'] = d.total_cost - d.delay_credit + d.carbon_cost
    d['cost_axis'] = d.adjusted_cost / d.requested_mwh                      # $/requested MWh: delay cannot hide behind less service
    return d


SPEED_NADIR_FULL = 40     # full-demand month treated as "slowest sensible" for the speed score (earliest is the pool's earliest)


def anchors(ref, month_value_m=0.0, carbon_price=0.0):
    """Payoff-table anchors: the true best and worst of each axis, taken from the single-objective optima at every opening month.
      ideal: cheapest cost per requested MWh found; cleanest intensity found (build_pareto.py searches both explicitly)
      nadir: worst value of an axis among the OTHER axis' optima (cost of the cleanest portfolio, intensity of the cheapest one)
    Using these instead of the pool's raw min/max stops junk or merely-coarse portfolios from setting the scale."""
    r = _axes(ref, ref.start.max(), month_value_m, carbon_price)
    cost_nadir, carbon_nadir = [], []
    for _, g in r.groupby('start'):
        cost_nadir.append(g.loc[g.intensity.idxmin()].cost_axis)           # price of the cleanest option at this opening month
        carbon_nadir.append(g.loc[g.cost_axis.idxmin()].intensity)        # dirtiness of the cheapest option at this opening month
    a = dict(cost=(float(r.cost_axis.min()), float(max(cost_nadir))), carbon=(float(r.intensity.min()), float(max(carbon_nadir))),
             speed=(float(ref.full_month.min()), float(min(ref.full_month.max(), SPEED_NADIR_FULL))))
    for k, (lo, hi) in list(a.items()):
        if hi <= lo:                                                        # degenerate: fall back to the pool's own range
            col = {'cost': r.cost_axis, 'carbon': r.intensity, 'speed': ref.full_month.astype(float)}[k]
            a[k] = (float(col.min()), float(col.max()))
    return a


def _axes(d, latest, month_value_m, carbon_price):
    d = d.copy()
    d['delay_credit'] = month_value_m * 1e6 * (latest - d.start)             # customer valuation (not a common cost)
    d['carbon_cost'] = carbon_price * d.co2_t                                # customer valuation (not a common cost)
    d['adjusted_cost'] = d.total_cost - d.delay_credit + d.carbon_cost
    d['cost_axis'] = d.adjusted_cost / d.requested_mwh                      # $/requested MWh: delay cannot hide behind less service
    return d


def rank(df, weights, month_value_m=0.0, carbon_price=0.0, reverse=(False, False, False), ref=None):
    """Weighted score (0-100). Each axis is scaled between its anchors (see anchors()), computed from the reference pool, NOT from the
    survivors, so adding a requirement can only remove portfolios; it never changes how the rest rank. Scores clip to [0, 1]."""
    ref = df if ref is None else ref
    latest = ref.start.max()
    d = _axes(df, latest, month_value_m, carbon_price)
    an = anchors(ref, month_value_m, carbon_price)

    def scale(key, v, rev):
        lo, hi = an[key]
        if not (np.isfinite(lo) and np.isfinite(hi)) or hi - lo < 1e-12:   # constant axis (e.g. one candidate, one opening month): it cannot separate
            return pd.Series(1.0, index=v.index)                           # portfolios, so every portfolio gets the full score on it
        z = ((hi - v) / (hi - lo)).clip(0.0, 1.0)                           # at the ideal -> 1, at the nadir -> 0
        return 1 - z if rev else z

    d['s_speed'] = scale('speed', d.full_month.astype(float), reverse[0])
    d['s_cost'] = scale('cost', d.cost_axis, reverse[1])
    d['s_carbon'] = scale('carbon', d.intensity, reverse[2])
    w = np.array(weights, float) / max(1e-9, sum(weights))
    d['score'] = 100 * (w[0] * d.s_speed + w[1] * d.s_cost + w[2] * d.s_carbon)
    d['pts_speed'], d['pts_cost'], d['pts_carbon'] = 100 * w[0] * d.s_speed, 100 * w[1] * d.s_cost, 100 * w[2] * d.s_carbon
    return d.sort_values(['score', 'total_cost'], ascending=[False, True]).reset_index(drop=True)


def caps_of(row):
    return {k: float(row[k]) for k in E.KEYS}


def caps_text(row):
    parts = [f"{LABELS[k]} {row[k]:g} MW" for k in E.KEYS if row[k] > 0]
    return ', '.join(parts) if parts else 'Merchant power only'


def simulate(caps, start, pol):
    """Hourly simulation of one portfolio for charts / export (alternative evaluator when available, else engine.py)."""
    delay, price, reloc, intr = pol
    if USE_ALT and price == 'Base':
        import alt_adapter as A
        return A.simulate(caps, start, delay, reloc, intr)
    return E.dispatch(caps, start, delay, 0.0, 60.0, price, reloc, intr)
