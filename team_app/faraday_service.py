"""Model service for the Faraday Power Intelligence website: turns the website's inputs into our model's recommendation and shapes every
number the page shows. No UI code here. Everything the page displays that the model does not produce is listed in FLAGS (and notes).

Inputs  (what the page lets the developer set): preset customer, three priority weights (speed / cost / carbon), optional limits
        (go-live month, carbon, cost above the cheapest) and resources marked unavailable.
        A limit is a hard filter (a portfolio that misses it is removed); the weights only rank what is left.
Outputs: recommended portfolio, dates, cost / carbon / clean share / maintenance, power over time, reliability, a stress hour, options tested,
        assumptions with their real values, and what the model cannot produce.
Scope:   openings in months 14-26 (full demand by month 36), the range the page's requirement choices span.
"""
import copy
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import casespec as CS  # noqa: E402  (installs per-thread case isolation)
import alt_adapter as A  # noqa: E402
import engine as E  # noqa: E402
import logic as L  # noqa: E402
import risk as R  # noqa: E402
import export as X  # noqa: E402
import report as RP  # noqa: E402
import report_charts as RC  # noqa: E402
import tempfile  # noqa: E402
import contextvars  # noqa: E402
import custom_pool as CP  # noqa: E402
import stress_data as SD  # noqa: E402
from contextlib import contextmanager  # noqa: E402

gm, cs = A.gm, E.cs
MAX_OPENING = 26                     # opening months 14..26  ->  full demand 24..36
GO_LIVE_CHOICES = list(range(14, MAX_OPENING + 1))     # first-power month the developer wants (any month in the searched window)
CARBON_CHOICES = [200.0, 150.0, 100.0, 50.0]            # highest acceptable average kg CO2 per MWh
COST_CHOICES = [20.0, 10.0, 5.0]                        # % above the cheapest portfolio that still meets the other limits
RESOURCE_LABELS = [('gas_turbine', 'Gas turbine'), ('gas_engine', 'Gas engine'), ('fuel_cell', 'Fuel cell (natural gas)'), ('battery_4h', 'Battery (4 h)'),
                   ('wind', 'Wind contract'), ('utility_solar', 'Solar contract'), ('nuclear', 'Nuclear PPA')]
DEFAULT_CO2_LINE = 200.0                                # reference line on the carbon chart when no carbon limit is set
# Presets: weights from the GridTech_Inputs.xlsx customer types; limits are optional (None = no limit)
PRESETS = {
    'Balanced':     dict(weights=[34, 33, 33], goLive=None, carbonMax=None, costPct=None),
    'Speed-first':  dict(weights=[70, 20, 10], goLive=18, carbonMax=None, costPct=None),
    'Cost-first':   dict(weights=[10, 80, 10], goLive=None, carbonMax=None, costPct=10.0),
    'Carbon-first': dict(weights=[10, 20, 70], goLive=None, carbonMax=100.0, costPct=None),
}

# What the page shows that the model does not (fully) produce. status: not-modeled | partial | derived | definition | scope
FLAGS = [
    dict(id='maintenance-intervals', area='Assumptions · Maintenance intervals', status='not-modeled',
         message='The model has no maintenance schedule or planned-outage input. It charges the packet\'s fixed O&M ($/kW-yr) and variable O&M ($/MWh) only, so there are no intervals to show.'),
    dict(id='battery-ride-through', area='Reliability · battery ride-through card', status='partial',
         message='The page assumes a 12 MW battery. The model picks batteries only when they pay, and an exact LP check shows they usually do not, so for most portfolios this card is 0 MWh / 0 minutes.'),
    dict(id='onsite-backup', area='Reliability · onsite backup', status='partial',
         message='The packet puts UPS and emergency backup out of scope. "Onsite backup" here is installed generators + battery MW (the workbook rule). The N-1 firm figure and the diesel top-up costs are our extension and use TEAM-ASSUMED diesel costs.'),
    dict(id='grid-stress-covered', area='Reliability · "grid-stress hours covered"', status='partial',
         message='A feasible portfolio always serves 100% of stress-hour demand (it is a hard rule). The model reports instead how many stress hours need no Tier 2 interruption.'),
    dict(id='cost-limit-baseline', area='Requirements · cost limit "above cheapest"', status='definition',
         message='"Cheapest" = the cheapest portfolio, openings M14-M26, that meets your other Priority requirements. The packet\'s Example 6 measures against the month-30 portfolio instead.'),
    dict(id='names', area='Options · portfolio names', status='derived',
         message='The model has no named strategies. Names are generated from the portfolio\'s composition (clean-led / balanced / gas-led).'),
    dict(id='next-step', area='Final recommendation · next step and "why"', status='derived',
         message='These sentences are templated from computed numbers (deltas against the runner-up, installed MW). The model produces the numbers, not the wording.'),
    dict(id='chart-sources', area='Power sources · legend', status='partial',
         message='The page lists four sources. The model also dispatches batteries and can relocate or interrupt demand, so those are added to the chart when present.'),
    dict(id='limits', area='Requirements · limits and weights', status='definition',
         message='A limit you set (go-live month, carbon, cost) is a hard rule: portfolios that miss it are removed. Leave it on "Any" for no limit. The three weights only rank the portfolios that remain, and always add up to 100%.'),
    dict(id='go-live', area='Requirements · go-live month', status='definition',
         message='Go-live is the month of first power. The model searches openings M14-M26 (full demand ten months later); the developer template sets the planned month for a custom case.'),
    dict(id='reliability-fixed', area='Requirements · reliability', status='definition',
         message='Reliability is not a choice: every recommendation serves the protected Tier 0 load in every hour and passes the official checker. Tier 1 relocation and Tier 2 interruption are priced, never free.'),
    dict(id='availability', area='Requirements · resource availability', status='definition',
         message='Marking a resource unavailable (for example wind blocked by regulation) removes every portfolio that uses it; the rest are re-ranked. Anchors for the scores are recomputed on what remains.'),
    dict(id='sens-backup', area='Price sensitivity · added backup', status='partial',
         message='Backup generation is our extension and sits outside the packet\'s cost. Ticking an option adds its cost (spread evenly over the model years with demand, diesel costs are team-assumed) to every scenario and to each option tested.'),
    dict(id='year-one', area='Year by year · early years', status='scope',
         message='Years are the packet\'s 12-month model years counted from development start. A year with no demand (the campus opens later) is not shown. Costs for a year include equipment charges once a resource is committed.'),
]

_lock = threading.RLock()                 # re-entrant: report code calls verify() while holding it
_render_lock = threading.Lock()          # chart rendering takes seconds; it must not block the service lock
_pools, _cache, _exports, _reports = {}, {}, {}, {}
CATALOG = SD.Catalog()                     # stress events by interconnection + the state list (bundled; replaced by an upload)
_templates = {}                            # uploaded developer templates: id -> CaseSpec
_builds = {}                               # case key -> dict(state, message, error, started, finished)
_build_lock = threading.Lock()
_delay_cv = contextvars.ContextVar('faraday_delay', default=0)
POLICY = lambda delay: (int(delay), 'Base', True, True)
GO_LIVE_STARTS = [14, 16, 18, 20, 22, 24, 26]


