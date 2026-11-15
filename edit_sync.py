r"""Sync the manuscript between the laptop working tree and the web editor on the VPS (3-way merge, never blind).

    python .review/edit_sync.py            sync both ways: web edits -> laptop .tex, laptop edits + figures -> web
    python .review/edit_sync.py --dry-run  show what would change, write nothing
    python .review/edit_sync.py --status   the same as --dry-run
    python .review/edit_sync.py --check    exit 0 if the laptop files equal the web text, 1 if they differ (no writes)
Exit codes: 0 synced (or in sync), 1 differ (--check only), 2 conflict (nothing written or pushed),
3 the web changed during the push (the laptop files already hold the merge: run again), other: error.

How it works. The server keeps a git repo: branch `web` = the text edited in the browser (one commit per save),
branch `laptop` = the last laptop state merged into it. For each configured document (config.json):
  L = the laptop file now, B = the server's `laptop` version (the last synced state), W = the server's `web` version.
  The merged text M = git merge-file(L, B, W): edits on either side since the last sync are combined.
  - Any conflict (the same lines changed on both sides): NOTHING is written or pushed; the conflicting merge, with
    <<<<<<< laptop / ======= / >>>>>>> web markers, goes to .review/conflicts/<file> for a manual resolution
    (edit the laptop file by hand, or undo the web edit in the History tab, then sync again).
  - Otherwise M is written to the laptop file (a copy of the old file goes to .review/sync-backups/ first; the file is
    re-read just before writing, and the sync stops if it changed in the meantime), the support files, figures and the
    baseline files are uploaded (changed ones only), and M is pushed to the server, which merges it into `web`.
Line endings of the laptop files are kept (CRLF); the server stores LF.
"""
import datetime, difflib, hashlib, io, json, os, re, subprocess, sys, tarfile, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prconfig import CFG  # noqa: E402

REPO = CFG['repo_path']
TREE = os.environ.get('PAPERREVIEW_TREE', REPO)      # where the .tex / figures are read and written (tests: a copy)
BASE_TAG = CFG['baseline_tag']
FILES = [d['file'] for d in CFG['docs']]
SUPPORT = CFG['support']
INCLUDE = re.compile(r'\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}')
PLINK = CFG['ssh']
PORT = int(os.environ.get('PAPERREVIEW_PORT', CFG['port']))
DATA = os.environ.get('PAPERREVIEW_DATA', CFG['server_root'] + '/data')


def api(method, path, body=None):
    cmd = f"curl -s -S -m 120 -w '\\n%{{http_code}}' -X {method} -H 'Content-Type: application/json' " + \
          (f"--data-binary @- " if body is not None else '') + f"http://127.0.0.1:{PORT}{path}"
    r = subprocess.run(PLINK + [cmd], input=json.dumps(body).encode('utf8') if body is not None else None,
                       capture_output=True)
    out = r.stdout.decode('utf8', errors='replace')
    text, _, code = out.rpartition('\n')
    if not code.strip().isdigit():
        raise SystemExit(f'server not reachable ({r.stderr.decode(errors="replace").strip() or out[:200]})')
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        data = {'error': text[:300]}
    return int(code), data


def git(*a):
    return subprocess.run(['git', '-C', REPO, *a], capture_output=True, check=True).stdout


def merge3(L, B, W):
    """(merged text, number of conflicts) of laptop L and web W against base B. Line-level first (a paragraph is one
    source line); if that conflicts, a word-level merge (edits to different words of one paragraph combine; edits to
    the same or adjacent words still conflict). The returned conflict text has line-level markers."""
    m, n = merge_lines(L, B, W)
    if n == 0:
        return m, 0
    tok = lambda s: re.findall(r'\s+|[^\s]+', s)
    enc = lambda s: ''.join(json.dumps(t) + '\n' for t in tok(s))
    wm, wn = merge_lines(enc(L), enc(B), enc(W))
    if wn == 0:
        return ''.join(json.loads(t) for t in wm.split('\n') if t), 0
    return m, n


