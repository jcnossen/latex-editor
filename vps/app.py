"""Paper review app (VPS): serves the review site, stores review decisions and comments in SQLite, and runs the live
editor (editor.py: git working copy, fast pdfLaTeX compile, synctex, laptop sync, background latexdiff rebuild).

Runs behind nginx under an unguessable path, https://<host>/<token>/ (nginx strips that prefix; see deploy/).
Python standard library only. Project settings: config.json next to this file (see prconfig.py).
  GET  /...                      static files from SITE (index.html, main.pdf, SI.pdf, figs/...)
  GET  /live/<doc>.pdf           the last good fast compile of the web text (doc = main | SI)
  GET  /diff/...                 the server's latexdiff rebuild (PDFs, figure PNGs)
  GET  /api/decisions            {hunk_id: {status, text, updated}}
  PUT  /api/decisions/<id>       body {status: accept|reject|edit|open, text?} (status 'open' deletes the decision)
  GET  /api/comments             [{id, where, quote, text, created, resolved}]
  POST /api/comments             body {where, quote, text}
  PUT  /api/comments/<id>        body {resolved: bool}
  -- editor
  GET  /api/editor               {ready, rev, docs: {doc: build status}, diff: status}
  GET  /api/build/<doc>?since=N  build status; waits (up to 25 s) until its version is > N
  GET  /api/src/<doc>            {rev, text} (LF line endings)
  GET  /api/synctex/<doc>?page=&x=&y=   {line}: the source line at a PDF point (points, from the top left)
  GET  /api/boxes/<doc>?from=&to=       [{page, x, y, w, h}]: where source lines from..to are typeset
  POST /api/edit/<doc>           body {rev, start, old, new}: replace a block of lines; 409 if it changed meanwhile
  GET  /api/history[?n=]         [{rev, time, msg, merge}] of the web branch
  GET  /api/history/<rev>        {patch}
  POST /api/revert/<rev>         undo one saved edit (409 if later edits overlap)
  GET  /api/review-data          the latexdiff rebuild's {meta, docs, figures, hunks} (404 until there is one)
  POST /api/rebuild-diff         start the latexdiff rebuild now
  -- laptop sync (edit_sync.py, through ssh to 127.0.0.1)
  GET  /api/sync/state           {web, laptop, files: {fn: {web, base}}}
  POST /api/sync/push            body {base, files: {fn: text}}  -> merges into web; 409 on conflict
  GET  /api/sync/manifest        {asset path: sha1}
  POST /api/sync/assets          assets were updated (recompile everything)
Usage: python3 app.py --site /srv/paperreview/site --db /srv/paperreview/data/review.db --port 8801 [--data DIR]
"""
import argparse, hashlib, json, mimetypes, os, sqlite3, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from editor import CFG, DOCS, Compiler, Conflict, DiffBuilder, NotReady, Paths, Repo   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--site', required=True)
ap.add_argument('--db', required=True)
ap.add_argument('--data', default=None, help='editor data dir (default: the directory of --db)')
ap.add_argument('--port', type=int, default=None, help='default: port in config.json')
ap.add_argument('--diff-delay', type=float, default=30, help='seconds after the last edit before the latexdiff rebuild')
A = ap.parse_args()
SITE = os.path.realpath(A.site)
LOCK = threading.Lock()
P = Paths(os.path.realpath(A.data or os.path.dirname(A.db)))
REPO = Repo(P)
COMP = {d: Compiler(P, REPO, d) for d in DOCS}
DIFF = DiffBuilder(P, REPO, delay=A.diff_delay)


def on_change(reason):
    for d, c in COMP.items():
        if reason in (d, 'sync', 'revert', 'assets'):
            c.request()
    DIFF.request()


REPO.listeners.append(on_change)


def db():
    c = sqlite3.connect(A.db, timeout=10)
    c.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, status TEXT, text TEXT, updated REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, location TEXT, quote TEXT, '
              'text TEXT, created REAL, resolved INTEGER DEFAULT 0)')
    return c


