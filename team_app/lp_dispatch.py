"""Exact (LP) dispatch for a given portfolio, on the packet's common rules and the alternative evaluator's accounting.

Why an LP: without storage every hour is independent and the cheapest-first fill (gridmodel.dispatch) is already
cost-optimal. A battery couples the hours through its state of charge, so its best use is a linear program.

Formulation (per model year, hours from the opening month on; SOC = 0 at each year boundary, which is a small restriction
because the packet only requires SOC = 0 at the horizon end):
  fixed   : contract energy serving load = the no-battery merit-order dispatch (zero marginal cost, already paid for)
  choose  : merchant m, gas units (fuel cell / engine / turbine), Tier-1 relocation, Tier-2 interruption,
            battery discharge d, battery charge c, state of charge s
  balance : m + gas + relocation + interruption + d = load - contracts_to_load            (every hour, no unmet demand)
  charge  : c <= contract energy left over after serving load ("spare"), so charging never uses merchant or fossil power
  grid cap: m + c <= import cap - contracts_to_load                                       (merchant and contracts share the cap)
  battery : s_t = s_{t-1} + eta*c_t - d_t/eta, 0 <= s <= 4P, c,d <= P, annual discharge cycles <= packet limit
  cost    : merchant price, gas variable O&M + fuel, relocation $/MWh, interruption $/MWh (fixed charges do not depend on dispatch)
A tiny cost on c and d removes pointless simultaneous charge/discharge. Relocation and interruption exist only in stress events
(their limits are zero otherwise), so Tier 0 is never touched.
"""
import sys
from pathlib import Path

import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alt_adapter as A  # noqa: E402

gm = A.gm
BAT = 'battery_4h'
EPS_COST = 1e-4


def _solve_year(args):
    """One year's LP. All inputs are plain arrays so this can run in a worker process."""
    (R, H, spare, price, gas_cost, gas_cap, reloc_lim, intr_lim, reloc_price, intr_price, P_t, Pc_t, E_t, eta, allow) = args
    n = len(R)
    g = len(gas_cost)
    nb = 1 + g + 2 + 3                                     # merchant, gas..., reloc, intr, d, c, s
    iM, iG0, iRL, iIN, iD, iC, iS = 0, 1, 1 + g, 2 + g, 3 + g, 4 + g, 5 + g
    N = nb * n
    idx = lambda k: np.arange(n) + k * n
    cost = np.zeros(N)
    cost[idx(iM)] = price
    for j in range(g):
        cost[idx(iG0 + j)] = gas_cost[j]
    cost[idx(iRL)] = reloc_price
    cost[idx(iIN)] = intr_price
    cost[idx(iD)] = EPS_COST
    cost[idx(iC)] = EPS_COST
    ub = np.full(N, np.inf)
    ub[idx(iM)] = H
    for j in range(g):
        ub[idx(iG0 + j)] = gas_cap[j]
    ub[idx(iRL)] = reloc_lim
    ub[idx(iIN)] = intr_lim
    ub[idx(iD)] = P_t
    ub[idx(iC)] = Pc_t
    ub[idx(iS)] = E_t
    ub[iS * n + n - 1] = 0.0                               # SOC ends at 0
    t = np.arange(n)
    rows, cols, vals = [], [], []
    for k in list(range(iM, iIN + 1)) + [iD]:              # balance: all supply + discharge = R
        rows.append(t); cols.append(idx(k)); vals.append(np.ones(n))
    # SOC: s_t - s_{t-1} - eta*c + d/eta = 0
    rows.append(n + t); cols.append(idx(iS)); vals.append(np.ones(n))
    rows.append(n + t[1:]); cols.append(idx(iS)[:-1]); vals.append(-np.ones(n - 1))
    rows.append(n + t); cols.append(idx(iC)); vals.append(np.full(n, -eta))
    rows.append(n + t); cols.append(idx(iD)); vals.append(np.full(n, 1 / eta))
    A_eq = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(2 * n, N))
    b_eq = np.concatenate([R, np.zeros(n)])
    # headroom m + c <= H (H already folded into ub of m only when c = 0, so add the coupling row) and the cycle limit
    r2 = np.concatenate([t, t, np.full(n, n)])
    c2 = np.concatenate([idx(iM), idx(iC), idx(iD)])
    v2 = np.concatenate([np.ones(n), np.ones(n), np.full(n, 1 / eta)])
    A_ub = sp.csr_matrix((v2, (r2, c2)), shape=(n + 1, N))
    b_ub = np.concatenate([H, [allow]])
    out = linprog(cost, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=np.column_stack([np.zeros(N), ub]), method='highs')
    if out.status != 0:
        return None
    x = out.x.reshape(nb, n)
    return dict(m=x[iM], gas=x[iG0:iG0 + g], rl=x[iRL], intr=x[iIN], d=x[iD], c=x[iC], s=x[iS], obj=out.fun)


