"""Customer inputs -> recommendation.

REQUIRED inputs are hard filters (rows that miss them are removed).
PREFERENCES are weights that rank the surviving rows (min-max normalised, lower is better).
SCENARIO inputs change the physics (grid delay, Tier-2 policy, relocation cap) and select which
cached portfolio table is read. CUSTOMER VALUATIONS (carbon price, value of a month) are applied only
in a separately labelled 'customer-adjusted cost' column; they never alter the common accounting.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from pathlib import Path
import json
import numpy as np
import pandas as pd
import gridmodel as gm
import search as S

CAPS = gm.RESOURCES
LABEL = {"utility_solar": "solar contract", "wind": "wind contract", "battery_4h": "4h battery",
         "gas_turbine": "gas turbine", "nuclear": "nuclear PPA", "gas_engine": "gas engine", "fuel_cell": "fuel cell"}


@dataclass
class CustomerProfile:
    # ---------------- REQUIRED (hard filters; None = not applied) ----------------
    full_demand_by: int | None = None          # deadline: full demand no later than this month
    latest_full_demand: int = 36               # outer limit of the opening dates searched
    cost_ceiling_usd: float | None = None      # max total decision cost over the horizon
    premium_pct: float | None = None           # max % above the reference result (cheapest at reference month)
    reference_full_demand_month: int = 30
    max_kg_co2_per_mwh: float | None = None    # combined electricity intensity limit
    min_clean_share: float | None = None
    tier2_max_fraction: float = 1.0            # 0 = no Tier-2 interruption at all (Example 4)
    tier1_max_mw: float | None = None          # cap on relocation (local-equivalent MW); None = packet limit
    exclude: tuple = ()                        # resource ids the customer will not accept
    # ---------------- PREFERENCES (rank survivors) ----------------
    weights: dict = field(default_factory=lambda: {"speed": 34, "cost": 33, "carbon": 33})
    cost_metric: str = "total"                 # 'total' decision cost | 'per_mwh' (per requested MWh)
    # ---------------- SCENARIO ----------------
    grid_delay_months: int = 0
    dispatch_carbon_shadow_usd_per_t: float = 0.0   # makes dispatch prefer cleaner supply (advanced)
    # ---------------- CUSTOMER VALUATIONS (separate from common cost) ----------------
    carbon_price_usd_per_t: float = 0.0
    value_per_month_earlier_usd: float = 0.0

    def scenario(self) -> gm.Scenario:
        return gm.Scenario(grid_delay_months=self.grid_delay_months, tier2_max_fraction=self.tier2_max_fraction,
                           tier1_max_mw=self.tier1_max_mw, carbon_shadow_usd_per_t=self.dispatch_carbon_shadow_usd_per_t)

    def start_range(self) -> list[int]:
        last_full = min(self.latest_full_demand, self.full_demand_by or 10 ** 9)
        return [s for s in range(14, last_full - 10 + 1)]

    def has_valuations(self) -> bool:
        return bool(self.carbon_price_usd_per_t or self.value_per_month_earlier_usd)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


PRESETS = {
    "Fast to power":          CustomerProfile(full_demand_by=24, weights=dict(speed=60, cost=20, carbon=20)),
    "Lowest cost":            CustomerProfile(latest_full_demand=36, weights=dict(speed=0, cost=100, carbon=0)),
    "Under 100 kg CO2/MWh":   CustomerProfile(max_kg_co2_per_mwh=100, latest_full_demand=36, weights=dict(speed=50, cost=25, carbon=25)),
    "No Tier-2 interruption": CustomerProfile(tier2_max_fraction=0.0, weights=dict(speed=34, cost=33, carbon=33)),
    "Grid slips 6 months":    CustomerProfile(full_demand_by=24, grid_delay_months=6, weights=dict(speed=50, cost=30, carbon=20)),
}


# ------------------------------------------------------------------ scoring / filtering
def _cost_col(p: CustomerProfile, df: pd.DataFrame) -> pd.Series:
    base = df["total_decision_cost_usd"] if p.cost_metric == "total" else df["decision_cost_usd_per_requested_mwh"]
    return base


def add_customer_columns(p: CustomerProfile, df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["customer_adjusted_cost_usd"] = (df["total_decision_cost_usd"]
                                        + p.carbon_price_usd_per_t * df["combined_operational_co2_t"]
                                        + p.value_per_month_earlier_usd * (df["full_demand_month"] - p.reference_full_demand_month))
    return df


def apply_requirements(p: CustomerProfile, df: pd.DataFrame, baseline_cost: float | None) -> tuple[pd.DataFrame, list]:
    """Return surviving rows + ordered list of (label, mask) for diagnostics."""
    checks = []
    if p.exclude:
        checks.append(("excluded resources", ~(df[list(p.exclude)] > 0).any(axis=1)))
    if p.full_demand_by is not None:
        checks.append((f"full demand by month {p.full_demand_by}", df["full_demand_month"] <= p.full_demand_by))
    checks.append((f"full demand no later than month {p.latest_full_demand}", df["full_demand_month"] <= p.latest_full_demand))
    if p.cost_ceiling_usd is not None:
        checks.append((f"cost <= ${p.cost_ceiling_usd/1e6:,.0f}M", df["total_decision_cost_usd"] <= p.cost_ceiling_usd))
    if p.premium_pct is not None and baseline_cost is not None:
        checks.append((f"<= {p.premium_pct:g}% above month-{p.reference_full_demand_month} baseline",
                       df["total_decision_cost_usd"] <= baseline_cost * (1 + p.premium_pct / 100)))
    if p.max_kg_co2_per_mwh is not None:
        checks.append((f"<= {p.max_kg_co2_per_mwh:g} kg CO2/MWh", df["combined_electricity_co2_kg_per_mwh"] <= p.max_kg_co2_per_mwh))
    if p.min_clean_share is not None:
        checks.append((f">= {p.min_clean_share:.0%} clean share", df["clean_energy_share"] >= p.min_clean_share))
    ok = pd.Series(True, index=df.index)
    for _, m in checks:
        ok &= m
    return df[ok], checks


def _cost_for_score(p, df):
    if p.cost_metric == "total" and p.has_valuations():
        return df["customer_adjusted_cost_usd"]
    return _cost_col(p, df)


def score_rows(p: CustomerProfile, df: pd.DataFrame, bounds: dict) -> pd.Series:
    w = {k: max(float(v), 0.0) for k, v in p.weights.items()}
    tot = sum(w.values()) or 1.0
    series = {"speed": df["full_demand_month"], "cost": _cost_for_score(p, df), "carbon": df["combined_electricity_co2_kg_per_mwh"]}
    s = pd.Series(0.0, index=df.index)
    for k, v in series.items():
        lo, hi = bounds[k]
        s += (w.get(k, 0) / tot) * ((v - lo) / (hi - lo) if hi > lo else 0.0)
    return s


@dataclass
class Recommendation:
    profile: CustomerProfile
    scenario: gm.Scenario
    n_screened: int
    n_feasible_physics: int
    n_meeting_requirements: int
    table: pd.DataFrame              # surviving rows, scored, best first
    best: pd.Series | None
    alternatives: dict               # label -> row
    diagnostics: list                # relaxation hints when nothing survives
    baseline_cost: float | None
    starts_tested: list
    refined_evals: int = 0

    def caps(self, row=None) -> dict:
        row = self.best if row is None else row
        return {o: float(row[o]) for o in CAPS if row[o] > 0}


def _baseline(p, store, sc, df_all):
    """Cheapest portfolio at the reference opening month under the customer's exclusions only."""
    s0 = p.reference_full_demand_month - 10
    if s0 < 14:
        return None
    store.ensure(sc, [s0], "fast", verbose=False)
    ref = store.load(sc, [s0])
    if p.exclude and len(ref):
        ref = ref[~(ref[list(p.exclude)] > 0).any(axis=1)]
    return float(ref["total_decision_cost_usd"].min()) if len(ref) else None


