"""Report layer: per-model-year results, cost by source, unit costs per source, asset use, a readable summary, and the charts (report_charts.py).

Ideas and the layout of the yearly tables come from the second model (model/GridTech.ipynb: accounting, yearly_resource_use, resource_unit_costs,
yearly_cost_table). The code is rebuilt over OUR dispatch and OUR case tables (casepatch/casespec), because the notebook hard-codes the packet
benchmark constants, so a custom case would silently use the wrong prices. The yearly totals are tested against the official checker's
annual_results (validate_model.py), to the dollar.
"""
import copy
import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import casespec  # noqa: E402,F401  (per-thread case isolation)
import alt_adapter as A  # noqa: E402
import engine as E  # noqa: E402

gm, cs = A.gm, E.cs
CLEAN = ['utility_solar', 'wind', 'nuclear']
GAS = ['gas_turbine', 'gas_engine', 'fuel_cell']
RES_ORDER = ['utility_solar', 'wind', 'nuclear', 'gas_turbine', 'gas_engine', 'fuel_cell', 'battery_4h']
SRC_ORDER = ['nuclear', 'wind', 'utility_solar', 'dis', 'fuel_cell', 'gas_engine', 'gas_turbine', 'grid', 'reloc', 'curt', 'unserved']
NAME = {'nuclear': 'Nuclear contract', 'wind': 'Wind contract', 'utility_solar': 'Solar contract', 'dis': 'Battery', 'fuel_cell': 'Fuel cell (onsite gas)',
        'gas_engine': 'Gas engine (onsite)', 'gas_turbine': 'Gas turbine (onsite)', 'grid': 'Grid power (bought)', 'reloc': 'Work moved to remote site',
        'curt': 'Work paused', 'unserved': 'NOT SERVED', 'battery_4h': 'Battery', 'response': 'Grid-stress response'}
COLOR = {'nuclear': '#6a3d9a', 'wind': '#1f78b4', 'utility_solar': '#f1c40f', 'dis': '#2ecc71', 'fuel_cell': '#f5b041', 'gas_engine': '#d35400',
         'gas_turbine': '#873600', 'grid': '#95a5a6', 'reloc': '#85c1e9', 'curt': '#e74c3c', 'unserved': '#000000', 'battery_4h': '#2ecc71', 'response': '#e74c3c'}
NYEAR = 10


