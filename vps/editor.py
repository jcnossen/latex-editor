"""Live editor backend for the review app: a git working copy of the two manuscript files, a fast pdfLaTeX compile
per document, synctex lookups, laptop sync (3-way merge via git), and a background latexdiff rebuild.

Data layout (all under --data, default /srv/paperreview/data):
  repo/         git repo; branch `web` (checked out) = the text being edited on the site, one commit per save;
                branch `laptop` = the last laptop state merged into `web` (the base for the laptop's 3-way merge)
  assets/       support files (bib, cls, ...) and figures/ as uploaded from the laptop; baseline/ holds
                the baseline .tex files (the configured git tag) and their figures, for the diff build
  figcache/     each figure compressed once (ghostscript /ebook; bitmaps converted to PDF), by content hash
  compile/<doc> fast compile dir: precompiled preamble (mylatexformat), aux, synctex
  live/         the last good PDF + synctex of each document (what the editor shows)
  diff/         background latexdiff build (diff PDFs, markers, figures, hunks) -> diff/out/
The fast compile: the preamble up to the last \\usepackage line is dumped into a format once (rebuilt when it changes);
a save runs one pdfLaTeX pass with compressed figures (~1 s), then bibtex / a second pass only when citations or
labels changed.
"""
import glob, gzip, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prconfig import CFG, DOCS, TITLES, SUPPORT  # noqa: E402,F401  (config.json next to the app)
INCLUDE = re.compile(r'\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}')


class Conflict(Exception):
    def __init__(self, msg, **info):
        super().__init__(msg); self.info = info


class NotReady(Exception):
    pass


def author_name(who):
    """The author recorded for an edit: 'mcp' = an AI client, 'web' / 'review' = the review page (optionally with the
    user's name after a colon, e.g. 'web:Jelmer'), 'laptop' = a laptop sync."""
    who = (who or 'web').strip()
    kind, _, name = who.partition(':')
    kind = kind.lower()
    if kind == 'mcp':
        return 'AI (MCP)' + (f' for {name}' if name else '')
    if kind in ('web', 'review'):
        return f'{name} (web)' if name else 'web editor'
    if kind in ('laptop', 'revert'):
        return kind
    return who[:40]


class Paths:
    def __init__(self, data):
        self.data = data
        for k in ('repo', 'assets', 'figcache', 'compile', 'live', 'diff', 'home', 'texmf-var'):
            setattr(self, k.replace('-', '_'), os.path.join(data, k))
        for d in (self.assets, self.figcache, self.compile, self.live, self.diff, self.home, self.texmf_var):
            os.makedirs(d, exist_ok=True)

    def env(self):
        return {**os.environ, 'HOME': self.home, 'TEXMFVAR': self.texmf_var, 'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_AUTHOR_NAME': 'review editor', 'GIT_AUTHOR_EMAIL': 'editor@paperreview',
                'GIT_COMMITTER_NAME': 'review editor', 'GIT_COMMITTER_EMAIL': 'editor@paperreview', 'LC_ALL': 'C.UTF-8'}