def recommend(p: CustomerProfile, store: S.PortfolioStore, refine: bool = True, levels="fast", workers=1,
              verbose=False) -> Recommendation:
    sc, starts = p.scenario(), p.start_range()
    df = store.ensure(sc, starts, levels, workers, verbose)
    if not len(df):
        return Recommendation(p, sc, 0, 0, 0, df, None, {}, [("no feasible portfolio found at any tested opening month", None)], None, starts)
    n_all = len(df)
    baseline = _baseline(p, store, sc, df) if p.premium_pct is not None else None
    df = add_customer_columns(p, df)
    ok, checks = apply_requirements(p, df, baseline)
    refined = 0
    if len(ok) and refine:
        ok, extra = _refine(p, store, sc, ok, baseline)
        refined = extra
        df = add_customer_columns(p, store.load(sc, starts))
        n_all = len(df)
        ok, checks = apply_requirements(p, df, baseline)
    if not len(ok):
        return Recommendation(p, sc, n_all, n_all, 0, ok, None, {}, diagnose(p, df, checks), baseline, starts, refined)
    bnd = _bounds_from(p, ok)
    ok = ok.assign(score=score_rows(p, ok, bnd)).sort_values(["score", "total_decision_cost_usd"]).reset_index(drop=True)
    best = ok.iloc[0]
    alts = {}
    pick = lambda col, asc=True, tie="total_decision_cost_usd": ok.sort_values([col, tie], ascending=[asc, True]).iloc[0]
    alts["Fastest"] = pick("full_demand_month")
    alts["Cheapest"] = pick("total_decision_cost_usd")
    alts["Cleanest"] = pick("combined_electricity_co2_kg_per_mwh")
    return Recommendation(p, sc, n_all, n_all, len(ok), ok, best, alts, [], baseline, starts, refined)