def build(start, caps, hourly, delay=0, ctx=None):
    """Everything the report needs for one dispatched portfolio. hourly = the dispatch.csv columns as arrays (A.simulate()['_hourly']).
    ctx: optional hourly-input object (used by the price scenarios); default is the case in force."""
    start, delay = int(start), int(delay)
    ctx = ctx if ctx is not None else gm.get_ctx(start, delay)
    fr = ctx.base['frame']
    yr = np.asarray(ctx.year)
    caps = {k: float(caps.get(k, 0.0)) for k in RES_ORDER}
    OPT = gm.OPT
    num = E.NUM
    rate = float(gm.CASE['discount_rate_real'])
    mult = float(gm.RESP['remote_energy_multiplier'])
    h = {k: np.asarray(v, float) for k, v in hourly.items()}
    n = ctx.n
    zeros = np.zeros(n)
    bc = lambda a: np.bincount(yr, weights=np.asarray(a, float), minlength=NYEAR)[:NYEAR]
    gen = {k: h[f'{k}_mw'] for k in CLEAN + GAS}
    ch, dis, soc = h['battery_4h_charge_mw'], h['battery_4h_discharge_mw'], h['battery_4h_soc_mwh']
    grid, rel, intr = h['grid_mw'], h['tier1_relocated_mw'], h['tier2_curtailed_mw']
    price, gasp = np.asarray(ctx.grid_price), np.asarray(ctx.gas_price)

    cost_h = {}                                                             # all-in hourly cost per resource
    capacity, contract, gascost = zeros.copy(), zeros.copy(), zeros.copy()
    for k in RES_ORDER:
        mw = caps[k]
        if mw <= 0:
            continue
        o = OPT.loc[k]
        life = num(o.life_years)
        crf = rate / (1 - (1 + rate) ** (-life))
        fixed = ctx.mask[k] * mw * 1000 * (num(o.capex_usd_per_kw) * crf + num(o.fom_usd_per_kw_year)) / 8760
        g = gen.get(k, zeros)
        pay = mw * ctx.prof[k] * num(o.payment_usd_per_mwh) if o.payment_basis == 'available_generation' else g * num(o.payment_usd_per_mwh)
        fuel = g * gasp * num(o.heat_rate_mmbtu_per_mwh_hhv) if k in GAS else zeros
        cost_h[k] = fixed + pay + fuel
        capacity += fixed
        if k in CLEAN:
            contract += pay
        elif k in GAS:
            gascost += pay + fuel
    merchant = grid * price
    reloc_cost, intr_cost = rel * ctx.reloc_price, intr * ctx.intr_price

    local = ctx.load - rel - intr
    clean = sum(gen[k] for k in CLEAN) - ch + dis
    gas_co2 = sum(gen[k] * num(OPT.loc[k, 'co2_t_per_mwh']) for k in GAS)
    Y = {'requested_mwh': bc(ctx.load), 'served_local_mwh': bc(local), 'relocated_mwh': bc(rel), 'interrupted_mwh': bc(intr), 'unserved_mwh': bc(zeros),
         'remote_mwh': bc(rel * mult), 'merchant_mwh': bc(grid)}
    for k in CLEAN + GAS:
        Y[f'{k}_mwh'] = bc(gen[k])
    Y['battery_discharge_mwh'], Y['battery_charge_mwh'], Y['clean_mwh'] = bc(dis), bc(ch), bc(clean)
    Y['capacity_cost'], Y['contract_cost'], Y['gas_cost'], Y['merchant_cost'] = bc(capacity), bc(contract), bc(gascost), bc(merchant)
    Y['local_energy_cost'] = Y['capacity_cost'] + Y['contract_cost'] + Y['gas_cost'] + Y['merchant_cost']
    Y['relocation_cost'], Y['interruption_cost'] = bc(reloc_cost), bc(intr_cost)
    Y['total_decision_cost'] = Y['local_energy_cost'] + Y['relocation_cost'] + Y['interruption_cost']
    Y['local_co2_t'] = bc(grid * np.asarray(ctx.grid_co2) + gas_co2)
    Y['remote_co2_t'] = bc(rel * mult * np.asarray(ctx.remote_co2))
    Y['combined_co2_t'] = Y['local_co2_t'] + Y['remote_co2_t']
    yearly = pd.DataFrame(Y, index=range(1, NYEAR + 1))
    yearly.loc['horizon'] = yearly.sum()
    nz = lambda s: s.replace(0, np.nan)
    yearly['local_usd_per_mwh'] = yearly.local_energy_cost / nz(yearly.served_local_mwh)
    yearly['decision_usd_per_mwh'] = yearly.total_decision_cost / nz(yearly.requested_mwh)
    yearly['combined_kg_co2_per_mwh'] = 1000 * yearly.combined_co2_t / nz(yearly.served_local_mwh + yearly.remote_mwh)
    yearly['clean_share'] = yearly.clean_mwh / nz(yearly.served_local_mwh)
    yearly['merchant_share'] = yearly.merchant_mwh / nz(yearly.served_local_mwh)

    # the dispatch in the notebook's column names, for the charts
    d = pd.DataFrame({'grid': grid, 'utility_solar': gen['utility_solar'], 'wind': gen['wind'], 'nuclear': gen['nuclear'], 'gas_turbine': gen['gas_turbine'],
                      'gas_engine': gen['gas_engine'], 'fuel_cell': gen['fuel_cell'], 'ch': ch, 'dis': dis, 'soc': soc, 'reloc': rel, 'curt': intr, 'unserved': zeros})
    av = {k: np.asarray(ctx.mask[k], float) for k in RES_ORDER}

    # cost by source per year ($M)
    C = pd.DataFrame({'grid': merchant, **{k: v for k, v in cost_h.items()}, 'response': reloc_cost + intr_cost}).groupby(yr).sum() / 1e6
    C.index = range(1, NYEAR + 1)
    C.loc['horizon'] = C.sum()
    # energy by source per year (GWh)
    src = [k for k in SRC_ORDER if d[k].sum() > 1e-6]
    mix = d.groupby(yr)[src].sum() / 1000
    mix.index = range(1, NYEAR + 1)
    # unit cost per MWh delivered, per source per year
    cmap = {'Merchant grid': merchant, **{lab: cost_h[k] for lab, k in (('Solar', 'utility_solar'), ('Wind', 'wind'), ('Nuclear', 'nuclear'), ('Gas turbine', 'gas_turbine'),
                                                                        ('Gas engine', 'gas_engine'), ('Fuel cell', 'fuel_cell'), ('Battery', 'battery_4h')) if k in cost_h}}
    mmap = {'Merchant grid': grid, 'Solar': gen['utility_solar'], 'Wind': gen['wind'], 'Nuclear': gen['nuclear'], 'Gas turbine': gen['gas_turbine'], 'Gas engine': gen['gas_engine'],
            'Fuel cell': gen['fuel_cell'], 'Battery': dis}
    UC = pd.DataFrame(cmap).groupby(yr).sum()
    UM = pd.DataFrame({k: mmap[k] for k in cmap}).groupby(yr).sum()
    UC.index = UM.index = range(1, NYEAR + 1)
    UC.loc['horizon'], UM.loc['horizon'] = UC.sum(), UM.sum()
    unit = UC / UM.where(UM > 1e-6)
    unit['Portfolio avg (local $/MWh served)'] = yearly.local_energy_cost / nz(yearly.served_local_mwh)
    # asset use
    cols = {'grid': 'Merchant grid', 'utility_solar': 'Solar', 'wind': 'Wind', 'nuclear': 'Nuclear', 'gas_turbine': 'Gas turbine', 'gas_engine': 'Gas engine', 'fuel_cell': 'Fuel cell',
            'ch': 'Battery charge', 'dis': 'Battery discharge'}
    peak = d.groupby(yr)[list(cols)].max().rename(columns=cols)
    peak.index = range(1, NYEAR + 1)
    peak.loc['horizon'] = peak.max()
    energy_gwh = d.groupby(yr)[list(cols)].sum().rename(columns=cols) / 1000
    energy_gwh.index = range(1, NYEAR + 1)
    energy_gwh.loc['horizon'] = energy_gwh.sum()
    land = float(sum(caps[k] * num(OPT.loc[k, 'land_acres_per_mw']) for k in RES_ORDER))
    return dict(start=start, delay=delay, caps=caps, ctx=ctx, t=fr, d=d, av=av, res=OPT, yearly=yearly, cost=C, mix=mix, unit=unit, peak=peak, energy=energy_gwh, land=land,
                full_month=start + int(gm.CASE['ramp_duration_months']), land_limit=float(gm.CASE['onsite_energy_land_acres']))