# ---------------------------------------------------------------------------------------------------------------- repo
class Repo:
    def __init__(self, P):
        self.P = P
        self.lock = threading.RLock()
        self.listeners = []          # called (reason) after the web text changed
        self._blame_cache = {}

    def git(self, *a, input=None, check=True, author=None):
        env = self.P.env()
        if author:      # who made this version: shown in the history and in the per-line blame (hover in the page)
            env.update(GIT_AUTHOR_NAME=author[:60], GIT_AUTHOR_EMAIL='editor@paperreview')
        r = subprocess.run(['git', '-C', self.P.repo, *a], input=input, capture_output=True, env=env)
        if check and r.returncode:
            raise RuntimeError(f'git {a[0]}: {r.stderr.decode(errors="replace").strip()}')
        return r.stdout.decode('utf8', errors='replace')

    def ensure(self):
        if not os.path.isdir(os.path.join(self.P.repo, '.git')):
            os.makedirs(self.P.repo, exist_ok=True)
            self.git('init', '-q', '-b', 'web')
            self.git('config', 'core.autocrlf', 'false')

    def head(self, ref='web'):
        if not os.path.isdir(os.path.join(self.P.repo, '.git')):
            return None
        r = subprocess.run(['git', '-C', self.P.repo, 'rev-parse', '--verify', '-q', ref + '^{commit}'],
                           capture_output=True, env=self.P.env())
        return r.stdout.decode().strip() or None

    def read(self, fn, ref=None):
        if ref:
            return self.git('show', f'{ref}:{fn}')
        p = os.path.join(self.P.repo, fn)
        return open(p, encoding='utf8', newline='').read() if os.path.exists(p) else None

    def changed(self, reason):
        for f in self.listeners:
            try:
                f(reason)
            except Exception:
                traceback.print_exc()

    def edit(self, doc, rev, start, old, new, who='web'):
        """Replace the source lines `old` (a block starting at line `start`, as read at commit `rev`) by `new`."""
        fn = DOCS[doc]
        old, new = old.replace('\r\n', '\n'), new.replace('\r\n', '\n')
        if not old.strip():
            raise Conflict('empty block')
        with self.lock:
            head = self.head()
            if not head:
                raise NotReady('the working copy is not initialised yet (run edit_sync.py on the laptop)')
            lines = self.read(fn).split('\n')
            ol = old.split('\n')
            i = start - 1
            if lines[i:i + len(ol)] != ol:
                cands = [k for k in range(len(lines) - len(ol) + 1) if lines[k:k + len(ol)] == ol]
                if not cands:
                    raise Conflict('this text was changed in the meantime (another save or a laptop sync)', head=head)
                i = min(cands, key=lambda k: abs(k - (start - 1)))
            if old == new:
                return {'rev': head, 'start': i + 1, 'changed': False}
            lines[i:i + len(ol)] = new.split('\n')
            with open(os.path.join(self.P.repo, fn), 'w', encoding='utf8', newline='') as f:
                f.write('\n'.join(lines))
            self.git('add', fn)
            summary = re.sub(r'\s+', ' ', new).strip()[:60]
            self.git('commit', '-q', '-m', f'{who}: {doc} line {i + 1}: {summary}', author=author_name(who))
            head = self.head()
        self.changed(doc)
        return {'rev': head, 'start': i + 1, 'changed': True}

    def log(self, n=60):
        if not self.head():
            return []
        out = self.git('log', 'web', f'-n{n}', '--format=%H%x1f%ct%x1f%s%x1f%P%x1e')
        res = []
        for line in out.split('\x1e'):
            line = line.strip('\n')
            if line:
                h, t, s, p = (line.split('\x1f') + ['', '', ''])[:4]
                res.append({'rev': h, 'time': int(t), 'msg': s, 'merge': len(p.split()) > 1})
        return res

    def show(self, rev):
        if not re.fullmatch(r'[0-9a-f]{7,40}', rev):
            raise ValueError('bad rev')
        return self.git('show', '--format=%H%n%ci%n%s%n', '--first-parent', '-m', '--patch', '--word-diff=plain',
                        '--word-diff-regex=[^[:space:]]+', rev)

    def revert(self, rev):
        if not re.fullmatch(r'[0-9a-f]{7,40}', rev):
            raise ValueError('bad rev')
        with self.lock:
            r = subprocess.run(['git', '-C', self.P.repo, 'revert', '--no-edit', '-m', '1', rev] if self._is_merge(rev)
                               else ['git', '-C', self.P.repo, 'revert', '--no-edit', rev],
                               capture_output=True, env=self.P.env())
            if r.returncode:
                self.git('revert', '--abort', check=False)
                self.git('reset', '-q', '--hard', 'web', check=False)
                raise Conflict('this edit cannot be undone automatically: later edits changed the same text')
            head = self.head()
        self.changed('revert')
        return {'rev': head}

    def _is_merge(self, rev):
        return len(self.git('rev-list', '--parents', '-n1', rev).split()) > 2

    def blame(self, doc):
        """Per source line of the web text: {author, time, summary} of the version that last changed it."""
        fn = DOCS[doc]
        with self.lock:
            head = self.head()
            if not head:
                return {'rev': None, 'lines': []}
            if self._blame_cache.get(doc, (None,))[0] == head:
                return self._blame_cache[doc][1]
            out = self.git('blame', '--line-porcelain', 'web', '--', fn)
        commits, lines, cur = {}, [], None
        for line in out.split('\n'):
            if re.match(r'^[0-9a-f]{40} ', line):
                cur = commits.setdefault(line[:40], {'rev': line[:10]})
            elif cur is not None and line.startswith('author '):
                cur['author'] = line[7:]
            elif cur is not None and line.startswith('author-time '):
                cur['time'] = int(line[12:])
            elif cur is not None and line.startswith('summary '):
                cur['summary'] = line[8:]
            elif line.startswith('\t') and cur is not None:
                lines.append(cur)
        for c in commits.values():      # versions saved before authors were recorded: infer the source
            c['who'] = author_name(c.get('summary', '').split(':', 1)[0]) if c.get('author') in (None, 'review editor') else c['author']
            if c.get('summary', '').startswith('laptop sync') or c.get('summary', '').startswith('Merge'):
                c['who'] = 'laptop'
            elif c.get('summary', '').startswith('Revert'):
                c['who'] = 'undo in the web editor'
        res = {'rev': head, 'lines': [[c['who'], c.get('time'), c.get('summary', '')[:80], c['rev']] for c in lines]}
        self._blame_cache[doc] = (head, res)
        return res

    # ---- laptop sync
    def state(self):
        with self.lock:
            web, lap = self.head('web'), self.head('laptop')
            files = {}
            for fn in DOCS.values():
                files[fn] = {'web': self.read(fn, 'web') if web else None, 'base': self.read(fn, 'laptop') if lap else None}
            return {'web': web, 'laptop': lap, 'files': files}

    def push(self, base, files, msg='laptop sync'):
        """Commit the laptop's (already merged) files on `laptop` (parents: the previous laptop commit and `base`, the
        web commit the laptop merged), then merge `laptop` into `web`. A conflict (web edits made while the laptop was
        merging) leaves everything as it was and raises Conflict."""
        if set(files) != set(DOCS.values()):
            raise ValueError('push needs both manuscript files')
        with self.lock:
            self.ensure()
            web, lap = self.head('web'), self.head('laptop')
            if web and not base:
                raise Conflict('the laptop must merge the web edits first (base missing)')
            if web and base:
                if subprocess.run(['git', '-C', self.P.repo, 'merge-base', '--is-ancestor', base, 'web'],
                                  env=self.P.env()).returncode:
                    raise Conflict('base is not a commit of the web branch')
            entries = []
            for fn in sorted(files):
                sha = self.git('hash-object', '-w', '--stdin', input=files[fn].replace('\r\n', '\n').encode('utf8')).strip()
                entries.append(f'100644 blob {sha}\t{fn}')
            tree = self.git('mktree', input=('\n'.join(entries) + '\n').encode()).strip()
            if lap and self.git('rev-parse', lap + '^{tree}').strip() == tree and web and \
                    self.git('rev-parse', 'web^{tree}').strip() == tree:
                return {'web': web, 'laptop': lap, 'changed': False}
            parents = []
            for p in (lap, base):
                if p and p not in parents:
                    parents.append(p)
            commit = self.git('commit-tree', tree, *sum((['-p', p] for p in parents), []), '-m', msg, author='laptop').strip()
            self.git('update-ref', 'refs/heads/laptop', commit)
            if not web:
                self.git('update-ref', 'refs/heads/web', commit)
                self.git('reset', '-q', '--hard', 'web')
            else:
                r = subprocess.run(['git', '-C', self.P.repo, 'merge', '-q', '--no-edit', '-m', msg + ' (merge)', 'laptop'],
                                   capture_output=True, env=self.P.env())
                if r.returncode:
                    files_c = self.git('diff', '--name-only', '--diff-filter=U', check=False).split()
                    self.git('merge', '--abort', check=False)
                    self.git('reset', '-q', '--hard', 'web', check=False)
                    if lap:
                        self.git('update-ref', 'refs/heads/laptop', lap)
                    else:
                        self.git('update-ref', '-d', 'refs/heads/laptop')
                    raise Conflict('new web edits conflict with this laptop sync; run the sync again', files=files_c)
            res = {'web': self.head('web'), 'laptop': self.head('laptop'), 'changed': True}
        self.changed('sync')
        return res


