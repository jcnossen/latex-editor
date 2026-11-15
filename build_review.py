r"""Build the review page (latexdiff of the configured documents against the baseline tag; see config.json).

Baseline: the git tag `baseline_tag`; current: the working tree. The files are staged and sent to the server (the
`ssh` command of config.json), where latexdiff + pdfLaTeX run in <server_root>/build (remote/run.sh, remote/mark.py);
the compressed diff PDFs, the change positions and the change texts come back, and site/index.html is generated from
template.html with that data. The site is uploaded to <server_root>/site, where the review app (vps/app.py) serves it.
With "flatten_markup": true the markup of a previous revision round (\textcolor{blue}{..} additions,
\textcolor{red}{\sout{..}} deletions) is accepted on both sides first, so the new changes are the only coloured text.

Usage (from anywhere):
  python .review/build_review.py                 sync laptop <-> web (edit_sync.py; stops on a conflict), then the
                                                 server rebuilds the tracked changes from that text and this waits
  python .review/build_review.py --laptop-build  the old full build from the laptop files + upload; refuses when the
                                                 laptop files and the web text differ (--force overrides)
  python .review/build_review.py --local         the same, site/ only (no upload)
  python .review/build_review.py --page          template.html changes only: re-render + upload the page

Since the live editor (vps/editor.py) the server rebuilds the latexdiff data itself from the web text after every
edit or sync, and the page prefers that rebuild, so laptop and web never show different text once synced.
"""
import datetime, html, io, json, os, re, subprocess, sys, tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prconfig import CFG  # noqa: E402
from hunks import all_hunks  # noqa: E402

REPO = CFG['repo_path']
BASE = CFG['baseline_tag']
DOCS = [(d['key'], d['file'], d.get('title', d['key'])) for d in CFG['docs']]
SUPPORT = CFG['support']
REMOTE = CFG['server_root'] + '/build'
SITE_REMOTE = CFG['server_root'] + '/site'
PLINK = CFG['ssh']


def flatten_first_revision(t: bytes) -> bytes:
    """Accept the first revision's markup: unwrap \\textcolor{blue}{..}, drop \\textcolor{red}{\\sout{..}}."""
    s = t.decode('utf8')
    def arg_end(s, i):          # s[i] == '{': index after the matching '}'
        d = 0
        for j in range(i, len(s)):
            d += {'{': 1, '}': -1}.get(s[j], 0)
            if d == 0:
                return j + 1
        return len(s)
    for pat, keep in (('\\textcolor{red}{\\sout{', False), ('\\textcolor{blue}{', True)):
        while (i := s.find(pat)) >= 0:
            k = i + len('\\textcolor{red}' if not keep else '\\textcolor{blue}')
            e = arg_end(s, k)
            s = s[:i] + (s[k + 1:e - 1] if keep else '') + s[e:]
    return s.encode('utf8')


def git(*a):
    return subprocess.run(['git', '-C', REPO, *a], capture_output=True, check=True).stdout


def stage() -> bytes:
    buf = io.BytesIO()
    figs = set()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        def add(name, data: bytes):
            ti = tarfile.TarInfo(name); ti.size = len(data); tar.addfile(ti, io.BytesIO(data))
        for key, fn, _ in DOCS:
            old = git('show', f'{BASE}:{fn}')
            new = open(os.path.join(REPO, fn), 'rb').read()
            old, new = old.replace(b'\r\n', b'\n'), new.replace(b'\r\n', b'\n')   # same line endings on both sides
            if CFG['flatten_markup']:
                old, new = flatten_first_revision(old), flatten_first_revision(new)
            add(f'old_{key}.tex', old); add(fn, new)
            for t in (old, new):
                figs |= set(re.findall(rb'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}', t))
            # the baseline's figure files as they were at the baseline commit (a figure regenerated under the same
            # file name must still show its old version), under old/
            for f in set(re.findall(rb'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}', old)):
                try:
                    add('old/' + f.decode(), git('show', f'{BASE}:{f.decode()}'))
                except subprocess.CalledProcessError:
                    print('baseline figure not in git:', f.decode())
        for fn in SUPPORT:
            add(fn, open(os.path.join(REPO, fn), 'rb').read())
        for f in sorted(figs):
            p = os.path.join(REPO, f.decode())
            if os.path.exists(p):
                add(f.decode(), open(p, 'rb').read())
            else:
                print('missing figure (left out):', f.decode())
        for fn in ('mark.py', 'run.sh', 'figures.py'):   # Unix line endings for bash / python on the server
            add(fn, open(os.path.join(HERE, 'remote', fn), 'rb').read().replace(b'\r\n', b'\n'))
        add('prconfig.py', open(os.path.join(HERE, 'prconfig.py'), 'rb').read().replace(b'\r\n', b'\n'))
        add('config.json', open(CFG['_path'], 'rb').read())
    return buf.getvalue()


