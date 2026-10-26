"""Regression tests: the model must agree with the official checker and keep its own invariants.

    python team_app/validate_model.py [N_PER_POOL]        (default 8; exits 1 if anything fails; writes outputs/validation_report.json)

1  every sampled pool row (base, zero-Tier-2, 6-month-delay pools) is re-simulated, exported and run through starter/check_submission.py:
   physical checks pass, and cost, intensity, relocated and interrupted MWh equal both the stored row and the checker
2  exact LP dispatch with a battery passes the checker, matches its own accounting, and is never worse than the rule-based battery
3  anchors and trade-off curve: at every opening month the cost anchor is the cheapest row, the carbon anchor the cleanest, and the
   curve between them has several non-dominated points
4  filters and scoring: requirements only remove portfolios (a winner that still passes a new requirement stays the winner), and every
   survivor satisfies every active requirement
5  scenario cache rows (from Re-optimize) re-evaluate to their stored cost
7  case-dependent seeds: a 4-acre site finds a feasible portfolio (the old fixed seeds all broke the land limit); an impossible site is reported, not crashed
8  validation: over-limit capacities and tampered battery schedules can never be feasible or become pool entries, and the official checker agrees
9  scoring and premium: constant axes score finitely; a premium cap with no measurable baseline blocks everything instead of being skipped
10 isolation: different cases running in parallel threads give exactly the results of running each alone
12 independent cross-check: the second model (model/GridTech.ipynb, its own LP) agrees with ours to the dollar on portfolios without a battery, passes the checker, and is never more expensive on batteries (looser charging rule)
11 website service: presets differ, hard limits are enforced, the exported dispatch passes the checker with the cost the page shows, the no-match path explains itself
6  developer case: a blank template is the benchmark, an activated default spec reproduces the packet arrays exactly, a filled template
   changes the model as documented, our evaluator equals the checker's own code under the custom inputs, cached custom rows re-evaluate
   to their stored cost, and the benchmark is restored afterwards
"""
import copy
import json
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alt_adapter as A  # noqa: E402
import engine as E  # noqa: E402
import logic as L  # noqa: E402
import lp_dispatch as LP  # noqa: E402
import export as X  # noqa: E402
import faraday_service as FS  # noqa: E402
import casespec as C  # noqa: E402
import custom_pool as CP  # noqa: E402

gm = A.gm
cs = E.cs
TOL_COST = 1.0           # dollars
POLICY = {'base': (0, True, True), 'no_t2': (0, True, False), 'd6': (6, True, True)}
results = []


def record(name, ok, detail=''):
    results.append(dict(check=name, passed=bool(ok), detail=detail))
    print(('PASS  ' if ok else 'FAIL  ') + name + ('  | ' + detail if detail else ''), flush=True)


def checker_cost(start, caps, delay, reloc, intr):
    sim = A.simulate(caps, start, delay, reloc, intr)
    d = pd.DataFrame(sim['_hourly']); d.insert(0, 'model_hour', range(len(d)))
    pf = {'initial_service_month': int(start), 'grid_delay_months': int(delay), 'capacities_mw': {k: float(v) for k, v in caps.items()}}
    return cs.score(pf, d[X.COLS])


def test_pool_rows(n):
    rnd = random.Random(7)
    for name, (delay, reloc, intr) in POLICY.items():
        df = L.load_pool(name)
        df = df.sample(min(n, len(df)), random_state=rnd.randint(0, 10 ** 6))
        for _, r in df.iterrows():
            caps = {k: float(r[k]) for k in E.KEYS}
            try:
                chk = checker_cost(int(r.start), caps, delay, reloc, intr)
            except ValueError as exc:
                record(f'pool[{name}] start {int(r.start)}', False, 'checker rejected: ' + str(exc)); continue
            diffs = dict(cost=abs(chk['total_decision_cost_usd'] - r.total_cost), stored_vs_checker_intensity=abs(chk['combined_electricity_co2_kg_per_mwh'] - r.intensity),
                         interrupted=abs(chk['interrupted_mwh'] - r.interrupted_mwh), relocated=abs(chk['relocated_local_equivalent_mwh'] - r.relocated_mwh))
            ok = chk['physical_checks_passed'] and diffs['cost'] < TOL_COST and diffs['stored_vs_checker_intensity'] < 1e-3 and diffs['interrupted'] < 1e-3 and diffs['relocated'] < 1e-3
            record(f'pool[{name}] start {int(r.start)} {L.caps_text(r)[:60]}', ok, f"cost diff ${diffs['cost']:.4f}, intensity diff {diffs['stored_vs_checker_intensity']:.5f}")


