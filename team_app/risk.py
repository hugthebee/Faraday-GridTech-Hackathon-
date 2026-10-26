"""Per-portfolio extras for the recommendation: maintenance (O&M) cost, backup-power options, and a computed
"what could break" list.

* Maintenance (O&M) is NOT a new assumption: it is the packet's fixed O&M ($/kW-yr) and variable O&M ($/MWh), split out of the cost the
  evaluator already charges. Contract payments (solar/wind/nuclear $/MWh) are energy prices, not maintenance.
* Backup power is an EXTENSION. The packet puts UPS and emergency protection out of scope, so backup costs are shown separately and
  never added to the common decision cost. The diesel numbers below are TEAM ASSUMPTIONS (edit BACKUP); the gas-engine backup option
  reuses the packet's own gas_engine row.
* "What could break" re-runs the portfolio under stresses (grid slip, no remote relocation, loss of the largest unit, low renewables,
  price shocks) with the same evaluator and reports what changes. Nothing here is boilerplate; every line carries a computed number.
"""
import copy
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alt_adapter as A  # noqa: E402
import engine as E  # noqa: E402

gm = A.gm
HORIZON = 120
BACKUP = dict(capex_usd_per_kw=850.0, fom_usd_per_kw_year=12.0, life_years=20, unit_mw=2.5)   # diesel genset set: TEAM ASSUMPTION
SEV_ORDER = {'Breaks': 0, 'High': 1, 'Medium': 2, 'Low': 3, 'Holds': 4}


def _crf(life):
    r = gm.CASE['discount_rate_real']
    return r / (1 - (1 + r) ** (-life))


def _annual_cost(mw, capex, fom, life):
    return mw * 1000 * (capex * _crf(life) + fom)


def _caps(caps):
    return {k: float(caps.get(k, 0.0)) for k in gm.RESOURCES}


# ------------------------------------------------------------------------------------------------ maintenance
def maintenance(caps, start, hourly, delay=0):
    """Fixed + variable O&M of the onsite equipment, from the packet's catalog (a split of the common cost, not an add-on)."""
    ctx = gm.get_ctx(int(start), int(delay))
    rows, fixed_t, var_t = [], 0.0, 0.0
    for o in gm.RESOURCES:
        mw = float(caps.get(o, 0.0))
        r = gm.OPT.loc[o]
        if mw <= 0 or gm.OPT.loc[o, 'location'] != 'onsite':
            continue
        fixed = float(ctx.mask[o].sum() * mw * 1000 * r.fom_usd_per_kw_year / 8760)
        gen = hourly.get(o + '_mw')
        var = float(np.asarray(gen).sum() * r.payment_usd_per_mwh) if gen is not None and r.payment_basis == 'dispatched_generation' else 0.0
        rows.append(dict(resource=o, mw=mw, fixed_om=fixed, variable_om=var, total=fixed + var))
        fixed_t += fixed; var_t += var
    return dict(fixed_om=fixed_t, variable_om=var_t, total=fixed_t + var_t, by_resource=rows)


# ------------------------------------------------------------------------------------------------ backup power (extension)
def _units(caps):
    """Onsite dispatchable units as a list of unit sizes (one unit per catalog block)."""
    sizes = {'gas_turbine': 25.0, 'gas_engine': 5.0, 'fuel_cell': 5.0}
    out = []
    for o, sz in sizes.items():
        mw = float(caps.get(o, 0.0))
        out += [sz] * int(round(mw / sz))
    return out