def safe_join(root, rel):
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, rel))
    return full if (full.startswith(root + os.sep) and os.path.isfile(full)) else None


class H(BaseHTTPRequestHandler):
    server_version = 'paperreview/2'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # quiet
        pass

    def send(self, code, body=b'', ctype='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf8')
            ctype = 'application/json; charset=utf-8'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 4_000_000:
            raise ValueError('too large')
        return json.loads(self.rfile.read(n) or b'{}')

    def file(self, full):
        if not full:
            return self.send(404, {'error': 'not found'})
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript', 'application/json'):
            ctype += '; charset=utf-8'
        with open(full, 'rb') as f:
            return self.send(200, f.read(), ctype)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = unquote(u.path), {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            return self.get(path, q)
        except (Conflict, NotReady, ValueError, KeyError) as e:
            return self.send(400, {'error': str(e)})
        except Exception as e:
            return self.send(500, {'error': f'{type(e).__name__}: {e}'})

    def get(self, path, q):
        if path == '/api/decisions':
            with LOCK, db() as c:
                rows = c.execute('SELECT id, status, text, updated FROM decisions').fetchall()
            return self.send(200, {r[0]: {'status': r[1], 'text': r[2], 'updated': r[3]} for r in rows})
        if path == '/api/comments':
            with LOCK, db() as c:
                rows = c.execute('SELECT id, location, quote, text, created, resolved FROM comments ORDER BY id').fetchall()
            return self.send(200, [{'id': r[0], 'where': r[1], 'quote': r[2], 'text': r[3], 'created': r[4], 'resolved': bool(r[5])} for r in rows])
        if path == '/api/editor':
            rev = REPO.head()
            return self.send(200, {'ready': bool(rev), 'rev': rev, 'docs': {d: {**c.status, 'ver': c.ver} for d, c in COMP.items()},
                                   'diff': DIFF.status})
        if path.startswith('/api/build/'):
            c = COMP[path.rsplit('/', 1)[1]]
            return self.send(200, c.wait(int(q.get('since', -1)), timeout=min(25.0, float(q.get('timeout', 25)))))
        if path.startswith('/api/src/'):
            fn = DOCS[path.rsplit('/', 1)[1]]
            with REPO.lock:
                rev = REPO.head()
                if not rev:
                    raise NotReady('not initialised')
                return self.send(200, {'rev': rev, 'text': REPO.read(fn)})
        if path.startswith('/api/synctex/'):
            c = COMP[path.rsplit('/', 1)[1]]
            return self.send(200, {'line': c.synctex_edit(int(q['page']), float(q['x']), float(q['y']))})
        if path.startswith('/api/boxes/'):
            c = COMP[path.rsplit('/', 1)[1]]
            return self.send(200, c.synctex_view(int(q['from']), int(q['to'])))
        if path == '/api/history':
            return self.send(200, REPO.log(int(q.get('n', 60))))
        if path.startswith('/api/history/'):
            return self.send(200, {'patch': REPO.show(path.rsplit('/', 1)[1])})
        if path == '/api/review-data':
            p = os.path.join(P.diff, 'out', 'review.json')
            if not os.path.exists(p):
                return self.send(404, {'error': 'no server diff build yet'})
            d = json.load(open(p, encoding='utf8'))
            d['meta']['status'] = DIFF.status
            return self.send(200, d)
        if path == '/api/sync/state':
            return self.send(200, REPO.state())
        if path == '/api/sync/manifest':
            out = {}
            for root, _, files in os.walk(P.assets):
                for f in files:
                    full = os.path.join(root, f)
                    out[os.path.relpath(full, P.assets).replace(os.sep, '/')] = hashlib.sha1(open(full, 'rb').read()).hexdigest()
            return self.send(200, out)
        if path.startswith('/live/'):
            name = path[len('/live/'):]
            doc = name[:-4] if name.endswith('.pdf') else None
            if doc in DOCS:
                return self.file(safe_join(P.live, DOCS[doc][:-4] + '.pdf'))
            return self.send(404, {'error': 'not found'})
        if path.startswith('/diff/'):
            return self.file(safe_join(os.path.join(P.diff, 'out'), path[len('/diff/'):]))
        return self.file(safe_join(SITE, path.lstrip('/') or 'index.html'))

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        try:
            b = self.body()
        except Exception:
            return self.send(400, {'error': 'bad body'})
        if path.startswith('/api/decisions/'):
            hid = path.rsplit('/', 1)[1][:64]
            st = b.get('status')
            if st not in ('accept', 'reject', 'edit', 'open'):
                return self.send(400, {'error': 'bad status'})
            with LOCK, db() as c:
                if st == 'open':
                    c.execute('DELETE FROM decisions WHERE id = ?', (hid,))
                else:
                    c.execute('INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?)', (hid, st, b.get('text'), time.time()))
            return self.send(200, {'ok': True})
        if path.startswith('/api/comments/'):
            try:
                cid = int(path.rsplit('/', 1)[1])
            except ValueError:
                return self.send(400, {'error': 'bad id'})
            with LOCK, db() as c:
                c.execute('UPDATE comments SET resolved = ? WHERE id = ?', (1 if b.get('resolved') else 0, cid))
            return self.send(200, {'ok': True})
        return self.send(404, {'error': 'not found'})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            b = self.body()
        except Exception:
            return self.send(400, {'error': 'bad body'})
        try:
            return self.post(path, b)
        except Conflict as e:
            return self.send(409, {'error': str(e), **e.info})
        except NotReady as e:
            return self.send(503, {'error': str(e)})
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {'error': f'{type(e).__name__}: {e}'})
        except Exception as e:
            return self.send(500, {'error': f'{type(e).__name__}: {e}'})

    def post(self, path, b):
        if path == '/api/comments':
            text = str(b.get('text', '')).strip()
            if not text:
                return self.send(400, {'error': 'empty comment'})
            with LOCK, db() as c:
                cur = c.execute('INSERT INTO comments (location, quote, text, created) VALUES (?, ?, ?, ?)',
                                (str(b.get('where', ''))[:500], str(b.get('quote', ''))[:4000], text[:8000], time.time()))
            return self.send(200, {'ok': True, 'id': cur.lastrowid})
        if path.startswith('/api/edit/'):
            doc = path.rsplit('/', 1)[1]
            if doc not in DOCS:
                raise KeyError(doc)
            r = REPO.edit(doc, str(b.get('rev') or ''), int(b['start']), str(b['old']), str(b['new']),
                          who=str(b.get('who') or 'web')[:20])
            return self.send(200, {**r, 'ver': COMP[doc].ver})
        if path.startswith('/api/revert/'):
            return self.send(200, REPO.revert(path.rsplit('/', 1)[1]))
        if path == '/api/rebuild-diff':
            DIFF.request(delay=0)
            return self.send(200, {'ok': True})
        if path == '/api/sync/push':
            return self.send(200, REPO.push(b.get('base'), b['files'], str(b.get('msg') or 'laptop sync')[:200]))
        if path == '/api/sync/assets':
            on_change('assets')
            return self.send(200, {'ok': True})
        return self.send(404, {'error': 'not found'})


if __name__ == '__main__':
    os.makedirs(os.path.dirname(A.db), exist_ok=True)
    db().close()
    if REPO.head():
        for d, c in COMP.items():
            if not os.path.exists(os.path.join(P.live, DOCS[d][:-4] + '.pdf')):
                c.request()
    srv = ThreadingHTTPServer(('127.0.0.1', A.port or int(CFG['port'])), H)
    srv.daemon_threads = True
    srv.serve_forever()
