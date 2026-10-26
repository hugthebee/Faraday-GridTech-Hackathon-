"""INDEPENDENT CROSS-CHECK ENGINE (not part of our model).

Verbatim functions from model/GridTech.ipynb (first code cell, everything before its RUNNER section; the tkinter folder dialog and the charts are
left out). It solves one hourly LP per model year for a fixed portfolio and has no code in common with our evaluator, so agreement between the two
is a real validation. Used only by validate_model.py. Known differences: constants are hard-coded to the packet benchmark (no custom cases), and its
battery may charge in hours when merchant power also serves the load (allowed by the checker's literal rule, stricter in our LP).
"""
"""
GRIDTECH - yearly dispatch engine + runner in ONE file.

Jupyter: paste into a cell, or save as gridtech_all.py and `%run gridtech_all.py`.
Edit DATA_DIR / INITIAL_SERVICE_MONTH / CAPS in the "EDIT HERE" block near the bottom.
"""
# GRIDTECH yearly dispatch engine.
# 
# For a FIXED portfolio (capacities chosen upfront for the whole horizon), solve one
# hourly dispatch LP per model year (10 x 8,760 h) with HiGHS via scipy.linprog,
# stitch the years into the 87,600-hour dispatch.csv, and compute common accounting.
# 
# Year decomposition assumption: battery SOC is forced to 0 at the end of every
# model year (and so starts each year at 0). The rules only require SOC=0 at hour 0
# and at the final hour, so this is a conservative restriction - every schedule it
# produces is still valid under the common rules, it just leaves a little
# cross-year storage value on the table (negligible for a 4-hour battery).

from pathlib import Path
import numpy as np
import pandas as pd
import scipy.sparse as sp
from scipy.optimize import linprog

# ---------------------------------------------------------------- constants
HPY, HPM, HORIZON_H = 8760, 730, 87600
ETA = np.sqrt(0.85)                         # one-way battery efficiency
REMOTE_MW_LOCAL = 12.0 / 1.05               # local-equivalent relocation cap
RELOC_USD, INTERRUPT_USD = 1.05 * 70 + 50, 1000.0
REMOTE_CO2_0 = 0.33287420383294             # t/MWh at model_hour 0
RATE_WHOLESALE, RATE_HH, RATE_CO2 = 0.05, 0.03, -0.03
CRF_R = 0.08
UNSERVED_PENALTY = 1e5                      # $/MWh slack: flags an inadequate portfolio
EPS = 1e-3                                  # tiny battery throughput cost -> no simultaneous ch/dis
GRID_SCHEDULE = [(0, 0), (18, 25), (24, 50), (30, 75)]
LAND_ACRES = 8.0

RES_ORDER = ["utility_solar", "wind", "nuclear", "gas_turbine", "gas_engine", "fuel_cell", "battery_4h"]
GAS = ["gas_turbine", "gas_engine", "fuel_cell"]
CLEAN = ["utility_solar", "wind", "nuclear"]
OFFSITE = CLEAN
PROFILE = {"utility_solar": "solar_proxy_cf", "wind": "wind_proxy_cf",
           "nuclear": "firm_clean_available_fraction"}

# LP variable blocks (each block has n = hours-in-year entries)
VARS = ["grid", "utility_solar", "wind", "nuclear", "gas_turbine", "gas_engine",
        "fuel_cell", "ch", "dis", "soc", "reloc", "curt", "unserved"]
IDX = {v: i for i, v in enumerate(VARS)}


# ---------------------------------------------------------------- inputs
def load_resources(data_dir):
    r = pd.read_csv(Path(data_dir) / "resource_options.csv").set_index("option_id")
    return r


