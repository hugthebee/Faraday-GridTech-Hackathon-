"""Candidate portfolios for a CUSTOM developer case (the precomputed pools only fit the packet benchmark).

For every opening month in the window it runs the same single-objective searches as build_pareto.py (cheapest, cleanest, and a few
cost-minimising searches under carbon caps between them) with the case's inputs active, then caches the rows on disk.
Typical cost: 1-3 minutes for a handful of opening months on 8 cores."""
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alt_adapter as A  # noqa: E402
import casespec as C  # noqa: E402
import engine as E  # noqa: E402
import logic as L  # noqa: E402

CACHE = HERE / 'scenario_cache'
N_EPS = 4


def path(spec, policy):
    return CACHE / f'custom_{spec.key()}_{L.policy_key(*policy)}.json'


def starts_for(spec, firm):
    p = spec.planned_opening()
    if firm:
        return [p]
    top = 109 - max(0, spec.ramp_months() - 10)
    return sorted(s for s in {14, p - 4, p - 2, p, p + 2, p + 4, p + 8} if 14 <= s <= top)


def _job(args):
    spec_d, policy, start, mode, eps = args
    spec = C.CaseSpec.from_dict(spec_d)
    C.activate(spec)
    delay, _price, reloc, intr = policy
    ev = lambda caps, cpr: A.evaluate_entry(start, caps, delay, reloc, intr, 0.0, allow_infeasible=True)
    rec = []
    if mode == 'cost':
        E.search(start, 0.0, record=rec, evaluate=ev, max_iter=60)
    elif mode == 'carbon':
        E.search(start, 3000.0, record=rec, evaluate=ev, max_iter=60)
    else:
        E.search(start, 0.0, max_intensity=eps, record=rec, evaluate=ev, max_iter=60)
    return start, mode, eps, rec


def have(spec, policy, starts):
    f = path(spec, policy)
    if not f.exists():
        return False
    done = set(json.loads(f.read_text()).get('starts', []))
    return set(starts) <= done


def build(spec, policy, starts, workers=8, progress=None):
    """Run the searches in worker processes and cache the rows. Returns {'found', 'by_start', 'empty_starts'}: an opening month with zero
    feasible portfolios (e.g. a site too small for the load) is reported, never silently dropped."""
    spec_d = spec.to_dict()
    rows, anchors = {}, {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for start, mode, _eps, rec in ex.map(_job, [(spec_d, policy, s, m, None) for s in starts for m in ('cost', 'carbon')]):
            for e in rec:
                rows[(e['start'], tuple(e['caps']))] = e
            if rec:
                best = min(rec, key=(lambda e: e['total_cost']) if mode == 'cost' else (lambda e: e['intensity']))
                anchors.setdefault(start, {})[mode] = best['intensity']
        if progress:
            progress('anchors done')
        jobs = []
        for s in starts:
            a = anchors.get(s, {})
            if 'cost' in a and 'carbon' in a and a['cost'] - a['carbon'] > 1.0:
                jobs += [(spec_d, policy, s, 'eps', float(x)) for x in np.linspace(a['carbon'], a['cost'], N_EPS + 2)[1:-1]]
        for start, mode, _eps, rec in ex.map(_job, jobs):
            for e in rec:
                rows[(e['start'], tuple(e['caps']))] = e
    f = path(spec, policy)
    old = json.loads(f.read_text()) if f.exists() else {'starts': [], 'rows': []}
    merged = {(e['start'], tuple(e['caps'])): e for e in old['rows']}
    merged.update(rows)
    CACHE.mkdir(exist_ok=True)
    by_start = {}
    for e in merged.values():
        by_start[e['start']] = by_start.get(e['start'], 0) + 1
    f.write_text(json.dumps({'starts': sorted(set(old['starts']) | set(starts)), 'rows': list(merged.values()), 'counts': {str(s): by_start.get(s, 0) for s in starts}}))
    return {'found': len(rows), 'by_start': {s: by_start.get(s, 0) for s in starts}, 'empty_starts': [s for s in starts if by_start.get(s, 0) == 0]}


def counts(spec, policy):
    """Feasible portfolios found per opening month in the cache ({} if nothing was built)."""
    f = path(spec, policy)
    if not f.exists():
        return {}
    data = json.loads(f.read_text())
    out = {}
    for e in data['rows']:
        out[e['start']] = out.get(e['start'], 0) + 1
    return {s: out.get(s, 0) for s in data.get('starts', [])}


def load(spec, policy, firm_start=None):
    """Cached candidates as a DataFrame (full-demand month uses the case's own ramp length), or None if not built yet."""
    f = path(spec, policy)
    if not f.exists():
        return None
    data = json.loads(f.read_text())
    return L._frame(data['rows'], offset=spec.ramp_months()) if data['rows'] else None