def D():
    """Grid delay (months) of the case in scope."""
    return _delay_cv.get()


@contextmanager
def case_scope(spec):
    """Run a block against a developer case (stress events / template values); None or a default spec = the packet benchmark."""
    token = _delay_cv.set(int(getattr(spec, 'grid_delay_months', None) or 0))
    try:
        with CS.using(spec):
            yield
    finally:
        _delay_cv.reset(token)


def is_custom(spec):
    return spec is not None and (not spec.is_default() or int(spec.grid_delay_months or 0) != 0)


def case_key(spec):
    return 'bench' if not is_custom(spec) else f'{spec.key()}_d{int(spec.grid_delay_months or 0)}'


def spec_from_payload(payload):
    """The case the page asked for: an uploaded template and/or the stress events of a chosen state's interconnection."""
    cp = (payload or {}).get('case') or {}
    tpl = _templates.get(cp.get('templateId'))
    spec = copy.deepcopy(tpl) if tpl is not None and not tpl.errors else CS.CaseSpec()
    if cp.get('state'):
        area = CATALOG.area_of(cp['state'], cp.get('area'))
        ev = CATALOG.events_of(area) if area else None
        if ev is not None and len(ev):
            spec.stress_events, spec.stress_area = SD.events_as_list(ev), area
    return spec


def starts_for(spec):
    p = spec.planned_opening()
    return sorted(set(GO_LIVE_STARTS) | ({p} if 14 <= p <= MAX_OPENING else set()))


def _pool(no_t2):
    key = 'no_t2' if no_t2 else 'base'
    with _lock:
        if key not in _pools:
            df = L.load_pool(key)
            _pools[key] = df[(df.start >= 14) & (df.start <= MAX_OPENING)].reset_index(drop=True)
        return _pools[key]


def case_pool(spec):
    """Candidate portfolios for the case in scope: the packet's pool, or the case's own table once it has been built (None until then)."""
    if not is_custom(spec):
        return _pool(False)
    key = ('custom', case_key(spec))
    with _lock:
        if key in _pools:
            return _pools[key]
    df = CP.load(spec, POLICY(spec.grid_delay_months or 0))
    if df is None or df.empty:
        return None
    df = df[(df.start >= 14) & (df.start <= MAX_OPENING)].reset_index(drop=True)
    with _lock:
        _pools[key] = df
    return df


def warm():
    """Load both pools and one context so the first request is fast."""
    _pool(False), _pool(True)
    gm.get_ctx(20, 0)


# ------------------------------------------------------------------------------------------------ helpers
def f(x):
    return float(x) if x is not None and np.isfinite(x) else None


def name_portfolio(r):
    clean, nuc = float(r.utility_solar + r.wind), float(r.nuclear)
    gas = float(r.gas_turbine + r.gas_engine + r.fuel_cell)
    firm = clean + nuc
    if gas == 0 and firm == 0:
        style = 'Merchant-only'
    elif gas == 0:
        style = 'Contracts-led'
    elif firm == 0:
        style = 'Gas-led'
    else:
        style = 'Clean-led' if firm > 2 * gas else ('Balanced' if firm > gas / 2 else 'Gas-led')
    parts = []
    for k, lab in (('gas_turbine', 'turbine'), ('gas_engine', 'engines'), ('fuel_cell', 'fuel cell')):
        if r[k] > 0:
            parts.append(f'{lab} {r[k]:g}')
    for k, lab in (('wind', 'wind'), ('utility_solar', 'solar'), ('nuclear', 'nuclear'), ('battery_4h', 'battery')):
        if r[k] > 0:
            parts.append(f'{lab} {r[k]:g}')
    return f'{style} portfolio', ' + '.join(parts) + ' MW' if parts else 'grid and merchant power only'


def caps_of(r):
    return {k: float(r[k]) for k in E.KEYS}


def onsite_mw(r):
    return float(r.gas_turbine + r.gas_engine + r.fuel_cell + r.battery_4h)


def _row_key(r):
    return (int(r.start),) + tuple(float(r[k]) for k in E.KEYS)


def _export_id(start, caps, intr):
    return hashlib.sha1(json.dumps([int(start), sorted(caps.items()), bool(intr), _case_id()]).encode()).hexdigest()[:12]


# ------------------------------------------------------------------------------------------------ meta
def _stress_info(spec):
    """What the stress events of this case look like, for the page's text and event table."""
    ev = spec.stress_events if spec is not None and spec.stress_events is not None else CS.REF_EVENTS
    area = (spec.stress_area if spec is not None and spec.stress_area else SD.ERCOT)
    t = pd.DataFrame(ev)
    st, en = pd.to_datetime(t.start_utc, utc=True), pd.to_datetime(t.end_utc, utc=True)
    hrs = (en - st).dt.total_seconds() / 3600
    months = sorted(set(st.dt.month_name().tolist()), key=lambda m: pd.Timestamp(f'{m} 1 2023').month)
    listed = CATALOG.event_list(area) if area in set(CATALOG.events.interconnection_area) else []
    return dict(area=area, count=int(len(t)), minHours=float(hrs.min()), maxHours=float(hrs.max()), totalHours=float(hrs.sum()), months=months,
                importPct=float(t.import_fraction.median() * 100), noticeMin=float(t.notice_minutes.median()), events=listed, source=CATALOG.source)


def case_info(spec, payload=None):
    cp = (payload or {}).get('case') or {}
    k = case_key(spec)
    b = _builds.get(k, {})
    custom = is_custom(spec)
    ready = (not custom) or case_pool(spec) is not None
    return dict(key=k, custom=custom, ready=bool(ready), state=cp.get('state'), area=spec.stress_area if spec.stress_events is not None else None, templateApplied=bool(cp.get('templateId')) and cp.get('templateId') in _templates,
                build=dict(status=('ready' if ready else b.get('state', 'idle')), message=b.get('message', ''), error=b.get('error'), seconds=float(b['finished'] - b['started']) if b.get('finished') else (float(time.time() - b['started']) if b.get('started') else 0.0)),
                starts=starts_for(spec), goLive=spec.planned_opening())


def meta(payload=None):
    spec = spec_from_payload(payload)
    with case_scope(spec):
        return _meta(spec, payload)