def lp_dispatch(start, caps, delay=0, reloc=True, intr=True, workers=8, return_hourly=True):
    """Return (metrics, res) where res has the gridmodel.dispatch layout (so gm.account / to_dispatch_frame work)."""
    caps = {k: float(v) for k, v in caps.items()}
    sc = A.scenario(delay, reloc, intr)
    ctx = gm.get_ctx(int(start), int(delay))
    P = caps.get(BAT, 0.0)
    res0 = gm.dispatch(ctx, {**caps, BAT: 0.0}, sc)                    # exact when there is no storage
    if P <= 0:
        return gm.account(ctx, caps, res0), res0
    n, h0 = ctx.n, ctx.h0
    opt = gm.OPT.loc[BAT]
    eta = float(np.sqrt(opt.round_trip_efficiency))
    reloc_lim, intr_lim = gm._tier_limits(ctx, sc)
    contracts = [o for o in ctx.contracts if caps.get(o, 0) > 0]
    avail = {o: caps[o] * ctx.prof[o] for o in contracts}
    g_pre = sum((res0[o] for o in contracts), np.zeros(n))
    spare_k = {o: np.maximum(avail[o] - res0[o], 0.0) for o in contracts}
    spare = sum(spare_k.values(), np.zeros(n))
    R = ctx.load - g_pre
    H = np.maximum(ctx.grid_limit - g_pre, 0.0)
    mask = ctx.mask[BAT]
    gas_cost = [gm.OPT.loc[o].payment_usd_per_mwh + gm.OPT.loc[o].heat_rate_mmbtu_per_mwh_hhv * ctx.gas_price for o in ctx.gas]
    gas_cap = [caps.get(o, 0.0) * ctx.mask[o] for o in ctx.gas]
    jobs, spans = [], []
    for y in range(n // 8760):
        lo, hi = max(y * 8760, h0), (y + 1) * 8760
        if hi <= lo:
            continue
        sl = slice(lo, hi)
        allow = P * opt.duration_hours * opt.max_cycles_per_year * mask[y * 8760:(y + 1) * 8760].sum() / 8760 * 0.999999
        jobs.append((R[sl], H[sl], np.minimum(spare[sl], P) * mask[sl], ctx.grid_price[sl], [c[sl] for c in gas_cost], [c[sl] for c in gas_cap],
                     reloc_lim[sl], intr_lim[sl], ctx.reloc_price, ctx.intr_price, P * mask[sl], np.minimum(spare[sl], P) * mask[sl], P * opt.duration_hours * mask[sl], eta, allow))
        spans.append(sl)
    if workers > 1 and len(jobs) > 1:
        import multiprocessing as mp
        with mp.get_context('fork').Pool(min(workers, len(jobs))) as pool:
            sols = pool.map(_solve_year, jobs)
    else:
        sols = [_solve_year(j) for j in jobs]
    if any(s is None for s in sols):
        raise RuntimeError('LP infeasible: portfolio cannot serve the demand')
    z = lambda: np.zeros(n)
    m_, rl, itr, d, c, s = z(), z(), z(), z(), z(), z()
    gas_out = {o: z() for o in ctx.gas}
    for sl, sol in zip(spans, sols):
        m_[sl], rl[sl], itr[sl], d[sl], c[sl], s[sl] = sol['m'], sol['rl'], sol['intr'], sol['d'], sol['c'], sol['s']
        for j, o in enumerate(ctx.gas):
            gas_out[o][sl] = sol['gas'][j]
    for arr in (m_, rl, itr, d, c, s):
        np.clip(arr, 0.0, None, out=arr)
    res = dict(res0)
    for o in contracts:                                                # charging energy comes out of each contract's leftover
        share = np.divide(spare_k[o], spare, out=np.zeros(n), where=spare > 1e-12)
        res[o] = res0[o] + c * share
    for o in ctx.gas:
        res[o] = gas_out[o]
    res['grid_mw'], res['tier1_relocated_mw'], res['tier2_curtailed_mw'] = m_, rl, itr
    res['charge'], res['discharge'], res['soc'], res['battery'] = c, d, s, BAT
    res['imports'] = g_pre + c + m_
    res['unmet_mw'] = np.zeros(n)
    res['violations'] = gm.validate_caps(caps) + gm.validate_dispatch(ctx, caps, res, sc)
    if res['violations']:
        raise RuntimeError('LP produced an invalid schedule: ' + '; '.join(res['violations']))
    res['feasible'] = True
    return gm.account(ctx, caps, res), res


def battery_scan(start, caps, sizes=(0, 3, 6, 12), delay=0, reloc=True, intr=True, workers=8):
    """LP-optimal total cost for several battery sizes on an otherwise fixed portfolio (land limit respected)."""
    rows = []
    for mw in sizes:
        c = {**{k: float(v) for k, v in caps.items()}, BAT: float(mw)}
        if not gm.catalog_ok(c):
            continue
        m, res = lp_dispatch(start, c, delay, reloc, intr, workers)
        rows.append(dict(battery_mw=float(mw), total_cost=m['total_decision_cost_usd'], co2_kg_per_mwh=m['combined_electricity_co2_kg_per_mwh'],
                         interrupted_mwh=m['interrupted_mwh'], relocated_mwh=m['relocated_local_equivalent_mwh'],
                         discharge_mwh=float(res['discharge'].sum()) if mw > 0 else 0.0))
    for r in rows:
        r['vs_no_battery'] = r['total_cost'] - rows[0]['total_cost'] if rows and rows[0]['battery_mw'] == 0 else float('nan')
    return rows
