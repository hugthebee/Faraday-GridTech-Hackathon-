"""Dispatch + costing engine for the GridTech benchmark (team code; packet files untouched).

Method (see README_method.md):
  * Offsite contracts (solar/wind/nuclear) are paid whether used or not, so they are dispatched first.
  * The remaining demand is filled hour by hour in merit order (cheapest first) from: merchant power,
    fuel cell, gas engine, gas turbine, then (event hours only) Tier-1 relocation and Tier-2 interruption.
  * An optional carbon shadow price re-orders the merit order and the search objective only. It is never
    added to the reported common costs.
  * The battery charges only from otherwise-curtailed contract energy and discharges when the marginal cost
    of the residual supply is above a threshold, or during stress events.
"""
import sys
from pathlib import Path
import numpy as np

PACKET = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKET / 'starter'))
import check_submission as cs  # noqa: E402  (official checker; used only for input building and verification)

OPT = cs.options.set_index('option_id')
NUM = lambda v: float(v) if v != '' else 0.0
CTX_CACHE = {}


# Price scenarios. "Base" is the packet's trajectory. High and Flat are TEAM extensions (disclosed assumptions,
# not checkable by the supplied checker, which always uses the Base rates).
PRICE_SCENARIOS = {
    'Base': dict(cs.trajectory['annual_real_rates']),
    'High': {'wholesale_energy': 0.08, 'henry_hub': 0.05, 'grid_co2': -0.01},
    'Flat': {'wholesale_energy': 0.0, 'henry_hub': 0.0, 'grid_co2': 0.0},
}


def _base_context(start, delay=0):
    key = (start, delay)
    if key not in CTX_CACHE:
        CTX_CACHE[key] = cs.prepare_inputs({'initial_service_month': start, 'grid_delay_months': delay, 'capacities_mw': {}})
    return CTX_CACHE[key]


def context(start, delay=0, price='Base', allow_reloc=True, allow_intr=True):
    key = (start, delay, price, allow_reloc, allow_intr)
    if key in CTX_CACHE:
        return CTX_CACHE[key]
    ctx = _base_context(start, delay)
    if price != 'Base' or not allow_reloc or not allow_intr:
        ctx = dict(ctx)
        if price != 'Base':
            rates = PRICE_SCENARIOS[price]
            ref = ctx['ref']; yrs = np.arange(ctx['n']) / cs.case['hours_per_year']
            col = lambda c: cs.hourly[c].to_numpy(float)[ref]
            ctx['grid_price'] = col('wholesale_price_usd_per_mwh') * (1 + rates['wholesale_energy']) ** yrs + col('grid_adder_usd_per_mwh')
            ctx['gas_price'] = col('henry_hub_usd_per_mmbtu') * (1 + rates['henry_hub']) ** yrs + col('delivered_gas_adder_usd_per_mmbtu')
            ctx['grid_co2'] = col('grid_co2_t_per_mwh') * (1 + rates['grid_co2']) ** yrs
            ctx['remote_co2'] = cs.response['remote_co2_t_per_mwh'] * (1 + rates['grid_co2']) ** yrs
        if not allow_reloc:
            ctx['relocation_limit'] = np.zeros(ctx['n'])
        if not allow_intr:
            ctx['interruption_limit'] = np.zeros(ctx['n'])
    CTX_CACHE[key] = ctx
    return ctx


def _mask(ctx, k):
    return ctx['month'] >= max(ctx['start'], int(OPT.loc[k, 'available_month']))


def land_ok(caps):
    land = sum(caps.get(k, 0) * NUM(OPT.loc[k, 'land_acres_per_mw']) for k in OPT.index)
    return land <= cs.case['onsite_energy_land_acres'] + 1e-9


def fixed_cost_per_hour(k, mw):
    r = OPT.loc[k]
    life = NUM(r.life_years)
    rate = cs.case['discount_rate_real']
    crf = rate / (1 - (1 + rate) ** (-life))
    return mw * 1000 * (NUM(r.capex_usd_per_kw) * crf + NUM(r.fom_usd_per_kw_year)) / 8760