def _meta(spec, payload):
    start0 = spec.planned_opening() if 14 <= spec.planned_opening() <= 109 else 20
    ctx = gm.get_ctx(start0, D())
    hourly = cs.hourly
    yrs = lambda a: [float(a[ctx.year == y].mean()) for y in range(10)]
    grid = '; '.join(f'{x:g} MW from month {int(m)}' for m, x in zip(cs.grid_schedule.from_month, cs.grid_schedule.max_import_mw))
    stress = _stress_info(spec)
    srcs = [
        ('Campus demand ramp', f'Load at opening, then rising until full at opening + {int(cs.case["ramp_duration_months"])} months. Peak {float(ctx.load.max()):.0f} MW, protected Tier 0 peak {float(ctx.tier0.max()):.1f} MW.', 'PACKET · CASE ASSUMPTION'),
        ('ERCOT grid emissions', f'{yrs(ctx.grid_co2)[0]:.3f} t/MWh in year 1, falling 3%/yr to {yrs(ctx.grid_co2)[9]:.3f} by year 10 (annual average, not marginal).', 'PACKET · PUBLIC DATA'),
        ('Natural gas price', f'Henry Hub 2023 +3%/yr plus $1 adder: ${yrs(ctx.gas_price)[0]:.2f} to ${yrs(ctx.gas_price)[9]:.2f}/MMBtu delivered.', 'PACKET · PUBLIC DATA'),
        ('Fuel cell heat rate', f'{float(gm.OPT.loc["fuel_cell", "heat_rate_mmbtu_per_mwh_hhv"]):g} MMBtu/MWh = {float(gm.OPT.loc["fuel_cell", "co2_t_per_mwh"]) * 1000:.0f} kg CO₂/MWh. Natural gas: no clean credit.', 'PACKET · CASE ASSUMPTION'),
        ('Solar capacity factor', f'{float(hourly.solar_proxy_cf.mean()) * 100:.1f}% average (ERCOT 2023 regional profile, repeated each year).', 'PACKET · PUBLIC DATA'),
        ('Wind generation profile', f'{float(hourly.wind_proxy_cf.mean()) * 100:.1f}% average (ERCOT 2023 regional profile, repeated each year).', 'PACKET · PUBLIC DATA'),
        ('Battery round-trip efficiency', f'{float(gm.OPT.loc["battery_4h", "round_trip_efficiency"]) * 100:.0f}% (4 hours), up to {int(gm.OPT.loc["battery_4h", "max_cycles_per_year"])} cycles a year, charged only from clean contract energy.', 'PACKET · PUBLIC DATA'),
        ('Maintenance intervals', 'Not modeled. The model charges fixed O&M ($/kW-yr) and variable O&M ($/MWh) from the packet; there is no maintenance schedule or outage input.', 'NOT MODELED'),
        ('Grid import schedule', f'{grid}; {stress["importPct"]:.0f}% of that during stress events.', 'PACKET · CASE ASSUMPTION' if not spec.grid_schedule else 'DEVELOPER TEMPLATE'),
        ('Relocated load value', f'${float(ctx.reloc_price):.2f}/MWh (remote power plus service charge), up to {float(gm.RESP["remote_capacity_mw"]):g} MW at the remote site; interruption ${float(ctx.intr_price):,.0f}/MWh.', 'PACKET · CASE ASSUMPTION'),
        ('Stress events', f'{stress["count"]} events a year, {stress["minHours"]:g}–{stress["maxHours"]:g} hours each ({", ".join(stress["months"])}); the grid connection is cut to {stress["importPct"]:.0f}% of its limit. Interconnection: {stress["area"]}.', 'PACKET · CASE ASSUMPTION' if stress['source'] == 'bundled' else 'UPLOADED FILE'),
    ]
    return dict(
        campusPeakMw=float(ctx.load.max()), tier0PeakMw=float(ctx.tier0.max()), months=int(cs.case['horizon_months']), hours=int(ctx.n), resources=len(gm.RESOURCES),
        gridSchedule=[dict(fromMonth=int(m), maxImportMw=float(x)) for m, x in zip(cs.grid_schedule.from_month, cs.grid_schedule.max_import_mw)],
        choices=dict(goLive=(GO_LIVE_CHOICES if not is_custom(spec) else starts_for(spec)), carbonMax=CARBON_CHOICES, costPct=COST_CHOICES, resources=[dict(id=k, label=l) for k, l in RESOURCE_LABELS],
                     rampMonths=int(cs.case['ramp_duration_months'])),
        presets={k: dict(v) for k, v in PRESETS.items()},
        defaults=dict(weights=[34, 33, 33]),
        assumptions=[dict(name=n, detail=d, source=s) for n, d, s in srcs],
        flags=FLAGS, window=dict(minOpening=14, maxOpening=MAX_OPENING),
        poolSizes=dict(base=len(_pool(False)), noTier2=len(_pool(True))),
        response=dict(relocationUsdPerMwh=float(ctx.reloc_price), interruptionUsdPerMwh=float(ctx.intr_price), remoteMw=float(gm.RESP['remote_capacity_mw'])),
        stress=stress, case=case_info(spec, payload),
        catalog=catalog_info(),
    )


def catalog_info():
    return dict(source=CATALOG.source, areas=CATALOG.areas(), states=CATALOG.state_rows())


# ------------------------------------------------------------------------------------------------ the recommendation
def _opt(payload, key, allowed=None):
    v = payload.get(key)
    if v is None or v == '':
        return None
    v = float(v)
    return v if allowed is None or v in allowed else None


def _parse(payload):
    w = [max(0.0, float(x)) for x in (payload.get('weights') or [1, 1, 1])][:3]
    if len(w) != 3 or sum(w) <= 0:
        w = [1.0, 1.0, 1.0]
    go = _opt(payload, 'goLive', GO_LIVE_CHOICES)
    excl = [k for k in (payload.get('excluded') or []) if k in E.KEYS]
    return dict(weights=w, go_live=None if go is None else int(go), carbon=_opt(payload, 'carbonMax'), cost=_opt(payload, 'costPct'), excluded=excl)


def select(payload, spec=None):
    """Filter and rank. Returns a dict with the chosen row, the pools and the bookkeeping the page needs.
    Limits (go-live month, carbon, cost) are hard filters; the weights only rank what is left."""
    p = _parse(payload)
    w, limit, excl = p['weights'], dict(go_live=p['go_live'], carbon=p['carbon'], cost=p['cost']), p['excluded']
    notes = []
    spec = spec if spec is not None else spec_from_payload(payload)
    pool = case_pool(spec)
    if pool is None:
        return dict(status='needs_build', pool=None, weights=w, limit=limit, excluded=excl, notes=notes, month_value=0.0)
    if excl:
        pool = pool[(pool[excl] == 0).all(axis=1)].reset_index(drop=True)
    base = dict(weights=w, limit=limit, excluded=excl, notes=notes, month_value=0.0)
    if pool.empty:
        return dict(status='no_match', pool=pool, **base)
    ref = pool
    mask = pd.Series(True, index=pool.index)
    if limit['go_live'] is not None:
        mask &= pool.start <= limit['go_live']
    if limit['carbon'] is not None:
        mask &= pool.intensity <= limit['carbon']
    base_pool = pool[mask]
    if base_pool.empty:
        return dict(status='no_match', pool=pool, **base)
    cheapest = float(base_pool.total_cost.min())
    cap = cheapest * (1 + limit['cost'] / 100.0) if limit['cost'] is not None else None
    if cap is not None:
        mask &= pool.total_cost <= cap
    surv = pool[mask]
    ranked = L.rank(surv, w, 0.0, 0.0, ref=ref).sort_values(['score', 'total_cost'], ascending=[False, True]).reset_index(drop=True)
    return dict(status='ok', pool=pool, surv=surv, ranked=ranked, cheapest=cheapest, cap=cap, ref=ref, **base)


