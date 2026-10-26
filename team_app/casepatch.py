"""Per-thread case isolation for the packet tables.

The supplied checker, the alternative evaluator and our engine all read module-level tables (options, case, response, ramp, grid schedule,
reference load profile). Swapping those globals for a custom developer case would let two cases running at the same time corrupt each other.

Instead, each of those names is replaced ONCE by a read-only live proxy that resolves, on every access, to the tables of the case selected for
the CURRENT THREAD (a contextvar). A case's tables are built once, never mutated, and shared read-only. Input caches (hourly arrays per opening
month) are keyed by case, so cases never see each other's data. A thread that has selected nothing sees the packet benchmark.

    import casepatch as P
    with P.using_tables(tables): ...      # scoped
    P.set_tables(tables)                   # for the rest of this thread (e.g. one Streamlit script run)
"""
import contextvars
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent / 'model_alt', HERE.parent / 'starter'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
import check_submission as cs  # noqa: E402
import gridmodel as gm  # noqa: E402
import engine as E  # noqa: E402


class Tables:
    """Everything a case changes. Treated as immutable once built."""
    def __init__(self, key, options, case, response, ramp, grid_schedule, hourly, gm_opt, e_opt, e_max, e_blocks):
        self.key, self.options, self.case, self.response, self.ramp = key, options, case, response, ramp
        self.grid_schedule, self.hourly, self.gm_opt, self.e_opt, self.e_max, self.e_blocks = grid_schedule, hourly, gm_opt, e_opt, e_max, e_blocks


_already = getattr(cs, '_gridtech_case_proxies', None)
if _already is None:
    BENCH = Tables('bench', cs.options, cs.case, cs.response, cs.ramp, cs.grid_schedule, cs.hourly, gm.OPT, E.OPT, E.MAXES, E.BLOCKS)
    _orig_validate = cs.validate_inputs
else:                                                           # module reloaded: keep the original tables
    BENCH, _orig_validate = _already

_cur = contextvars.ContextVar('gridtech_case', default=BENCH)


def current():
    return _cur.get()


def set_tables(t):
    _cur.set(t)


@contextmanager
def using_tables(t):
    token = _cur.set(t)
    try:
        yield
    finally:
        _cur.reset(token)


class _Live:
    """Read-only stand-in for a table: forwards everything to the current thread's table."""
    def __init__(self, pick):
        object.__setattr__(self, '_pick', pick)

    def __getattr__(self, name):
        return getattr(self._pick(_cur.get()), name)

    def __getitem__(self, k):
        return self._pick(_cur.get())[k]

    def __iter__(self):
        return iter(self._pick(_cur.get()))

    def __len__(self):
        return len(self._pick(_cur.get()))

    def __contains__(self, k):
        return k in self._pick(_cur.get())

    def __repr__(self):
        return f'<live table for case {_cur.get().key!r}>'


class _KeyedCache(dict):
    """E.CTX_CACHE replacement: keys are prefixed with the current case, so hourly inputs are never shared across cases."""
    def _k(self, k):
        return (_cur.get().key, k)

    def __contains__(self, k):
        return dict.__contains__(self, self._k(k))

    def __getitem__(self, k):
        return dict.__getitem__(self, self._k(k))

    def __setitem__(self, k, v):
        dict.__setitem__(self, self._k(k), v)


_CTX, _CTX_LOCK = {}, threading.Lock()


def _get_ctx(start, delay):
    key = (_cur.get().key, int(start), int(delay))
    c = _CTX.get(key)
    if c is None:
        with _CTX_LOCK:
            c = _CTX.get(key)
            if c is None:
                c = gm.Ctx(int(start), int(delay))               # built under THIS thread's case (the proxies resolve here)
                _CTX[key] = c
    return c


_get_ctx.cache_clear = lambda: _CTX.clear()


def _validate_inputs():
    return _orig_validate() if _cur.get().key == 'bench' else {'input_checks_passed': True, 'note': 'consistency check against the released files relaxed for a custom case'}


if _already is None:
    cs.options, cs.case, cs.response = _Live(lambda t: t.options), _Live(lambda t: t.case), _Live(lambda t: t.response)
    cs.ramp, cs.grid_schedule, cs.hourly = _Live(lambda t: t.ramp), _Live(lambda t: t.grid_schedule), _Live(lambda t: t.hourly)
    cs.validate_inputs = _validate_inputs
    gm.OPT, gm.CASE, gm.RESP = _Live(lambda t: t.gm_opt), _Live(lambda t: t.case), _Live(lambda t: t.response)
    gm.get_ctx = _get_ctx
    E.OPT, E.MAXES, E.BLOCKS = _Live(lambda t: t.e_opt), _Live(lambda t: t.e_max), _Live(lambda t: t.e_blocks)
    E.CTX_CACHE = _KeyedCache()
    cs._gridtech_case_proxies = (BENCH, _orig_validate)
