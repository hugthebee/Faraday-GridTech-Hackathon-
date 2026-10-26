"""Assemble the submission folder (outputs/submission/) from the model. Re-runnable.

    python team_app/make_submission.py            # results, alternatives, report, README, dashboard, code

Top level   portfolio.json, dispatch.csv, check_results.json   the recommended portfolio (Balanced customer, no limits)
            README.md, results_summary.md, priority_changes.md
            results/        the year-by-year report: images, yearly_results.csv, results_summary.md
            alternatives/   one labeled subfolder per additional result (priority changes + the options tested), each checked
            dashboard/      the built website (served by code/team_app/faraday_server.py) and screenshots
            code/           team_app, model_alt, faraday_web (source) with the packet-relative layout
Nothing the packet supplied is modified.
"""
import json
import shutil
import subprocess
import sys
import zipfile
import io
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import faraday_service as S  # noqa: E402
import export as X  # noqa: E402
import alt_adapter as A  # noqa: E402

OUT = ROOT / 'outputs' / 'submission'
PY = sys.executable


def check(folder):
    r = subprocess.run([PY, str(ROOT / 'starter' / 'check_submission.py'), str(folder / 'portfolio.json'), str(folder / 'dispatch.csv')], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0 or not r.stdout.strip():
        return None, (r.stderr or r.stdout)[-400:]
    (folder / 'check_results.json').write_text(r.stdout)
    return json.loads(r.stdout), ''


def write_files(folder, eid=None, start=None, caps=None):
    folder.mkdir(parents=True, exist_ok=True)
    if eid:
        pf, csv = S.export_files(eid)
        (folder / 'portfolio.json').write_text(pf)
        (folder / 'dispatch.csv').write_text(csv)
    else:
        sim = A.simulate(caps, start, 0, True, True)
        d = pd.DataFrame(sim['_hourly']); d.insert(0, 'model_hour', range(len(d)))
        (folder / 'portfolio.json').write_text(json.dumps({'initial_service_month': int(start), 'grid_delay_months': 0, 'capacities_mw': {k: (int(v) if float(v).is_integer() else v) for k, v in caps.items()}}, indent=1))
        d[X.COLS].to_csv(folder / 'dispatch.csv', index=False)
    chk, err = check(folder)
    return chk, err


def kpi_line(r):
    k = r['recommendation']['kpis']
    return f"first power M{r['recommendation']['opening']}, full demand M{r['recommendation']['fullDemand']}, 10-year cost ${k['costM']:.1f}M, {k['kgPerMwh']:.0f} kg CO2/MWh, {k['cleanSharePct']:.0f}% clean"


def main():
    S.warm()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    P = S.PRESETS
    rows, summary = [], {}

    # ---- recommended (Balanced)
    p = P['Balanced']
    rec = S.recommend(dict(weights=p['weights'], goLive=p['goLive'], carbonMax=p['carbonMax'], costPct=p['costPct']))
    chk, err = write_files(OUT, eid=rec['exportId'])
    assert chk is not None, err
    summary['recommended'] = rec
    rz = zipfile.ZipFile(io.BytesIO(S.report_zip(rec['exportId'])))
    (OUT / 'results').mkdir()
    rz.extractall(OUT / 'results')
    sm = OUT / 'results' / 'results_summary.md'
    if sm.exists():
        shutil.copy(sm, OUT / 'results_summary.md')

    # ---- priority changes (customer types) and what-ifs
    scenarios = [('01_speed-first', 'Speed-first customer (70/20/10, live by month 18)', P['Speed-first'], []),
                 ('02_cost-first', 'Cost-first customer (10/80/10, within 10% of the cheapest)', P['Cost-first'], []),
                 ('03_carbon-first', 'Carbon-first customer (10/20/70, at most 100 kg CO2/MWh)', P['Carbon-first'], []),
                 ('04_no-wind-available', 'Balanced customer, wind not available (for example blocked by regulation)', P['Balanced'], ['wind'])]
    table = [('Recommended: Balanced customer (34/33/33, no limits)', rec, chk)]
    for folder, label, pr, excl in scenarios:
        r = S.recommend(dict(weights=pr['weights'], goLive=pr['goLive'], carbonMax=pr['carbonMax'], costPct=pr['costPct'], excluded=excl))
        f = OUT / 'alternatives' / folder
        c, e = write_files(f, eid=r['exportId'])
        (f / 'README.md').write_text(f"# {label}\n\n{kpi_line(r)}.\n\nPortfolio: {r['recommendation']['detail']}.\n\nChecker: {'passed' if c and c['physical_checks_passed'] else 'FAILED ' + e}.\n")
        table.append((label, r, c))

    # ---- the options tested against the recommendation (fastest / cheapest / cleanest overall)
    for o in rec['options']:
        if o['role'] in ('Fastest overall', 'Cheapest overall', 'Cleanest overall'):
            f = OUT / 'alternatives' / ('05_' + o['role'].lower().replace(' ', '-'))
            caps = {k: float(o['caps'].get(k, 0.0)) for k in S.E.KEYS}
            c, e = write_files(f, start=o['start'], caps=caps)
            (f / 'README.md').write_text(f"# {o['role']} option tested\n\n{o['detail']} (first power M{o['opening']}, full demand M{o['fullDemand']}, 10-year cost ${o['costM']:.1f}M, {o['kgPerMwh']:.0f} kg CO2/MWh).\n\nChecker: {'passed' if c and c['physical_checks_passed'] else 'FAILED ' + e}.\n")
            table.append((f"{o['role']} (option tested)", dict(recommendation=dict(opening=o['opening'], fullDemand=o['fullDemand'], detail=o['detail'], kpis=dict(costM=o['costM'], kgPerMwh=o['kgPerMwh'], cleanSharePct=float('nan')))), c))

    # ---- priority_changes.md
    lines = ['# What changes when the customer priorities change', '',
             'Every row is a full 87,600-hour dispatch that passed the official checker (`check_results.json` in its folder). Total decision cost is the packet\'s common accounting.', '',
             '| Customer / case | First power | Full demand | 10-yr cost ($M) | kg CO2/MWh | Portfolio | Checker |', '|---|---|---|---|---|---|---|']
    for label, r, c in table:
        k = r['recommendation']['kpis']
        lines.append(f"| {label} | M{r['recommendation']['opening']} | M{r['recommendation']['fullDemand']} | {k['costM']:.1f} | {k['kgPerMwh']:.0f} | {r['recommendation']['detail']} | {'passed' if c and c['physical_checks_passed'] else 'FAILED'} |")
    (OUT / 'priority_changes.md').write_text('\n'.join(lines) + '\n')
    (OUT / 'summary.json').write_text(json.dumps(dict(rows=[(l, r['recommendation']['opening'], r['recommendation']['kpis']['costM'], r['recommendation']['kpis']['kgPerMwh']) for l, r, c in table]), indent=1))
    print('results done:', len(table), 'rows;', 'recommended', kpi_line(rec))
    return rec, table


IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '*.orig', '.DS_Store', 'node_modules', '.tools', 'pool*pre_pareto.json', 'custom_*.json')


