"""Build pool2_*.json: alternative-model cached tables + our pool portfolios re-scored with the alternative evaluator.
Usage: python build_pool2.py   (~minutes; uses 8 processes)"""
import glob, json, sys, time
import multiprocessing as mp
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alt_adapter as A, engine as E, logic as L

CACHE = A.ALT / 'cache'
TABLES = {'base': 'd0_t2-1_t1-pkt_cp0_bt100', 'no_t2': 'd0_t2-0_t1-pkt_cp0_bt100', 'd6': 'd6_t2-1_t1-pkt_cp0_bt100'}


def theirs(name):
    rows = []
    for f in sorted(glob.glob(str(CACHE / TABLES[name] / 'start_*.csv'))):
        df = pd.read_csv(f)
        rows += [A.entry_from_cache_row(r) for _, r in df.iterrows()]
    return rows


def ours(name):
    """Our portfolios (start, caps) for this scenario, re-scored later with the alternative evaluator."""
    if name == 'd6':
        df = L.frontier_candidates(109, (), per_start=12)
        return [(int(r.start), {k: float(r[k]) for k in E.KEYS}) for r in df]
    df = L.load_pool('base' if name == 'base' else 'no_t2', alt=False)
    return [(int(r.start), {k: float(r[k]) for k in E.KEYS}) for _, r in df.iterrows()]


if __name__ == '__main__':
    t0 = time.time()
    for name, (delay, intr) in {'base': (0, True), 'no_t2': (0, False), 'd6': (6, True)}.items():
        pool = {(e['start'], tuple(e['caps'])): e for e in theirs(name)}
        jobs = [(s, c, delay, True, intr) for s, c in ours(name) if (s, tuple(c.get(k, 0.0) for k in E.KEYS)) not in pool]
        with mp.get_context('fork').Pool(8) as p:
            for e in p.imap_unordered(A._job, jobs, chunksize=16):
                if e: pool[(e['start'], tuple(e['caps']))] = e
        Path(HERE / f'pool2_{name}.json').write_text(json.dumps(list(pool.values())))
        print(name, 'theirs+ours:', len(pool), 're-scored ours:', len(jobs), f'{time.time()-t0:.0f}s', flush=True)