def dispatch(caps, start, delay=0, carbon_price=0.0, batt_theta=60.0, price='Base', allow_reloc=True, allow_intr=True):
    """Return dict with hourly arrays, common-accounting costs, CO2 and feasibility info."""
    ctx = context(start, delay, price, allow_reloc, allow_intr)
    n = ctx['n']
    load, cap_grid = ctx['load'], ctx['grid_limit']
    price, co2g, gas = ctx['grid_price'], ctx['grid_co2'], ctx['gas_price']
    g = lambda k: caps.get(k, 0.0)

    # --- contracts first (pay-for-available, zero marginal cost) ---
    avail = {k: g(k) * cs.profile(OPT.loc[k], ctx) for k in ('utility_solar', 'wind', 'nuclear')}
    avail_tot = sum(avail.values())
    contract = np.minimum(np.minimum(avail_tot, load), cap_grid)
    share = np.divide(contract, avail_tot, out=np.zeros(n), where=avail_tot > 1e-12)
    R = load - contract

    # --- battery (rule based, first pass uses the pre-battery residual) ---
    P = g('battery_4h')
    ch = np.zeros(n); di = np.zeros(n); soc = np.zeros(n)
    plan = _merit(caps, ctx, R, cap_grid - contract, carbon_price)
    if P > 0:
        bmask = _mask(ctx, 'battery_4h')
        eta = np.sqrt(OPT.loc['battery_4h', 'round_trip_efficiency'])
        cap_e = 4 * P
        surplus = np.where(R <= 1e-9, np.minimum(np.minimum(avail_tot - contract, cap_grid - contract), P), 0.0) * bmask
        marg = plan['marginal']
        want_dis = ((marg > batt_theta) | ctx['active']) & (R > 1e-9) & bmask
        year = ctx['year']; used = np.zeros(10); lim = P * 4 * 365 * np.array([_mask(ctx, 'battery_4h')[year == y].sum() for y in range(10)]) / 8760
        s = 0.0
        for t in range(n):
            tail = t >= n - 24
            if want_dis[t] or (tail and R[t] > 1e-9 and bmask[t]):
                d = min(P, R[t], s * eta, max(0.0, (lim[year[t]] - used[year[t]]) * eta))
                if d > 1e-12:
                    di[t] = d; s -= d / eta; used[year[t]] += d / eta
            elif surplus[t] > 1e-9 and not tail:
                c = min(surplus[t], (cap_e - s) / eta)
                if c > 1e-12:
                    ch[t] = c; s += eta * c
            soc[t] = s
        if soc[-1] > 1e-9:  # leftover energy at the horizon end: cannot be discharged -> remove last charges
            for t in range(n - 1, -1, -1):
                if soc[-1] <= 1e-9: break
                if ch[t] > 0:
                    ch[t] = 0.0; s2 = 0.0
                    for u in range(n):
                        s2 = max(0.0, s2 + eta * ch[u] - di[u] / eta); soc[u] = s2
        R = R - di
        contract = contract + ch
        share = np.divide(contract, avail_tot, out=np.zeros(n), where=avail_tot > 1e-12)
        plan = _merit(caps, ctx, R, cap_grid - contract, carbon_price)

    out = {k: avail[k] * share for k in avail}
    merchant, fc, eng, turb, reloc, intr = (plan['alloc'][:, i] for i in range(6))
    short = R - plan['alloc'].sum(axis=1)

    # --- common accounting (mirrors check_submission.score) ---
    cost = merchant * price
    co2 = merchant * co2g
    local_energy = cost.copy()
    ledger_fixed = 0.0
    for k in OPT.index:
        mw = g(k)
        if mw <= 0: continue
        r = OPT.loc[k]
        fixed = _mask(ctx, k) * fixed_cost_per_hour(k, mw)
        gen = {'gas_turbine': turb, 'gas_engine': eng, 'fuel_cell': fc}.get(k, out.get(k))
        if r.kind == 'storage':
            local_energy = local_energy + fixed; continue
        if r.payment_basis == 'available_generation':
            pay = mw * cs.profile(r, ctx) * NUM(r.payment_usd_per_mwh)
        else:
            pay = gen * NUM(r.payment_usd_per_mwh)
        fuel = gen * gas * NUM(r.heat_rate_mmbtu_per_mwh_hhv)
        local_energy = local_energy + fixed + pay + fuel
        co2 = co2 + gen * NUM(r.co2_t_per_mwh)
    reloc_cost = reloc * ctx['relocation_price']
    intr_cost = intr * ctx['interruption_price']
    remote_co2 = reloc * cs.response['remote_energy_multiplier'] * ctx['remote_co2']
    local_served = load - reloc - intr
    clean = sum(out.values()) + di - ch
    res = dict(
        caps=dict(caps), start=start, delay=delay,
        local_energy_cost=float(local_energy.sum()), relocation_cost=float(reloc_cost.sum()), interruption_cost=float(intr_cost.sum()),
        co2_t=float(co2.sum() + remote_co2.sum()), unserved_mwh=float(np.maximum(short, 0).sum()),
        served_mwh=float(local_served.sum()), requested_mwh=float(load.sum()), relocated_mwh=float(reloc.sum()), interrupted_mwh=float(intr.sum()),
        merchant_mwh=float(merchant.sum()), clean_mwh=float(clean.sum()),
        intensity=float(1000 * (co2.sum() + remote_co2.sum()) / max(1e-9, local_served.sum() + 1.05 * reloc.sum())),
    )
    res['total_cost'] = res['local_energy_cost'] + res['relocation_cost'] + res['interruption_cost']
    res['_hourly'] = dict(grid_mw=merchant, tier1_relocated_mw=reloc, tier2_curtailed_mw=intr, utility_solar_mw=out['utility_solar'], wind_mw=out['wind'],
                          battery_4h_charge_mw=ch, battery_4h_discharge_mw=di, battery_4h_soc_mwh=soc, gas_turbine_mw=turb, nuclear_mw=out['nuclear'],
                          gas_engine_mw=eng, fuel_cell_mw=fc)
    return res


