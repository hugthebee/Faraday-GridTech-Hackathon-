# Faraday Power Intelligence — GridTech Hackathon

A power-planning model and website for a data-center campus that has to open before its grid connection is fully built. Given a customer's priorities (speed, cost, carbon) and limits, it searches thousands of candidate energy portfolios — grid imports, solar / wind / nuclear contracts, onsite gas, fuel cells and batteries — simulates every hour of the 10-year horizon, and recommends the portfolio that fits best. Every recommendation can be exported and verified with the hackathon's official checker.

## What it does

- **Recommends a portfolio** from pools of ~20,000 pre-scored portfolios (openings from month 14 to month 26), using hard limits as filters and priority weights to rank what's left.
- **Hourly dispatch** of all 87,600 model hours: contracts first, then merchant power and onsite generation in merit order, then Tier 1 relocation and Tier 2 interruption during grid-stress events. Portfolios with a battery use an exact LP dispatch.
- **Stress events by state**: pick a US state and the model re-dispatches against that interconnection's 2023 stress events instead of the packet's seven ERCOT events.
- **Custom developer cases**: upload a filled developer input template (`.xlsx`) to change the case inputs; the model builds a new portfolio pool for that case.
- **Risk and maintenance**: O&M cost, backup-power options and a "what could go wrong" list for the recommended portfolio.
- **Check & export**: download `portfolio.json` and `dispatch.csv`, run them through `starter/check_submission.py`, and get per-year report tables and charts.

## Repository layout

| Path | Contents |
|---|---|
| `data/` | The hackathon packet: hourly reference data, resource options, grid schedule, stress events, price trajectory, assumptions. **Do not edit** — the checker hashes these files. |
| `examples/` | Model rules (`MODEL_RULES.md`), the portfolio JSON schema and the dispatch CSV header. |
| `starter/` | The official submission checker (`check_submission.py`). |
| `team_app/` | Our model: dispatch engine, portfolio search, decision logic, risk, reports, the HTTP server and the service the website calls. |
| `model_alt/` | Alternative evaluator (`gridmodel.py`), candidate search and customer logic; the app reads it through `team_app/alt_adapter.py`. |
| `faraday_web/` | The website: React 19 + Vite + Tailwind v4, first built in Figma Make. See [`faraday_web/INTEGRATION.md`](faraday_web/INTEGRATION.md) for how each part of the page maps to the model. |

Key files in `team_app/`:

| File | Role |
|---|---|
| `faraday_server.py` | HTTP server (standard library only): serves the API and the built site from `faraday_web/dist`. |
| `faraday_service.py` | Turns website inputs into a recommendation and shapes every number the page shows. |
| `engine.py`, `lp_dispatch.py` | Merit-order dispatch and costing; exact LP dispatch when there is a battery. |
| `logic.py` | Candidate pools, hard filters, ranking, relaxation advice. |
| `casespec.py`, `casepatch.py`, `custom_pool.py` | Custom developer cases, applied in memory with per-thread isolation. |
| `stress_data.py` | Stress events by interconnection and state. |
| `risk.py`, `report.py`, `report_charts.py` | Risk list, per-year report tables and charts. |
| `build_pool*.py`, `build_pareto.py` | Offline builders for the `pool*.json` portfolio tables. |
| `make_submission.py` | Builds the submission folder in `outputs/submission/`. |
| `validate_model.py` | Regression suite against the official checker. |

## Getting started

### Requirements

- Python 3.11 with `pandas`, `numpy`, `scipy`, `matplotlib` and `openpyxl`
- Node.js 22 (only to build the website)

### Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install pandas numpy scipy matplotlib openpyxl

cd faraday_web
npm install
npx vite build
cd ..
```

### Run the app

```bash
.venv/bin/python team_app/faraday_server.py --port 8770
```

Open <http://127.0.0.1:8770>. The server serves both the page and the API, and needs no API keys or online services.

`faraday_web/run.sh` does the same in one step. It expects a Python virtualenv at `.venv/` and Node 22 under `faraday_web/.tools/`; neither is included in the repo, so edit the paths in the script if your setup differs.

For frontend development with hot reload, keep the server running and in `faraday_web/` run:

```bash
PORT=5173 npm run dev     # proxies /api to the model server
```

## API

All endpoints take and return JSON unless noted.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Server status |
| `GET /api/meta`, `POST /api/meta {case}` | Benchmark data, presets, flags |
| `POST /api/recommend {weights, goLive, carbonMax, costPct, excluded, case}` | Recommended portfolio plus alternatives |
| `POST /api/risks` | "What could go wrong" list for the recommendation |
| `GET /api/verify/<exportId>` | Runs the official checker on that recommendation's dispatch |
| `GET /api/export/<exportId>/portfolio.json` \| `dispatch.csv` | Submission files |
| `GET /api/report/<exportId>` | Per-year results, cost by source, unit costs, list of charts |
| `GET /api/report/<exportId>/<name>.png` \| `report.zip` | Report charts, or everything for the submission folder |
| `POST /api/upload` | Developer template (`.xlsx`) or stress/state catalog (`.csv` / `.xlsx`) |
| `GET /api/template` | Blank developer input template |
| `GET /api/stress/catalog` | Stress events by interconnection |
| `POST /api/case/build`, `POST /api/case/status` | Build and poll the portfolio pool for a custom case |

## Command-line tools

```bash
# Check a submission with the official checker
python starter/check_submission.py portfolio.json dispatch.csv

# Regression suite: model vs. official checker (exits 1 on failure; writes outputs/validation_report.json)
python team_app/validate_model.py [N_PER_POOL]

# Build the submission folder (outputs/submission/)
python team_app/make_submission.py

# Compare the GridTech_Inputs.xlsx workbook with the model
python team_app/compare_excel.py /path/to/GridTech_Inputs.xlsx

# Rebuild the trade-off pools (about 10 minutes on 8 cores)
python team_app/build_pareto.py
```

## Modeling notes and limits

The model follows `examples/MODEL_RULES.md` (GRIDTECH v5.5.1). Things it does **not** model, or that are our own extensions, are flagged on the page and listed under `flags` in `GET /api/meta`. The main ones:

- **Maintenance schedules** are not modeled; only the packet's fixed and variable O&M costs are charged.
- **Backup power** (UPS, emergency generators) is out of scope in the packet. The backup costs shown are an extension that uses diesel costs we assumed.
- **Choosing another state changes only the stress events** (and any template values). Prices, wind and solar profiles, gas costs and grid emissions stay at the packet's ERCOT 2023 data.
- **Batteries** are chosen only when they pay for themselves; an exact LP check shows that usually they don't.
- Costs are summed annualized screening costs in constant 2023 USD, not a project cash-flow NPV.

The full list is in [`faraday_web/INTEGRATION.md`](faraday_web/INTEGRATION.md).
