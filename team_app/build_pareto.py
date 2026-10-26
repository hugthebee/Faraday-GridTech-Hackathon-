"""True anchors + trade-off curve for every opening month, then merge into pool3_*.json.
 phase 1: cost optimiser (min cost) and carbon optimiser (min kg CO2/MWh) at each opening month
 phase 2: epsilon-constraint sweep: min cost subject to intensity <= eps, eps between the two anchors
Every search uses the alternative evaluator with plain (cost-ordered) dispatch, so rows match the exported dispatch.
Usage: python build_pareto.py   (about 10 minutes on 8 cores)"""
import json, sys, time
from multiprocessing import Pool
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alt_adapter as A, engine as E

POLICIES = {'base': True, 'no_t2': False}          # name -> Tier-2 interruption allowed?
STARTS = list(range(14, 31)) + [34, 40]
N_EPS = 8


def job(args):
    pol, start, mode, eps = args
    intr = POLICIES[pol]
    ev = lambda caps, cpr: A.evaluate_entry(start, caps, 0, True, intr, 0.0, allow_infeasible=True)
    rec = []
    if mode == 'cost':
        E.search(start, 0.0, record=rec, evaluate=ev, max_iter=60)
    elif mode == 'carbon':
        E.search(start, 3000.0, record=rec, evaluate=ev, max_iter=60)
    else:
        E.search(start, 0.0, max_intensity=eps, record=rec, evaluate=ev, max_iter=60)
    return pol, start, mode, eps, rec


def main():
    t0 = time.time()
    new = {p: {} for p in POLICIES}
    anchors = {}
    def absorb(pol, rec):
        for e in rec:
            new[pol][(e['start'], tuple(e['caps']))] = e
    with Pool(8) as p:
        jobs1 = [(pol, s, m, None) for pol in POLICIES for s in STARTS for m in ('cost', 'carbon')]
        for pol, s, m, _, rec in p.imap_unordered(job, jobs1):
            absorb(pol, rec)
            best = min(rec, key=(lambda e: e['total_cost']) if m == 'cost' else (lambda e: e['intensity']))
            anchors.setdefault(pol, {}).setdefault(s, {})[m] = dict(intensity=best['intensity'], total_cost=best['total_cost'], caps=best['caps'])
        print('phase 1 done', len(jobs1), 'searches', f'{time.time()-t0:.0f}s', flush=True)
        jobs2 = []
        for pol in POLICIES:
            for s in STARTS:
                a = anchors[pol][s]
                lo, hi = a['carbon']['intensity'], a['cost']['intensity']
                if hi - lo > 1.0:
                    jobs2 += [(pol, s, 'eps', float(e)) for e in np.linspace(lo, hi, N_EPS + 2)[1:-1]]
        for i, (pol, s, m, eps, rec) in enumerate(p.imap_unordered(job, jobs2)):
            absorb(pol, rec)
            if i % 40 == 0: print(f'  phase 2: {i}/{len(jobs2)} {time.time()-t0:.0f}s', flush=True)
    for pol in POLICIES:
        old = json.loads((HERE / f'pool2_{"base" if pol == "base" else "no_t2"}.json').read_text())
        merged = {(e['start'], tuple(e['caps'])): e for e in old}
        merged.update(new[pol])
        (HERE / f'pool3_{pol}.json').write_text(json.dumps(list(merged.values())))
        print(pol, 'old', len(old), 'new', len(new[pol]), 'merged', len(merged), flush=True)
    (HERE / 'anchors_phase1.json').write_text(json.dumps(anchors))
    print('done', f'{time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
