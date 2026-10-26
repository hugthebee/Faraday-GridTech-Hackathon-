# Faraday Power Intelligence × our model

The Figma Make site (`src/App.tsx`, React + Vite) now shows numbers from our model instead of sample numbers. The Streamlit app is no longer used.

## Run it
```
faraday_web/run.sh            # model + website on http://127.0.0.1:8770
```
`run.sh` builds the site if needed (it uses the Node 22 in `.tools/`, because the project needs Node 22 and the system has 18) and starts `team_app/faraday_server.py`, which serves both the page and the API. No keys, no online services.
Development with hot reload: `PORT=5173 npm run dev` in this folder (proxies `/api` to the model server), with the server running separately.

## How it fits together
`src/App.tsx` → `src/api.ts` → `team_app/faraday_server.py` → `team_app/faraday_service.py` → our model (pools of 21,644 / 18,721 portfolios, hourly dispatch of all 87,600 hours, the supplied checker for verification).
`GET /api/meta` · `POST /api/recommend` · `POST /api/risks` · `GET /api/verify/<id>` · `GET /api/export/<id>/portfolio.json|dispatch.csv`

## What each part of the page now shows
| Page element | Source |
|---|---|
| Hero stats (campus peak, Tier 0 peak, months) | model benchmark data |
| Customer presets (Balanced, Speed-first, Cost-first, Carbon-first) | weights, limits and value of a month from the GridTech_Inputs customer types |
| Four requirement cards + Priority / Preference | Priority = hard filter; Preference = score bonus when the target is met (half that priority's weight) |
| Priority sliders, value of one month earlier | the model's weighted score, scaled against true single-objective optima; the month value is a separate customer valuation |
| Power over time | hourly dispatch of the recommended portfolio, averaged by month, with the grid cap |
| Reliability and maintenance cards | installed MW, battery size, fixed + variable O&M from the packet, stress hours |
| Stress hour | the busiest stress hour after full demand: grid, gas, battery, relocated, interrupted |
| Options tested | recommended, fastest, cheapest, cleanest overall, each marked meets-all or which requirement it misses |
| Assumptions | the model's real values with their source |
| Final recommendation | portfolio, dates, 8 KPIs, capacity; exported dispatch can be run through the official checker |

## Flagged: shown by the page, not (fully) produced by the model
These are also listed live at the bottom of the page and in `GET /api/meta` under `flags`.
1. **Maintenance intervals** (assumptions list): not modeled. The model charges fixed and variable O&M only; no schedule or planned outages.
2. **Battery ride-through card**: assumes a 12 MW battery; the model picks a battery only when it pays (an exact LP check says it usually does not), so this card is often 0.
3. **Onsite backup**: the packet puts UPS and emergency backup out of scope. The card is generators + battery MW; the N-1 firm figure and diesel top-up cost are our extension with team-assumed diesel costs.
4. **"Grid-stress hours covered"**: always 100% for a feasible portfolio (a hard rule); the page reports stress hours with no interruption instead.
5. **Priority / Preference**: the model has limits and weights. The bonus size for Preference is a modeling choice.
6. **Cost limit "above cheapest"**: cheapest = cheapest portfolio (openings M14-M26) that meets your other Priority requirements. The packet's Example 6 uses the month-30 portfolio.
7. **Speed choices**: the page offers two; the model searches openings M14-M26 and can take any deadline.
8. **Portfolio names**, **next step**, **"why this portfolio"**, **customer "Cares about / Sets / Expect"**: text generated from model numbers or static; the model produces the numbers, not the wording.
9. **Power-over-time legend**: the page lists four sources; battery discharge is added when present, and relocated / interrupted demand is footnoted (it is not supply).
10. **Not in the page**: grid delay, price scenarios, excluded resources, clean-share floor, relocation off, and the developer-template case.

## Added to the page (not in the original design)
A "Not modeled" section and small flag pills; a "Check & export" card (portfolio.json, dispatch.csv, run the official checker); a notes list for each run; a clear "model not connected" state (the page never shows numbers of its own).
`src/App.original.tsx.txt` is the untouched Make version of App.tsx.

## Tested
65-check regression suite (`python team_app/validate_model.py`) including the service; the page was driven in Chrome: presets, toggles, sliders, server-down state, no console errors.

---
## v4 changes (current)
* **Inputs.** Priority/Preference buttons, the reliability requirement and the value-of-a-month input were removed. Inputs are three weights (speed / cost / carbon, always 100%), optional hard limits (go-live month, carbon, cost above the cheapest) and resources marked unavailable. `POST /api/recommend {weights, goLive, carbonMax, costPct, excluded, case}`.
* **Case = state and/or template.** `case: {state, area, templateId}`. A state picks an interconnection from `team_app/stress_data/` (bundled copies of `stress_events_all_interconnections_2023.csv` and `states_by_interconnection.csv`, or an upload); its events replace the packet's seven ERCOT events in the hourly table of that case (`casespec.CaseSpec.stress_events`). A filled developer template (`/api/template` downloads the blank one) sets the other case values. A case other than the benchmark needs its own portfolio table: `POST /api/case/build`, poll `POST /api/case/status`; `recommend` answers `needs_build` until it is ready.
* **Endpoints added.** `POST /api/upload` (template xlsx or stress/state csv/xlsx, detected), `GET /api/template`, `GET /api/stress/catalog`, `POST /api/meta {case}`, `POST /api/case/build`, `POST /api/case/status`.
* **Page.** One upload button and the template download at the top; state picker with the stress events; trade-offs card; one chart with three layers (power sources, stress events, price sensitivity) on the same model-year axis; backup costs can be added to the price; folds for assumptions under the chart; one final-recommendation box and one export.
* **Limit worth knowing.** Choosing another state changes only the stress events (and template values): prices, wind and solar profiles, gas and grid emissions remain the packet's ERCOT 2023 data.