def _bounds_from(p, ok):
    return {"speed": (float(ok.full_demand_month.min()), float(ok.full_demand_month.max())),
            "cost": (float(_cost_for_score(p, ok).min()), float(_cost_for_score(p, ok).max())),
            "carbon": (float(ok.combined_electricity_co2_kg_per_mwh.min()), float(ok.combined_electricity_co2_kg_per_mwh.max()))}


def _refine(p, store, sc, ok, baseline, rounds=6, top=3):
    """Local search: from the best few rows try +/- one step per resource; keep what improves the profile score."""
    bnd = _bounds_from(p, ok)
    seen = set(); added = 0
    key = lambda s, c: (int(s),) + tuple(float(c.get(o, 0)) for o in CAPS)
    for _, r in ok.iterrows():
        seen.add(key(r.initial_service_month, {o: r[o] for o in CAPS}))
    cur = ok.assign(score=score_rows(p, ok, bnd)).sort_values("score").head(top)
    for _ in range(rounds):
        new_rows = []
        for _, r in cur.iterrows():
            s = int(r.initial_service_month)
            ctx = gm.get_ctx(s, sc.grid_delay_months)
            for nb in S.neighbours({o: float(r[o]) for o in CAPS}):
                k = key(s, nb)
                if k in seen:
                    continue
                seen.add(k)
                if p.exclude and any(nb.get(o, 0) > 0 for o in p.exclude):
                    continue
                if not (gm.catalog_ok(nb) and gm.adequacy_ok(ctx, nb, sc)):
                    continue
                row = S.eval_row((s, nb, sc))
                if row:
                    new_rows.append(row)
        if not new_rows:
            break
        for s in {r["initial_service_month"] for r in new_rows}:
            store.add_rows(sc, s, [r for r in new_rows if r["initial_service_month"] == s])
        added += len(new_rows)
        nd = add_customer_columns(p, pd.DataFrame(new_rows))
        nd, _ = apply_requirements(p, nd, baseline)
        if not len(nd):
            break
        pool = pd.concat([cur.drop(columns=["score"], errors="ignore"), nd], ignore_index=True)
        pool = pool.assign(score=score_rows(p, pool, bnd)).sort_values("score")
        newcur = pool.head(top)
        if abs(newcur.score.min() - cur.score.min()) < 1e-12 and set(map(tuple, newcur[CAPS].values)) == set(map(tuple, cur[CAPS].values)):
            break
        cur = newcur
    return ok, added