def backup_options(caps, start, delay=0, portfolio_cost=None):
    ctx = gm.get_ctx(int(start), int(delay))
    critical = float(ctx.tier0.max())                    # peak protected (Tier 0) demand at full load
    units = _units(caps)
    total, largest = sum(units), (max(units) if units else 0.0)
    firm = total - largest                               # N-1: the largest onsite unit may be the one that fails
    covered = min(firm, critical)
    shortfall = max(0.0, critical - covered)
    years = max(0.0, (HORIZON - int(start)) / 12.0)
    u = BACKUP['unit_mw']
    eng = gm.OPT.loc['gas_engine']

    def diesel(mw_needed):
        n = math.ceil(mw_needed / u) + 1 if mw_needed > 0 else 0          # N+1 sets
        mw = n * u
        ann = _annual_cost(mw, BACKUP['capex_usd_per_kw'], BACKUP['fom_usd_per_kw_year'], BACKUP['life_years'])
        return mw, ann, ann * years

    def engines(mw_needed):
        n = math.ceil(mw_needed / 5.0) + 1 if mw_needed > 0 else 0
        mw = n * 5.0
        ann = _annual_cost(mw, eng.capex_usd_per_kw, eng.fom_usd_per_kw_year, eng.life_years)
        return mw, ann, ann * years

    opts = []
    opts.append(dict(option='Use this portfolio\'s own onsite gas as backup', added_mw=0.0, annual_cost=0.0, total_cost=0.0, covers_mw=covered,
                     note=(f'Firm onsite capacity (largest unit out) is {firm:.0f} MW against {critical:.1f} MW of protected load. '
                           'It shares the gas supply with normal operation, so a pipeline outage takes out both.' if total else
                           'No onsite generation in this portfolio, so there is nothing to fall back on.')))
    mw, ann, tot = diesel(shortfall)
    opts.append(dict(option='Top up the shortfall with diesel sets (N+1)', added_mw=mw, annual_cost=ann, total_cost=tot, covers_mw=critical,
                     note='Independent of the gas pipeline. Cost is team-assumed.' if shortfall > 0 else 'Not needed: own gas already covers the protected load.'))
    mw, ann, tot = engines(shortfall)
    opts.append(dict(option='Top up the shortfall with gas engines (packet costs, N+1)', added_mw=mw, annual_cost=ann, total_cost=tot, covers_mw=critical,
                     note='Uses the packet\'s gas_engine cost row. Shares the gas supply.' if shortfall > 0 else 'Not needed: own gas already covers the protected load.'))
    mw, ann, tot = diesel(critical)
    opts.append(dict(option='Independent diesel backup for the full protected load (N+1)', added_mw=mw, annual_cost=ann, total_cost=tot, covers_mw=critical,
                     note='Full independence from gas and the grid; ignores the portfolio\'s own units. Cost is team-assumed.'))
    for o in opts:
        o['pct_of_portfolio_cost'] = (o['total_cost'] / portfolio_cost * 100.0) if portfolio_cost else float('nan')
    return dict(critical_mw=critical, onsite_total_mw=total, firm_mw=firm, shortfall_mw=shortfall, years=years, options=opts,
                assumptions=dict(BACKUP, source='team assumption (outside the packet; edit BACKUP in risk.py)'))


# ------------------------------------------------------------------------------------------------ what could break
def _evaluate(caps, start, delay, reloc=True, intr=True, ctx_mod=None):
    sc = A.scenario(delay, reloc, intr)
    if ctx_mod is None:
        m, res, ctx = gm.evaluate(int(start), caps, sc)
        return m, res, ctx
    ctx = ctx_mod(gm.get_ctx(int(start), int(delay)))
    res = gm.dispatch(ctx, caps, sc)
    return gm.account(ctx, caps, res), res, ctx


def _sev_cost(pct):
    return 'High' if pct >= 5 else 'Medium' if pct >= 2 else 'Low' if pct >= 0.5 else 'Holds'


def _shortage(res, ctx):
    unmet = np.asarray(res['unmet_mw'])
    bad = unmet > 1e-6
    if not bad.any():
        return None
    months = np.unique(ctx.month[bad])
    return dict(mwh=float(unmet.sum()), peak=float(unmet.max()), hours=int(bad.sum()), first=int(months.min()), last=int(months.max()))