def build_timeline(data_dir, initial_service_month=20, grid_delay_months=0):
    """Expand reference_hourly.csv to 87,600 h for any opening month (MODEL_RULES s1, s2, s5)."""
    ref = pd.read_csv(Path(data_dir) / "reference_hourly.csv")
    h = np.arange(HORIZON_H)
    rh = h % HPY
    month = h // HPM
    R = ref.iloc[rh].reset_index(drop=True)

    k = month - initial_service_month
    frac = np.where(k < 0, 0.0, np.minimum(1.0, 1 / 3 + k / 15))

    cap = np.zeros(HORIZON_H)
    for m0, mw in GRID_SCHEDULE:
        start = m0 + (grid_delay_months if mw > 0 else 0)
        cap[month >= start] = mw

    yrs = h / HPY
    t = pd.DataFrame({
        "model_hour": h, "development_month": month, "model_year": h // HPY + 1,
        "reference_hour": rh, "demand_fraction": frac,
        "load_mw": R.load_mw * frac,
        "tier0_protected_mw": R.tier0_protected_mw * frac,
        "tier1_relocatable_mw": R.tier1_relocatable_mw * frac,
        "tier2_interruptible_mw": R.tier2_interruptible_mw * frac,
        "ordinary_grid_limit_mw": cap,
        "effective_grid_limit_mw": cap * R.stress_import_fraction,
        "stress_active": R.stress_active,
        "wind_proxy_cf": R.wind_proxy_cf, "solar_proxy_cf": R.solar_proxy_cf,
        "firm_clean_available_fraction": R.firm_clean_available_fraction,
        "grid_co2_t_per_mwh": R.grid_co2_t_per_mwh * (1 + RATE_CO2) ** yrs,
        "merchant_price_usd_per_mwh": R.wholesale_price_usd_per_mwh * (1 + RATE_WHOLESALE) ** yrs
                                      + R.grid_adder_usd_per_mwh,
        "delivered_gas_price_usd_per_mmbtu": R.henry_hub_usd_per_mmbtu * (1 + RATE_HH) ** yrs
                                             + R.delivered_gas_adder_usd_per_mmbtu,
    })
    return t


def availability(res, caps, t, start_month):
    """1/0 mask per resource: on from max(initial_service_month, available_month)."""
    mon = t.development_month.values
    return {r: ((mon >= max(start_month, res.loc[r, "available_month"])) & (caps.get(r, 0) > 0)).astype(float)
            for r in RES_ORDER}


def check_portfolio(res, caps):
    for r, mw in caps.items():
        b, mx = res.loc[r, "block_mw"], res.loc[r, "max_mw"]
        assert mw >= 0 and mw <= mx and abs(mw / b - round(mw / b)) < 1e-9, f"{r}: {mw} MW breaks block/max"
    land = sum(mw * res.loc[r, "land_acres_per_mw"] for r, mw in caps.items())
    assert land <= LAND_ACRES + 1e-9, f"land {land:.2f} acres > 8"
    return land