def diagnose(p, df, checks) -> list:
    """For each requirement: how far it must move for a feasible set to exist given all the others.
    Phrased as 'found within the dates/portfolios tested', never as proof of impossibility."""
    out = []
    for i, (label, _) in enumerate(checks):
        others = pd.Series(True, index=df.index)
        for j, (_, m) in enumerate(checks):
            if j != i:
                others &= m
        sub = df[others]
        if not len(sub):
            continue
        if label.startswith("full demand by"):
            out.append((label, f"best full-demand month found with all other requirements met: {int(sub.full_demand_month.min())}"))
        elif label.startswith("cost") or "above month" in label:
            out.append((label, f"cheapest option meeting the other requirements: ${sub.total_decision_cost_usd.min()/1e6:,.0f}M"))
        elif "kg CO2" in label:
            out.append((label, f"lowest intensity found meeting the other requirements: {sub.combined_electricity_co2_kg_per_mwh.min():.0f} kg/MWh"))
        elif "clean share" in label:
            out.append((label, f"highest clean share found meeting the other requirements: {sub.clean_energy_share.max():.0%}"))
        elif "no later than" in label:
            out.append((label, f"earliest full demand needing latest-month relaxation: {int(sub.full_demand_month.min())}..{int(sub.full_demand_month.max())}"))
        else:
            out.append((label, f"{len(sub)} options would survive if this requirement were dropped"))
    return out or [("requirements jointly", "no single relaxation helps; relax two or more requirements")]


# ------------------------------------------------------------------ explanation + reporting
def _m(x): return f"${x/1e6:,.1f}M"


def describe_row(r) -> str:
    parts = [f"{LABEL[o]} {r[o]:g} MW" for o in CAPS if r[o] > 0]
    return ", ".join(parts) if parts else "no energy resources (grid only)"