def merge_lines(L, B, W):
    if W == B or L == W:
        return L, 0
    if L == B:
        return W, 0
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, t in (('laptop', L), ('base', B), ('web', W)):
            p = os.path.join(d, name)
            open(p, 'wb').write(t.encode('utf8'))
            paths.append(p)
        r = subprocess.run(['git', 'merge-file', '-p', '-L', 'laptop', '-L', 'base (last sync)', '-L', 'web', *paths],
                           capture_output=True)
        if r.returncode < 0 or r.returncode > 127:
            raise SystemExit('git merge-file failed: ' + r.stderr.decode(errors='replace'))
        return r.stdout.decode('utf8'), r.returncode


def summary(a, b, label):
    """Short listing of the changed lines a -> b."""
    out = []
    al, bl = a.split('\n'), b.split('\n')
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=al, b=bl, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        out.append(f'    {label} line {j1 + 1}: ' + words('\n'.join(al[i1:i2]), '\n'.join(bl[j1:j2])))
    return out


def words(a, b, ctx=4, limit=240):
    """Word-level change of a -> b with a little context: '… kept [-old-]{+new+} kept …'."""
    A, B = a.split(), b.split()
    parts = []
    ops = difflib.SequenceMatcher(a=A, b=B, autojunk=False).get_opcodes()
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == 'equal':
            w = A[i1:i2]
            if len(w) > 2 * ctx:
                w = (w[:ctx] if k else []) + ['…'] + (w[-ctx:] if k < len(ops) - 1 else [])
            parts.append(' '.join(w))
        else:
            if i2 > i1:
                parts.append('[-' + ' '.join(A[i1:i2]) + '-]')
            if j2 > j1:
                parts.append('{+' + ' '.join(B[j1:j2]) + '+}')
    s = ' '.join(p for p in parts if p)
    return s if len(s) <= limit else s[:limit] + ' …'


def assets_wanted(merged):
    """{asset path on the server: bytes} for the support files, the figures and the baseline."""
    want = {}
    for f in SUPPORT:
        want[f] = open(os.path.join(TREE, f), 'rb').read()
    for t in merged.values():
        for g in set(INCLUDE.findall(t)):
            p = os.path.join(TREE, g.strip())
            if os.path.isfile(p):
                want[g.strip()] = open(p, 'rb').read()
            else:
                print('  missing figure (not uploaded):', g)
    if not BASE_TAG:
        return want
    want['baseline/commit'] = git('rev-parse', '--short', BASE_TAG).strip() + b'\n'
    for fn in FILES:
        old = git('show', f'{BASE_TAG}:{fn}')
        want['baseline/' + fn] = old.replace(b'\r\n', b'\n')
        for g in set(INCLUDE.findall(old.decode('utf8'))):
            try:
                want['baseline/' + g.strip()] = git('show', f'{BASE_TAG}:{g.strip()}')
            except subprocess.CalledProcessError:
                pass
    return want