def _meets(r, limit, cap):
    return dict(speed=limit['go_live'] is None or bool(r.start <= limit['go_live']), carbon=limit['carbon'] is None or bool(r.intensity <= limit['carbon']),
                cost=cap is None or bool(r.total_cost <= cap + 1e-6))


def _option_rows(sel):
    pool, ranked, limit, cap = sel['pool'], sel['ranked'], sel['limit'], sel['cap']
    best = ranked.iloc[0]
    picks = [('Recommended', best)]
    seen = {_row_key(best)}
    cands = [('Fastest overall', pool.sort_values(['full_month', 'total_cost']).iloc[0]), ('Cheapest overall', pool.sort_values(['total_cost', 'full_month']).iloc[0]),
             ('Cleanest overall', pool.sort_values(['intensity', 'total_cost']).iloc[0])]
    if len(ranked) > 1:
        cands.append(('Runner-up', ranked.iloc[1]))
    for role, r in cands:
        if _row_key(r) not in seen and len(picks) < 4:
            picks.append((role, r)); seen.add(_row_key(r))
    for i in range(1, min(len(ranked), 8)):
        r = ranked.iloc[i]
        if len(picks) >= 4:
            break
        if _row_key(r) not in seen:
            picks.append(('Next best', r)); seen.add(_row_key(r))
    rows = []
    short = dict(speed='GO-LIVE', carbon='CARBON', cost='COST')
    for role, r in picks:
        mt = _meets(r, limit, cap)
        miss = [short[k] for k, ok in mt.items() if not ok]
        nm, detail = name_portfolio(r)
        label = 'Recommended portfolio' if role == 'Recommended' else role
        n_same = sum(1 for x in rows if x['role'] == role)
        rows.append(dict(role=role, name=nm, label=label if not n_same else f'{label} ({n_same + 1})', detail=detail, caps={k: v for k, v in caps_of(r).items() if v}, start=int(r.start), opening=int(r.start), fullDemand=int(r.full_month), costM=float(r.total_cost / 1e6), kgPerMwh=float(r.intensity),
                         backupMw=onsite_mw(r), meets=mt, tag='RECOMMENDED' if role == 'Recommended' else ('MEETS ALL' if not miss else 'MISSES ' + ' + '.join(miss))))
    return rows



TABLE_ORDER = [('gas_turbine', 'Gas turbine', 'Onsite'), ('gas_engine', 'Gas engine', 'Onsite'), ('fuel_cell', 'Fuel cell (natural gas)', 'Onsite'), ('battery_4h', 'Battery (4 h)', 'Onsite'),
               ('wind', 'Wind contract', 'Via grid'), ('utility_solar', 'Solar contract', 'Via grid'), ('nuclear', 'Nuclear PPA', 'Via grid')]


def portfolio_table(row, hourly, ctx, start):
    """Every resource the model can pick, selected or not: size, what it delivers, what it costs."""
    num, rate = E.NUM, float(gm.CASE['discount_rate_real'])
    served = float((ctx.load - hourly['tier1_relocated_mw'] - hourly['tier2_curtailed_mw']).sum())
    out = []
    for k, label, loc in TABLE_ORDER:
        o = gm.OPT.loc[k]
        mw = float(row[k])
        gen = np.asarray(hourly['battery_4h_discharge_mw'] if k == 'battery_4h' else hourly[f'{k}_mw'], float)
        hours = float(ctx.mask[k].sum())
        crf = rate / (1 - (1 + rate) ** (-num(o.life_years)))
        fixed = mw * 1000 * (num(o.capex_usd_per_kw) * crf + num(o.fom_usd_per_kw_year)) / 1e6
        out.append(dict(id=k, label=label, location=loc, selected=mw > 0, mw=mw, maxMw=float(num(o.max_mw)), blockMw=float(num(o.block_mw)), availableMonth=int(num(o.available_month)),
                        fromMonth=int(max(start, num(o.available_month))), landAcres=float(mw * num(o.land_acres_per_mw)), fixedChargeMPerYear=float(fixed) if mw > 0 and num(o.capex_usd_per_kw) > 0 else None,
                        priceUsdPerMwh=float(num(o.payment_usd_per_mwh)) if o.payment_basis == 'available_generation' else None, energyGwh=float(gen.sum() / 1000),
                        sharePct=float(gen.sum() / served * 100) if served > 0 and k != 'battery_4h' else None, capacityFactorPct=float(gen.sum() / (mw * hours) * 100) if mw > 0 and hours > 0 else None))
    return out


def cost_breakdown(R, maint):
    H = R['yearly'].loc['horizon']
    tot = float(H.total_decision_cost)
    rows = [('Equipment capacity charges (CAPEX annuity + fixed O&M)', H.capacity_cost, 'Paid every hour once a resource is committed, used or not'),
            ('Contract payments (wind, solar, nuclear)', H.contract_cost, 'Paid for all available energy, including curtailed'),
            ('Gas fuel + variable O&M', H.gas_cost, 'Only when the units run'),
            ('Merchant (grid) electricity', H.merchant_cost, 'Hourly market price + $20/MWh adder'),
            ('Relocation of Tier 1 work', H.relocation_cost, 'Remote power + service charge, stress events only'),
            ('Interruption of Tier 2 work', H.interruption_cost, 'Consequence charge, stress events only')]
    out = [dict(label=l, valueM=float(v / 1e6), sharePct=float(v / tot * 100) if tot else 0.0, note=n) for l, v, n in rows]
    return dict(rows=out, totalM=tot / 1e6, maintenanceM=float(maint['total'] / 1e6), maintenanceFixedM=float(maint['fixed_om'] / 1e6), maintenanceVariableM=float(maint['variable_om'] / 1e6))


def _case_id():
    return CS.active_key() + f'_d{D()}'