def explain(rec: Recommendation) -> str:
    p = rec.profile
    if rec.best is None:
        lines = ["No portfolio met all required inputs within the dates and portfolios tested "
                 f"(opening months {rec.starts_tested[0] if rec.starts_tested else '-'}-{rec.starts_tested[-1] if rec.starts_tested else '-'}). "
                 "This is a search result, not proof of impossibility. Closest relaxations:"]
        lines += [f"  - {a}: {b}" for a, b in rec.diagnostics]
        return "\n".join(lines)
    b = rec.best
    lines = [f"Recommended: open month {int(b.initial_service_month)}, full demand month {int(b.full_demand_month)}; {describe_row(b)}.",
             f"Cost {_m(b.total_decision_cost_usd)} (energy {_m(b.local_energy_cost_usd)}, relocation {_m(b.relocation_cost_usd)}, "
             f"interruption {_m(b.interruption_consequence_usd)}); intensity {b.combined_electricity_co2_kg_per_mwh:.0f} kg CO2/MWh; "
             f"clean share {b.clean_energy_share:.0%}; merchant {b.merchant_share_of_local:.0%} of local energy; "
             f"relocated {b.relocated_local_equivalent_mwh:,.0f} MWh, interrupted {b.interrupted_mwh:,.0f} MWh."]
    req = []
    if p.full_demand_by: req.append(f"full demand by month {p.full_demand_by}")
    if p.cost_ceiling_usd: req.append(f"cost <= {_m(p.cost_ceiling_usd)}")
    if p.premium_pct is not None and rec.baseline_cost: req.append(f"<= {p.premium_pct:g}% over {_m(rec.baseline_cost)} baseline")
    if p.max_kg_co2_per_mwh: req.append(f"<= {p.max_kg_co2_per_mwh:g} kg/MWh")
    if p.min_clean_share: req.append(f">= {p.min_clean_share:.0%} clean")
    if p.tier2_max_fraction == 0: req.append("zero Tier-2 interruption")
    if p.exclude: req.append("excluding " + ", ".join(LABEL[e] for e in p.exclude))
    lines.append(f"Required inputs applied: {', '.join(req) if req else 'none'}. {rec.n_meeting_requirements:,} of {rec.n_feasible_physics:,} physically feasible portfolios met them "
                 f"(opening months {rec.starts_tested[0]}-{rec.starts_tested[-1]} tested; {rec.refined_evals} extra local-search evaluations).")
    w = p.weights; tot = sum(w.values()) or 1
    lines.append("Preferences (weights): " + ", ".join(f"{k} {100*v/tot:.0f}%" for k, v in w.items()) + f"; cost measured as {'total decision cost' if p.cost_metric=='total' else 'cost per requested MWh'}.")
    for lab, r in rec.alternatives.items():
        if r.name == b.name and np.allclose(r[CAPS].astype(float).values, b[CAPS].astype(float).values) and r.initial_service_month == b.initial_service_month:
            lines.append(f"{lab}: the recommendation is already the {lab.lower()} option among those meeting your requirements.")
        else:
            lines.append(f"{lab}: month {int(r.initial_service_month)+10} full demand, {describe_row(r)} -> "
                         f"{(r.total_decision_cost_usd-b.total_decision_cost_usd)/1e6:+,.1f}M cost, "
                         f"{r.combined_electricity_co2_kg_per_mwh-b.combined_electricity_co2_kg_per_mwh:+,.0f} kg/MWh, "
                         f"{int(r.full_demand_month-b.full_demand_month):+d} months to full demand.")
    return "\n".join(lines)


def compare(a: Recommendation, b: Recommendation, name_a="A", name_b="B") -> str:
    """Priority-change demo: what moved and why."""
    if a.best is None or b.best is None:
        return "One of the two profiles had no feasible recommendation; see explain()."
    ra, rb = a.best, b.best
    rows = [("opening month", int(ra.initial_service_month), int(rb.initial_service_month)),
            ("full-demand month", int(ra.full_demand_month), int(rb.full_demand_month)),
            ("total decision cost ($M)", ra.total_decision_cost_usd / 1e6, rb.total_decision_cost_usd / 1e6),
            ("cost per requested MWh ($)", ra.decision_cost_usd_per_requested_mwh, rb.decision_cost_usd_per_requested_mwh),
            ("intensity (kg CO2/MWh)", ra.combined_electricity_co2_kg_per_mwh, rb.combined_electricity_co2_kg_per_mwh),
            ("clean share", ra.clean_energy_share, rb.clean_energy_share),
            ("merchant share", ra.merchant_share_of_local, rb.merchant_share_of_local),
            ("relocated MWh", ra.relocated_local_equivalent_mwh, rb.relocated_local_equivalent_mwh),
            ("interrupted MWh", ra.interrupted_mwh, rb.interrupted_mwh)] + \
           [(f"{LABEL[o]} (MW)", ra[o], rb[o]) for o in CAPS if ra[o] > 0 or rb[o] > 0]
    t = pd.DataFrame(rows, columns=["metric", name_a, name_b])
    t["change"] = [f"{y - x:+,.2f}" if isinstance(x, float) or isinstance(y, float) else f"{y - x:+d}" for x, y in zip(t[name_a], t[name_b])]
    same = (np.allclose(ra[CAPS].astype(float).values, rb[CAPS].astype(float).values) and ra.initial_service_month == rb.initial_service_month)
    head = "Recommendation unchanged: the same portfolio and date remain best under the new priorities." if same else "Recommendation changed."
    return head + "\n" + t.to_string(index=False, float_format=lambda v: f"{v:,.2f}")