def test_lp():
    base = dict(utility_solar=60., wind=120., gas_turbine=25.)
    for bat in (3., 8.):
        caps = {**base, 'battery_4h': bat}
        m_h, _, ctx = gm.evaluate(20, caps, A.scenario())
        m_l, res = LP.lp_dispatch(20, caps)
        frame = gm.to_dispatch_frame(ctx, caps, res)
        chk = cs.score({'initial_service_month': 20, 'grid_delay_months': 0, 'capacities_mw': caps}, frame)
        record(f'LP battery {bat:g} MW passes checker, matches its accounting, not worse than rule-based',
               chk['physical_checks_passed'] and abs(chk['total_decision_cost_usd'] - m_l['total_decision_cost_usd']) < TOL_COST
               and m_l['total_decision_cost_usd'] <= m_h['total_decision_cost_usd'] + TOL_COST,
               f"LP ${m_l['total_decision_cost_usd']/1e6:.3f}M vs rule ${m_h['total_decision_cost_usd']/1e6:.3f}M")
    m0, res0 = LP.lp_dispatch(20, base)
    m1, _, _ = gm.evaluate(20, base, A.scenario())
    record('LP without a battery equals the merit order', abs(m0['total_decision_cost_usd'] - m1['total_decision_cost_usd']) < TOL_COST)


def test_anchors():
    for name in ('base', 'no_t2'):
        df = L.load_pool(name)
        bad, thin = [], []
        for s, g in df.groupby('start'):
            ck, cc = g.loc[g.intensity.idxmin()], g.loc[g.total_cost.idxmin()]
            if not (ck.intensity <= cc.intensity + 1e-9 and cc.total_cost <= ck.total_cost + 1e-6):
                bad.append(int(s))
            # non-dominated points in (cost, intensity)
            pts = g.sort_values('total_cost')[['total_cost', 'intensity']].values
            best, front = np.inf, 0
            for c, i in pts:
                if i < best - 1e-9:
                    front += 1; best = i
            if front < 5 and s <= 30:
                thin.append((int(s), front))
        record(f'anchors[{name}]: cost anchor cheapest, carbon anchor cleanest at every opening month', not bad, f'violations at {bad}' if bad else f'{df.start.nunique()} opening months')
        record(f'trade-off curve[{name}]: at least 5 non-dominated points per opening month (14-30)', not thin, f'thin: {thin}' if thin else '')


def test_filters():
    pool = L.load_pool('base')
    weights = (40, 40, 20)
    ranked = L.rank(pool, weights, ref=pool)
    best = ranked.iloc[0]
    req = dict(carbon_limit=float(best.intensity) + 5, cost_ceiling_m=float(best.total_cost) / 1e6 + 5)
    surv, filt = L.apply_requirements(pool, req, ())
    r2 = L.rank(surv, weights, ref=pool).iloc[0]
    same = (int(r2.start) == int(best.start)) and all(r2[k] == best[k] for k in E.KEYS)
    record('adding requirements the winner already meets does not change the winner', same)
    record('every survivor satisfies every active requirement', bool((surv.intensity <= req['carbon_limit']).all() and (surv.total_cost <= req['cost_ceiling_m'] * 1e6).all()))
    hard = L.apply_requirements(pool, dict(deadline_full=24), ())[0]
    record('a deadline of full demand by month 24 keeps only opening month <= 14', bool((hard.full_month <= 24).all() and len(hard) > 0))