# ---------------------------------------------------------------- one-year LP
def solve_year(t_year, res, caps, avail_year, zero_tier2=False):
    """Hourly dispatch LP for one model year. Returns DataFrame of dispatch + status."""
    n = len(t_year)
    nv = len(VARS) * n
    col = lambda v: IDX[v] * n + np.arange(n)
    I = np.arange(n)

    load = t_year.load_mw.values
    stress = t_year.stress_active.values.astype(bool)
    gp = t_year.delivered_gas_price_usd_per_mmbtu.values
    mw_b = caps.get("battery_4h", 0)

    # ---- objective (only avoidable / variable costs; contract & capacity charges are sunk)
    c = np.zeros(nv)
    c[col("grid")] = t_year.merchant_price_usd_per_mwh.values
    for g in GAS:
        c[col(g)] = res.loc[g, "payment_usd_per_mwh"] + res.loc[g, "heat_rate_mmbtu_per_mwh_hhv"] * gp
    c[col("ch")] = EPS
    c[col("dis")] = EPS
    c[col("reloc")] = RELOC_USD
    c[col("curt")] = INTERRUPT_USD
    c[col("unserved")] = UNSERVED_PENALTY

    # ---- bounds
    lb, ub = np.zeros(nv), np.zeros(nv)
    ub[col("grid")] = np.inf                                     # capped by import row below
    for r in CLEAN:
        ub[col(r)] = caps.get(r, 0) * t_year[PROFILE[r]].values * avail_year[r]
    for g in GAS:
        ub[col(g)] = caps.get(g, 0) * avail_year[g]
    ub[col("ch")] = mw_b * avail_year["battery_4h"]
    ub[col("dis")] = mw_b * avail_year["battery_4h"]
    ub[col("soc")] = 4 * mw_b * avail_year["battery_4h"]
    ub[col("soc")[-1]] = 0.0                                     # year-end SOC = 0
    ub[col("reloc")] = np.where(stress, np.minimum(t_year.tier1_relocatable_mw.values, REMOTE_MW_LOCAL), 0)
    ub[col("curt")] = 0 if zero_tier2 else np.where(stress, t_year.tier2_interruptible_mw.values, 0)
    ub[col("unserved")] = load

    rows, cols, vals, rhs_eq, rhs_ub = [], [], [], [], []

    # ---- equality: energy balance  (supply + dis - ch + reloc + curt + unserved = load)
    A_eq = []
    blocks = [("grid", 1), ("utility_solar", 1), ("wind", 1), ("nuclear", 1), ("gas_turbine", 1),
              ("gas_engine", 1), ("fuel_cell", 1), ("dis", 1), ("ch", -1), ("reloc", 1),
              ("curt", 1), ("unserved", 1)]
    r_, c_, v_ = [], [], []
    for v, s in blocks:
        r_.append(I); c_.append(col(v)); v_.append(np.full(n, s, float))
    # ---- equality: SOC dynamics  soc_t - soc_{t-1} - eta*ch_t + dis_t/eta = 0
    r_ += [n + I, n + I[1:], n + I, n + I]
    c_ += [col("soc"), col("soc")[:-1], col("ch"), col("dis")]
    v_ += [np.ones(n), -np.ones(n - 1), np.full(n, -ETA), np.full(n, 1 / ETA)]
    A_eq = sp.csr_matrix((np.concatenate(v_), (np.concatenate(r_), np.concatenate(c_))), shape=(2 * n, nv))
    b_eq = np.concatenate([load, np.zeros(n)])

    # ---- inequalities
    r_, c_, v_ = [], [], []
    # import cap: grid + offsite contracts <= effective limit
    for v in ["grid"] + OFFSITE:
        r_.append(I); c_.append(col(v)); v_.append(np.ones(n))
    # charge <= contemporaneous clean dispatch
    r_.append(n + I); c_.append(col("ch")); v_.append(np.ones(n))
    for v in CLEAN:
        r_.append(n + I); c_.append(col(v)); v_.append(-np.ones(n))
    # annual cycle limit: sum(dis/eta) <= 365*avail_h/8760 * 4*MW
    r_.append(np.full(n, 2 * n)); c_.append(col("dis")); v_.append(np.full(n, 1 / ETA))
    A_ub = sp.csr_matrix((np.concatenate(v_), (np.concatenate(r_), np.concatenate(c_))), shape=(2 * n + 1, nv))
    avail_h = avail_year["battery_4h"].sum()
    b_ub = np.concatenate([t_year.effective_grid_limit_mw.values, np.zeros(n),
                           [365 * avail_h / HPY * 4 * mw_b]])

    sol = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=np.column_stack([lb, ub]), method="highs")
    if sol.status != 0:
        raise RuntimeError(f"LP failed: {sol.message}")
    x = np.clip(sol.x, 0, None)
    out = pd.DataFrame({v: x[col(v)] for v in VARS})
    out.loc[out.soc < 1e-7, "soc"] = 0.0
    return out