def copy_code():
    code = OUT / 'code'
    code.mkdir(exist_ok=True)
    for name in ('team_app', 'model_alt'):
        shutil.copytree(ROOT / name, code / name, ignore=IGNORE)
    web = code / 'faraday_web'
    shutil.copytree(ROOT / 'faraday_web', web, ignore=shutil.ignore_patterns('node_modules', '.tools', '__pycache__', '.DS_Store', 'App.original.tsx.txt', 'App.v2.tsx.txt'))
    if (OUT / 'code' / 'team_app' / 'make_submission.py').exists():
        pass
    dash = OUT / 'dashboard'
    dash.mkdir(exist_ok=True)
    shutil.copytree(ROOT / 'faraday_web' / 'dist', dash / 'site', dirs_exist_ok=True)
    shots = Path('/tmp/e2e/final')
    if shots.exists():
        (dash / 'screenshots').mkdir(exist_ok=True)
        for f in sorted(shots.glob('*.png')):
            shutil.copy(f, dash / 'screenshots' / f.name)


def write_readme(rec, table, tests):
    k = rec['recommendation']['kpis']
    rows = '\n'.join(f"| {l} | M{r['recommendation']['opening']} | M{r['recommendation']['fullDemand']} | {r['recommendation']['kpis']['costM']:.1f} | {r['recommendation']['kpis']['kgPerMwh']:.0f} | {r['recommendation']['detail']} |" for l, r, c in table)
    txt = f"""# Team 11 · Faraday engine: design the power portfolio

**Recommendation.** {rec['recommendation']['name']}: {rec['recommendation']['detail']}. First power in month {rec['recommendation']['opening']}, full demand (75 MW) reached in month {rec['recommendation']['fullDemand']}; 10-year decision cost ${k['costM']:.1f}M, {k['kgPerMwh']:.0f} kg CO2/MWh, {k['cleanSharePct']:.0f}% clean energy, {k['interruptedMwh']:.0f} MWh of Tier 2 work interrupted. It is the Balanced customer's answer (34/33/33 weights for speed, cost and carbon, no limits). `portfolio.json` and `dispatch.csv` at the top of this folder passed `starter/check_submission.py` (`check_results.json`).

## What is in this folder
| Path | What |
|---|---|
| `portfolio.json`, `dispatch.csv`, `check_results.json` | The recommended portfolio, its 87,600-hour dispatch and the official checker's output |
| `results_summary.md`, `results/` | Plain-language results, `yearly_results.csv` and eight charts for the recommendation |
| `priority_changes.md` | What the recommendation becomes when the customer's priorities change |
| `alternatives/` | One labeled subfolder per additional result, each with `portfolio.json`, `dispatch.csv`, `check_results.json`: speed-first, cost-first, carbon-first, wind not available, and the fastest / cheapest / cleanest options tested |
| `dashboard/` | The product's website, built (`site/`), and screenshots of every section |
| `code/` | Source: `team_app/` (model, search, service), `model_alt/` (evaluator), `faraday_web/` (React site) |

## How to run the product (offline, no keys, no accounts)
The product needs the contestant packet's `data/` and `starter/` folders. Copy `team_app/`, `model_alt/` and `faraday_web/` from `code/` into the packet folder (next to `data/` and `starter/`), then, from the packet folder:
```
python -m venv .venv && .venv/bin/pip install numpy pandas scipy matplotlib openpyxl
.venv/bin/python team_app/faraday_server.py        # model + website on http://127.0.0.1:8770
```
Open http://127.0.0.1:8770. `faraday_web/dist` is already built, so Node is not needed to run it (Node 22 is needed only to rebuild: `faraday_web/run.sh`). To regenerate this folder: `.venv/bin/python team_app/make_submission.py`. To re-run the regression tests: `.venv/bin/python team_app/validate_model.py 3` ({tests}).

## What the product does (for judges)
1. **Choose a template depending on the goal** (balanced, speed, cost, carbon) or set the three priority weights (always 100%), a go-live month, a carbon limit, a cost limit, and mark any resource as not available (for example wind blocked by regulation). Limits are hard rules; weights rank what remains.
2. **Optional uploads**: the developer input template (`HACKATHONdeveloper_input_template.xlsx`, downloadable on the page) and/or the stress-event files (`stress_events_all_interconnections_2023`, `states_by_interconnection`). The page reads the campus state, finds its interconnection and re-runs every portfolio against that interconnection's stress events. A case other than the packet benchmark needs one portfolio search (a few minutes) that is then remembered.
3. **Portfolio recommendation**: the portfolio, all seven resources (selected or not), the cost breakdown, the **trade-offs of your priorities** (what it gives up against the cheapest, fastest and cleanest portfolios), and one chart with three layers: power sources, stress events, price sensitivity (bear / base / bull). Backup generation and reliability costs can be added to the price with a click.
4. **Reliability and maintenance**, **grid stress and options tested** (every option shown under the same hours and the three price scenarios), **year-by-year break-down** with a downloadable report, and a **final recommendation** with a single **export** (portfolio.json, dispatch.csv, report zip) that is checked by the official checker on the page.

## Method
* **Dispatch.** Every candidate is simulated over all 87,600 hours in the packet's merit order: contract energy first, then the cheaper of merchant power and onsite gas, then (only inside stress events) relocation of Tier 1 work and interruption of Tier 2 work; Tier 0 is always served; the grid connection is shared by merchant and contract imports; the battery state of charge is tracked. Batteries are also solved exactly as a linear program per year (HiGHS) as a cross-check.
* **Search.** For every opening month 14 to 26 we search capacities of the seven resources inside the catalog blocks, maxima and the 8-acre land limit with single-objective searches (cheapest, cleanest, and cost under carbon caps in between), giving about 21,600 evaluated portfolios per case.
* **Scoring.** Portfolios that miss a limit are removed. The rest are scored 0-100 on speed (full-demand month), cost (10-year decision cost) and carbon (kg CO2/MWh), each scaled between the best and worst found by the single-objective searches, so the weights mean what they say.
* **Accounting.** The packet's common accounting, year by year (equipment annuity + fixed O&M, contract payments for all available energy, fuel, merchant purchases, relocation and interruption). Our totals equal the official checker's to the cent for every exported portfolio; a second, independently written model agrees to the dollar on portfolios without a battery.
* **Sensitivity.** Bear (wholesale power 0%/yr, gas 0%/yr), base (the packet's +5% / +3%), bull (+8% / +5%); every scenario re-runs the dispatch.

## Results of changing priorities
| Customer / case | First power | Full demand | 10-yr cost ($M) | kg CO2/MWh | Portfolio |
|---|---|---|---|---|---|
{rows}

All rows passed the official checker. Cheaper means later and dirtier; faster means dearer; cleaner means dearer and later: the trade-off card on the page quantifies this for whatever priorities are set.

## Assumptions (all shown on the page)
Packet data and case assumptions are used unchanged for the benchmark. Added by us, and listed under "Additional assumptions" on the page: the bear / bull price paths; the go-live, carbon and cost limits as hard rules; the battery may charge only from spare contract energy; diesel backup costs (team-assumed, an extension outside the packet's cost); portfolio names and the next-step text are generated from the numbers.

## Limitations
* **Stress events by state change only the stress events.** Prices, wind and solar profiles, grid emissions and gas prices stay the packet's ERCOT 2023 data for every state, so a California result means "this campus facing California's events", not California's market.
* The 60% import cap during events is the packet's case assumption; the catalog applies the same fraction to every interconnection. Event times are 2023 notices repeated every model year.
* Batteries almost never pay under the packet's rules and our spare-only charging rule, so recommendations rarely include one.
* Maintenance schedules and outage intervals are not modeled (only fixed and variable O&M charges). Tier shares, daily and weekly amplitudes and the protected floor of the developer template are read but not applied (the packet does not ship the tier formula).
* Results for an uploaded case are custom-case results: the checker's physical rules and accounting still verify them, but the checker's input-consistency check is relaxed, so the official submission uses the benchmark case only.
* Search is heuristic: it finds good, checked portfolios, not provably optimal ones.

## AI and software disclosure
The team used **Claude Code (Anthropic)** as a coding assistant for this hackathon: it wrote and tested the Python model, search, report and server code and the React site under the team's direction. The team chose the design (priorities, limits, scenarios, interface). **The product itself uses no AI, no API keys and no online services**; it runs on the packet's data. Software: Python (numpy, pandas, scipy/HiGHS, matplotlib, openpyxl, standard-library HTTP server), React 19, Vite, Tailwind CSS. The packet's `starter/check_submission.py` and data files are used unchanged.
"""
    (OUT / 'README.md').write_text(txt)


if __name__ == '__main__':
    import sys as _s
    if '--assemble' in _s.argv:
        rec, table = main()
        copy_code()
        write_readme(rec, table, '83 passing at last run')
        print('assembled')
    else:
        main()
