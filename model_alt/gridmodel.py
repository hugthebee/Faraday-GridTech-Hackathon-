"""GRIDTECH evaluator: fast hourly dispatch + common accounting for ANY portfolio / opening month.

Method (transparent merit order, not a black-box optimizer)
-----------------------------------------------------------
Every hour all supply options are ranked by marginal cost and filled cheapest-first:
  contracts (solar/wind/nuclear: already paid for, marginal cost ~0)  -> share the import cap
  merchant electricity (hourly price + adder)                         -> shares the import cap
  onsite gas units (variable O&M + heat rate x gas price)             -> no import cap
  Tier-1 relocation ($123.50/MWh) and Tier-2 interruption ($1,000/MWh) -> only inside stress events
Because relocation/interruption are ranked like supply, they are used only when cheaper than the
alternative (e.g. a merchant price spike), and only inside event hours (their availability is zero
otherwise). Tier 0 is never touched (relocation <= Tier 1, interruption <= Tier 2).
The battery is handled in a second pass: charge from otherwise-curtailed clean contract energy,
discharge when the marginal cost of the hour exceeds a threshold. This is a heuristic (see README).
Optional 'carbon shadow price' only changes the dispatch ORDER (cleaner-first); reported costs
always follow the common accounting rules and are validated by starter/check_submission.py.
"""
from __future__ import annotations
import sys
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "starter"))
import check_submission as cs  # unchanged supplied module: inputs + checker

OPT = cs.options.copy()
OPT["option_id"] = OPT["option_id"].astype(str)
OPT = OPT.set_index("option_id", drop=False)
CASE, RESP = cs.case, cs.response
RESOURCES = list(OPT.index)
HOURS_PER_MONTH = CASE["hours_per_model_month"]
EPS = 1e-9


@dataclass(frozen=True)
class Scenario:
    """Operating assumptions that change the physics/dispatch (part of the cache key)."""
    grid_delay_months: int = 0
    tier2_max_fraction: float = 1.0      # 0.0 = never interrupt Tier 2 (Example 4)
    tier1_max_mw: float | None = None    # cap on local-equivalent relocation (None = packet limit)
    carbon_shadow_usd_per_t: float = 0.0  # dispatch ordering only; not added to reported cost
    battery_threshold_usd_per_mwh: float = 100.0

    def key(self) -> str:
        t1 = "pkt" if self.tier1_max_mw is None else f"{self.tier1_max_mw:g}"
        return (f"d{self.grid_delay_months}_t2-{self.tier2_max_fraction:g}_t1-{t1}"
                f"_cp{self.carbon_shadow_usd_per_t:g}_bt{self.battery_threshold_usd_per_mwh:g}")


class Ctx:
    """Per-(opening month, grid delay) arrays, built once with the supplied prepare_inputs()."""
    def __init__(self, start: int, delay: int):
        base = cs.prepare_inputs({"initial_service_month": int(start),
                                  "grid_delay_months": int(delay), "capacities_mw": {}})
        self.start, self.delay, self.n = int(start), int(delay), base["n"]
        self.h0 = self.start * HOURS_PER_MONTH            # first hour with demand
        self.base = base
        for k in ["load", "tier0", "tier1", "tier2", "grid_limit", "relocation_limit",
                  "interruption_limit", "gas_price", "grid_price", "grid_co2", "remote_co2", "month", "year", "active"]:
            setattr(self, k, np.asarray(base[k]))
        self.reloc_price, self.intr_price = base["relocation_price"], base["interruption_price"]
        self.mask, self.prof = {}, {}
        for oid, r in OPT.iterrows():
            self.mask[oid] = cs.available_mask(r, base).astype(float)
            self.prof[oid] = np.asarray(cs.profile(r, base), float)  # per-MW availability incl. date mask
        # gas-fired onsite units (generic: onsite dispatchable); contracts (offsite); storage
        self.gas = [o for o in RESOURCES if OPT.loc[o, "kind"] != "storage" and OPT.loc[o, "location"] == "onsite"]
        self.contracts = [o for o in RESOURCES if OPT.loc[o, "location"] == "offsite"]
        self.storage = [o for o in RESOURCES if OPT.loc[o, "kind"] == "storage"]


