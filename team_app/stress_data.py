"""Stress events by interconnection and the states each interconnection covers.

The packet ships the seven ERCOT events of 2023. The developer can upload a catalog covering every interconnection
(stress_events_all_interconnections_2023.csv / .xlsx) and the state list (states_by_interconnection.csv). The page picks a state, finds
its interconnection, and the model re-dispatches every portfolio against THAT interconnection's events.

Bundled copies of both files sit in team_app/stress_data/ so the page works without an upload. Nothing here touches the packet's files.
"""
import io
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / 'stress_data'
ERCOT = 'ERCOT (Texas Interconnection)'
NEED_EVENTS = {'interconnection_area', 'start_utc', 'end_utc', 'import_fraction'}
NEED_STATES = {'interconnection_area', 'state'}


def _tables(src):
    """Every table in an uploaded file (csv: one; xlsx: one per sheet)."""
    if isinstance(src, (bytes, bytearray)):
        if bytes(src[:2]) == b'PK':
            return list(pd.read_excel(io.BytesIO(src), sheet_name=None, keep_default_na=False).values())
        return [pd.read_csv(io.BytesIO(src), keep_default_na=False)]
    p = Path(src)
    if p.suffix.lower() in ('.xlsx', '.xls'):
        return list(pd.read_excel(p, sheet_name=None, keep_default_na=False).values())
    return [pd.read_csv(p, keep_default_na=False)]


def parse(src):
    """Returns (events or None, states or None, problems). Accepts a file holding events, states, or (xlsx) both."""
    events = states = None
    problems = []
    for df in _tables(src):
        cols = set(df.columns)
        if NEED_EVENTS <= cols:
            events, bad = _events(df)
            problems += bad
        elif NEED_STATES <= cols:
            states = df[[c for c in ('interconnection_area', 'state', 'postal_code', 'coverage') if c in df.columns]].copy()
    if events is None and states is None:
        problems.append('The file has neither a stress-event table (needs interconnection_area, start_utc, end_utc, import_fraction) nor a state list (interconnection_area, state).')
    return events, states, problems


def _events(df):
    problems = []
    df = df.copy()
    df['start_utc'] = pd.to_datetime(df['start_utc'], utc=True, errors='coerce')
    df['end_utc'] = pd.to_datetime(df['end_utc'], utc=True, errors='coerce')
    df['import_fraction'] = pd.to_numeric(df['import_fraction'], errors='coerce')
    bad = df.start_utc.isna() | df.end_utc.isna() | (df.end_utc <= df.start_utc) | df.import_fraction.isna() | (df.import_fraction < 0) | (df.import_fraction > 1)
    if bad.any():
        problems.append(f'{int(bad.sum())} event row(s) have an unreadable time, an end before the start, or an import fraction outside 0-1 and were ignored.')
    df = df[~bad].reset_index(drop=True)
    if 'notice_utc' in df.columns:
        n = pd.to_datetime(df['notice_utc'], utc=True, errors='coerce')
        df['notice_minutes'] = ((df.start_utc - n).dt.total_seconds() / 60).clip(lower=0).fillna(30).round()
    else:
        df['notice_minutes'] = 30.0
    for c, d in (('operator', ''), ('event_type', ''), ('event_id', ''), ('timing_confidence', ''), ('restriction_basis', ''), ('source_url', '')):
        if c not in df.columns:
            df[c] = d
    df['duration_hours'] = ((df.end_utc - df.start_utc).dt.total_seconds() / 3600).round(2)
    return df, problems


def bundled():
    ev, st, _ = parse(DATA / 'stress_events_all_interconnections_2023.csv')
    _, st2, _ = parse(DATA / 'states_by_interconnection.csv')
    return ev, st2


class Catalog:
    """The events and state list the page works from (bundled, or replaced by an upload)."""
    def __init__(self):
        self.events, self.states = bundled()
        self.source = 'bundled'

    def update(self, events, states, name):
        if events is not None:
            self.events = events
        if states is not None:
            self.states = states
        self.source = f'uploaded: {name}'

    def areas(self):
        out = []
        for a, g in self.events.groupby('interconnection_area', sort=False):
            out.append(dict(area=a, operators=sorted({o for o in g.operator if o}), events=int(len(g)), minHours=float(g.duration_hours.min()), maxHours=float(g.duration_hours.max()),
                            totalHours=float(g.duration_hours.sum()), importFraction=float(g.import_fraction.median())))
        return out

    def state_rows(self):
        """One row per state with every interconnection it touches; the primary one (whole / most of the state) comes first."""
        have = set(self.events.interconnection_area)
        out = {}
        for r in self.states.itertuples():
            st = str(r.state)
            if st in ('', '(none)'):
                continue
            cov = str(getattr(r, 'coverage', ''))
            out.setdefault(st, dict(state=st, postal=str(getattr(r, 'postal_code', '')), areas=[]))['areas'].append(
                dict(area=str(r.interconnection_area), coverage=cov, hasEvents=r.interconnection_area in have, primary=cov.startswith(('Whole', 'Most of state'))))
        for v in out.values():
            v['areas'].sort(key=lambda a: not a['primary'])
            v['areas'][0]['primary'] = True
            for a in v['areas'][1:]:
                a['primary'] = False
        return list(out.values())

    def area_of(self, state, area=None):
        for v in self.state_rows():
            if v['state'].lower() == str(state).lower():
                if area and any(a['area'] == area for a in v['areas']):
                    return area
                return v['areas'][0]['area']
        return None

    def events_of(self, area):
        return self.events[self.events.interconnection_area == area].reset_index(drop=True)

    def event_list(self, area):
        g = self.events_of(area)
        return [dict(id=str(r.event_id), operator=str(r.operator), type=str(r.event_type), startUtc=r.start_utc.strftime('%Y-%m-%d %H:%M'), hours=float(r.duration_hours),
                     importPct=float(r.import_fraction * 100), noticeMin=float(r.notice_minutes), confidence=str(r.timing_confidence)) for r in g.itertuples()]


def events_as_list(df):
    """JSON-safe list the CaseSpec stores (and hashes into the case key)."""
    return [dict(id=str(r.event_id), start_utc=r.start_utc.strftime('%Y-%m-%dT%H:%M:%SZ'), end_utc=r.end_utc.strftime('%Y-%m-%dT%H:%M:%SZ'),
                 import_fraction=float(r.import_fraction), notice_minutes=float(r.notice_minutes)) for r in df.itertuples()]