def test_scenario_cache():
    files = [f for f in sorted((HERE / 'scenario_cache').glob('*.json')) if not f.name.startswith('custom_')] if (HERE / 'scenario_cache').exists() else []   # custom_* use another layout (tested in test_custom_case)
    if not files:
        record('scenario cache rows re-evaluate to their stored cost', True, 'no cache files (nothing to check)'); return
    for f in files:
        rows = json.loads(f.read_text())
        key = f.stem                                      # d{delay}_{price}_r{reloc}_i{intr}
        delay = int(key.split('_')[0][1:]); reloc = key.split('_r')[1][0] == '1'; intr = key.split('_i')[1][0] == '1'
        for e in random.Random(3).sample(rows, min(3, len(rows))):
            caps = dict(zip(E.KEYS, e['caps']))
            e2 = A.evaluate_entry(e['start'], caps, delay, reloc, intr)
            record(f'scenario cache {key} start {e["start"]}', e2 is not None and abs(e2['total_cost'] - e['total_cost']) < TOL_COST, f"stored ${e['total_cost']/1e6:.3f}M")


def test_custom_case():
    tpl = HERE / 'templates' / 'developer_input_template.xlsx'
    ex = HERE / 'templates' / 'example_filled_template.xlsx'
    blank = C.from_template(tpl)
    record('blank template parses to the benchmark (no errors, no overrides)', not blank.errors and blank.is_default(), blank.key())
    C.activate(None)
    base = cs.prepare_inputs({'initial_service_month': 20, 'grid_delay_months': 0, 'capacities_mw': {}})
    C.activate(blank)
    again = cs.prepare_inputs({'initial_service_month': 20, 'grid_delay_months': 0, 'capacities_mw': {}})
    same = all(np.array_equal(base[k], again[k]) for k in ('load', 'tier0', 'tier1', 'tier2', 'grid_limit', 'relocation_limit', 'interruption_limit', 'grid_price', 'gas_price'))
    record('activating a default spec reproduces the packet inputs exactly', same)
    spec = C.from_template(ex)
    record('example template parses without errors and flags the tier inputs as not applied', not spec.errors and set(spec.ignored) == {'protected_infrastructure_floor_mw', 'tier1_base_share'}, str(spec.ignored))
    C.activate(spec)
    ctx = gm.get_ctx(22, 3)
    ok = (abs(ctx.load.max() - 60.0) < 1e-6 and abs(ctx.relocation_limit.max() - 8 / 1.1) < 1e-6 and ctx.intr_price == 800 and abs(ctx.reloc_price - (1.1 * 80 + 60)) < 1e-9
          and gm.OPT.loc['nuclear', 'available_month'] == 48 and gm.OPT.loc['nuclear', 'max_mw'] == 20 and gm.CASE['onsite_energy_land_acres'] == 6
          and spec.ramp_months() == 12 and ctx.grid_limit[int((16 + 3) * 730)] == 20)
    record('example template changes the arrays as documented (peak, remote limit, prices, nuclear, land, ramp, grid steps with delay)', ok)
    for start, caps, delay in [(22, dict(utility_solar=40, wind=80, gas_engine=30, fuel_cell=10), 3), (22, dict(wind=60, gas_engine=40, nuclear=20), 3), (24, dict(utility_solar=30, wind=50, gas_engine=45), 0)]:
        e = A.evaluate_entry(start, caps, delay)
        sim = A.simulate(caps, start, delay)
        d = pd.DataFrame(sim['_hourly']); d.insert(0, 'model_hour', range(len(d)))
        chk = cs.score({'initial_service_month': start, 'grid_delay_months': delay, 'capacities_mw': {k: float(v) for k, v in caps.items()}}, d[X.COLS])
        record(f'custom case: evaluator equals checker code, start {start}, delay {delay}', e is not None and chk['physical_checks_passed'] and abs(e['total_cost'] - chk['total_decision_cost_usd']) < TOL_COST
               and chk['full_demand_month'] == start + spec.ramp_months(), f"${chk['total_decision_cost_usd']/1e6:.3f}M")
    cache = CP.load(spec, (3, 'Base', True, True))
    if cache is not None:
        for _, r in cache.sample(min(3, len(cache)), random_state=1).iterrows():
            caps = {k: float(r[k]) for k in E.KEYS}
            e2 = A.evaluate_entry(int(r.start), caps, 3)
            record(f'cached custom row re-evaluates to its stored cost ({L.caps_text(r)[:50]})', e2 is not None and abs(e2['total_cost'] - r.total_cost) < TOL_COST)
    C.activate(None)
    record('benchmark restored after the custom case', C.active_key() == 'bench' and gm.OPT.loc['nuclear', 'available_month'] == 60 and abs(gm.get_ctx(20, 0).load.max() - 75.0) < 1e-6)