@lru_cache(maxsize=64)
def get_ctx(start: int, delay: int) -> Ctx:
    return Ctx(start, delay)


def land_used(caps: dict) -> float:
    return float(sum(caps.get(o, 0) * OPT.loc[o, "land_acres_per_mw"] for o in RESOURCES))


def catalog_ok(caps: dict) -> bool:
    for o in RESOURCES:
        v = caps.get(o, 0)
        r = OPT.loc[o]
        if v < 0 or v > r.max_mw + 1e-9 or abs(v / r.block_mw - round(v / r.block_mw)) > 1e-9:
            return False
    return land_used(caps) <= CASE["onsite_energy_land_acres"] + 1e-9


TOL = 1e-5      # same absolute tolerance as the supplied checker


def validate_caps(caps: dict) -> list:
    """Reasons a capacity vector breaks the catalog or the site (empty list = fine). Mirrors the supplied checker's capacity rules."""
    v = []
    unknown = sorted(set(caps) - set(RESOURCES))
    if unknown:
        v.append(f"unknown resources: {unknown}")
    clean = {}
    for o in RESOURCES:
        try:
            x = float(caps.get(o, 0) or 0)
        except (TypeError, ValueError):
            v.append(f"{o}: not a number"); continue
        if not np.isfinite(x):
            v.append(f"{o}: not finite"); continue
        clean[o] = x
        r = OPT.loc[o]
        if x < -1e-9:
            v.append(f"{o}: negative capacity")
        if x > r.max_mw + 1e-7:
            v.append(f"{o}: {x:g} MW is above the {r.max_mw:g} MW offer")
        if abs(x / r.block_mw - round(x / r.block_mw)) > 1e-7:
            v.append(f"{o}: {x:g} MW is not a multiple of the {r.block_mw:g} MW block")
    land = land_used(clean)
    if land > CASE["onsite_energy_land_acres"] + 1e-7:
        v.append(f"onsite land {land:.2f} acres is above the {CASE['onsite_energy_land_acres']:g} acre limit")
    return v