def upload(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for name, data in files.items():
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
    a = f'{DATA}/assets'
    r = subprocess.run(PLINK + [f'mkdir -p {a} && tar --no-same-owner -xf - -C {a} && chown -R www-data:www-data {a} && echo ok'],
                       input=buf.getvalue(), capture_output=True)
    if b'ok' not in r.stdout:
        raise SystemExit('asset upload failed: ' + r.stderr.decode(errors='replace'))


def check(st):
    """Compare the laptop files with the web text: 0 = equal, 1 = they differ (prints what differs)."""
    diff = 0
    for fn in FILES:
        L = open(os.path.join(TREE, fn), 'rb').read().decode('utf8').replace('\r\n', '\n')
        srv = st['files'].get(fn) or {}
        W, B = srv.get('web'), srv.get('base')
        if W is None:
            print(f'{fn}: not on the server yet'); diff = 1; continue
        if L == W:
            print(f'{fn}: laptop == web'); continue
        diff = 1
        web = summary(B, W, 'web') if B is not None else []
        lap = summary(B, L, 'laptop') if B is not None else summary(W, L, 'laptop')
        print(f'{fn}: DIFFERS: {len(web)} web edit(s) not on the laptop, {len(lap)} laptop change(s) not on the web')
        for line in (web + lap)[:30]:
            print(line)
    if diff:
        print('Run `python .review/edit_sync.py` to merge them.')
    return diff


def main():
    dry = '--dry-run' in sys.argv or '--status' in sys.argv
    code, st = api('GET', '/api/sync/state')
    if code != 200:
        raise SystemExit(f'sync state: HTTP {code} {st}')
    if '--check' in sys.argv:
        sys.exit(check(st))
    raw, L, merged, conflicts, report = {}, {}, {}, {}, []
    for fn in FILES:
        raw[fn] = open(os.path.join(TREE, fn), 'rb').read()
        L[fn] = raw[fn].decode('utf8').replace('\r\n', '\n')
        srv = st['files'].get(fn) or {}
        W, B = srv.get('web'), srv.get('base')
        if st['web'] is None:
            merged[fn] = L[fn]; report.append(f'{fn}: first upload to the web editor')
            continue
        if B is None:
            raise SystemExit(f'{fn}: the server has web text but no laptop base; refusing to merge blindly')
        m, n = merge3(L[fn], B, W)
        merged[fn] = m
        if n:
            conflicts[fn] = (n, m)
            continue
        web_changes = summary(B, W, 'web')
        lap_changes = summary(B, L[fn], 'laptop')
        report.append(f'{fn}: {len(web_changes)} web edit(s) to bring in, {len(lap_changes)} laptop change(s) to send')
        report += web_changes[:40]
    print('\n'.join(report))
    if conflicts:
        os.makedirs(os.path.join(HERE, 'conflicts'), exist_ok=True)
        for fn, (n, m) in conflicts.items():
            p = os.path.join(HERE, 'conflicts', fn)
            open(p, 'wb').write(m.replace('\n', '\r\n').encode('utf8'))
            print(f'CONFLICT {fn}: {n} conflicting region(s), the same text was changed on the laptop and on the web.')
            print(f'  merge with markers: {p}')
        print('Nothing was written or pushed. Resolve (edit the laptop file, or undo the web edit in the History tab) and run again.')
        sys.exit(2)
    if dry:
        print('(dry run: nothing written)')
        return
    # assets (changed files only)
    want = assets_wanted(merged)
    code, manifest = api('GET', '/api/sync/manifest')
    changed = {k: v for k, v in want.items() if manifest.get(k) != hashlib.sha1(v).hexdigest()}
    if changed:
        print(f'uploading {len(changed)} asset(s), {sum(map(len, changed.values())) / 2**20:.1f} MB: ' +
              ', '.join(sorted(changed)[:8]) + (' …' if len(changed) > 8 else ''))
        upload(changed)
    # laptop files: back up, re-check, write
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    for fn in FILES:
        if merged[fn] == L[fn]:
            continue
        p = os.path.join(TREE, fn)
        if open(p, 'rb').read() != raw[fn]:
            raise SystemExit(f'{fn} changed on the laptop during the sync; nothing written for it. Run the sync again.')
        os.makedirs(os.path.join(HERE, 'sync-backups'), exist_ok=True)
        open(os.path.join(HERE, 'sync-backups', f'{stamp}_{fn}'), 'wb').write(raw[fn])
        crlf = b'\r\n' in raw[fn]
        open(p, 'wb').write((merged[fn].replace('\n', '\r\n') if crlf else merged[fn]).encode('utf8'))
        print(f'wrote {fn} (backup: .review/sync-backups/{stamp}_{fn})')
    code, r = api('POST', '/api/sync/push', {'base': st['web'], 'files': merged, 'msg': f'laptop sync {stamp}'})
    if code == 409:
        print('server: ' + r.get('error', 'conflict') + ' (the laptop files already contain the merged web edits; run the sync again)')
        sys.exit(3)
    if code != 200:
        raise SystemExit(f'push: HTTP {code} {r}')
    if changed and not r.get('changed'):
        api('POST', '/api/sync/assets', {})
    print(f"synced: web {r['web'][:8]}" + ('' if r.get('changed') else ' (text unchanged)') +
          ('; figures/support updated' if changed else ''))


if __name__ == '__main__':
    main()