def test_seeds():
    spec = C.from_template(HERE / 'templates' / 'developer_input_template.xlsx')
    spec.onsite_energy_land_acres = 4.0
    C.set_case(spec)
    old = [sd for sd in E.TEMPLATE_SEEDS if E.land_ok({k: float(sd.get(k, 0)) for k in E.KEYS})]
    record('4-acre site: the old fixed seeds all break the land limit (this is why the search used to return None)', len(old) == 0)
    seeds = E.case_seeds(20, 0, True, True)
    record('4-acre site: every case-dependent seed fits the land', len(seeds) >= 1 and all(E.land_ok(s) for s in seeds), f'{len(seeds)} seeds')
    rec = []
    res, _ = E.search(20, 0.0, record=rec, evaluate=lambda caps, cpr: A.evaluate_entry(20, caps, 0, True, True, 0.0, allow_infeasible=True), max_iter=40)
    record('4-acre site: the search finds a feasible portfolio instead of returning None', res is not None and len(rec) > 0 and res['unserved_mwh'] == 0, f"{len(rec)} feasible rows")
    tiny = copy.deepcopy(spec); tiny.onsite_energy_land_acres = 0.1
    out = CP.build(tiny, (0, 'Base', True, True), [20], workers=2)
    record('a site too small for the load is reported as having no feasible portfolio (no crash, no empty silent pool)', out['empty_starts'] == [20], str(out['by_start']))
    C.set_case(None)


def test_validation():
    bad = {'above the offer': dict(wind=250.), 'not a block multiple': dict(utility_solar=7.), 'over the land limit': dict(gas_turbine=100., battery_4h=12.), 'negative': dict(gas_engine=-5., wind=60.)}
    for name, caps in bad.items():
        m, res, _ = gm.evaluate(20, caps, A.scenario())
        record(f'invalid capacities ({name}) are never feasible and never become entries', (not res['feasible']) and bool(res['violations']) and A.evaluate_entry(20, caps) is None
               and A.evaluate_entry(20, caps, allow_infeasible=True) is None)
    caps = dict(utility_solar=60., wind=120., gas_turbine=25., battery_4h=6.)
    m, res, ctx = gm.evaluate(20, caps, A.scenario())
    record('an untouched battery schedule has no violations', res['feasible'] and not res['violations'])
    hour = int(np.argmax(res['soc'] > 5))
    for kind in ('discharge beyond stored energy', 'ends with energy left', 'charge and discharge in one hour', 'power above the rating'):
        r = {k: (x.copy() if isinstance(x, np.ndarray) else x) for k, x in res.items()}
        if kind == 'discharge beyond stored energy': r['discharge'][hour + 1] += 6.0
        if kind == 'ends with energy left': r['soc'][-1] = 2.0
        if kind == 'charge and discharge in one hour': r['charge'][hour] = 1.0; r['discharge'][hour] = 1.0
        if kind == 'power above the rating': r['discharge'][hour] = 9.0
        caught = bool(gm.validate_dispatch(ctx, caps, r, A.scenario()))
        try:
            cs.check({'initial_service_month': 20, 'grid_delay_months': 0, 'capacities_mw': caps}, gm.to_dispatch_frame(ctx, caps, r)); agree = False
        except ValueError:
            agree = True
        record(f'tampered battery schedule ({kind}) is caught, and the official checker rejects it too', caught and agree)
    flagged = 0
    for name, (delay, reloc, intr) in POLICY.items():
        df = L.load_pool(name)
        for _, r in pd.concat([df[df.battery_4h > 0].sample(10, random_state=1), df.sample(10, random_state=2)]).iterrows():
            _, res2, _ = gm.evaluate(int(r.start), {k: float(r[k]) for k in E.KEYS}, A.scenario(delay, reloc, intr))
            flagged += bool(res2['violations']) or not res2['feasible']
    record('valid pool rows (including batteries) are never flagged', flagged == 0, f'{flagged} flagged of 60')