def validate_dispatch(ctx, caps: dict, res: dict, sc=None) -> list:
    """Physical rules an hourly schedule must obey (empty list = valid). Vectorised; a subset of the supplied checker's rules that covers every
    way this evaluator could go wrong: flows, limits, balance, grid cap and the whole battery schedule. Unserved demand is NOT a violation here
    (it makes the portfolio infeasible through res['feasible'])."""
    sc = Scenario() if sc is None else sc
    v, n = [], ctx.n
    zeros = np.zeros(n)
    merchant, rel, intr = res["grid_mw"], res["tier1_relocated_mw"], res["tier2_curtailed_mw"]
    for name, a in (("merchant", merchant), ("Tier 1 relocation", rel), ("Tier 2 interruption", intr)):
        if np.min(a) < -TOL:
            v.append(f"{name}: negative flow")
    reloc_lim, intr_lim = _tier_limits(ctx, sc)
    if np.max(rel - reloc_lim) > TOL:
        v.append("Tier 1 relocation above its limit (outside events, notice or remote capacity)")
    if np.max(intr - intr_lim) > TOL:
        v.append("Tier 2 interruption above its limit (outside events or share)")
    local = ctx.load - rel - intr
    if np.max(ctx.tier0 - local) > TOL:
        v.append("protected (Tier 0) demand is not served locally")
    contracts_total, clean_total, gas_total = zeros.copy(), zeros.copy(), zeros.copy()
    for o in ctx.contracts:
        g = np.asarray(res.get(o, zeros))
        if np.min(g) < -TOL:
            v.append(f"{o}: negative output")
        if np.max(g - caps.get(o, 0) * ctx.prof[o]) > TOL:
            v.append(f"{o}: output above its available generation")
        contracts_total = contracts_total + g
        if OPT.loc[o, "clean_credit"]:
            clean_total = clean_total + g
    for o in ctx.gas:
        g = np.asarray(res.get(o, zeros))
        if np.min(g) < -TOL:
            v.append(f"{o}: negative output")
        if np.max(g - caps.get(o, 0) * ctx.mask[o]) > TOL:
            v.append(f"{o}: output above installed capacity or before its available month")
        gas_total = gas_total + g
    if np.max(contracts_total + merchant - ctx.grid_limit) > TOL:
        v.append("grid import cap exceeded (contracts + merchant)")
    bat = res.get("battery")
    ch = dis = soc = zeros
    P = float(caps.get(ctx.storage[0], 0)) if ctx.storage else 0.0
    if bat:
        ch, dis, soc = np.asarray(res["charge"]), np.asarray(res["discharge"]), np.asarray(res["soc"])
    for name, a in (("battery charge", ch), ("battery discharge", dis), ("battery state of charge", soc)):
        if np.min(a) < -TOL:
            v.append(f"{name}: negative")
    if ctx.storage:
        b = ctx.storage[0]
        o = OPT.loc[b]
        eta = float(np.sqrt(o.round_trip_efficiency))
        m = ctx.mask[b]
        if P <= 0 and (np.max(ch) > TOL or np.max(dis) > TOL or np.max(soc) > TOL):
            v.append("battery used although none is selected")
        if np.max(ch - P * m) > TOL or np.max(dis - P * m) > TOL:
            v.append("battery power above its rating or before its available month")
        if np.max(soc - P * o.duration_hours * m) > TOL:
            v.append("battery state of charge above its energy capacity")
        if abs(float(soc[-1])) > TOL:
            v.append("battery does not end the horizon at zero charge")
        if np.max(np.abs(soc - np.concatenate([[0.0], soc[:-1]]) - eta * ch + dis / eta)) > TOL:
            v.append("battery energy is not conserved from hour to hour")
        if np.max(np.minimum(ch, dis)) > TOL:
            v.append("battery charges and discharges in the same hour")
        if np.max(ch - clean_total) > TOL:
            v.append("battery charged from more than the clean contract energy dispatched that hour")
        for y in range(CASE["horizon_months"] // 12):
            sl = ctx.year == y
            allowed = P * o.duration_hours * o.max_cycles_per_year * float(m[sl].sum()) / 8760
            if float(dis[sl].sum()) / eta > allowed + TOL:
                v.append(f"battery annual cycle limit exceeded in model year {y + 1}")
                break
    unmet = np.asarray(res.get("unmet_mw", zeros))
    balance = contracts_total - ch + merchant + gas_total + dis - local
    if np.max(np.abs(balance + unmet)) > TOL:
        v.append("hourly energy balance does not close")
    return v


def _tier_limits(ctx: Ctx, sc: Scenario):
    reloc = ctx.relocation_limit if sc.tier1_max_mw is None else np.minimum(ctx.relocation_limit, sc.tier1_max_mw)
    intr = ctx.interruption_limit * sc.tier2_max_fraction
    return reloc, intr


def adequacy_ok(ctx: Ctx, caps: dict, sc: Scenario) -> bool:
    """Cheap necessary test (almost sufficient): in every hour, onsite power must cover
    load - max relocation - max interruption - grid cap. Contracts cannot lift the cap."""
    reloc, intr = _tier_limits(ctx, sc)
    s = slice(ctx.h0, ctx.n)
    need = ctx.load[s] - reloc[s] - intr[s] - ctx.grid_limit[s]
    if need.max() <= 1e-7:
        return True
    avail = np.zeros(ctx.n - ctx.h0)
    for o in ctx.gas:
        avail += caps.get(o, 0) * ctx.mask[o][s]
    # a 4-hour battery can only bridge short gaps: count it during stress events, not sustained gaps
    for o in ctx.storage:
        avail += caps.get(o, 0) * ctx.mask[o][s] * ctx.active[s]
    return bool(np.all(avail >= need - 1e-7))


def _fill(avail, cost, imp, need, G):
    """Vectorised cheapest-first fill with a shared import cap. Returns take matrix, unmet, imports."""
    m, k = avail.shape
    order = np.argsort(cost, axis=1, kind="stable")
    take = np.zeros((m, k))
    imports = np.zeros(m)
    need = need.copy()
    ar = np.arange(m)
    for r in range(k):
        j = order[:, r]
        a = avail[ar, j]
        isimp = imp[j]
        a = np.where(isimp, np.minimum(a, np.maximum(G - imports, 0.0)), a)
        t = np.maximum(np.minimum(a, need), 0.0)
        take[ar, j] = t
        need -= t
        imports += np.where(isimp, t, 0.0)
    return take, need, imports


def dispatch(ctx: Ctx, caps: dict, sc: Scenario = Scenario()) -> dict:
    n, h0 = ctx.n, ctx.h0
    s = slice(h0, n)
    m = n - h0
    lam = sc.carbon_shadow_usd_per_t
    reloc_lim, intr_lim = _tier_limits(ctx, sc)
    names, av, co, im = [], [], [], []
    for o in ctx.contracts:
        mw = caps.get(o, 0)
        if mw > 0:
            names.append(o); av.append(mw * ctx.prof[o][s]); co.append(np.zeros(m)); im.append(True)
    names.append("__merchant__"); av.append(np.full(m, 1e9))
    co.append(ctx.grid_price[s] + lam * ctx.grid_co2[s]); im.append(True)
    for o in ctx.gas:
        mw = caps.get(o, 0)
        if mw > 0:
            r = OPT.loc[o]
            names.append(o); av.append(mw * ctx.mask[o][s])
            co.append(r.payment_usd_per_mwh + r.heat_rate_mmbtu_per_mwh_hhv * ctx.gas_price[s] + lam * r.co2_t_per_mwh)
            im.append(False)
    names.append("__reloc__"); av.append(reloc_lim[s])
    co.append(ctx.reloc_price + lam * RESP["remote_energy_multiplier"] * ctx.remote_co2[s]); im.append(False)
    names.append("__intr__"); av.append(intr_lim[s]); co.append(np.full(m, ctx.intr_price)); im.append(False)
    A, C, I = np.column_stack(av), np.column_stack(co), np.array(im)
    G = ctx.grid_limit[s]
    load = ctx.load[s]
    take, unmet, imports = _fill(A, C, I, load, G)

    # ---- battery second pass ------------------------------------------------------------------
    bat = next((o for o in ctx.storage if caps.get(o, 0) > 0), None)
    ch = dis = soc_arr = None
    if bat:
        r = OPT.loc[bat]; P = float(caps[bat]); E = P * r.duration_hours; eta = np.sqrt(r.round_trip_efficiency)
        mb = ctx.mask[bat][s] > 0
        thr = sc.battery_threshold_usd_per_mwh
        expensive = (take * (C > thr)).sum(axis=1)
        cidx = [i for i, nme in enumerate(names) if nme in ctx.contracts]
        unused = (A[:, cidx] - take[:, cidx]).sum(axis=1) if cidx else np.zeros(m)
        surplus = np.minimum(unused, np.maximum(G - imports, 0.0))
        yr = ctx.year[s]
        allow = {y: P * r.duration_hours * r.max_cycles_per_year * mb[yr == y].sum() / 8760 * 0.999999
                 for y in np.unique(yr)}
        used = {y: 0.0 for y in allow}
        exp_l, sur_l, mb_l, yr_l, ld_l = expensive.tolist(), surplus.tolist(), mb.tolist(), yr.tolist(), load.tolist()
        chl, dil, soc_l = [0.0] * m, [0.0] * m, [0.0] * m
        soc = 0.0
        tail = m - 6
        for i in range(m):
            if mb_l[i]:
                if i >= tail:                                   # drain so final SOC = 0
                    if soc > 1e-12:
                        d = min(P, soc * eta, ld_l[i]); dil[i] = d; soc -= d / eta
                elif exp_l[i] > EPS and soc > 1e-12:
                    y = yr_l[i]
                    d = min(P, soc * eta, exp_l[i], max((allow[y] - used[y]) * eta, 0.0))
                    if d > EPS:
                        dil[i] = d; soc -= d / eta; used[y] += d / eta
                elif sur_l[i] > EPS and soc < E - 1e-12:
                    c = min(P, sur_l[i], (E - soc) / eta)
                    chl[i] = c; soc += eta * c
            soc_l[i] = soc if soc > 1e-12 else 0.0
        ch, dis, soc_arr = np.array(chl), np.array(dil), np.array(soc_l)
        A2 = np.column_stack([A, dis]); C2 = np.column_stack([C, np.full(m, -1e12)]); I2 = np.append(I, False)
        take2, unmet, imports = _fill(A2, C2, I2, load, G)
        take = take2[:, :-1]
        # charging energy is extra clean-contract dispatch (uses otherwise-curtailed contract output)
        rem = ch.copy()
        for i in cidx:
            ex = np.minimum(A[:, i] - take[:, i], rem)
            take[:, i] += ex; rem -= ex; imports += ex
    # ---- assemble full-length output ------------------------------------------------------------
    full = lambda v: np.concatenate([np.zeros(h0), v])
    out = {"unmet_mw": full(unmet), "imports": full(imports)}
    for j, nme in enumerate(names):
        out[nme] = full(take[:, j])
    out["grid_mw"] = out.pop("__merchant__")
    out["tier1_relocated_mw"] = out.pop("__reloc__")
    out["tier2_curtailed_mw"] = out.pop("__intr__")
    for o in RESOURCES:
        if o not in out and OPT.loc[o, "kind"] != "storage":
            out[o] = np.zeros(n)
    if bat:
        out["charge"], out["discharge"], out["soc"] = full(ch), full(dis), full(soc_arr)
    out["battery"] = bat
    out["violations"] = validate_caps(caps) + validate_dispatch(ctx, caps, out, sc)
    out["feasible"] = bool(unmet.max() <= 1e-6) and not out["violations"]      # an invalid portfolio or schedule is never "feasible"
    return out


def account(ctx: Ctx, caps: dict, res: dict, by_year: bool = False) -> dict:
    """Common accounting; formulas mirror starter/check_submission.py::score."""
    n = ctx.n
    cost, co2, clean = np.zeros(n), np.zeros(n), np.zeros(n)
    ledger = []
    for o in RESOURCES:
        r = OPT.loc[o]; mw = caps.get(o, 0)
        crf = CASE["discount_rate_real"] / (1 - (1 + CASE["discount_rate_real"]) ** (-r.life_years))
        fixed = ctx.mask[o] * (mw * 1000 * (r.capex_usd_per_kw * crf + r.fom_usd_per_kw_year)) / 8760
        gen = np.zeros(n) if r.kind == "storage" else res.get(o, np.zeros(n))
        billed = mw * ctx.prof[o] if r.payment_basis == "available_generation" else gen
        pay = billed * r.payment_usd_per_mwh
        fuel = gen * ctx.gas_price * r.heat_rate_mmbtu_per_mwh_hhv
        cost += fixed + pay + fuel
        co2 += gen * r.co2_t_per_mwh
        if r.clean_credit and r.kind != "storage":
            clean += gen
        if r.kind == "storage" and res.get("battery"):
            clean += res["discharge"] - res["charge"]
        if mw > 0:
            ledger.append({"option_id": o, "selected_mw": float(mw), "capacity_charges_usd": float(fixed.sum()),
                           "energy_payment_or_variable_om_usd": float(pay.sum()), "fuel_cost_usd": float(fuel.sum())})
    grid = res["grid_mw"]
    cost += grid * ctx.grid_price
    co2 += grid * ctx.grid_co2
    rel, intr = res["tier1_relocated_mw"], res["tier2_curtailed_mw"]
    remote = rel * RESP["remote_energy_multiplier"]
    local = ctx.load - rel - intr
    rc, ic = rel * ctx.reloc_price, intr * ctx.intr_price
    rco2 = remote * ctx.remote_co2

    def agg(msk):
        S = lambda a: float(a[msk].sum())
        req, srv, rem_, itr = S(ctx.load), S(local), S(remote), S(intr)
        lc, rr, ii, ce, re_, cm = S(cost), S(rc), S(ic), S(co2), S(rco2), S(clean)
        dv = lambda a, b: a / b if b > 0 else None
        return {"requested_mwh": req, "local_served_mwh": srv, "relocated_local_equivalent_mwh": S(rel),
                "remote_electricity_mwh": rem_, "interrupted_mwh": itr,
                "local_energy_cost_usd": lc, "relocation_cost_usd": rr, "interruption_consequence_usd": ii,
                "total_decision_cost_usd": lc + rr + ii,
                "local_energy_cost_usd_per_mwh": dv(lc, srv), "decision_cost_usd_per_requested_mwh": dv(lc + rr + ii, req),
                "local_operational_co2_t": ce, "remote_operational_co2_t": re_, "combined_operational_co2_t": ce + re_,
                "combined_electricity_co2_kg_per_mwh": dv(1000 * (ce + re_), srv + rem_),
                "clean_energy_mwh": cm, "clean_energy_share": dv(cm, srv), "merchant_mwh": S(grid),
                "merchant_share_of_local": dv(S(grid), srv)}
    tot = agg(np.ones(n, bool))
    tot.update(initial_service_month=ctx.start, full_demand_month=ctx.start + CASE["ramp_duration_months"],
               grid_delay_months=ctx.delay, land_acres=land_used(caps), feasible=res["feasible"],
               max_grid_import_mw=float(res["imports"].max()), violations=list(res.get("violations", [])), ledger=ledger)
    if by_year:
        tot["annual"] = [{"model_year": y + 1, **agg(ctx.year == y)} for y in range(CASE["horizon_months"] // 12)]
    return tot


def evaluate(start: int, caps: dict, sc: Scenario = Scenario(), by_year: bool = False):
    """One-call evaluation. Returns (metrics, dispatch_result, ctx)."""
    ctx = get_ctx(int(start), int(sc.grid_delay_months))
    res = dispatch(ctx, caps, sc)
    return account(ctx, caps, res, by_year), res, ctx


def to_dispatch_frame(ctx: Ctx, caps: dict, res: dict) -> pd.DataFrame:
    cols = cs.required_columns()
    d = {"model_hour": np.arange(ctx.n)}
    for c in cols[1:]:
        d[c] = np.zeros(ctx.n)
    d["grid_mw"], d["tier1_relocated_mw"], d["tier2_curtailed_mw"] = res["grid_mw"], res["tier1_relocated_mw"], res["tier2_curtailed_mw"]
    for o in RESOURCES:
        if OPT.loc[o, "kind"] == "storage":
            if res.get("battery") == o:
                d[o + "_charge_mw"], d[o + "_discharge_mw"], d[o + "_soc_mwh"] = res["charge"], res["discharge"], res["soc"]
        else:
            d[o + "_mw"] = res[o]
    return pd.DataFrame(d)[cols]


def export_submission(start: int, caps: dict, sc: Scenario, outdir: Path) -> dict:
    """Write portfolio.json + dispatch.csv and run the supplied checker; returns its accounting."""
    import json
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    metrics, res, ctx = evaluate(start, caps, sc)
    if not res["feasible"]:
        raise ValueError("portfolio is infeasible under this scenario; nothing to export")
    pf = {"initial_service_month": int(start), "grid_delay_months": int(sc.grid_delay_months),
          "capacities_mw": {o: float(caps.get(o, 0)) for o in RESOURCES}}
    (outdir / "portfolio.json").write_text(json.dumps(pf, indent=2))
    frame = to_dispatch_frame(ctx, caps, res)
    frame.to_csv(outdir / "dispatch.csv", index=False, float_format="%.9f")
    checked = cs.score(pf, pd.read_csv(outdir / "dispatch.csv"))
    (outdir / "check_results.json").write_text(__import__("json").dumps(checked, indent=2))
    return checked