# ---------------------------------------------------------------------------------------------------- website payload
def web_payload(R, co2_target=100.0):
    y = R['yearly']
    years = [i for i in range(1, NYEAR + 1) if y.loc[i, 'requested_mwh'] > 0]
    f = lambda v: None if v is None or not np.isfinite(v) else float(v)
    rows = []
    for i in years + ['horizon']:
        r = y.loc[i]
        rows.append(dict(year=i, requestedGwh=f(r.requested_mwh / 1000), servedGwh=f(r.served_local_mwh / 1000), relocatedMwh=f(r.relocated_mwh), interruptedMwh=f(r.interrupted_mwh),
                         capacityM=f(r.capacity_cost / 1e6), contractM=f(r.contract_cost / 1e6), gasM=f(r.gas_cost / 1e6), merchantM=f(r.merchant_cost / 1e6), localM=f(r.local_energy_cost / 1e6),
                         relocationM=f(r.relocation_cost / 1e6), interruptionM=f(r.interruption_cost / 1e6), totalM=f(r.total_decision_cost / 1e6),
                         localUsdPerMwh=f(r.local_usd_per_mwh), decisionUsdPerMwh=f(r.decision_usd_per_mwh), kgPerMwh=f(r.combined_kg_co2_per_mwh),
                         co2T=f(r.combined_co2_t), cleanSharePct=f(r.clean_share * 100), merchantSharePct=f(r.merchant_share * 100)))
    mix = {k: [f(R['mix'].loc[i, k]) for i in years] for k in R['mix'].columns}
    cost = {k: [f(R['cost'].loc[i, k]) for i in years] for k in R['cost'].columns if R['cost'][k].abs().sum() > 1e-9}
    unit = {k: [f(R['unit'].loc[i, k]) for i in years + ['horizon']] for k in R['unit'].columns}
    return dict(start=R['start'], fullMonth=R['full_month'], years=years, rows=rows, mix=mix, costM=cost, unit=unit, unitRows=[str(i) for i in years] + ['horizon'],
                names={k: NAME[k] for k in set(list(mix) + list(cost))}, colors={k: COLOR[k] for k in set(list(mix) + list(cost))}, co2Target=co2_target,
                sources=dict(mix=list(R['mix'].columns), cost=[k for k in cost]))


