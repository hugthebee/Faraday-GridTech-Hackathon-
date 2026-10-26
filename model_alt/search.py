"""Candidate generation + cached evaluation (the 'portfolio table' the recommender reads).

A portfolio table row = (opening month, capacities) -> common-accounting metrics, for one Scenario.
Rows are evaluated once and cached on disk, so changing customer priorities is instant: the
recommender only filters and ranks rows. Missing opening months / scenarios are evaluated lazily.
"""
from __future__ import annotations
import itertools, os, time
from pathlib import Path
import numpy as np
import pandas as pd
import gridmodel as gm

CAPS = gm.RESOURCES
# Coarse level sets (all multiples of the catalog block, below catalog maxima). Edit freely.
PRESETS = {
    "fast":   dict(utility_solar=[0, 50, 100], wind=[0, 50, 100], battery_4h=[0, 12], gas_turbine=[0, 25, 50, 75],
                   nuclear=[0, 25], gas_engine=[0, 20, 40], fuel_cell=[0, 15, 30]),
    "medium": dict(utility_solar=[0, 25, 50, 100], wind=[0, 25, 50, 100, 150], battery_4h=[0, 6, 12],
                   gas_turbine=[0, 25, 50, 75], nuclear=[0, 10, 25], gas_engine=[0, 10, 20, 40, 60],
                   fuel_cell=[0, 10, 20, 30]),
}
# Neighbour step per resource used by local refinement (one catalog-friendly step)
STEPS = dict(utility_solar=25, wind=25, battery_4h=3, gas_turbine=25, nuclear=5, gas_engine=5, fuel_cell=5)

METRIC_COLS = ["total_decision_cost_usd", "local_energy_cost_usd", "relocation_cost_usd", "interruption_consequence_usd",
               "capacity_charges_usd", "requested_mwh", "local_served_mwh", "relocated_local_equivalent_mwh",
               "interrupted_mwh", "remote_electricity_mwh", "combined_operational_co2_t",
               "combined_electricity_co2_kg_per_mwh", "clean_energy_share", "merchant_mwh", "merchant_share_of_local",
               "decision_cost_usd_per_requested_mwh", "land_acres", "max_grid_import_mw"]


def candidates(start: int, sc: gm.Scenario, levels: dict) -> list[dict]:
    """All catalog-feasible level combinations that pass the cheap adequacy screen."""
    ctx = gm.get_ctx(start, sc.grid_delay_months)
    out = []
    for combo in itertools.product(*[levels.get(o, [0]) for o in CAPS]):
        caps = {o: float(v) for o, v in zip(CAPS, combo)}
        if gm.catalog_ok(caps) and gm.adequacy_ok(ctx, caps, sc):
            out.append(caps)
    return out


def eval_row(args) -> dict | None:
    start, caps, sc = args
    m, res, _ = gm.evaluate(start, caps, sc)
    if not res["feasible"]:
        return None
    row = {"initial_service_month": start, "full_demand_month": m["full_demand_month"], **{o: caps.get(o, 0.0) for o in CAPS}}
    m["capacity_charges_usd"] = sum(l["capacity_charges_usd"] for l in m["ledger"])
    row.update({k: m[k] for k in METRIC_COLS})
    return row


class PortfolioStore:
    """Disk-backed cache: <root>/<scenario key>/start_<m>.csv"""
    def __init__(self, root="outputs/cache"):
        self.root = Path(root)

    def _path(self, sc, start):
        return self.root / sc.key() / f"start_{start}.csv"

    def load(self, sc, starts) -> pd.DataFrame:
        frames = [pd.read_csv(self._path(sc, s)) for s in starts if self._path(sc, s).exists()]
        frames = [f for f in frames if len(f)]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def have(self, sc, start) -> bool:
        return self._path(sc, start).exists()

    def save(self, sc, start, df: pd.DataFrame):
        p = self._path(sc, start); p.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(p, index=False)

    def add_rows(self, sc, start, rows: list[dict]):
        """Append refined rows (dedup on capacities)."""
        if not rows:
            return
        p = self._path(sc, start)
        old = pd.read_csv(p) if p.exists() else pd.DataFrame()
        df = pd.concat([old, pd.DataFrame(rows)], ignore_index=True).drop_duplicates(subset=CAPS + ["initial_service_month"])
        self.save(sc, start, df)

    def ensure(self, sc, starts, levels="fast", workers=1, verbose=True) -> pd.DataFrame:
        lv = PRESETS[levels] if isinstance(levels, str) else levels
        for s in starts:
            if self.have(sc, s):
                continue
            t0 = time.time()
            cands = candidates(s, sc, lv)
            jobs = [(s, c, sc) for c in cands]
            if workers > 1 and jobs:
                import multiprocessing as mp
                with mp.get_context("fork").Pool(workers) as pool:
                    rows = pool.map(eval_row, jobs, chunksize=16)
            else:
                rows = [eval_row(j) for j in jobs]
            rows = [r for r in rows if r]
            self.save(sc, s, pd.DataFrame(rows))
            if verbose:
                print(f"  start {s:>3} (full demand {s+10:>3}): {len(cands):>5} screened, {len(rows):>5} feasible, {time.time()-t0:5.0f}s", flush=True)
        return self.load(sc, starts)


def neighbours(caps: dict) -> list[dict]:
    """+/- one step on each resource (kept on the catalog block grid)."""
    out = []
    for o in CAPS:
        r = gm.OPT.loc[o]
        step = max(STEPS[o], r.block_mw)
        for sgn in (-1, 1):
            v = caps.get(o, 0) + sgn * step
            if 0 <= v <= r.max_mw + 1e-9:
                c = dict(caps); c[o] = float(v); out.append(c)
    return out