def _merit(caps, ctx, R, grid_room, carbon_price):
    n = ctx['n']
    g = lambda k: caps.get(k, 0.0)
    gas = ctx['gas_price']
    ev = ctx['active']
    cols_cap, cols_cost = [], []
    cols_cap.append(np.maximum(grid_room, 0)); cols_cost.append(ctx['grid_price'] + carbon_price * ctx['grid_co2'])
    for k in ('fuel_cell', 'gas_engine', 'gas_turbine'):
        r = OPT.loc[k]
        cols_cap.append(g(k) * _mask(ctx, k)); cols_cost.append(NUM(r.payment_usd_per_mwh) + NUM(r.heat_rate_mmbtu_per_mwh_hhv) * gas + carbon_price * NUM(r.co2_t_per_mwh))
    cols_cap.append(ctx['relocation_limit']); cols_cost.append(ctx['relocation_price'] + carbon_price * 1.05 * ctx['remote_co2'])
    cols_cap.append(ctx['interruption_limit']); cols_cost.append(np.full(n, ctx['interruption_price']))
    caps_m = np.column_stack(cols_cap); costs = np.column_stack(cols_cost)
    order = np.argsort(costs, axis=1, kind='stable')
    cs_sorted = np.take_along_axis(caps_m, order, 1)
    before = np.cumsum(cs_sorted, 1) - cs_sorted
    alloc_sorted = np.clip(R[:, None] - before, 0, cs_sorted)
    alloc = np.zeros_like(alloc_sorted)
    np.put_along_axis(alloc, order, alloc_sorted, 1)
    used_sorted = alloc_sorted > 1e-12
    marginal = np.where(used_sorted, np.take_along_axis(costs, order, 1), -np.inf).max(axis=1)
    return dict(alloc=alloc, marginal=marginal)


BLOCKS = {k: NUM(OPT.loc[k, 'block_mw']) for k in OPT.index}
MAXES = {k: NUM(OPT.loc[k, 'max_mw']) for k in OPT.index}
KEYS = ['utility_solar', 'wind', 'battery_4h', 'gas_turbine', 'gas_engine', 'fuel_cell', 'nuclear']


def objective(res, carbon_price=0.0, max_intensity=None, zero_tier2=False):
    j = res['total_cost'] + carbon_price * res['co2_t'] + 1e5 * res['unserved_mwh']
    if max_intensity is not None and res['intensity'] > max_intensity:
        j += 1e6 * (res['intensity'] - max_intensity) + 5e9
    if zero_tier2 and res['interrupted_mwh'] > 0:
        j += 5e9 + 1e4 * res['interrupted_mwh']
    return j


ONSITE = ('gas_turbine', 'gas_engine', 'fuel_cell', 'battery_4h')
TEMPLATE_SEEDS = [
    {'gas_turbine': 75, 'wind': 50},
    {'gas_engine': 60, 'fuel_cell': 30, 'wind': 50},
    {'gas_turbine': 50, 'gas_engine': 30, 'fuel_cell': 15, 'utility_solar': 50, 'wind': 100, 'battery_4h': 6},
]


def fit_to_land(seed):
    """Snap a seed to this case's catalog maxima and blocks, then shed onsite equipment (largest footprint first, one block at a time) until it
    fits the site's land. Seeds that ignore the case (e.g. 75 MW of turbine on a 4-acre site) would otherwise be skipped and the search would
    start with nothing."""
    s = {k: min(max(0.0, float(seed.get(k, 0))), MAXES[k]) for k in KEYS}
    for k in KEYS:
        s[k] = float(np.floor(s[k] / BLOCKS[k] + 1e-9) * BLOCKS[k])
    for _ in range(1000):
        if land_ok(s):
            break
        use = [(s[k] * NUM(OPT.loc[k, 'land_acres_per_mw']), k) for k in ONSITE if s[k] > 0]
        if not use:
            break
        _, k = max(use)
        s[k] = max(0.0, s[k] - BLOCKS[k])
    return s