def _simulate(row, intr):
    """Dispatch the chosen portfolio once and derive every time-series number the page needs. Cached by portfolio."""
    caps = caps_of(row)
    start = int(row.start)
    key = ('sim', _case_id(), start, tuple(sorted(caps.items())), bool(intr))
    with _lock:
        if key in _cache:
            return _cache[key]
    sim = A.simulate(caps, start, D(), True, intr)
    h = sim['_hourly']
    ctx = gm.get_ctx(start, D())
    n = ctx.n
    mon = lambda a: np.asarray(a, float).reshape(120, 730).mean(axis=1)
    gas = h['gas_turbine_mw'] + h['gas_engine_mw'] + h['fuel_cell_mw']
    contracts = h['utility_solar_mw'] + h['wind_mw'] + h['nuclear_mw']
    renew_to_load = h['utility_solar_mw'] + h['wind_mw'] - h['battery_4h_charge_mw']
    clean_gen = h['utility_solar_mw'] + h['wind_mw']
    to_load = np.divide(np.maximum(clean_gen - h['battery_4h_charge_mw'], 0), clean_gen, out=np.ones(n), where=clean_gen > 1e-9)      # energy that went into the battery is not serving load
    monthly = dict(gas=mon(gas), merchant=mon(h['grid_mw']), renewables=mon(np.maximum(renew_to_load, 0)), nuclear=mon(h['nuclear_mw']), battery=mon(h['battery_4h_discharge_mw']),
                   gas_turbine=mon(h['gas_turbine_mw']), gas_engine=mon(h['gas_engine_mw']), fuel_cell=mon(h['fuel_cell_mw']), wind=mon(h['wind_mw'] * to_load), solar=mon(h['utility_solar_mw'] * to_load),
                   relocated=mon(h['tier1_relocated_mw']), interrupted=mon(h['tier2_curtailed_mw']), demand=mon(ctx.load), gridCap=mon(ctx.base['frame'].ordinary_grid_limit_mw.to_numpy()))
    # stress hour: the highest-demand hour of a stress event in the first full year after full demand
    full = start + int(cs.case['ramp_duration_months'])
    y = next((yy for yy in range(1, 11) if (yy - 1) * 8760 >= full * 730), 10)
    sel = np.where(ctx.active.astype(bool) & (ctx.year == y - 1))[0]
    i = int(sel[np.argmax(ctx.load[sel])]) if len(sel) else int(np.argmax(ctx.load))
    ts = str(cs.hourly.timestamp_local.iloc[i % 8760])
    hour_label = ts[11:16] if len(ts) >= 16 else '17:00'
    imports = contracts + h['grid_mw']
    stress = dict(hour=hour_label, modelHour=i, demandMw=float(ctx.load[i]), tier0Mw=float(ctx.tier0[i]), gridMw=float(imports[i]), gridCapMw=float(ctx.grid_limit[i]), gasMw=float(gas[i]),
                  batteryMw=float(h['battery_4h_discharge_mw'][i]), relocatedMw=float(h['tier1_relocated_mw'][i]), interruptedMw=float(h['tier2_curtailed_mw'][i]))
    act = ctx.active.astype(bool)
    after = act & (ctx.month >= full)
    reliab = dict(stressHoursPerYear=int(act[ctx.year == 0].sum()),
                  stressHoursNoInterruptionPct=float(100.0 * (h['tier2_curtailed_mw'][act & (ctx.month >= start)] <= 1e-6).mean()) if (act & (ctx.month >= start)).any() else 100.0,
                  capMetPct=float(100.0 * (imports[act & (ctx.month >= start)] <= ctx.grid_limit[act & (ctx.month >= start)] + 1e-6).mean()) if (act & (ctx.month >= start)).any() else 100.0,
                  tier0ServedAlways=bool(np.all(ctx.load - h['tier1_relocated_mw'] - h['tier2_curtailed_mw'] >= ctx.tier0 - 1e-6)))
    yr = np.asarray(ctx.year)
    reloc, intr_ = np.asarray(h['tier1_relocated_mw'], float), np.asarray(h['tier2_curtailed_mw'], float)
    stress_years = [dict(eventHours=int((act & (yr == i)).sum()), relocatedMwh=float(reloc[yr == i].sum()), interruptedMwh=float(intr_[yr == i].sum())) for i in range(10)]
    ids = ctx.base['frame'].stress_event_id.to_numpy().astype(str)
    ev_mask = act & (yr == y - 1)
    stress_events = []
    for eid_ in dict.fromkeys(ids[ev_mask]):
        m = ev_mask & (ids == eid_)
        stress_events.append(dict(id=eid_.split('_', 1)[1] if '_' in eid_ else eid_, hours=int(m.sum()), demandMw=float(ctx.load[m].max()), gridCapMw=float(ctx.grid_limit[m].mean()), gridMw=float(imports[m].max()),
                                  gasMw=float(gas[m].max()), relocatedMwh=float(reloc[m].sum()), interruptedMwh=float(intr_[m].sum())))
    out = dict(sim=sim, monthly={k: v.tolist() for k, v in monthly.items()}, stress=stress, reliability=reliab, full=full, stress_years=stress_years, stress_events=stress_events, stress_year=int(y))
    with _lock:
        _cache[key] = out
        if len(_cache) > 64:
            _cache.pop(next(iter(_cache)))
    return out


def recommend(payload):
    spec = spec_from_payload(payload)
    with case_scope(spec):
        sel = select(payload, spec)
        if sel['status'] == 'needs_build':
            p_ = _parse(payload)
            return dict(status='needs_build', inputs=dict(weights=p_['weights'], goLive=p_['go_live'], carbonMax=p_['carbon'], costPct=p_['cost'], excluded=p_['excluded']), notes=[], flags=FLAGS, case=case_info(spec, payload))
        out = _recommend(payload, spec, sel)
        out['case'] = case_info(spec, payload)
        return out


