"""Server-side latexdiff rebuild of the review data from the web working copy (the laptop's build_review.py does the
same from the laptop files). Baseline: assets/baseline/<file>.tex and assets/baseline/figures/ (uploaded by
edit_sync.py from the git tag). Compiles with the compressed figures (fast); the figure before / after PNGs are made
from the original files. Output (swapped in atomically): diff/out/{main,SI}.pdf, figs/*.png, review.json.
"""
import concurrent.futures, datetime, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from editor import CFG, DOCS, TITLES, SUPPORT, INCLUDE, small_figure, place   # noqa: E402
from hunks import hunks                                                 # noqa: E402

BASE = CFG['baseline_tag']


def flatten_first_revision(s):
    """Accept the first revision's markup: unwrap \\textcolor{blue}{..}, drop \\textcolor{red}{\\sout{..}}."""
    def arg_end(s, i):
        d = 0
        for j in range(i, len(s)):
            d += {'{': 1, '}': -1}.get(s[j], 0)
            if d == 0:
                return j + 1
        return len(s)
    for pat, keep in (('\\textcolor{red}{\\sout{', False), ('\\textcolor{blue}{', True)):
        while True:
            i = s.find(pat)
            if i < 0:
                break
            k = i + len('\\textcolor{red}' if not keep else '\\textcolor{blue}')
            e = arg_end(s, k)
            s = s[:i] + (s[k + 1:e - 1] if keep else '') + s[e:]
    return s


def build(P, texts, rev):
    work, figwork = os.path.join(P.diff, 'work'), os.path.join(P.diff, 'figwork')
    for d in (work, figwork, os.path.join(figwork, 'figs')):
        os.makedirs(d, exist_ok=True)
    base_dir = os.path.join(P.assets, 'baseline')
    for f in SUPPORT:
        if os.path.exists(os.path.join(P.assets, f)):
            place(os.path.join(P.assets, f), os.path.join(work, f))
    for key, fn in DOCS.items():
        old_raw = open(os.path.join(base_dir, fn), encoding='utf8').read().replace('\r\n', '\n')
        old, new = old_raw, texts[fn]
        if CFG['flatten_markup']:
            old, new = flatten_first_revision(old), flatten_first_revision(new)
        for d in (work, figwork):
            open(os.path.join(d, f'old_{key}.tex'), 'w', encoding='utf8').write(old)
            open(os.path.join(d, fn), 'w', encoding='utf8').write(new)
        for g in set(INCLUDE.findall(new)):
            src = os.path.join(P.assets, g.strip())
            if os.path.exists(src) and '..' not in g:
                place(small_figure(P, src), os.path.join(work, g.strip()))
                place(src, os.path.join(figwork, g.strip()))
        for g in set(INCLUDE.findall(old)):
            src = os.path.join(base_dir, g.strip())
            if os.path.exists(src) and '..' not in g:
                place(src, os.path.join(figwork, 'old', g.strip()))
                # latexdiff --graphics-markup=new-only keeps only new figures in the PDF; an old-only figure still
                # needs a file under its name for the compile
                if not os.path.exists(os.path.join(work, g.strip())):
                    place(small_figure(P, src), os.path.join(work, g.strip()))
    env = P.env()

    def one(key):
        fn = DOCS[key]
        with open(os.path.join(work, f'{key}_diff.tex'), 'w', encoding='utf8') as out:
            subprocess.run(['latexdiff', '--type=UNDERLINE', '--graphics-markup=new-only', '--encoding=utf8',
                            f'old_{key}.tex', fn], cwd=work, stdout=out, stderr=subprocess.DEVNULL, env=env, timeout=300, check=True)
        subprocess.run([sys.executable, os.path.join(HERE, 'mark.py'), f'{key}_diff.tex'], cwd=work, env=env,
                       capture_output=True, timeout=60, check=True)
        subprocess.run(['latexmk', '-pdf', '-interaction=nonstopmode', '-f', f'{key}_diff.tex'], cwd=work, env=env,
                       capture_output=True, timeout=600)
        if not os.path.exists(os.path.join(work, f'{key}_diff.pdf')):
            raise RuntimeError(f'{key}: latexdiff PDF not produced (see {work}/{key}_diff.log)')

    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        list(ex.map(one, DOCS))
    subprocess.run([sys.executable, os.path.join(HERE, 'figures.py')], cwd=figwork, env=env, capture_output=True,
                   timeout=900, check=True)
    out_new = os.path.join(P.diff, 'out.new')
    shutil.rmtree(out_new, ignore_errors=True)
    os.makedirs(os.path.join(out_new, 'figs'))
    docs, report = [], []
    for key, fn in DOCS.items():
        shutil.copyfile(os.path.join(work, f'{key}_diff.pdf'), os.path.join(out_new, f'{key}.pdf'))
        marks = {}
        for line in open(os.path.join(work, f'{key}_diff.difmarks')).read().split('\n'):
            p = line.split()
            if len(p) == 5 and p[0] not in marks:
                try:
                    marks[p[0]] = (int(p[2]), int(p[3]) / 65536 * 72 / 72.27, int(p[4]) / 65536 * 72 / 72.27)
                except ValueError:
                    pass
        changes = json.load(open(os.path.join(work, f'{key}_diff.changes.json')))
        out = []
        for c in changes:
            m = marks.get(str(c['id']))
            if m and c['text']:
                out.append({'id': c['id'], 'type': c['type'], 'text': c['text'], 'after': c.get('after', ''),
                            'page': m[0], 'y': round(m[2], 1)})
        docs.append({'key': key, 'title': TITLES[key], 'file': f'diff/{key}.pdf', 'changes': out})
        report.append(f'{key}: {len(out)} changes')
    figures = json.load(open(os.path.join(figwork, 'figures.json'), encoding='utf8'))
    for f in figures:
        for k in ('old_png', 'new_png'):
            if f.get(k):
                shutil.copyfile(os.path.join(figwork, f[k]), os.path.join(out_new, f[k]))
                f[k] = 'diff/' + f[k]
    hs = []
    for key, fn in DOCS.items():
        old_raw = open(os.path.join(base_dir, fn), encoding='utf8').read().replace('\r\n', '\n')
        for h in hunks(old_raw, texts[fn], fn):
            h['doc'] = key; h['title'] = TITLES[key]
            hs.append(h)
    base_commit = ''
    if os.path.exists(os.path.join(base_dir, 'commit')):
        base_commit = open(os.path.join(base_dir, 'commit')).read().strip()
    meta = {'built': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), 'base': BASE, 'base_commit': base_commit,
            'head': rev[:7], 'dirty': [], 'source': 'web'}
    json.dump({'meta': meta, 'docs': docs, 'figures': figures, 'hunks': hs},
              open(os.path.join(out_new, 'review.json'), 'w', encoding='utf8'), ensure_ascii=False)
    out = os.path.join(P.diff, 'out')
    old = os.path.join(P.diff, 'out.old')
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(out):
        os.rename(out, old)
    os.rename(out_new, out)
    shutil.rmtree(old, ignore_errors=True)
    return ', '.join(report) + f', {len(hs)} hunks, {len(figures)} figures'