def case_seeds(start, delay=0, allow_reloc=True, allow_intr=True, price='Base'):
    """Starting portfolios built from THIS case: the onsite power its grid gap and stress events require (peak of load - relocation -
    interruption - grid cap after opening), one gas technology at a time, with and without wind/solar; the old fixed templates are kept for variety.
    Everything is fitted to the case's land and maxima."""
    ctx = context(start, delay, price, allow_reloc, allow_intr)
    s0 = int(start) * 730
    gap = (ctx['load'] - ctx['relocation_limit'] - ctx['interruption_limit'] - ctx['grid_limit'])[s0:]
    need = float(max(gap.max(), 0.0))
    up = lambda x, k: float(min(MAXES[k], np.ceil(x / BLOCKS[k] - 1e-9) * BLOCKS[k]))
    raw = [{'gas_engine': up(need, 'gas_engine'), 'wind': 50, 'utility_solar': 50},
           {'fuel_cell': up(need, 'fuel_cell'), 'wind': 120, 'utility_solar': 60},
           {'gas_turbine': up(need, 'gas_turbine'), 'wind': 100},
           {'gas_engine': up(need, 'gas_engine'), 'wind': 150, 'utility_solar': 100}] + TEMPLATE_SEEDS
    out, seen = [], set()
    for sd in raw:
        f = fit_to_land(sd)
        key = tuple(f[k] for k in KEYS)
        if key not in seen:
            seen.add(key); out.append(f)
    return out


def search(start, carbon_price=0.0, max_intensity=None, zero_tier2=False, delay=0, seeds=None, verbose=False, batt_theta=60.0, max_iter=60,
           price='Base', allow_reloc=True, allow_intr=True, exclude=(), record=None, evaluate=None):
    # evaluate(caps, carbon_price) -> pool entry dict or None (infeasible) replaces the built-in dispatch (see alt_adapter.py)
    """Coordinate local search over block counts. Returns best (res, J). Heuristic: no optimality claim."""
    cache = {}

    def ev(caps):
        key = tuple(caps.get(k, 0) for k in KEYS)
        if key not in cache and evaluate is not None:
            e = evaluate(caps, carbon_price)
            if e is None:
                cache[key] = (None, 1e18)
            else:
                unserved = float(e.get('unserved_mwh', 0.0))          # >0: cannot serve the demand yet; scored, never recorded
                r = {**e, 'unserved_mwh': unserved}
                if record is not None and unserved <= 1e-6:
                    record.append({k: v for k, v in e.items() if k != 'unserved_mwh'})
                cache[key] = (r, objective(r, carbon_price, max_intensity, zero_tier2))
        if key not in cache:
            # when recording pool rows, dispatch at carbon shadow 0 so the recorded metrics match the exported dispatch
            r = dispatch(caps, start, delay, 0.0 if record is not None else carbon_price, batt_theta, price, allow_reloc, allow_intr)
            if record is not None and r['unserved_mwh'] <= 1e-6:
                record.append(entry(r))
            cache[key] = (r, objective(r, carbon_price, max_intensity, zero_tier2))
        return cache[key]

    seeds = [fit_to_land(sd) for sd in (seeds or case_seeds(start, delay, allow_reloc, allow_intr, price))]   # always fit THIS case's land and maxima
    best = None
    for seed in seeds:
        cur = {k: (0.0 if k in exclude else float(seed.get(k, 0))) for k in KEYS}
        if not land_ok(cur): continue
        cr, cj = ev(cur)
        for it in range(max_iter):
            improved = False
            for k in KEYS:
                for step in (1, -1, 3, -3, 8, -8):
                    cand = dict(cur); v = cand[k] + step * BLOCKS[k]
                    if k == 'gas_turbine' and abs(step) > 1: continue
                    if v < 0 or v > MAXES[k] or not land_ok({**cand, k: v}) or (k in exclude and v > 0): continue
                    cand[k] = v
                    r, j = ev(cand)
                    if j < cj - 1e-6:
                        cur, cr, cj, improved = cand, r, j, True
            if verbose: print(' iter', it, round(cj / 1e6, 2), {k: v for k, v in cur.items() if v})
            if not improved: break
        if best is None or cj < best[1]: best = (cr, cj)
    return best if best is not None else (None, float('inf'))


METRIC_KEYS = ['total_cost', 'local_energy_cost', 'relocation_cost', 'interruption_cost', 'co2_t', 'served_mwh', 'requested_mwh',
               'relocated_mwh', 'interrupted_mwh', 'merchant_mwh', 'clean_mwh', 'intensity']


def entry(res):
    """Compact record of one evaluated portfolio (no hourly data)."""
    return dict(start=res['start'], caps=[res['caps'].get(k, 0.0) for k in KEYS], **{k: round(res[k], 3) for k in METRIC_KEYS})