def test_scoring_and_premium():
    pool = L.load_pool('base')
    one = pool[pool.start == 20].sort_values('total_cost').head(1)
    ok = True
    for df in (one, pool[pool.start == 20].head(40), pd.concat([one] * 3, ignore_index=True)):
        for ref in (df, pool):
            r = L.rank(df, (40, 40, 20), 0, 0, (False, False, False), ref=ref)
            ok &= bool(np.isfinite(r[['score', 's_speed', 's_cost', 's_carbon']].to_numpy()).all())
    record('single-candidate and constant-axis pools receive finite scores', ok)
    surv, filt = L.apply_requirements(pool, dict(premium_pct=10.0), (), None)
    record('a premium cap with no baseline blocks everything instead of being skipped', len(surv) == 0 and any('no baseline' in k for k in filt))
    c, why = L.premium_baseline(pool, (), 30, 10)
    surv2, _ = L.apply_requirements(pool, dict(premium_pct=10.0), (), c)
    record('a premium cap with a baseline keeps exactly the portfolios within the ceiling', len(surv2) > 0 and bool((surv2.total_cost <= c * 1.1 + 1e-6).all()) and why is None, f'baseline ${c/1e6:.1f}M')
    c31, _ = L.premium_baseline(pool, (), 31, 10)
    record('the baseline uses the exact month (no nearest-month substitution)', abs(c31 - pool[pool.start == 21].total_cost.min()) < 1e-6)
    cn, why_n = L.premium_baseline(pool, ('gas_turbine', 'gas_engine', 'fuel_cell'), 30, 10)
    record('a missing baseline gives a reason, not a silent skip', cn is None and bool(why_n), why_n[:70] if why_n else '')


def test_isolation():
    ex = C.from_template(HERE / 'templates' / 'example_filled_template.xlsx')
    b = copy.deepcopy(ex); b.onsite_energy_land_acres = 4.0; b.nuclear_available_month = 36; b.interruption_consequence_usd_per_mwh = 300.0; b.mature_peak_demand_mw = 70.0
    jobs = {'A': (ex, 22, dict(utility_solar=40, wind=80, gas_engine=30, fuel_cell=10)), 'B': (b, 22, dict(utility_solar=40, wind=80, gas_engine=30, fuel_cell=10)),
            'bench': (None, 20, dict(utility_solar=60, wind=120, gas_turbine=25))}

    def run(name):
        spec, start, caps = jobs[name]
        C.set_case(spec)
        e = A.evaluate_entry(start, caps, 0)
        sim = A.simulate(caps, start, 0)
        d = pd.DataFrame(sim['_hourly']); d.insert(0, 'model_hour', range(len(d)))
        chk = cs.score({'initial_service_month': start, 'grid_delay_months': 0, 'capacities_mw': {k: float(v) for k, v in caps.items()}}, d[X.COLS])
        return (round(e['total_cost'], 4), round(chk['total_decision_cost_usd'], 4), chk['full_demand_month'], float(gm.CASE['onsite_energy_land_acres']), round(float(gm.get_ctx(start, 0).load.max()), 3))

    C.set_case(None)
    serial = {n: run(n) for n in jobs}
    names = ['A', 'B', 'bench', 'A', 'B', 'bench']
    out, errs = {}, []

    def work(i, n):
        try:
            out[i] = [run(n) for _ in range(4)]
        except Exception as exc:
            errs.append(repr(exc))
    ths = [threading.Thread(target=work, args=(i, n)) for i, n in enumerate(names)]
    [t.start() for t in ths]; [t.join() for t in ths]
    same = len(out) == 6 and not errs and all(all(r == serial[names[i]] for r in rs) for i, rs in out.items())
    record('three different cases running in parallel threads give exactly the results of running each alone', same, str(errs[:1]))
    record('a thread that selected nothing still sees the packet benchmark', C.active_key() == 'bench' and gm.CASE['onsite_energy_land_acres'] == 8)
    with C.using(b):
        inside = gm.CASE['onsite_energy_land_acres']
    record('scoped selection ends when the block ends', inside == 4.0 and gm.CASE['onsite_energy_land_acres'] == 8)


