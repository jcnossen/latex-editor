"""Before / after view of every figure: match the figure environments of the baseline and the current .tex by label,
render each figure file to a PNG (PDF via pdftoppm, bitmaps via PIL) and write figures.json with both captions.
Old figure files come from old/figures/ (the baseline commit), new ones from figures/ (the working tree).
Usage: python3 figures.py   (in the staging directory; writes figs/*.png and figures.json)
"""
import hashlib, json, os, re, subprocess
from mark import plain  # the same de-LaTeXing as the change list
from prconfig import DOCS as _DOCS

DOCS = [(k, f'old_{k}.tex', f) for k, f in _DOCS.items()]
os.makedirs('figs', exist_ok=True)


def figures(tex):
    out = []
    for m in re.finditer(r'\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}', tex, re.S):
        body = m.group(1)
        lab = re.search(r'\\label\{([^}]*)\}', body)
        g = re.search(r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}', body)
        c = body.find('\\caption{')
        cap = ''
        if c >= 0:  # balanced braces
            i, depth = c + len('\\caption{'), 1
            j = i
            while j < len(body) and depth:
                depth += {'{': 1, '}': -1}.get(body[j], 0) if body[j - 1] != '\\' else 0
                j += 1
            cap = body[i:j - 1]
        out.append({'label': lab.group(1) if lab else None, 'file': g.group(1) if g else None, 'caption': cap})
    return out


def render(path, tag):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.md5(open(path, 'rb').read()).hexdigest()[:10]
    name = f'figs/{tag}_{h}.png'
    if not os.path.exists(name):
        if path.lower().endswith('.pdf'):
            subprocess.run(['pdftoppm', '-png', '-r', '130', '-singlefile', '-scale-to-x', '1400', '-scale-to-y', '-1',
                            path, name[:-4]], check=True)
        else:
            from PIL import Image
            im = Image.open(path).convert('RGB')
            if im.width > 1400:
                im = im.resize((1400, round(im.height * 1400 / im.width)), Image.LANCZOS)
            im.save(name, optimize=True)
    return name, h


res = []
for key, old_fn, new_fn in DOCS:
    old = {f['label'] or f['file']: f for f in figures(open(old_fn, encoding='utf8').read())}
    new_list = figures(open(new_fn, encoding='utf8').read())
    seen = set()
    for n, f in enumerate(new_list, 1):
        k = f['label'] or f['file']
        o = old.get(k); seen.add(k)
        ro = render(os.path.join('old', o['file']) if o and o['file'] else None, 'old')
        rn = render(f['file'], 'new')
        res.append({'doc': key, 'num': n, 'label': f['label'], 'new_file': f['file'], 'old_file': o and o['file'],
                    'old_png': ro and ro[0], 'new_png': rn and rn[0],
                    'image_changed': (ro and ro[1]) != (rn and rn[1]),
                    'old_caption': plain(o['caption'], True) if o else None, 'new_caption': plain(f['caption'], True)})
    for k, o in old.items():
        if k not in seen:
            ro = render(os.path.join('old', o['file']) if o['file'] else None, 'old')
            res.append({'doc': key, 'num': None, 'label': o['label'], 'new_file': None, 'old_file': o['file'],
                        'old_png': ro and ro[0], 'new_png': None, 'image_changed': True,
                        'old_caption': plain(o['caption'], True), 'new_caption': None})
json.dump(res, open('figures.json', 'w'), indent=0)
print(f'figures: {len(res)} ({sum(r["image_changed"] for r in res)} with a changed image)')