# ---------------------------------------------------------------------------------------------------- readable summary
def summary_markdown(R, info=None):
    """Plain-language results summary for the submission folder (R7)."""
    info = info or {}
    y, H = R['yearly'], R['yearly'].loc['horizon']
    caps = {k: v for k, v in R['caps'].items() if v}
    lab = {'utility_solar': 'Solar contract', 'wind': 'Wind contract', 'nuclear': 'Nuclear PPA', 'gas_turbine': 'Gas turbine', 'gas_engine': 'Gas engine', 'fuel_cell': 'Fuel cell', 'battery_4h': 'Battery (4h)'}
    L = [f"# Results summary: {info.get('name', 'recommended portfolio')}", '']
    if info.get('customer'):
        L += [f"**Customer inputs:** {info['customer']}", '']
    L += [f"**Portfolio:** {', '.join(f'{lab[k]} {v:g} MW' for k, v in caps.items()) or 'merchant power only'}  ", f"**Initial service:** month {R['start']}  ·  **Full demand:** month {R['full_month']}  ·  onsite land {R['land']:.2f} of {R['land_limit']:g} acres", '',
          '## Headline (10-year horizon, constant 2023 USD)', '',
          '| Metric | Value |', '|---|---|',
          f"| Total decision cost | ${H.total_decision_cost / 1e6:,.1f}M |", f"| Local energy cost | ${H.local_energy_cost / 1e6:,.1f}M |",
          f"| Relocation cost | ${H.relocation_cost / 1e6:,.2f}M |", f"| Interruption consequence | ${H.interruption_cost / 1e6:,.2f}M |",
          f"| Local energy cost per MWh served | ${H.local_usd_per_mwh:,.1f} |", f"| Decision cost per requested MWh | ${H.decision_usd_per_mwh:,.1f} |",
          f"| Combined CO2 intensity | {H.combined_kg_co2_per_mwh:,.0f} kg/MWh |", f"| Clean share | {H.clean_share * 100:.1f}% |", f"| Merchant share of local energy | {H.merchant_share * 100:.1f}% |",
          f"| Requested / served locally | {H.requested_mwh / 1e6:,.2f} / {H.served_local_mwh / 1e6:,.2f} TWh |",
          f"| Relocated / interrupted | {H.relocated_mwh:,.0f} / {H.interrupted_mwh:,.0f} MWh |", '']
    if info.get('maintenance'):
        L += [f"Maintenance (fixed + variable O&M, inside local energy cost): ${info['maintenance']:,.1f}M.", '']
    L += ['## Year by year', '', '| Year | Requested GWh | Local cost $M | Relocation $M | Interruption $M | Total $M | Local $/MWh | kg CO2/MWh | Clean % | Merchant % |', '|---|---|---|---|---|---|---|---|---|---|']
    for i in [k for k in range(1, NYEAR + 1) if y.loc[k, 'requested_mwh'] > 0] + ['horizon']:
        r = y.loc[i]
        L.append(f"| {'**Horizon**' if i == 'horizon' else i} | {r.requested_mwh / 1000:,.0f} | {r.local_energy_cost / 1e6:,.1f} | {r.relocation_cost / 1e6:,.2f} | {r.interruption_cost / 1e6:,.2f} | {r.total_decision_cost / 1e6:,.1f} | "
                 f"{r.local_usd_per_mwh:,.1f} | {r.combined_kg_co2_per_mwh:,.0f} | {r.clean_share * 100:.0f} | {r.merchant_share * 100:.0f} |")
    L += ['', 'Year 1 is development time before the campus opens, so it has no demand when the opening month is later than month 11.', '']
    if info.get('stress'):
        s = info['stress']
        L += ['## Grid-stress response', '', f"Busiest stress hour after full demand ({s['hour']}): demand {s['demandMw']:.1f} MW, grid {s['gridMw']:.1f} MW (cap {s['gridCapMw']:.1f}), onsite gas {s['gasMw']:.1f}, battery {s['batteryMw']:.1f}, "
              f"relocated {s['relocatedMw']:.1f}, interrupted {s['interruptedMw']:.1f}. Protected (Tier 0) demand is served locally in every hour.", '']
    if info.get('checker'):
        c = info['checker']
        L += ['## Official checker', '', f"`starter/check_submission.py`: physical checks {'passed' if c.get('passed') else 'FAILED'}; total decision cost ${c.get('totalDecisionCostM', float('nan')):,.3f}M.", '']
    if info.get('notes'):
        L += ['## Notes and limits', ''] + [f'- {n}' for n in info['notes']] + ['']
    L += ['Screening model: hourly adequacy, one 2023 profile repeated ten times, fictional offers. Not a project NPV.']
    return '\n'.join(L)