def remote(cmd, stdin=None) -> bytes:
    r = subprocess.run(PLINK + [cmd], input=stdin, capture_output=True)
    return r.stdout


def main():
    data = stage()
    print(f'staged {len(data) / 2**20:.1f} MB; building on the server ...')
    log = remote(f'rm -rf {REMOTE} && mkdir -p {REMOTE} && cd {REMOTE} && tar xf - && bash run.sh', stdin=data)
    print(log.decode(errors='replace'))
    site = os.path.join(HERE, 'site'); os.makedirs(site, exist_ok=True)
    docs = []
    for key, fn, title in DOCS:
        pdf = remote(f'cat {REMOTE}/{key}_review.pdf')
        if not pdf.startswith(b'%PDF'):
            raise SystemExit(f'{key}: no PDF (see {REMOTE}/{key}_build.log on the server)')
        open(os.path.join(site, f'{key}.pdf'), 'wb').write(pdf)
        marks = {}
        for line in remote(f'cat {REMOTE}/{key}_diff.difmarks').decode().split('\n'):
            p = line.split()
            if len(p) == 5:
                i, kind, page, x, y = p
                if i not in marks:          # first position (a block split over two lines keeps its start)
                    try:
                        marks[i] = (int(page), int(x) / 65536 * 72 / 72.27, int(y) / 65536 * 72 / 72.27)
                    except ValueError:      # roman page numbers etc.
                        pass
        changes = json.loads(remote(f'cat {REMOTE}/{key}_diff.changes.json') or b'[]')
        out = []
        for c in changes:
            m = marks.get(str(c['id']))
            if m and c['text']:
                out.append({'id': c['id'], 'type': c['type'], 'text': c['text'], 'after': c.get('after', ''),
                            'page': m[0], 'y': round(m[2], 1)})
        docs.append({'key': key, 'title': title, 'file': f'{key}.pdf', 'changes': out})
        print(f'{key}: {len(pdf) / 2**20:.1f} MB PDF, {len(out)} of {len(changes)} changes placed')
    # figures: before / after PNGs and both captions
    figdir = os.path.join(site, 'figs')
    if os.path.isdir(figdir):
        for f in os.listdir(figdir):
            os.remove(os.path.join(figdir, f))
    tar = remote(f'cd {REMOTE} && tar cf - figures.json figs')
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(site)
    figures = json.load(open(os.path.join(site, 'figures.json'), encoding='utf8'))
    os.remove(os.path.join(site, 'figures.json'))
    print(f'figures: {len(figures)}, {sum(f["image_changed"] for f in figures)} with a changed image')
    status = git('status', '--short', '--', '*.tex', *SUPPORT, 'figures').decode().strip().split('\n')
    meta = {'built': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), 'base': BASE,
            'base_commit': git('rev-parse', '--short', BASE).decode().strip(),
            'head': git('rev-parse', '--short', 'HEAD').decode().strip(), 'dirty': [s for s in status if s]}
    json.dump({'meta': meta, 'docs': docs, 'figures': figures}, open(os.path.join(site, '_data.json'), 'w', encoding='utf8'),
              ensure_ascii=False)
    write_pages(site, meta, docs, figures)