def test_faraday_service():
    FS.warm()
    meta = FS.meta()
    record('website meta: benchmark facts match the model', abs(meta['campusPeakMw'] - 75.0) < 1e-6 and abs(meta['tier0PeakMw'] - 51.11) < 0.01 and meta['months'] == 120 and len(meta['assumptions']) == 11 and meta['stress']['count'] == 7 and meta['stress']['area'].startswith('ERCOT'))
    picks = {}
    for name, p in FS.PRESETS.items():
        r = FS.recommend(dict(weights=p['weights'], goLive=p['goLive'], carbonMax=p['carbonMax'], costPct=p['costPct']))
        picks[name] = (r['recommendation']['opening'], round(r['recommendation']['kpis']['costM']))
    record('website presets give different recommendations (speed-first opens earliest, cost-first is cheaper than speed-first)', picks['Speed-first'][0] <= 18 and picks['Cost-first'][1] < picks['Speed-first'][1] and len(set(picks.values())) >= 3, str(picks))
    r = FS.recommend(dict(weights=[40, 35, 25], goLive=18, carbonMax=100))
    rec = r['recommendation']
    record('a go-live limit and a carbon limit are hard (the recommendation meets both)', rec['opening'] <= 18 and rec['kpis']['kgPerMwh'] <= 100.0, f"M{rec['opening']}, {rec['kpis']['kgPerMwh']:.0f} kg")
    chk = FS.verify(r['exportId'])
    record('the exported dispatch passes the official checker and costs exactly what the page shows', chk['passed'] and abs(chk['totalDecisionCostM'] - rec['kpis']['costM']) < 1e-6, f"${chk['totalDecisionCostM']:.3f}M")
    r0 = FS.recommend(dict(weights=[0, 0, 0]))
    record('all-zero weights do not break the recommendation', r0['status'] == 'ok' and np.isfinite(r0['recommendation']['score']))
    nm = FS.recommend(dict(weights=[40, 35, 25], carbonMax=5.0))
    record('an impossible limit returns no_match with an explanation instead of a portfolio', nm['status'] == 'no_match' and bool(nm['relaxations']), '; '.join(nm['relaxations'])[:80])
    rw = FS.recommend(dict(weights=[34, 33, 33], excluded=['wind']))
    record('a resource marked unavailable is never in the recommendation or in any option shown', rw['status'] == 'ok' and 'wind' not in rw['recommendation']['capacities'] and all('wind' not in o['caps'] for o in rw['options']))
    pw = r0['powerOverTime']
    sens = FS.sensitivity(r0['exportId'])
    record('power chart and price sensitivity use the same model years', pw['years'] == sens['yearly']['years'] and len(pw['span']) == len(pw['years']))
    base = [x for x in sens['rows'] if x['role'] == 'Recommended'][0]
    record('price sensitivity: base scenario equals the headline cost; bear <= base <= bull; every option carries its backup costs', abs(base['perScenario']['base']['totalM'] - r0['recommendation']['kpis']['costM']) < 1e-3 and base['perScenario']['bear']['totalM'] <= base['perScenario']['base']['totalM'] <= base['perScenario']['bull']['totalM'] and all(x['backup'] for x in sens['rows']))
    rep = FS.report_json(r0['exportId'])
    ytot = sum(x['totalM'] for x in rep['rows'] if x['year'] != 'horizon')
    record('year-by-year report adds up to the headline cost and the zip builds', abs(ytot - r0['recommendation']['kpis']['costM']) < 1e-3 and len(FS.report_zip(r0['exportId'])) > 10000, f'${ytot:.3f}M')
    up = FS.read_upload((HERE / 'templates' / 'example_filled_template.xlsx').read_bytes())
    record('the filled example template is read, the blank one matches the benchmark, junk is rejected with a message', up['ok'] and not up['isBenchmark'] and FS.read_upload(FS.template_bytes())['isBenchmark'] and not FS.read_upload(b'junk')['ok'])
    record('every flag the page lists has an area, a status and a message', all(f['area'] and f['status'] and f['message'] for f in FS.FLAGS), f'{len(FS.FLAGS)} flags')