def yearly_csv(R):
    y = R['yearly'].copy()
    y.index.name = 'model_year'
    return y.to_csv()


def report_zip(R, outdir_charts, info=None):
    """In-memory zip: charts, yearly_results.csv, results_summary.md."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path(outdir_charts).glob('*.png')):
            z.write(p, f'visuals/{p.name}')
        z.writestr('yearly_results.csv', yearly_csv(R))
        z.writestr('results_summary.md', summary_markdown(R, info))
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------------- price sensitivity (bear / base / bull)
def price_scenarios():
    """Variable-cost scenarios. Only the two prices that drive variable cost move: wholesale power and Henry Hub gas (real annual growth).
    Contract prices, equipment charges, the remote tariff and the grid CO2 path stay as in the packet. Base is the packet's own trajectory."""
    base = cs.trajectory['annual_real_rates']
    return {'bear': dict(label='Bear', wholesale=0.0, gas=0.0, text='Prices stay flat in real terms: wholesale power 0%/yr, gas 0%/yr'),
            'base': dict(label='Base', wholesale=float(base['wholesale_energy']), gas=float(base['henry_hub']),
                         text=f"The packet's trajectory: wholesale +{base['wholesale_energy'] * 100:.0f}%/yr, gas +{base['henry_hub'] * 100:.0f}%/yr"),
            'bull': dict(label='Bull', wholesale=0.08, gas=0.05, text='Prices rise faster: wholesale power +8%/yr, gas +5%/yr')}


def scenario_ctx(ctx, wholesale, gas):
    """Copy of the hourly inputs with only wholesale power and delivered gas re-priced (same 2023 shape, same adders)."""
    c = copy.copy(ctx)
    ref = np.asarray(ctx.base['ref'])
    yrs = np.arange(ctx.n) / float(gm.CASE['hours_per_year'])
    col = lambda name: cs.hourly[name].to_numpy(float)[ref]
    c.grid_price = col('wholesale_price_usd_per_mwh') * (1 + wholesale) ** yrs + col('grid_adder_usd_per_mwh')
    c.gas_price = col('henry_hub_usd_per_mmbtu') * (1 + gas) ** yrs + col('delivered_gas_adder_usd_per_mmbtu')
    return c


def price_sensitivity(start, caps, delay=0, reloc=True, intr=True):
    """Re-dispatch the portfolio under each price scenario (the merit order can change when prices do) and account it the same way.
    Returns {scenario: {...headline numbers, yearlyTotalM...}}."""
    start, delay = int(start), int(delay)
    caps = {k: float(caps.get(k, 0.0)) for k in RES_ORDER}
    ctx = gm.get_ctx(start, delay)
    sc = A.scenario(delay, reloc, intr)
    out = {}
    for key, s in price_scenarios().items():
        c = scenario_ctx(ctx, s['wholesale'], s['gas'])
        res = gm.dispatch(c, caps, sc)
        frame = gm.to_dispatch_frame(c, caps, res)
        hourly = {col: frame[col].to_numpy() for col in frame.columns if col != 'model_hour'}
        R = build(start, caps, hourly, delay, ctx=c)
        y, H = R['yearly'], R['yearly'].loc['horizon']
        yrs = [i for i in range(1, NYEAR + 1) if y.loc[i, 'requested_mwh'] > 0]
        out[key] = dict(label=s['label'], text=s['text'], feasible=bool(res['feasible']), totalM=float(H.total_decision_cost / 1e6), localUsdPerMwh=float(H.local_usd_per_mwh),
                        merchantSharePct=float(H.merchant_share * 100), gasMwhShare=float((H.gas_turbine_mwh + H.gas_engine_mwh + H.fuel_cell_mwh) / H.served_local_mwh * 100),
                        merchantM=float(H.merchant_cost / 1e6), gasM=float(H.gas_cost / 1e6), kgPerMwh=float(H.combined_kg_co2_per_mwh),
                        yearlyTotalM=[float(y.loc[i, 'total_decision_cost'] / 1e6) for i in yrs], years=yrs)
    return out