# ----------------------------------------------------------------------------------------------------------- figures
_fig_memo, _fig_lock = {}, threading.Lock()


def small_figure(P, src):
    """A compressed copy of a figure file for the fast preview (cached by content); falls back to the file itself."""
    try:
        st = os.stat(src)
    except OSError:
        return None
    key = (src, st.st_size, st.st_mtime_ns)
    with _fig_lock:
        if key in _fig_memo and os.path.exists(_fig_memo[key]):
            return _fig_memo[key]
        h = hashlib.sha1(open(src, 'rb').read()).hexdigest()[:16]
        out = os.path.join(P.figcache, h + '.pdf')
        ext = os.path.splitext(src)[1].lower()
        if not os.path.exists(out):
            tmp = out + '.tmp'
            try:
                inp = src
                if ext in ('.png', '.jpg', '.jpeg'):
                    inp = out + '.img.pdf'
                    subprocess.run(['mutool', 'convert', '-o', inp, src], check=True, capture_output=True, timeout=120)
                elif ext != '.pdf':
                    return src
                subprocess.run(['gs', '-sDEVICE=pdfwrite', '-dPDFSETTINGS=/ebook', '-dCompatibilityLevel=1.5', '-dNOPAUSE',
                                '-dBATCH', '-dQUIET', '-sOutputFile=' + tmp, inp], check=True, capture_output=True, timeout=300)
                if ext == '.pdf' and os.path.getsize(tmp) >= st.st_size:
                    shutil.copyfile(src, tmp)       # nothing gained: keep the original
                os.replace(tmp, out)
            except Exception:
                traceback.print_exc()
                return src
            finally:
                for f in (tmp, out + '.img.pdf'):
                    if os.path.exists(f):
                        os.remove(f)
        _fig_memo[key] = out
        return out