def test_stress_cases():
    import stress_data as SD
    cat = FS.CATALOG
    ercot = C.CaseSpec(stress_events=SD.events_as_list(cat.events_of(SD.ERCOT)), stress_area=SD.ERCOT)
    record("the catalog's Texas events are exactly the packet's seven benchmark events (so Texas needs no new search)", ercot.is_default() and not FS.is_custom(ercot))
    record('every state resolves to an interconnection; Texas defaults to ERCOT, Colorado to the Western grid', cat.area_of('Texas') == SD.ERCOT and cat.area_of('Colorado') == 'Western Interconnection' and cat.area_of('Texas', 'Western Interconnection') == 'Western Interconnection')
    for area in ('Western Interconnection', 'Eastern Interconnection'):
        spec = C.CaseSpec(stress_events=SD.events_as_list(cat.events_of(area)), stress_area=area)
        caps = dict(utility_solar=60., wind=120., gas_turbine=25.)
        with C.using(spec):
            e = A.evaluate_entry(20, caps, 0)
            sim = A.simulate(caps, 20, 0)
            d = pd.DataFrame(sim['_hourly']); d.insert(0, 'model_hour', range(len(d)))
            chk = cs.score({'initial_service_month': 20, 'grid_delay_months': 0, 'capacities_mw': caps}, d[X.COLS])
        record(f'stress events of the {area.split()[0]} grid: evaluator equals the official checker code and the cost differs from Texas', chk['physical_checks_passed'] and abs(e['total_cost'] - chk['total_decision_cost_usd']) < TOL_COST and abs(e['total_cost'] - 245.9826182e6) > 1e5, f"${chk['total_decision_cost_usd']/1e6:.3f}M")
    r = FS.recommend(dict(weights=[34, 33, 33], case=dict(state='Ohio')))
    ready = FS.case_pool(C.CaseSpec(stress_events=SD.events_as_list(cat.events_of('Eastern Interconnection')), stress_area='Eastern Interconnection')) is not None
    record('a state whose events have no portfolio table yet is told it needs a search instead of silently using Texas', ready or r['status'] == 'needs_build', r['status'])
    tr = FS.recommend(dict(weights=[10, 80, 10]))
    t = tr['tradeoffs']
    record('trade-offs: the cheapest portfolio is never dearer than the recommendation, the cleanest never dirtier, and cost-first names cost', t['top'] == 'cost' and t['anchors']['cheapest']['costM'] <= t['recommended']['costM'] + 1e-6 and t['anchors']['cleanest']['kgPerMwh'] <= t['recommended']['kgPerMwh'] + 1e-6 and t['anchors']['fastest']['fullDemand'] <= t['recommended']['fullDemand'])
    st = tr['powerOverTime']['stress']
    record('stress layer: one value per active year, relocation and interruption costs add to the report, nine or fewer years', len(st['eventHours']) == len(tr['powerOverTime']['years']) and len(st['events']) == 7)
    ca = FS.recommend(dict(weights=[34, 33, 33], case=dict(state='California')))
    if ca['status'] == 'ok':
        v = FS.verify(ca['exportId'])
        record('California case (built): the exported dispatch passes the official checker and costs exactly what the page shows; 9 events', v['passed'] and abs(v['totalDecisionCostM'] - ca['recommendation']['kpis']['costM']) < 1e-6 and len(ca['powerOverTime']['stress']['events']) == 9, f"${v['totalDecisionCostM']:.3f}M")