def _recommend(payload, spec, sel):
    lim = sel['limit']
    meta_ = dict(weights=sel['weights'], goLive=lim['go_live'], carbonMax=lim['carbon'], costPct=lim['cost'], excluded=sel['excluded'])
    if sel['status'] == 'no_match':
        pool = sel['pool']
        names = dict(RESOURCE_LABELS)
        relax = []
        if pool.empty:
            relax.append('No tested portfolio avoids ' + ', '.join(names[k] for k in sel['excluded']) + '. Make at least one of them available again.')
        else:
            by_go = pool if lim['go_live'] is None else pool[pool.start <= lim['go_live']]
            by_co = pool if lim['carbon'] is None else pool[pool.intensity <= lim['carbon']]
            if len(by_go) == 0:
                relax.append(f'No tested portfolio can go live by month {lim["go_live"]}. The earliest tested opening is month {int(pool.start.min())}.')
            if len(by_co) == 0:
                relax.append(f'No tested portfolio reaches {lim["carbon"]:.0f} kg CO₂/MWh. The cleanest found is {pool.intensity.min():.0f} kg/MWh.')
            if len(by_go) and len(by_co):
                relax.append(f'Going live by month {lim["go_live"]} and staying under {lim["carbon"]:.0f} kg CO₂/MWh cannot both be met: the cleanest portfolio live by then reaches {by_go.intensity.min():.0f} kg/MWh.')
            if sel['excluded']:
                relax.append('Resources marked unavailable: ' + ', '.join(names[k] for k in sel['excluded']) + '.')
        return dict(status='no_match', inputs=meta_, notes=sel['notes'], message='No portfolio meets every limit you set.', relaxations=relax,
                    tested=int(len(pool)), flags=FLAGS)
    ranked, pool = sel['ranked'], sel['pool']
    best = ranked.iloc[0]
    intr_allowed = True
    simd = _simulate(best, intr_allowed)
    caps = caps_of(best)
    start = int(best.start)
    ctx = gm.get_ctx(start, D())
    maint = R.maintenance(caps, start, simd['sim']['_hourly'])
    bk = R.backup_options(caps, start, D(), float(best.total_cost))
    batt_mw = float(best.battery_4h)
    bat = gm.OPT.loc['battery_4h']
    deliver = batt_mw * bat.duration_hours * float(np.sqrt(bat.round_trip_efficiency))
    ride = float(deliver / ctx.tier0.max() * 60.0) if batt_mw > 0 else 0.0
    gen_mw = float(best.gas_turbine + best.gas_engine + best.fuel_cell)
    clean_mw = float(best.utility_solar + best.wind + best.nuclear)
    _style, detail = name_portfolio(best)
    name = 'Recommended portfolio'
    rows = _option_rows(sel)
    runner = ranked.iloc[1] if len(ranked) > 1 else None
    gap = float(best.score - runner.score) if runner is not None else None
    why = f'It scores {best.score:.0f}/100 on your priorities' + ('' if gap is None else (', essentially tied with the runner-up' if gap < 0.5 else f', {gap:.0f} point{"s" if round(gap) != 1 else ""} ahead of the runner-up')) + '.'
    if runner is not None:
        dc, dm, dk = (runner.total_cost - best.total_cost) / 1e6, int(runner.full_month - best.full_month), runner.intensity - best.intensity
        bits = []
        if abs(dc) >= 0.5: bits.append(f'${abs(dc):.0f}M {"cheaper" if dc > 0 else "more expensive"}')
        if dm: bits.append(f'{abs(dm)} month{"s" if abs(dm) != 1 else ""} {"sooner" if dm > 0 else "later"}')
        if abs(dk) >= 1: bits.append(f'{abs(dk):.0f} kg/MWh {"cleaner" if dk > 0 else "dirtier"}')
        if bits:
            why += ' Against the runner-up it is ' + ', '.join(bits) + '.'
    nstep = (f'Validate the utility milestones for month {start}, then issue an RFP for {gen_mw:.0f} MW of onsite gas' if gen_mw > 0 else f'Validate the utility milestones for month {start}')
    nstep += f' and {float(best.utility_solar + best.wind):.0f} MW of wind and solar contracts.' if best.utility_solar + best.wind > 0 else '.'
    sel_cap = [f'{gen_mw:.0f} MW onsite gas' if gen_mw else 'no onsite gas', f'{deliver / np.sqrt(bat.round_trip_efficiency):.0f} MWh battery' if batt_mw else 'no battery', f'{clean_mw:.0f} MW contracted clean']
    full = int(best.full_month)
    monthly = simd['monthly']
    act = [i for i in range(10) if max(monthly['demand'][12 * i:12 * i + 12]) > 1e-9]       # model years with demand: same years as the price sensitivity
    yavg = lambda a_: [float(np.mean(a_[12 * i:12 * i + 12])) for i in act]
    power = dict(years=[i + 1 for i in act], span=[f'M{12 * i}–M{12 * i + 11}' for i in act], series={k: yavg(v) for k, v in monthly.items()})
    eid = _export_id(start, caps, intr_allowed)
    with _lock:
        _exports[eid] = dict(start=start, caps=caps, sim=simd['sim'], spec=spec, delay=D())
        if len(_exports) > 16:
            _exports.pop(next(iter(_exports)))
    R_ = _report(eid)
    ptable = portfolio_table(best, simd['sim']['_hourly'], ctx, start)
    cbreak = cost_breakdown(R_, maint)
    yl, sy = R_['yearly'], simd['stress_years']
    power['stress'] = dict(eventHours=[sy[i]['eventHours'] for i in act], relocatedMwh=[sy[i]['relocatedMwh'] for i in act], interruptedMwh=[sy[i]['interruptedMwh'] for i in act],
                           relocationM=[float(yl.loc[i + 1, 'relocation_cost'] / 1e6) for i in act], interruptionM=[float(yl.loc[i + 1, 'interruption_cost'] / 1e6) for i in act],
                           events=simd['stress_events'], eventYear=simd['stress_year'])
    allp = sel['pool']
    pt = lambda r: dict(opening=int(r.start), fullDemand=int(r.full_month), costM=float(r.total_cost / 1e6), kgPerMwh=float(r.intensity), cleanSharePct=float(r.clean_share * 100), detail=name_portfolio(r)[1])
    w_ = sel['weights']
    names_ = ['speed', 'cost', 'carbon']
    top = 'balanced' if max(w_) - min(w_) <= 0.15 * sum(w_) else names_[int(np.argmax(w_))]
    tradeoffs = dict(top=top, weights=[float(x) for x in w_], recommended=pt(best),
                     anchors=dict(cheapest=pt(allp.sort_values(['total_cost', 'full_month']).iloc[0]), fastest=pt(allp.sort_values(['full_month', 'total_cost']).iloc[0]),
                                  cleanest=pt(allp.sort_values(['intensity', 'total_cost']).iloc[0])))
    with _lock:
        _exports[eid]['options'] = rows
        _exports[eid]['intr'] = bool(intr_allowed)
    notes = list(sel['notes'])
    if is_custom(spec):
        bits = (['your stress events (' + str(spec.stress_area) + ')'] if spec.stress_events is not None and spec.stress_area else []) + (['your template values'] if spec.overrides().keys() - {'stress_events'} or spec.grid_delay_months else [])
        notes.append('This run uses ' + ' and '.join(bits or ['a custom case']) + ', not the packet benchmark. The exported files are custom-case results: the checker\'s physical rules and accounting still verify them, but the official submission must use the benchmark inputs.')
    if batt_mw == 0:
        notes.append('This portfolio has no battery, so the battery ride-through card shows 0 (see flags).')
    if bk['shortfall_mw'] > 0.05:
        notes.append(f'Firm onsite capacity (largest unit out) is {bk["firm_mw"]:.0f} MW against a {bk["critical_mw"]:.1f} MW protected load: a {bk["shortfall_mw"]:.1f} MW backup gap (extension, not in the common cost).')
    lims = [f'first power by month {lim["go_live"]}' if lim['go_live'] is not None else 'any go-live month',
            f'at most {lim["carbon"]:.0f} kg CO2/MWh' if lim['carbon'] is not None else 'no carbon limit',
            f'within {lim["cost"]:.0f}% of the cheapest' if lim['cost'] is not None else 'no cost limit']
    if sel['excluded']:
        lims.append('unavailable: ' + ', '.join(dict(RESOURCE_LABELS)[k] for k in sel['excluded']))
    cust = f"priorities speed/cost/carbon {sel['weights'][0]:g}/{sel['weights'][1]:g}/{sel['weights'][2]:g}; " + '; '.join(lims)
    with _lock:
        _exports[eid].update(co2_target=float(lim['carbon'] if lim['carbon'] is not None else DEFAULT_CO2_LINE), info=dict(name=f'{name} ({detail})', customer=cust, maintenance=float(maint['total'] / 1e6), stress=simd['stress'], notes=list(notes)))
    return dict(
        status='ok', inputs=meta_, notes=notes, flags=FLAGS, exportId=eid,
        search=dict(tested=int(len(pool)), passing=int(len(sel['surv'])), scored=int(len(ranked)), cheapestM=f(sel['cheapest'] / 1e6), costCapM=None if sel['cap'] is None else f(sel['cap'] / 1e6)),
        recommendation=dict(name=name, detail=detail, opening=start, fullDemand=full, score=float(best.score), capacities={k: v for k, v in caps.items() if v},
                            capacityText=' · '.join(sel_cap), why=why, nextStep=nstep,
                            kpis=dict(costM=float(best.total_cost / 1e6), maintenanceM=float(maint['total'] / 1e6), dollarsPerMwh=float(best.local_energy_cost / best.served_mwh), kgPerMwh=float(best.intensity),
                                      cleanSharePct=float(best.clean_share * 100), onsiteBackupMw=onsite_mw(best), servedTwh=float(best.served_mwh / 1e6), interruptedMwh=float(best.interrupted_mwh),
                                      relocatedMwh=float(best.relocated_mwh), decisionCostPerRequestedMwh=float(best.total_cost / best.requested_mwh))),
        options=rows, portfolioTable=ptable, costBreakdown=cbreak, tradeoffs=tradeoffs,
        powerOverTime=power,
        stress=simd['stress'],
        reliability=dict(onsiteBackupMw=onsite_mw(best), tier0PeakMw=float(ctx.tier0.max()), batteryMw=batt_mw, batteryMwh=float(batt_mw * bat.duration_hours), batteryDeliverableMwh=float(deliver),
                         rideThroughMinutes=ride, maintenanceM=float(maint['total'] / 1e6), maintenanceFixedM=float(maint['fixed_om'] / 1e6), maintenanceVariableM=float(maint['variable_om'] / 1e6),
                         **simd['reliability'], backup=dict(criticalMw=bk['critical_mw'], firmN1Mw=bk['firm_mw'], shortfallMw=bk['shortfall_mw'], options=[dict(name=o['option'], addedMw=o['added_mw'], totalM=o['total_cost'] / 1e6, note=o['note']) for o in bk['options']])),
    )


