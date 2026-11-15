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
import argparse, hashlib, json, mimetypes, os, queue, sqlite3, subprocess, sys, threading, time
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


# comments carry where they point: document, page, a region (rect, normalized 0..1 page coordinates), a drawing
# (strokes: [[[x, y], ...], ...] in the same coordinates), the source lines under it, and an optional voice note
CMT_EXTRA = [('kind', 'TEXT'), ('doc', 'TEXT'), ('page', 'INTEGER'), ('rect', 'TEXT'), ('strokes', 'TEXT'),
             ('src_from', 'INTEGER'), ('src_to', 'INTEGER'), ('audio_id', 'INTEGER'), ('author', 'TEXT')]
_schema_done = False


def db():
    global _schema_done
    c = sqlite3.connect(A.db, timeout=10)
    if not _schema_done:
        c.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, status TEXT, text TEXT, updated REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, location TEXT, quote TEXT, '
                  'text TEXT, created REAL, resolved INTEGER DEFAULT 0)')
        have = {r[1] for r in c.execute('PRAGMA table_info(comments)')}
        for col, typ in CMT_EXTRA:
            if col not in have:
                c.execute(f'ALTER TABLE comments ADD COLUMN {col} {typ}')
        c.execute('CREATE TABLE IF NOT EXISTS audio (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, file TEXT, mime TEXT, '
                  'state TEXT, transcript TEXT, language TEXT, duration REAL, error TEXT)')
        c.commit()
        _schema_done = True
    return c


def comment_rows(where='', args=()):
    with LOCK, db() as c:
        rows = c.execute('SELECT c.id, c.location, c.quote, c.text, c.created, c.resolved, c.kind, c.doc, c.page, c.rect, '
                         'c.strokes, c.src_from, c.src_to, c.audio_id, a.state, a.transcript, a.duration, c.author '
                         'FROM comments c LEFT JOIN audio a ON a.id = c.audio_id ' + where + ' ORDER BY c.id', args).fetchall()
    out = []
    for r in rows:
        out.append({'id': r[0], 'where': r[1], 'quote': r[2], 'text': r[3], 'created': r[4], 'resolved': bool(r[5]),
                    'kind': r[6] or 'text', 'doc': r[7], 'page': r[8], 'rect': json.loads(r[9]) if r[9] else None,
                    'strokes': json.loads(r[10]) if r[10] else None, 'src_from': r[11], 'src_to': r[12],
                    'audio_id': r[13], 'audio_state': r[14], 'transcript': r[15], 'audio_duration': r[16], 'author': r[17]})
    return out


# ---- voice notes: stored under data/audio, transcribed one at a time by vps/transcribe.py (faster-whisper)
AUDIO_DIR = os.path.join(P.data, 'audio')
os.makedirs(AUDIO_DIR, exist_ok=True)
AUDIO_Q = queue.Queue()
VENV_PY = CFG.get('mcp_python') or (CFG['server_root'] + '/mcpvenv/bin/python')


def audio_worker():
    while True:
        aid = AUDIO_Q.get()
        with LOCK, db() as c:
            r = c.execute('SELECT file FROM audio WHERE id = ?', (aid,)).fetchone()
            c.execute("UPDATE audio SET state = 'transcribing' WHERE id = ?", (aid,))
        try:
            p = subprocess.run([VENV_PY, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transcribe.py'),
                                os.path.join(AUDIO_DIR, r[0]), '--model', CFG.get('whisper_model', 'base'),
                                '--models-dir', os.path.join(P.data, 'models')] +
                               (['--prompt', CFG['whisper_prompt']] if CFG.get('whisper_prompt') else []),
                               capture_output=True, timeout=900, env=P.env())
            res = json.loads(p.stdout.decode('utf8') or '{}') if p.returncode == 0 else None
            with LOCK, db() as c:
                if res:
                    c.execute("UPDATE audio SET state = 'done', transcript = ?, language = ?, duration = ? WHERE id = ?",
                              (res.get('text', ''), res.get('language'), res.get('duration'), aid))
                else:
                    c.execute("UPDATE audio SET state = 'error', error = ? WHERE id = ?",
                              (p.stderr.decode(errors='replace')[-1500:], aid))
        except Exception as e:
            with LOCK, db() as c:
                c.execute("UPDATE audio SET state = 'error', error = ? WHERE id = ?", (f'{type(e).__name__}: {e}', aid))


threading.Thread(target=audio_worker, daemon=True, name='audio').start()