# ---------------------------------------------------------------- full horizon
def run_portfolio(t, res, caps, start_month, zero_tier2=False, verbose=True):
    caps = {r: caps.get(r, 0) for r in RES_ORDER}
    check_portfolio(res, caps)
    av = availability(res, caps, t, start_month)
    parts = []
    for y in range(1, 11):
        m = (t.model_year == y).values
        ty = t[m].reset_index(drop=True)
        ay = {r: a[m] for r, a in av.items()}
        if ty.load_mw.sum() == 0:                       # nothing to serve this year
            parts.append(pd.DataFrame(0.0, index=range(len(ty)), columns=VARS))
        else:
            parts.append(solve_year(ty, res, caps, ay, zero_tier2))
        if verbose:
            print(f"  year {y:2d} solved, unserved {parts[-1].unserved.sum():8.1f} MWh")
    d = pd.concat(parts, ignore_index=True)
    d.insert(0, "model_hour", t.model_hour.values)
    return d, av


def crf(life):
    return CRF_R / (1 - (1 + CRF_R) ** (-life))


def accounting(t, res, caps, d, av):
    """Common accounting per model year + horizon (MODEL_RULES s5-6)."""
    caps = {r: caps.get(r, 0) for r in RES_ORDER}
    yrs = t.model_hour.values / HPY
    gp = t.delivered_gas_price_usd_per_mmbtu.values
    remote_co2 = REMOTE_CO2_0 * (1 + RATE_CO2) ** yrs
    f = pd.DataFrame({"model_year": t.model_year.values})

    # capacity charges (owned equipment) per available hour
    cap_cost = np.zeros(len(t))
    for r in GAS + ["battery_4h"]:
        ann = caps[r] * 1000 * (res.loc[r, "capex_usd_per_kw"] * crf(res.loc[r, "life_years"])
                                + res.loc[r, "fom_usd_per_kw_year"])
        cap_cost += ann / HPY * av[r]
    # available-generation contracts: pay for all available energy
    contract = np.zeros(len(t))
    for r in CLEAN:
        contract += res.loc[r, "payment_usd_per_mwh"] * caps[r] * t[PROFILE[r]].values * av[r]
    fuel = np.zeros(len(t)); gas_co2 = np.zeros(len(t))
    for g in GAS:
        fuel += d[g].values * (res.loc[g, "payment_usd_per_mwh"] + res.loc[g, "heat_rate_mmbtu_per_mwh_hhv"] * gp)
        gas_co2 += d[g].values * res.loc[g, "co2_t_per_mwh"]
    merchant = d.grid.values * t.merchant_price_usd_per_mwh.values

    f["requested_mwh"] = t.load_mw.values
    f["served_local_mwh"] = t.load_mw.values - d.reloc.values - d.curt.values
    f["relocated_mwh"] = d.reloc.values
    f["interrupted_mwh"] = d.curt.values
    f["unserved_mwh"] = d.unserved.values
    f["remote_mwh"] = 1.05 * d.reloc.values
    f["merchant_mwh"] = d.grid.values
    for r in CLEAN + GAS:
        f[f"{r}_mwh"] = d[r].values
    f["clean_mwh"] = d[CLEAN].sum(axis=1).values - d.ch.values + d.dis.values
    f["capacity_cost"] = cap_cost
    f["contract_cost"] = contract
    f["gas_cost"] = fuel
    f["merchant_cost"] = merchant
    f["local_energy_cost"] = cap_cost + contract + fuel + merchant
    f["relocation_cost"] = RELOC_USD * d.reloc.values
    f["interruption_cost"] = INTERRUPT_USD * d.curt.values
    f["total_decision_cost"] = f.local_energy_cost + f.relocation_cost + f.interruption_cost
    f["local_co2_t"] = d.grid.values * t.grid_co2_t_per_mwh.values + gas_co2
    f["remote_co2_t"] = f.remote_mwh * remote_co2
    f["combined_co2_t"] = f.local_co2_t + f.remote_co2_t

    yearly = f.groupby("model_year").sum()
    yearly.loc["horizon"] = yearly.sum()
    yearly["local_usd_per_mwh"] = yearly.local_energy_cost / yearly.served_local_mwh.replace(0, np.nan)
    yearly["decision_usd_per_mwh"] = yearly.total_decision_cost / yearly.requested_mwh.replace(0, np.nan)
    yearly["combined_kg_co2_per_mwh"] = 1000 * yearly.combined_co2_t / (yearly.served_local_mwh + yearly.remote_mwh).replace(0, np.nan)
    yearly["clean_share"] = yearly.clean_mwh / yearly.served_local_mwh.replace(0, np.nan)
    yearly["merchant_share"] = yearly.merchant_mwh / yearly.served_local_mwh.replace(0, np.nan)
    return yearly