def _scoped(fn):
    """Run an export-id function inside the case that export was made under."""
    def wrapper(eid, *a, **k):
        with _lock:
            e = _exports.get(eid)
        if e is None:
            return fn(eid, *a, **k)
        with case_scope(e.get('spec')):
            return fn(eid, *a, **k)
    wrapper.__name__ = fn.__name__
    return wrapper


@_scoped
def verify(export_id):
    """Run the official checker on the exported dispatch (benchmark case)."""
    with _lock:
        e = _exports.get(export_id)
    if e is None:
        return None
    if 'verify' in e:
        return e['verify']
    d = pd.DataFrame(e['sim']['_hourly']); d.insert(0, 'model_hour', range(len(d)))
    pf = {'initial_service_month': e['start'], 'grid_delay_months': D(), 'capacities_mw': {k: (int(v) if float(v).is_integer() else v) for k, v in e['caps'].items()}}
    try:
        chk = cs.score(pf, d[X.COLS])
    except ValueError as exc:
        return dict(passed=False, error=str(exc))
    res_ = dict(passed=bool(chk['physical_checks_passed']), totalDecisionCostM=float(chk['total_decision_cost_usd'] / 1e6), kgPerMwh=float(chk['combined_electricity_co2_kg_per_mwh']),
                maxGridImportMw=float(chk['max_grid_import_mw']), fullDemandMonth=int(chk['full_demand_month']))
    with _lock:
        e['verify'] = res_
    return res_


@_scoped
def export_files(export_id):
    with _lock:
        e = _exports.get(export_id)
    if e is None:
        return None
    d = pd.DataFrame(e['sim']['_hourly']); d.insert(0, 'model_hour', range(len(d)))
    pf = {'initial_service_month': e['start'], 'grid_delay_months': D(), 'capacities_mw': {k: (int(v) if float(v).is_integer() else v) for k, v in e['caps'].items()}}
    return json.dumps(pf, indent=1), d[X.COLS].to_csv(index=False)


def risks(payload):
    """The 'what could go wrong' list for the recommended portfolio (not shown by the page; available to it)."""
    spec = spec_from_payload(payload)
    with case_scope(spec):
        sel = select(payload, spec)
        if sel['status'] != 'ok':
            return []
        best = sel['ranked'].iloc[0]
        return R.risk_report(int(best.start), caps_of(best), (D(), 'Base', True, True))


# ------------------------------------------------------------------------------------------------ year-by-year report
@_scoped
def _report(eid):
    with _lock:
        e = _exports.get(eid)
        if e is None:
            return None
        if eid in _reports:
            return _reports[eid]
    R = RP.build(e['start'], e['caps'], e['sim']['_hourly'], D())
    with _lock:
        _reports[eid] = R
        if len(_reports) > 16:
            _reports.pop(next(iter(_reports)))
    return R


@_scoped
def report_json(eid):
    R = _report(eid)
    if R is None:
        return None
    e = _exports[eid]
    p = RP.web_payload(R, e.get('co2_target', 100.0))
    keys = [k for k in RC.TITLES if not (k == '5_asset_use' and not any(R['caps'].values()))]
    p['images'] = [dict(key=k, title=RC.TITLES[k], url=f'/api/report/{eid}/{k}.png') for k in keys]
    p['zipUrl'] = f'/api/report/{eid}/report.zip'
    return p


@_scoped
def _charts_dir(eid):
    R = _report(eid)
    if R is None:
        return None
    d = Path(tempfile.gettempdir()) / f'faraday_report_{eid}'
    done = d / '.done'
    if not done.exists():
        chk = verify(eid) or dict(passed=True)
        with _render_lock:
            if not done.exists():
                RC.render_all(R, d, _exports[eid].get('co2_target', 100.0), dict(ok=bool(chk.get('passed')), text='Verified by the official checker' if chk.get('passed') else 'The official checker rejected this export'))
                done.write_text('ok')
    return d