# ---- what the user is looking at / has selected right now (reported by the page; read by the MCP endpoint)
FOCUS = {}


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
            return self.send(200, comment_rows())
        if path.startswith('/api/comments/'):
            rows = comment_rows('WHERE c.id = ?', (int(path.rsplit('/', 1)[1]),))
            return self.send(200, rows[0]) if rows else self.send(404, {'error': 'no such comment'})
        if path == '/api/focus':
            return self.send(200, {**FOCUS, 'age': round(time.time() - FOCUS['time'], 1)} if FOCUS else {})
        if path.startswith('/api/audio/'):
            name = path.rsplit('/', 1)[1]
            aid = int(name.split('.')[0])
            with LOCK, db() as c:
                r = c.execute('SELECT file, mime, state, transcript, language, duration, error FROM audio WHERE id = ?', (aid,)).fetchone()
            if not r:
                return self.send(404, {'error': 'no such audio'})
            if name.endswith('.json'):
                return self.send(200, {'id': aid, 'state': r[2], 'transcript': r[3], 'language': r[4], 'duration': r[5],
                                       'error': (r[6] or '')[-300:]})
            full = safe_join(AUDIO_DIR, r[0])
            if not full:
                return self.send(404, {'error': 'audio file missing'})
            with open(full, 'rb') as f:
                return self.send(200, f.read(), r[1] or 'application/octet-stream')
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
        if path.startswith('/api/blame/'):
            return self.send(200, REPO.blame(path.rsplit('/', 1)[1]))
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
        if path == '/api/audio':          # a raw recording (audio/webm, audio/ogg, audio/mp4, ...), up to 25 MB
            return self.post_audio()
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

    def post_audio(self):
        n = int(self.headers.get('Content-Length') or 0)
        mime = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if not 0 < n <= 25_000_000 or not mime.startswith('audio/'):
            return self.send(400, {'error': 'send one audio recording (audio/*, up to 25 MB)'})
        data = self.rfile.read(n)
        ext = {'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3', 'audio/wav': 'wav',
               'audio/x-wav': 'wav'}.get(mime, 'bin')
        with LOCK, db() as c:
            aid = c.execute("INSERT INTO audio (created, mime, state) VALUES (?, ?, 'queued')", (time.time(), mime)).lastrowid
            fn = f'{aid}.{ext}'
            c.execute('UPDATE audio SET file = ? WHERE id = ?', (fn, aid))
        with open(os.path.join(AUDIO_DIR, fn), 'wb') as f:
            f.write(data)
        AUDIO_Q.put(aid)
        return self.send(200, {'id': aid, 'state': 'queued'})

    def post(self, path, b):
        if path == '/api/comments':
            text = str(b.get('text', '')).strip()
            strokes, rect = b.get('strokes'), b.get('rect')
            audio_id = int(b['audio_id']) if b.get('audio_id') else None
            if not text and not strokes and not audio_id:
                return self.send(400, {'error': 'empty comment'})
            num = lambda v: int(v) if v not in (None, '') else None   # noqa: E731
            with LOCK, db() as c:
                cur = c.execute('INSERT INTO comments (location, quote, text, created, kind, doc, page, rect, strokes, src_from, '
                                'src_to, audio_id, author) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                (str(b.get('where', ''))[:500], str(b.get('quote', ''))[:4000], text[:8000], time.time(),
                                 str(b.get('kind') or ('drawing' if strokes else 'text'))[:20],
                                 b.get('doc') if b.get('doc') in DOCS else None, num(b.get('page')),
                                 json.dumps(rect)[:2000] if rect else None, json.dumps(strokes)[:200_000] if strokes else None,
                                 num(b.get('src_from')), num(b.get('src_to')), audio_id, str(b.get('author') or '')[:60] or None))
            return self.send(200, {'ok': True, 'id': cur.lastrowid})
        if path == '/api/focus':
            # keep only plain, size-limited fields
            f = {k: b.get(k) for k in ('view', 'doc', 'page', 'selection', 'last_selection', 'drawing', 'editor') if k in b}
            s = json.dumps(f)
            if len(s) > 60_000:
                return self.send(400, {'error': 'focus too large'})
            FOCUS.clear(); FOCUS.update(json.loads(s)); FOCUS['time'] = time.time()
            return self.send(200, {'ok': True})
        if path.startswith('/api/edit/'):
            doc = path.rsplit('/', 1)[1]
            if doc not in DOCS:
                raise KeyError(doc)
            r = REPO.edit(doc, str(b.get('rev') or ''), int(b['start']), str(b['old']), str(b['new']),
                          who=str(b.get('who') or 'web')[:60])
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