def write_pages(site, meta, docs, figures):
    hs = all_hunks()
    print(f'source changes: {len(hs)} hunks')
    tpl = open(os.path.join(HERE, 'template.html'), encoding='utf8').read()
    tpl = tpl.replace('__TITLE__', html.escape(CFG['title']))
    cfg_docs = [{'key': d['key'], 'title': d.get('title', d['key']), 'file': d['file'], 'fig_prefix': d.get('fig_prefix', 'Fig. ')}
                for d in CFG['docs']]
    def page(mode):
        blob = json.dumps({'meta': {**meta, 'mode': mode, 'cfg_docs': cfg_docs, 'flatten': CFG['flatten_markup']},
                           'docs': docs, 'figures': figures, 'hunks': hs}, ensure_ascii=False).replace('</', '<\\/')
        return tpl.replace('/*__REVIEW_DATA__*/null', blob)
    open(os.path.join(site, 'index.html'), 'w', encoding='utf8').write(page('claude'))
    print('wrote', os.path.join(site, 'index.html'))
    if '--beta' in sys.argv:        # a test instance: only its page (it serves the PDFs of the server's own builds)
        b = CFG['server_root'] + '/beta/site/index.html'
        r = subprocess.run(PLINK + [f'cat > {b} && chmod a+r {b} && echo ok'], input=page('vps').encode('utf8'), capture_output=True)
        print('beta page:', r.stdout.decode().strip() or r.stderr.decode(errors='replace'))
    elif '--local' not in sys.argv:
        upload(site, page('vps').encode('utf8'))


def upload(site, index: bytes):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        def add(name, data):
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644; tar.addfile(ti, io.BytesIO(data))
        add('index.html', index)
        for root, _, files in os.walk(site):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), site).replace(os.sep, '/')
                if rel != 'index.html' and not rel.startswith('_') and not rel.endswith('.bak'):
                    add(rel, open(os.path.join(root, f), 'rb').read())
    new = SITE_REMOTE + '.new'
    log = remote(f'rm -rf {new} && mkdir -p {new} && cd {new} && tar --no-same-owner -xf - && chmod -R a+rX {new} && '
                 f'rm -rf {SITE_REMOTE}.old && mv {SITE_REMOTE} {SITE_REMOTE}.old && mv {new} {SITE_REMOTE} && echo uploaded',
                 stdin=buf.getvalue())
    print(log.decode(errors='replace').strip(), f'{len(buf.getvalue()) / 2**20:.1f} MB to {SITE_REMOTE}')


def sync_and_server_rebuild():
    """Default: sync laptop <-> web (edit_sync.py), then let the server rebuild the tracked-changes data from the web
    text (which now equals the laptop text) and wait for it."""
    import time
    r = subprocess.run([sys.executable, os.path.join(HERE, 'edit_sync.py')])
    if r.returncode:
        sys.exit(r.returncode)              # 2 = conflict, 3 = web changed during the push: nothing rebuilt
    def get(path, method='GET'):
        out = remote(f"curl -s -m 30 -X {method} http://127.0.0.1:{CFG['port']}{path}")
        return json.loads(out or b'{}')
    before = (get('/api/editor').get('diff') or {}).get('built') or 0
    get('/api/rebuild-diff', 'POST')
    print('server rebuild of the tracked changes started ...', flush=True)
    t = time.time()
    while time.time() - t < 600:
        time.sleep(5)
        d = get('/api/editor').get('diff') or {}
        if d.get('state') == 'error':
            raise SystemExit('server rebuild failed: ' + d.get('message', ''))
        if (d.get('built') or 0) > before and d.get('state') != 'running':
            print(f"server rebuild done in {d.get('duration')} s: {d.get('message')}")
            return
    raise SystemExit('server rebuild still running after 10 min (check `journalctl -u paperreview` on the VPS)')


def check_in_sync():
    """For a laptop-side build: refuse when the laptop files and the web text differ (unless --force)."""
    r = subprocess.run([sys.executable, os.path.join(HERE, 'edit_sync.py'), '--check'])
    if r.returncode and '--force' not in sys.argv:
        raise SystemExit('The laptop files and the web text differ: run `python .review/edit_sync.py` first '
                         '(or pass --force to build from the laptop files anyway).')


if __name__ == '__main__':
    if '--page' in sys.argv:    # template / hunk changes only: reuse the last build's PDFs, positions and figures
        site = os.path.join(HERE, 'site')
        d = json.load(open(os.path.join(site, '_data.json'), encoding='utf8'))
        write_pages(site, d['meta'], d['docs'], d['figures'])
    elif '--laptop-build' in sys.argv or '--local' in sys.argv:
        check_in_sync()
        main()
    else:
        sync_and_server_rebuild()
