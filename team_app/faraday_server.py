"""HTTP server for the Faraday Power Intelligence website. No dependencies beyond the standard library and the model; no keys, no online services.

    python team_app/faraday_server.py [--port 8770]

API  (JSON)
    GET  /api/health
    GET  /api/meta                       benchmark facts, preset customers, requirement choices, assumptions with real values, flags
    POST /api/recommend                  {weights:[speed,cost,carbon], goLive:month|null, carbonMax:kg|null, costPct:pct|null, excluded:[resource ids]}
    POST /api/risks                      same body; the "what could go wrong" list for the recommended portfolio
    GET  /api/verify/<exportId>          runs the official checker on that recommendation's dispatch
    GET  /api/report/<exportId>          per-year results, cost by source, unit costs + the list of report images
    GET  /api/report/<exportId>/<name>.png | report.zip   the report charts / everything for the submission folder
    GET  /api/export/<exportId>/portfolio.json | dispatch.csv
Also serves the built website from faraday_web/dist (run `npm run build` there) so the page and the model share one address.
"""
import os
for _v in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_v, '1')          # one math thread per process: the portfolio search runs 8 worker processes, more threads only slow it down
import argparse
import json
import mimetypes
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import faraday_service as S  # noqa: E402

DIST = HERE.parent / 'faraday_web' / 'dist'
_meta_cache = {}


def _json_default(o):
    import numpy as np
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    raise TypeError(type(o))


class Handler(BaseHTTPRequestHandler):
    server_version = 'FaradayModel/1.0'

    def log_message(self, fmt, *args):
        if '--quiet' not in sys.argv:
            sys.stderr.write('%s %s\n' % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=_json_default).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b'')

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/health':
                return self._send(200, dict(ok=True))
            if path == '/api/template':
                return self._send(200, S.template_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                  {'Content-Disposition': 'attachment; filename="HACKATHONdeveloper_input_template.xlsx"'})
            if path == '/api/meta':
                return self._send(200, S.meta())
            if path == '/api/stress/catalog':
                return self._send(200, S.catalog_info())
            if path.startswith('/api/sensitivity/'):
                out = S.sensitivity(path.rsplit('/', 1)[1])
                return self._send(200 if out else 404, out or dict(error='unknown export id; request a recommendation first'))
            if path.startswith('/api/report/'):
                parts = path.split('/')
                eid = parts[3] if len(parts) > 3 else ''
                if len(parts) == 4:
                    out = S.report_json(eid)
                    return self._send(200 if out else 404, out or dict(error='unknown export id; request a recommendation first'))
                if parts[4] == 'report.zip':
                    z = S.report_zip(eid)
                    return self._send(200 if z else 404, z or b'{"error": "unknown export id"}', 'application/zip' if z else 'application/json',
                                      {'Content-Disposition': 'attachment; filename="gridtech_report.zip"'} if z else None)
                png = S.report_png(eid, parts[4])
                return self._send(200 if png else 404, png or b'{"error": "unknown image"}', 'image/png' if png else 'application/json', {'Cache-Control': 'no-cache'})
            if path.startswith('/api/verify/'):
                out = S.verify(path.rsplit('/', 1)[1])
                return self._send(200 if out else 404, out or dict(error='unknown export id; request a recommendation first'))
            if path.startswith('/api/export/'):
                parts = path.split('/')
                files = S.export_files(parts[3]) if len(parts) >= 5 else None
                if not files:
                    return self._send(404, dict(error='unknown export id; request a recommendation first'))
                if parts[4] == 'portfolio.json':
                    return self._send(200, files[0], 'application/json', {'Content-Disposition': 'attachment; filename="portfolio.json"'})
                if parts[4] == 'dispatch.csv':
                    return self._send(200, files[1], 'text/csv', {'Content-Disposition': 'attachment; filename="dispatch.csv"'})
                return self._send(404, dict(error='unknown file'))
            return self._static(path)
        except Exception as exc:                                     # never let a model error kill the server
            traceback.print_exc()
            self._send(500, dict(error=str(exc)))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(n) or b'{}'
            if path == '/api/upload':
                return self._send(200, S.upload_any(raw, self.headers.get('X-File-Name', 'file')))
            if path == '/api/stress/upload':
                return self._send(200, S.stress_upload(raw, self.headers.get('X-File-Name', 'file')))
            payload = json.loads(raw)
            if path == '/api/recommend':
                return self._send(200, S.recommend(payload))
            if path == '/api/meta':
                return self._send(200, S.meta(payload))
            if path == '/api/case/build':
                return self._send(200, S.start_build(payload))
            if path == '/api/case/status':
                return self._send(200, S.case_status(payload))
            if path == '/api/risks':
                return self._send(200, S.risks(payload))
            self._send(404, dict(error='unknown endpoint'))
        except Exception as exc:
            traceback.print_exc()
            self._send(500, dict(error=str(exc)))

    def _static(self, path):
        if not DIST.exists():
            return self._send(200, '<h3>Model server is running.</h3><p>The website build was not found. Run <code>npm run build</code> in <code>faraday_web</code>, '
                                   'or start the dev server there (it proxies /api to this server).</p>', 'text/html')
        rel = path.lstrip('/') or 'index.html'
        f = (DIST / rel).resolve()
        if DIST.resolve() not in f.parents and f != DIST.resolve():
            return self._send(403, dict(error='forbidden'))
        if not f.is_file():
            f = DIST / 'index.html'                                  # single-page app fallback
        ctype = mimetypes.guess_type(str(f))[0] or 'application/octet-stream'
        self._send(200, f.read_bytes(), ctype, {'Cache-Control': 'no-cache'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8770)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()
    S.warm()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f'Faraday model server on http://{a.host}:{a.port}  (website: {"built" if DIST.exists() else "not built yet"})', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
