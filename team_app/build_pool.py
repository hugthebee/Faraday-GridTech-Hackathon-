"""Build the candidate-portfolio pool the app filters and ranks (offline, parallel). Usage: python build_pool.py"""
import json, sys, time
from multiprocessing import Pool
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E

STARTS = [14, 16, 18, 20, 22, 24, 26, 28, 30, 34, 40]
JOBS = [('base', s, cp) for s in STARTS for cp in (0, 60, 200)] + [('no_t2', s, cp) for s in STARTS for cp in (0, 100)]


def job(j):
    pol, start, cp = j
    rec = []
    E.search(start, carbon_price=cp, allow_intr=(pol == 'base'), record=rec, max_iter=40)
    return pol, rec


if __name__ == '__main__':
    t = time.time(); out = {'base': {}, 'no_t2': {}}
    with Pool(8) as p:
        for pol, rec in p.imap_unordered(job, JOBS):
            for e in rec:
                out[pol][(e['start'], tuple(e['caps']))] = e
            print(pol, len(rec), round(time.time() - t), flush=True)
    for pol, d in out.items():
        Path(__file__).with_name(f'pool_{pol}.json').write_text(json.dumps(list(d.values())))
        print(pol, len(d))