def place(src, dst):
    """Make dst a symlink to src (replace a stale one)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.islink(dst) and os.readlink(dst) == src:
        return
    if os.path.lexists(dst):
        os.remove(dst)
    os.symlink(src, dst)


# ----------------------------------------------------------------------------------------------------------- compile
ERR = re.compile(r'^(?:\./)?([^\s:]+\.(?:tex|cls|sty|bbl)):(\d+): (.*)$')


def parse_log(log):
    errors = []
    lines = log.split('\n')
    for k, line in enumerate(lines):
        m = ERR.match(line)
        if m:
            ctx = ' '.join(x.strip() for x in lines[k + 1:k + 3] if x.strip())[:240]
            errors.append({'file': m.group(1), 'line': int(m.group(2)), 'msg': m.group(3)[:200], 'context': ctx})
        elif line.startswith('! '):
            errors.append({'file': None, 'line': None, 'msg': line[2:200], 'context': ''})
    warnings = len(re.findall(r'^(?:LaTeX|Package \w+) Warning', log, re.M))
    undefined = sorted(set(re.findall(r"Reference `([^']+)' on page", log)))
    rerun = bool(re.search(r'Rerun to get|Label\(s\) may have changed|Rerun LaTeX', log))
    return errors, warnings, undefined, rerun


class Compiler:
    def __init__(self, P, repo, doc):
        self.P, self.repo, self.doc, self.fn = P, repo, doc, DOCS[doc]
        self.dir = os.path.join(P.compile, doc)
        self.job = self.fn[:-4]
        self.cv = threading.Condition()
        self.pending = self.running = False
        self.ver = 0                  # bumps on every status change (long polls wait on it)
        self.status = {'doc': doc, 'state': 'idle', 'seq': 0, 'rev': None, 'errors': [], 'message': ''}
        p = os.path.join(P.live, doc + '.json')
        if os.path.exists(p):
            try:
                self.status.update(json.load(open(p)), state='idle')
            except Exception:
                pass

    def _set(self, **kw):
        with self.cv:
            self.status.update(kw); self.ver += 1; self.cv.notify_all()

    def request(self):
        with self.cv:
            self.pending = True
            if not self.running:
                self.running = True
                threading.Thread(target=self._loop, daemon=True, name='compile-' + self.doc).start()

    def wait(self, since, timeout=25):
        with self.cv:
            self.cv.wait_for(lambda: self.ver > since, timeout)
            return {**self.status, 'ver': self.ver}

    def _loop(self):
        while True:
            with self.cv:
                if not self.pending:
                    self.running = False; return
                self.pending = False
            self._set(state='running', started=time.time())
            try:
                self._compile()
            except Exception as e:
                traceback.print_exc()
                self._set(state='error', message=f'{type(e).__name__}: {e}')

    def _run(self, args, timeout=90):
        t = time.time()
        r = subprocess.run(args, cwd=self.dir, capture_output=True, env=self.P.env(), timeout=timeout, stdin=subprocess.DEVNULL)
        return r, time.time() - t

    def _compile(self):
        with self.repo.lock:
            rev = self.repo.head()
            text = self.repo.read(self.fn) if rev else None
        if text is None:
            self._set(state='idle', message='not initialised'); return
        os.makedirs(self.dir, exist_ok=True)
        t0 = time.time()
        for f in SUPPORT:
            src = os.path.join(self.P.assets, f)
            if os.path.exists(src):
                place(src, os.path.join(self.dir, f))
        missing = []
        for g in sorted(set(INCLUDE.findall(text))):
            g = g.strip()
            if '..' in g or g.startswith('/'):
                continue
            src = os.path.join(self.P.assets, g)
            small = small_figure(self.P, src)
            if small is None:
                missing.append(g); continue
            place(small, os.path.join(self.dir, g))
        t_fig = time.time() - t0
        # precompiled preamble: everything up to the last \usepackage line before \begin{document}; \endofdump is
        # appended to that line (line numbers stay the same for synctex and error messages)
        lines = text.split('\n')
        bd = next((k for k, l in enumerate(lines) if l.lstrip().startswith('\\begin{document}')), None)
        cut = None
        if bd is not None:
            cut = max((k for k in range(bd) if re.match(r'\s*\\(usepackage|RequirePackage|usetikzlibrary)\b', lines[k])), default=None)
        if cut is not None:
            pre = '\n'.join(lines[:cut + 1])
            lines[cut] = lines[cut] + '\\endofdump'
        else:
            pre = ''
        with open(os.path.join(self.dir, self.fn), 'w', encoding='utf8', newline='') as f:
            f.write('\n'.join(lines))
        dep = ''.join(f'{f}:{os.path.getmtime(os.path.join(self.P.assets, f)) if os.path.exists(os.path.join(self.P.assets, f)) else 0};'
                      for f in SUPPORT if f.endswith(('.cls', '.sty', '.ldf')))
        fkey = hashlib.sha1((pre + '\0' + dep).encode()).hexdigest()
        fmt = os.path.join(self.dir, self.job + '_fmt.fmt')
        t_fmt = 0.0
        use_fmt = cut is not None
        if use_fmt and (not os.path.exists(fmt) or self._read('fmt.key') != fkey):
            r, t_fmt = self._run(['pdflatex', '-ini', '-interaction=nonstopmode', '-jobname=' + self.job + '_fmt',
                                  '&pdflatex', 'mylatexformat.ltx', self.fn])
            if os.path.exists(fmt) and r.returncode == 0:
                self._write('fmt.key', fkey)
            else:
                use_fmt = False
                if os.path.exists(fmt):
                    os.remove(fmt)
        aux = os.path.join(self.dir, self.job + '.aux')
        if os.path.exists(aux):
            shutil.copyfile(aux, aux + '.good')
        cmd = ['pdflatex'] + (['-fmt=' + self.job + '_fmt'] if use_fmt else []) + \
              ['-interaction=nonstopmode', '-synctex=1', '-file-line-error', self.fn]
        passes, times, bib = 0, [], False
        while True:
            r, dt = self._run(cmd); passes += 1; times.append(round(dt, 2))
            log = open(os.path.join(self.dir, self.job + '.log'), encoding='latin-1').read() \
                if os.path.exists(os.path.join(self.dir, self.job + '.log')) else ''
            errors, warnings, undefined, rerun = parse_log(log)
            pdf = os.path.join(self.dir, self.job + '.pdf')
            ok = os.path.exists(pdf) and 'Output written on' in log
            if not ok:
                if os.path.exists(aux + '.good'):
                    shutil.copyfile(aux + '.good', aux)
                tail = '\n'.join(log.strip().split('\n')[-25:])
                self._set(state='error', rev=rev, errors=errors[:20], message='no PDF produced; the last good PDF is shown',
                          log_tail=tail, finished=time.time())
                return
            # citations: run bibtex when the aux cites keys the .bbl does not have (or no .bbl yet)
            need_bib = False
            if not bib:
                cites = set(k.strip() for c in re.findall(r'\\citation\{([^}]*)\}', open(aux, encoding='latin-1').read())
                            for k in c.split(','))
                bbl = os.path.join(self.dir, self.job + '.bbl')
                have = set(re.findall(r'\\bibitem(?:\[[^\]]*\])?\{([^}]*)\}', open(bbl, encoding='latin-1').read())) \
                    if os.path.exists(bbl) else set()
                need_bib = bool(cites - have) or (bool(have - cites) and passes == 1)
            self._publish(rev, passes, times, t_fig, t_fmt, errors, warnings, undefined, missing,
                          final=not (need_bib or (rerun and passes < 3)), t0=t0)
            if need_bib:
                self._run(['bibtex', self.job]); bib = True
                continue
            if rerun and passes < 3:
                continue
            return

    def _publish(self, rev, passes, times, t_fig, t_fmt, errors, warnings, undefined, missing, final, t0):
        for ext in ('.pdf', '.synctex.gz'):
            src = os.path.join(self.dir, self.job + ext)
            if os.path.exists(src):
                tmp = os.path.join(self.P.live, self.job + ext + '.tmp')
                shutil.copyfile(src, tmp)
                os.replace(tmp, os.path.join(self.P.live, self.job + ext))
        seq = self.status.get('seq', 0) + 1
        st = dict(state='ok' if final else 'running', rev=rev, seq=seq, passes=passes, pass_times=times,
                  fig_time=round(t_fig, 2), fmt_time=round(t_fmt, 2), total=round(time.time() - t0, 2),
                  errors=errors[:20], warnings=warnings, undefined=undefined[:20], missing=missing,
                  message='' if final else 'updating cross-references', finished=time.time(),
                  pdf=f'live/{self.doc}.pdf?v={seq}', log_tail='')
        self._set(**st)
        try:
            json.dump({k: v for k, v in self.status.items() if k != 'state'}, open(os.path.join(self.P.live, self.doc + '.json'), 'w'))
        except Exception:
            pass

    def _read(self, name):
        p = os.path.join(self.dir, name)
        return open(p).read() if os.path.exists(p) else None

    def _write(self, name, s):
        open(os.path.join(self.dir, name), 'w').write(s)

    # ---- synctex on the published PDF
    def synctex_edit(self, page, x, y):
        pdf = os.path.join(self.P.live, self.job + '.pdf')
        r = subprocess.run(['synctex', 'edit', '-o', f'{int(page)}:{float(x):.2f}:{float(y):.2f}:{pdf}'],
                           capture_output=True, env=self.P.env(), timeout=10)
        out = r.stdout.decode(errors='replace')
        for inp, line in re.findall(r'^Input:(.*)\nLine:(-?\d+)', out, re.M):
            if os.path.basename(inp) == self.fn and int(line) > 0:
                return int(line)
        return None

    def synctex_view(self, line_from, line_to):
        """Boxes (page, x, y top, w, h in PDF points from the top left) of the typeset material of a source range."""
        pdf = os.path.join(self.P.live, self.job + '.pdf')
        src = os.path.join(self.dir, self.fn)
        boxes = []
        span = range(int(line_from), int(line_to) + 1)
        if len(span) > 40:                   # long blocks: sample
            span = sorted(set(list(span)[::max(1, len(span) // 40)] + [line_to]))
        for ln in span:
            r = subprocess.run(['synctex', 'view', '-i', f'{ln}:0:{src}', '-o', pdf], capture_output=True,
                               env=self.P.env(), timeout=10)
            for blk in r.stdout.decode(errors='replace').split('Output:')[1:]:
                d = dict(re.findall(r'^(\w+):(.*)$', blk, re.M))
                try:
                    W, H, h, v = float(d['W']), float(d['H']), float(d['h']), float(d['v'])
                    if W <= 0:
                        continue
                    boxes.append({'page': int(d['Page']), 'x': h, 'y': v - H, 'w': W, 'h': H + float(d.get('d', 0) or 0)})
                except (KeyError, ValueError):
                    pass
        # one box per page: the union
        per = {}
        for b in boxes:
            u = per.get(b['page'])
            if not u:
                per[b['page']] = dict(b)
            else:
                x0, y0 = min(u['x'], b['x']), min(u['y'], b['y'])
                x1, y1 = max(u['x'] + u['w'], b['x'] + b['w']), max(u['y'] + u['h'], b['y'] + b['h'])
                u.update(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
        return sorted(per.values(), key=lambda b: b['page'])


# ------------------------------------------------------------------------------------------------- latexdiff rebuild
class DiffBuilder:
    """Rebuild the latexdiff review data from the web text (debounced): diff PDFs with change markers, the figure
    before / after list, and the source hunks. Output: diff/out/{main,SI}.pdf, figs/, review.json."""

    def __init__(self, P, repo, delay=20):
        self.P, self.repo, self.delay = P, repo, delay
        self.cv = threading.Condition()
        self.due = None
        self.status = {'state': 'idle', 'built': None, 'rev': None, 'message': ''}
        p = os.path.join(P.diff, 'out', 'review.json')
        if os.path.exists(p):
            try:
                self.status.update(json.load(open(p))['meta'].get('status', {}), state='idle')
            except Exception:
                pass
        threading.Thread(target=self._loop, daemon=True, name='diff').start()

    def request(self, delay=None):
        with self.cv:
            self.due = time.time() + (self.delay if delay is None else delay); self.cv.notify_all()

    def _loop(self):
        while True:
            with self.cv:
                while self.due is None or time.time() < self.due:
                    self.cv.wait(None if self.due is None else max(0.1, self.due - time.time()))
                self.due = None
            try:
                self.status.update(state='running', started=time.time())
                self._build()
            except Exception as e:
                traceback.print_exc()
                self.status.update(state='error', message=f'{type(e).__name__}: {e}')

    def _build(self):
        sys.path.insert(0, HERE)
        from diffdata import build      # noqa: E402  (server port of build_review.py's data assembly)
        with self.repo.lock:
            rev = self.repo.head()
            texts = {fn: self.repo.read(fn) for fn in DOCS.values()} if rev else None
        if not texts:
            self.status.update(state='idle', message='not initialised'); return
        t = time.time()
        msg = build(self.P, texts, rev)
        self.status.update(state='ok', built=time.time(), rev=rev, message=msg, duration=round(time.time() - t, 1))
        meta_p = os.path.join(self.P.diff, 'out', 'review.json')
        d = json.load(open(meta_p, encoding='utf8'))
        d['meta']['status'] = {k: v for k, v in self.status.items() if k != 'state'}
        json.dump(d, open(meta_p + '.tmp', 'w', encoding='utf8'), ensure_ascii=False)
        os.replace(meta_p + '.tmp', meta_p)