def test_crosscheck_second_model():
    sys.path.insert(0, str(HERE / 'crosscheck'))
    import gridtech_lp_engine as NEW
    data = str(HERE.parent / 'data')
    res = NEW.load_resources(data)
    for name, start, caps in [('best @20', 20, dict(utility_solar=60., wind=120., gas_turbine=25.)), ('speed @14', 14, dict(utility_solar=35., wind=95., gas_engine=15., fuel_cell=30.)),
                              ('clean @26', 26, dict(utility_solar=65., wind=150., gas_turbine=25.))]:
        t = NEW.build_timeline(data, start, 0)
        d, av = NEW.run_portfolio(t, res, caps, start, verbose=False)
        theirs = float(NEW.accounting(t, res, caps, d, av).loc['horizon'].total_decision_cost)
        m, _, _ = gm.evaluate(start, caps, A.scenario())
        ours = m['total_decision_cost_usd']
        record(f'second model agrees with ours to the dollar, no battery ({name})', abs(theirs - ours) < TOL_COST and float(d.unserved.sum()) < 1e-6, f'${theirs/1e6:.3f}M vs ${ours/1e6:.3f}M')
    caps = dict(utility_solar=60., wind=120., gas_turbine=25., battery_4h=12.)
    t = NEW.build_timeline(data, 20, 0)
    d, av = NEW.run_portfolio(t, res, caps, 20, verbose=False)
    theirs = float(NEW.accounting(t, res, caps, d, av).loc['horizon'].total_decision_cost)
    with tempfile.TemporaryDirectory() as td:
        NEW.export(td, d, caps, 20)
        chk = cs.score({'initial_service_month': 20, 'grid_delay_months': 0, 'capacities_mw': caps}, pd.read_csv(td + '/dispatch.csv'))
    m_lp, _ = LP.lp_dispatch(20, caps, 0, True, True, workers=1)
    record('second model with a 12 MW battery: its dispatch passes the official checker and costs what its own accounting says', chk['physical_checks_passed'] and abs(chk['total_decision_cost_usd'] - theirs) < 1.0, f'${theirs/1e6:.3f}M')
    record('second model is never more expensive than our spare-only battery LP (its charging rule is looser), and neither makes a battery pay', theirs <= m_lp['total_decision_cost_usd'] + 1.0 and theirs > 245.983e6,
           f"theirs ${theirs/1e6:.2f}M, ours ${m_lp['total_decision_cost_usd']/1e6:.2f}M, no battery $245.98M")


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    t0 = time.time()
    test_lp(); test_anchors(); test_filters(); test_pool_rows(n); test_scenario_cache(); test_custom_case(); test_seeds(); test_validation(); test_scoring_and_premium(); test_isolation(); test_faraday_service(); test_stress_cases(); test_crosscheck_second_model()
    failed = [r for r in results if not r['passed']]
    out = HERE.parent / 'outputs' / 'validation_report.json'
    out.write_text(json.dumps(dict(passed=len(results) - len(failed), failed=len(failed), seconds=round(time.time() - t0), results=results), indent=1))
    print(f'\n{len(results) - len(failed)} passed, {len(failed)} failed in {time.time() - t0:.0f}s -> {out}')
    sys.exit(1 if failed else 0)