def risk_report(start, caps, policy=(0, 'Base', True, True), backup=None):
    """List of dicts {severity, title, detail}. Severity: Breaks (cannot serve demand) > High > Medium > Low > Holds."""
    start = int(start)
    caps = _caps(caps)
    delay, _price, reloc, intr = policy
    m0, res0, ctx0 = _evaluate(caps, start, delay, reloc, intr)
    total0 = m0['total_decision_cost_usd']
    items = []
    add = lambda sev, title, detail: items.append(dict(severity=sev, title=title, detail=detail))
    pct = lambda x: (x / total0 - 1) * 100

    # 1. grid connection slips
    res_txt, worst = [], 'Holds'
    for d in (3, 6, 12):
        m, res, ctx = _evaluate(caps, start, delay + d, reloc, intr)
        sh = None if res['feasible'] else _shortage(res, ctx)
        if sh:
            res_txt.append(f"{d} months: CANNOT serve demand ({sh['mwh']:,.0f} MWh unmet, up to {sh['peak']:.1f} MW, months {sh['first']}-{sh['last']})")
            worst = 'Breaks'
        else:
            p = pct(m['total_decision_cost_usd'])
            res_txt.append(f"{d} months: cost {p:+.1f}%, interrupted {m['interrupted_mwh']:,.0f} MWh")
            if worst != 'Breaks' and SEV_ORDER[_sev_cost(p)] < SEV_ORDER[worst]:
                worst = _sev_cost(p)
    add(worst, 'The grid connection arrives late', '; '.join(res_txt) + '. Grid upgrades slip, equipment dates do not.')

    # 2. remote relocation unavailable
    if reloc:
        m, res, ctx = _evaluate(caps, start, delay, False, intr)
        sh = None if res['feasible'] else _shortage(res, ctx)
        if sh:
            add('Breaks', 'The remote site cannot take relocated work', f"Without relocation the portfolio cannot meet the stress-event import caps ({sh['mwh']:,.0f} MWh unmet, up to {sh['peak']:.1f} MW). "
                f"It relies on up to {res0['tier1_relocated_mw'].max():.1f} MW of Tier 1 moving to a {gm.RESP['remote_capacity_mw']:g} MW remote site.")
        else:
            p = pct(m['total_decision_cost_usd'])
            di = m['interrupted_mwh'] - m0['interrupted_mwh']
            add(_sev_cost(p) if di < 1 else max(_sev_cost(p), 'Medium', key=lambda s: -SEV_ORDER[s]),
                'The remote site cannot take relocated work', f"Without relocation: cost {p:+.1f}%, interrupted MWh {di:+,.0f}. Base case relocates {m0['relocated_local_equivalent_mwh']:,.0f} MWh "
                f"(peak {res0['tier1_relocated_mw'].max():.1f} MW of the {gm.RESP['remote_capacity_mw'] / gm.RESP['remote_energy_multiplier']:.1f} MW the remote site can take).")

    # 3. largest onsite unit fails
    units = _units(caps)
    if units:
        key = 'gas_turbine' if caps['gas_turbine'] > 0 else ('gas_engine' if caps['gas_engine'] > 0 else 'fuel_cell')
        sz = {'gas_turbine': 25.0, 'gas_engine': 5.0, 'fuel_cell': 5.0}[key]
        c2 = dict(caps); c2[key] -= sz
        m, res, ctx = _evaluate(c2, start, delay, reloc, intr)
        sh = None if res['feasible'] else _shortage(res, ctx)
        if sh:
            add('Breaks', f'The largest onsite unit ({sz:.0f} MW {key.replace("_", " ")}) is out', f"Demand cannot be met: {sh['mwh']:,.0f} MWh unmet over {sh['hours']:,} hours "
                f"(up to {sh['peak']:.1f} MW, months {sh['first']}-{sh['last']}). Tested as a permanent loss of that unit.")
        else:
            p = pct(m['total_decision_cost_usd'])
            add(_sev_cost(p), f'The largest onsite unit ({sz:.0f} MW {key.replace("_", " ")}) is out', f"Still serves all demand; cost {p:+.1f}%, interrupted {m['interrupted_mwh'] - m0['interrupted_mwh']:+,.0f} MWh (permanent loss tested).")
    else:
        add('High', 'No onsite generation at all', 'Every MWh arrives through the grid connection. During a stress event the campus must relocate or interrupt whatever exceeds 60% of the cap, '
            'and nothing keeps protected load running if the connection is lost.')

    # 4. low renewable output
    if any(caps[o] > 0 for o in ctx0.contracts):
        def scale(c):
            c2 = copy.copy(c); c2.prof = {o: (v * 0.8 if o in c.contracts else v) for o, v in c.prof.items()}; return c2
        m, res, ctx = _evaluate(caps, start, delay, reloc, intr, ctx_mod=scale)
        sh = None if res['feasible'] else _shortage(res, ctx)
        p = pct(m['total_decision_cost_usd'])
        di = m['combined_electricity_co2_kg_per_mwh'] - m0['combined_electricity_co2_kg_per_mwh']
        add('Breaks' if sh else _sev_cost(p), 'Wind and solar deliver 20% less than the reference year',
            ('Demand cannot be met. ' if sh else '') + f"Cost {p:+.1f}%, carbon intensity {di:+.0f} kg/MWh (to {m['combined_electricity_co2_kg_per_mwh']:.0f}). "
            f"The packet repeats one historical year ten times, so a weak-wind decade is not in the numbers.")

    # 5-6. prices (own engine; ratio of like-for-like runs, so evaluator differences cancel)
    def e_run(price):
        r = E.dispatch(caps, start, delay, 0.0, 60.0, price, reloc, intr)
        return r
    try:
        rb, rh, rf = e_run('Base'), e_run('High'), e_run('Flat')
        p = (rh['total_cost'] / rb['total_cost'] - 1) * 100
        add(_sev_cost(p), 'Power and gas prices rise faster than the packet\'s trajectory', f"High-price scenario (wholesale +8%/yr, gas +5%/yr): cost {p:+.1f}%. "
            f"Merchant electricity supplies {m0['merchant_share_of_local'] * 100:.0f}% of local energy.")
        di = rf['intensity'] - rb['intensity']
        add('Medium' if di / max(rb['intensity'], 1) > 0.10 else 'Low' if di > 1 else 'Holds', 'Grid CO2 does not keep falling',
            f"The packet assumes grid CO2 falls 3%/yr. If it stays flat, intensity moves {di:+.0f} kg/MWh (to {rf['intensity']:.0f}); "
            f"{'a carbon limit near the current value would be broken' if di > 5 else 'small effect for this portfolio'}.")
    except Exception as exc:                                            # never let an extension break the page
        add('Low', 'Price scenarios could not be run', str(exc))
    fuel = sum(l['fuel_cost_usd'] for l in m0['ledger'])
    if fuel > 0:
        p = 0.5 * fuel / total0 * 100
        add(_sev_cost(p), 'Delivered gas gets 50% more expensive', f"First-order estimate (dispatch held fixed): cost {p:+.1f}% (${0.5 * fuel / 1e6:,.1f}M). Gas is {fuel / total0 * 100:.0f}% of total cost "
            f"and all gas units share one fuel supply.")

    # 7. merchant exposure
    spend = float((res0['grid_mw'] * ctx0.grid_price).sum())
    sh_cost = spend / total0 * 100
    add('High' if m0['merchant_share_of_local'] > 0.4 else 'Medium' if m0['merchant_share_of_local'] > 0.25 else 'Low',
        'Exposure to wholesale prices and spikes', f"{m0['merchant_share_of_local'] * 100:.0f}% of local energy is bought from the market (${spend / 1e6:,.1f}M, {sh_cost:.0f}% of total cost). "
        f"The packet's price spikes are real 2023 hours, but a different year could be worse.")

    # 8. overbuilt contracts
    waste, spend_c = 0.0, 0.0
    for o in ctx0.contracts:
        if caps[o] > 0:
            avail = caps[o] * ctx0.prof[o]
            price = gm.OPT.loc[o, 'payment_usd_per_mwh']
            waste += float(((avail - res0[o]).clip(min=0)).sum() * price)
            spend_c += float(avail.sum() * price)
    if spend_c > 0:
        w = waste / spend_c * 100
        add('High' if w > 20 else 'Medium' if w > 10 else 'Low', 'Paying for renewable energy that is not used', f"Contracts pay for all available energy. ${waste / 1e6:,.1f}M ({w:.0f}%) of the "
            f"${spend_c / 1e6:,.1f}M paid to wind/solar/nuclear buys energy that is curtailed (no export, no resale).")

    # 9. land
    land = gm.land_used(caps)
    head = gm.CASE['onsite_energy_land_acres'] - land
    if sum(caps[o] for o in ('gas_turbine', 'gas_engine', 'fuel_cell', 'battery_4h')) > 0:
        add('Medium' if head < 0.5 else 'Low' if head < 2 else 'Holds', 'Land left for more onsite equipment', f"Uses {land:.2f} of {gm.CASE['onsite_energy_land_acres']:.2f} acres, leaving {head:.2f} acres. "
            f"The packet reserves the full envelope up front, so later expansion would need new land.")

    # 10. stress-event reliance
    if m0['interrupted_mwh'] > 1 or m0['relocated_local_equivalent_mwh'] > 1:
        add('Medium' if m0['interrupted_mwh'] > 1 else 'Low', 'Relies on shedding or moving demand in stress events',
            f"{m0['interrupted_mwh']:,.0f} MWh of Tier 2 is interrupted (${m0['interruption_consequence_usd'] / 1e6:,.2f}M) and {m0['relocated_local_equivalent_mwh']:,.0f} MWh of Tier 1 is relocated "
            f"over the horizon. A customer with a 'never interrupt' requirement would reject this.")
    else:
        add('Holds', 'Stress events', 'No Tier 1 relocation or Tier 2 interruption is needed: the portfolio rides through all events on its own supply.')

    # 11. early ramp
    late = [o for o in gm.RESOURCES if caps[o] > 0 and int(gm.OPT.loc[o, 'available_month']) > start]
    first = (ctx0.month >= start) & (ctx0.month < start + 12)
    gas_e = sum(float(res0[o][first].sum()) for o in ctx0.gas)
    load_e = float(ctx0.load[first].sum())
    if late:
        names = ', '.join(f"{o.replace('_', ' ')} (month {int(gm.OPT.loc[o, 'available_month'])})" for o in late)
        add('Medium' if gas_e / max(load_e, 1) > 0.3 else 'Low', 'Some purchased capacity is not there at opening', f"{names} {'is' if len(late) == 1 else 'are'} idle at opening month {start}, so the first year runs on gas "
            f"({gas_e / max(load_e, 1) * 100:.0f}% of that year's energy) and merchant power.")
    elif gas_e / max(load_e, 1) > 0.3:
        add('Low', 'The first year leans on onsite gas', f"{gas_e / max(load_e, 1) * 100:.0f}% of the first year's energy comes from onsite gas while the grid connection is still small.")

    # 12. backup power
    b = backup or backup_options(caps, start, delay, total0)
    if b['shortfall_mw'] > 0.05:
        best = b['options'][1]
        add('High', 'Backup power does not cover the protected load', f"Protected demand peaks at {b['critical_mw']:.1f} MW. Firm onsite capacity (largest unit out) is {b['firm_mw']:.0f} MW, "
            f"a {b['shortfall_mw']:.1f} MW gap. Closing it with diesel sets costs about ${best['total_cost'] / 1e6:,.1f}M over the horizon (team-assumed, outside the common cost).")
    elif b['onsite_total_mw'] > 0:
        add('Low', 'Backup shares one fuel supply', f"Own gas covers the {b['critical_mw']:.1f} MW protected load with a unit out, but the same gas supply feeds normal operation, "
            f"so a pipeline outage removes both. Independent diesel would add ${b['options'][3]['total_cost'] / 1e6:,.1f}M (team-assumed).")

    items.sort(key=lambda d: SEV_ORDER[d['severity']])
    return items