@_scoped
def report_png(eid, filename):
    d = _charts_dir(eid)
    if d is None or '/' in filename or not filename.endswith('.png'):
        return None
    f = d / filename
    return f.read_bytes() if f.exists() else None


@_scoped
def report_zip(eid):
    d = _charts_dir(eid)
    if d is None:
        return None
    info = dict(_exports[eid].get('info', {}))
    info['checker'] = verify(eid)
    return RP.report_zip(_report(eid), d, info)


@_scoped
def sensitivity(eid):
    """Bear / base / bull price sensitivity for the recommended portfolio and each option tested (re-dispatched under each scenario)."""
    with _lock:
        e = _exports.get(eid)
        if e is None:
            return None
        if 'sens' in e:
            return e['sens']
        options, intr = e.get('options', []), e.get('intr', True)
    scen = RP.price_scenarios()
    rows, yearly = [], None
    for o in options:
        s = RP.price_sensitivity(o['start'], o['caps'], D(), True, intr)
        base = s['base']['totalM']
        bk = R.backup_options({k: float(o['caps'].get(k, 0.0)) for k in E.KEYS}, o['start'], D())
        rows.append(dict(label=o['label'], role=o['role'], detail=o['detail'], opening=o['start'], fullDemand=o['fullDemand'], kgPerMwh=o['kgPerMwh'], tag=o['tag'],
                         backup=[dict(name=x['option'], totalM=float(x['total_cost'] / 1e6), addedMw=float(x['added_mw'])) for x in bk['options']], perScenario={k: {kk: vv for kk, vv in v.items() if kk not in ('yearlyTotalM', 'years', 'text', 'label')} for k, v in s.items()},
                         deltaPct={k: float((s[k]['totalM'] / base - 1) * 100) for k in ('bear', 'bull')}))
        if o['role'] == 'Recommended':
            yearly = dict(years=s['base']['years'], series={k: s[k]['yearlyTotalM'] for k in s})
    out = dict(scenarios=[dict(key=k, label=v['label'], text=v['text'], wholesalePct=v['wholesale'] * 100, gasPct=v['gas'] * 100) for k, v in scen.items()], rows=rows, yearly=yearly,
               note='Only wholesale power and delivered gas are re-priced; contracts, equipment charges and the grid CO2 path stay as in the packet. Each scenario re-dispatches the portfolio.')
    with _lock:
        e['sens'] = out
    return out


# ------------------------------------------------------------------------------------------------ developer template (download / upload)
TEMPLATE = HERE / 'templates' / 'developer_input_template.xlsx'


def template_bytes():
    return TEMPLATE.read_bytes()


def read_upload(data: bytes):
    """Parse a filled developer template, remember it as a case the page can run, and say what it changes against the packet benchmark."""
    import io
    try:
        spec = CS.from_template(io.BytesIO(data))
    except Exception as exc:                                    # not an xlsx, corrupt file...
        return dict(ok=False, errors=[f'The file could not be read as the developer template: {exc}'], warnings=[], changed=[], ignored=[], isBenchmark=True)
    rows = [r for r in spec.diff_rows() if r['status'] in ('changed',)]
    tid = hashlib.sha1(data).hexdigest()[:10]
    if not spec.errors:
        _templates[tid] = spec
    return dict(ok=not spec.errors, errors=list(spec.errors), warnings=list(spec.warnings),
                changed=[dict(parameter=str(r['parameter']), packet=str(r['packet']), yours=str(r['yours']), status=r['status'], note=str(r['note'])) for r in rows],
                ignored=[dict(parameter=k, value=str(v)) for k, v in spec.ignored.items()], isBenchmark=not is_custom(spec),
                goLive=spec.default_initial_service_month, templateId=tid if not spec.errors else None)


# ------------------------------------------------------------------------------------------------ stress-event catalog and case building
def upload_any(data: bytes, name: str):
    """One upload button: a filled developer template (xlsx with a 'Project Inputs' sheet) or a stress-event / state file (csv or xlsx)."""
    if bytes(data[:2]) == b'PK':
        try:
            import io, openpyxl
            names = openpyxl.load_workbook(io.BytesIO(data), read_only=True).sheetnames
        except Exception:
            names = []
        if 'Project Inputs' in names:
            return dict(kind='template', **read_upload(data))
    return dict(kind='stress', **stress_upload(data, name))


def stress_upload(data: bytes, name: str):
    """A stress-event catalog (all interconnections) and/or the state list, as csv or xlsx."""
    try:
        events, states, problems = SD.parse(data)
    except Exception as exc:
        return dict(ok=False, problems=[f'The file could not be read: {exc}'], catalog=catalog_info())
    if events is None and states is None:
        return dict(ok=False, problems=problems, catalog=catalog_info())
    CATALOG.update(events, states, name)
    with _lock:
        for k in [k for k in _pools if isinstance(k, tuple)]:
            _pools.pop(k, None)
    return dict(ok=True, problems=problems, read=dict(events=0 if events is None else int(len(events)), states=0 if states is None else int(len(states))), catalog=catalog_info())


def case_status(payload):
    spec = spec_from_payload(payload)
    with case_scope(spec):
        return case_info(spec, payload)


def start_build(payload):
    """Build the case's own portfolio table in the background (about as long as a coffee). Safe to call again while it runs."""
    spec = spec_from_payload(payload)
    k = case_key(spec)
    if not is_custom(spec):
        return case_status(payload)
    with case_scope(spec):
        ready = case_pool(spec) is not None
    if ready:
        return case_status(payload)
    with _build_lock:
        if any(b.get('state') == 'building' for b in _builds.values()):
            if _builds.get(k, {}).get('state') != 'building':
                _builds[k] = dict(state='error', error='Another case is being built. Wait for it to finish, then press the button again.', started=time.time(), finished=time.time())
            return case_status(payload)
        _builds[k] = dict(state='building', message='Searching portfolios for every go-live month…', started=time.time())
    delay = int(spec.grid_delay_months or 0)

    def work():
        try:
            for v in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'MKL_NUM_THREADS'):
                __import__('os').environ[v] = '1'        # one math thread per worker process (more only oversubscribe the cores)
            r = CP.build(spec, POLICY(delay), starts_for(spec), workers=min(8, max(2, (__import__('os').cpu_count() or 4))), progress=lambda m: _builds[k].update(message=f'Searching portfolios: {m}'))
            with _lock:
                _pools.pop(('custom', k), None)
            if r['found'] == 0:
                _builds[k].update(state='error', error='No feasible portfolio was found for this case (for example the site is too small for the load).', finished=time.time())
            else:
                _builds[k].update(state='ready', message=f"{r['found']} portfolios found", finished=time.time())
        except Exception as exc:
            _builds[k].update(state='error', error=str(exc), finished=time.time())

    threading.Thread(target=work, daemon=True).start()
    return case_status(payload)