# ---------------------------------------------------------------- export
def yearly_resource_use(t, d, caps, av):
    """Per model year: peak hourly MW, energy (GWh) and capacity factor for every resource."""
    caps = {r: caps.get(r, 0) for r in RES_ORDER}
    yr = t.model_year.values
    cols = {"grid": "Merchant grid", "utility_solar": "Solar", "wind": "Wind", "nuclear": "Nuclear",
            "gas_turbine": "Gas turbine", "gas_engine": "Gas engine", "fuel_cell": "Fuel cell",
            "ch": "Battery charge", "dis": "Battery discharge"}
    peak = d.groupby(yr)[list(cols)].max().rename(columns=cols)
    peak["Battery max SOC (MWh)"] = d.groupby(yr).soc.max()
    peak["Total grid import"] = (d[["grid"] + OFFSITE].sum(axis=1)).groupby(yr).max()
    peak["Requested load"] = t.groupby("model_year").load_mw.max().values
    peak.loc["horizon"] = peak.max()

    energy = d.groupby(yr)[list(cols)].sum().rename(columns=cols) / 1000
    energy.loc["horizon"] = energy.sum()

    cf = pd.DataFrame(index=peak.index)
    capkey = {"Solar": "utility_solar", "Wind": "wind", "Nuclear": "nuclear", "Gas turbine": "gas_turbine",
              "Gas engine": "gas_engine", "Fuel cell": "fuel_cell", "Battery discharge": "battery_4h"}
    for lab, r in capkey.items():
        if caps[r] == 0:
            continue
        avail_h = pd.Series(av[r]).groupby(yr).sum()
        avail_h.loc["horizon"] = avail_h.sum()
        cf[lab] = (energy[lab] * 1000 / (caps[r] * avail_h)).where(avail_h > 0)
    return peak.round(2), energy.round(2), cf.round(3)


def resource_unit_costs(t, res, caps, d, av):
    """All-in realised cost per MWh DELIVERED, per resource per model year.
    Owned assets: (capacity charge + fuel/VOM) / dispatched MWh.
    Contracts:    payment for all AVAILABLE energy / dispatched MWh (so curtailment raises it).
    Battery:      capacity charge / discharged MWh (charging energy is paid via its source).
    Merchant:     hourly price paid / MWh bought."""
    caps = {r: caps.get(r, 0) for r in RES_ORDER}
    yr = t.model_year.values
    gp = t.delivered_gas_price_usd_per_mmbtu.values
    cost, mwh = {}, {}
    cost["Merchant grid"] = d.grid.values * t.merchant_price_usd_per_mwh.values
    mwh["Merchant grid"] = d.grid.values
    names = {"utility_solar": "Solar", "wind": "Wind", "nuclear": "Nuclear"}
    for r, lab in names.items():
        if caps[r]:
            cost[lab] = res.loc[r, "payment_usd_per_mwh"] * caps[r] * t[PROFILE[r]].values * av[r]
            mwh[lab] = d[r].values
    names = {"gas_turbine": "Gas turbine", "gas_engine": "Gas engine", "fuel_cell": "Fuel cell"}
    for r, lab in names.items():
        if caps[r]:
            ann = caps[r] * 1000 * (res.loc[r, "capex_usd_per_kw"] * crf(res.loc[r, "life_years"])
                                    + res.loc[r, "fom_usd_per_kw_year"])
            cost[lab] = ann / HPY * av[r] + d[r].values * (res.loc[r, "payment_usd_per_mwh"]
                                                           + res.loc[r, "heat_rate_mmbtu_per_mwh_hhv"] * gp)
            mwh[lab] = d[r].values
    if caps["battery_4h"]:
        r = "battery_4h"
        ann = caps[r] * 1000 * (res.loc[r, "capex_usd_per_kw"] * crf(res.loc[r, "life_years"])
                                + res.loc[r, "fom_usd_per_kw_year"])
        cost["Battery"] = ann / HPY * av[r]
        mwh["Battery"] = d.dis.values
    C = pd.DataFrame(cost).groupby(yr).sum(); C.loc["horizon"] = C.sum()
    M = pd.DataFrame(mwh).groupby(yr).sum(); M.loc["horizon"] = M.sum()
    unit = (C / M.where(M > 1e-6)).round(2)
    unit["Portfolio avg (local $/MWh served)"] = (C.sum(axis=1) /
        (pd.Series(t.load_mw.values - d.reloc.values - d.curt.values).groupby(yr).sum()
         .pipe(lambda s: pd.concat([s, pd.Series({"horizon": s.sum()})])))).round(2)
    return unit