def stress_report(start: int, caps: dict, sc: gm.Scenario, year: int = 5) -> pd.DataFrame:
    """Hourly response in every stress event of one model year (default year 5: full demand reached for most opening months)."""
    m, res, ctx = gm.evaluate(start, caps, sc)
    sl = (ctx.year == year - 1) & ctx.active
    onsite = sum(res[o] for o in ctx.gas)
    if res.get("battery"):
        onsite = onsite + res["discharge"]
    df = pd.DataFrame({"model_hour": np.where(sl)[0], "event": np.asarray(ctx.base["frame"]["stress_event_id"])[sl],
                       "demand_mw": ctx.load[sl], "tier0_mw": ctx.tier0[sl], "import_cap_mw": ctx.grid_limit[sl],
                       "imports_mw": res["imports"][sl], "onsite_mw": onsite[sl],
                       "relocated_mw": res["tier1_relocated_mw"][sl], "interrupted_mw": res["tier2_curtailed_mw"][sl]})
    df["tier0_served"] = (df.demand_mw - df.relocated_mw - df.interrupted_mw) >= df.tier0_mw - 1e-6
    df["cap_met"] = df.imports_mw <= df.import_cap_mw + 1e-6
    return df


def summary_markdown(rec: Recommendation, name="Recommended portfolio") -> str:
    b = rec.best
    p = rec.profile
    md = [f"# {name}", "", "```", explain(rec), "```", ""]
    if b is None:
        return "\n".join(md)
    m, res, ctx = gm.evaluate(int(b.initial_service_month), rec.caps(), rec.scenario, by_year=True)
    ann = pd.DataFrame(m["annual"])[["model_year", "requested_mwh", "local_energy_cost_usd", "total_decision_cost_usd",
                                      "combined_electricity_co2_kg_per_mwh", "clean_energy_share", "merchant_mwh",
                                      "relocated_local_equivalent_mwh", "interrupted_mwh"]]
    ann = ann.assign(model_year=ann.model_year.astype(int))
    ann = ann.rename(columns={"requested_mwh": "requested MWh", "local_energy_cost_usd": "energy cost $", "total_decision_cost_usd": "decision cost $",
                              "combined_electricity_co2_kg_per_mwh": "kg CO2/MWh", "clean_energy_share": "clean share", "merchant_mwh": "merchant MWh",
                              "relocated_local_equivalent_mwh": "relocated MWh", "interrupted_mwh": "interrupted MWh"})
    md += ["## Annual results (year 1 is pre-opening for later start dates)", "", ann.to_string(index=False, na_rep="-", float_format=lambda v: f"{v:,.2f}"), ""]
    sr = stress_report(int(b.initial_service_month), rec.caps(), rec.scenario)
    if len(sr):
        g = sr.groupby("event").agg(hours=("model_hour", "count"), peak_demand_mw=("demand_mw", "max"), cap_mw=("import_cap_mw", "max"),
                                    peak_import_mw=("imports_mw", "max"), peak_onsite_mw=("onsite_mw", "max"),
                                    relocated_mwh=("relocated_mw", "sum"), interrupted_mwh=("interrupted_mw", "sum"),
                                    tier0_always_served=("tier0_served", "all"), import_cap_met=("cap_met", "all")).reset_index()
        md += ["## Stress-event response (model year 5)", "", g.to_string(index=False, float_format=lambda v: f"{v:,.1f}"), ""]
    md += ["## Customer profile", "", "```json", p.to_json(), "```"]
    return "\n".join(md)


def export(rec: Recommendation, outdir) -> dict:
    """portfolio.json + dispatch.csv (checker-verified) + results_summary.md in outdir."""
    outdir = Path(outdir)
    checked = gm.export_submission(int(rec.best.initial_service_month), rec.caps(), rec.scenario, outdir)
    (outdir / "results_summary.md").write_text(summary_markdown(rec))
    (outdir / "customer_profile.json").write_text(rec.profile.to_json())
    return checked