def yearly_cost_table(yearly):
    """Per model year cost breakdown in $M plus unit costs."""
    c = pd.DataFrame(index=yearly.index)
    c["Capacity charges ($M)"] = yearly.capacity_cost / 1e6
    c["Contract payments ($M)"] = yearly.contract_cost / 1e6
    c["Gas fuel+VOM ($M)"] = yearly.gas_cost / 1e6
    c["Merchant energy ($M)"] = yearly.merchant_cost / 1e6
    c["Local energy cost ($M)"] = yearly.local_energy_cost / 1e6
    c["Relocation ($M)"] = yearly.relocation_cost / 1e6
    c["Interruption ($M)"] = yearly.interruption_cost / 1e6
    c["Total decision cost ($M)"] = yearly.total_decision_cost / 1e6
    c["Local $/MWh served"] = yearly.local_usd_per_mwh
    c["Decision $/MWh requested"] = yearly.decision_usd_per_mwh
    c["Combined kg CO2/MWh"] = yearly.combined_kg_co2_per_mwh
    c["Clean share"] = yearly.clean_share
    return c.round(3)


def export(out_dir, d, caps, start_month, grid_delay=0):
    import json
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    sub = pd.DataFrame({
        "model_hour": d.model_hour, "grid_mw": d.grid,
        "tier1_relocated_mw": d.reloc, "tier2_curtailed_mw": d.curt,
        "utility_solar_mw": d.utility_solar, "wind_mw": d.wind,
        "battery_4h_charge_mw": d.ch, "battery_4h_discharge_mw": d.dis,
        "battery_4h_soc_mwh": d.soc, "gas_turbine_mw": d.gas_turbine,
        "nuclear_mw": d.nuclear, "gas_engine_mw": d.gas_engine, "fuel_cell_mw": d.fuel_cell,
    })
    sub.to_csv(out / "dispatch.csv", index=False, float_format="%.6f")
    json.dump({"initial_service_month": start_month, "grid_delay_months": grid_delay,
               "capacities_mw": {k: v for k, v in caps.items() if v}},
              open(out / "portfolio.json", "w"), indent=2)


def self_check(t, d, caps):
    """Quick physical checks on the stitched schedule."""
    supply = d[["grid"] + CLEAN + GAS].sum(axis=1) + d.dis - d.ch
    bal = supply + d.reloc + d.curt + d.unserved - t.load_mw.values
    imp = d[["grid"] + OFFSITE].sum(axis=1) - t.effective_grid_limit_mw.values
    simul = ((d.ch > 1e-6) & (d.dis > 1e-6)).sum()
    soc = d.soc.values; prev = np.concatenate([[0], soc[:-1]])
    socerr = np.abs(soc - prev - ETA * d.ch.values + d.dis.values / ETA).max()
    return {"max_balance_err": float(np.abs(bal).max()), "max_import_violation": float(imp.max()),
            "simultaneous_ch_dis_hours": int(simul), "max_soc_err": float(socerr),
            "final_soc": float(soc[-1]), "unserved_mwh": float(d.unserved.sum())}



