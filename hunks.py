"""Source-level change hunks between the baseline (git tag) and the working tree, and applying review decisions.

hunks(old, new, file): paragraph-level diff of two LaTeX sources (paragraphs = blocks separated by blank lines).
Each hunk: {id, file, line, old, new, before}; id = sha1(file, old, new)[:12], so a decision stays attached to a hunk
until either side of it changes.

apply(decisions, docs): decisions = {id: {status: 'accept'|'reject'|'edit', text?}}; for 'reject' the hunk's new text
in the working-tree file is replaced by its old text, for 'edit' by the edited text; 'accept' changes nothing.
Note: on the live editor site Reject / Edit change the web text directly (they reach the laptop via edit_sync.py);
`apply` is only needed for decisions made before that, and skips hunks that no longer exist.
Usage:  python .review/hunks.py pull                              (decisions + comments from the VPS app)
        python .review/hunks.py apply .review/decisions.json      (apply them to the working tree)
"""
import difflib, hashlib, json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:                        # the laptop tools; on the server only hunks() is used
    from prconfig import CFG
    REPO, BASE = CFG['repo_path'], CFG['baseline_tag']
    FILES = [(d['key'], d['file'], d.get('title', d['key'])) for d in CFG['docs']]
except SystemExit:
    CFG, REPO, BASE, FILES = None, None, None, []


def paragraphs(s):
    """[(line_number, text)] for every non-empty source line: in this manuscript every paragraph is one source line
    (equations span a few lines; a changed run of lines with a different count becomes one hunk)."""
    return [(i + 1, line) for i, line in enumerate(s.split('\n')) if line.strip()]


def hid(file, old, new):
    return hashlib.sha1(f'{file}\0{old}\0{new}'.encode('utf8')).hexdigest()[:12]


def hunks(old, new, file):
    A, B = paragraphs(old), paragraphs(new)
    OL, NL = old.split('\n'), new.split('\n')

    def span(P, L, a, b):   # the exact source text of units a..b-1, blank lines between them included
        return '\n'.join(L[P[a][0] - 1:P[b - 1][0]]) if b > a else ''
    sm = difflib.SequenceMatcher(a=[t for _, t in A], b=[t for _, t in B], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        # a replaced run of k paragraphs by k paragraphs: one card per paragraph pair
        pairs = [((i, i + 1), (j, j + 1)) for i, j in zip(range(i1, i2), range(j1, j2))] if (tag == 'replace' and i2 - i1 == j2 - j1) \
            else [((i1, i2), (j1, j2))]
        for (a1, a2), (b1, b2) in pairs:
            o = span(A, OL, a1, a2); n = span(B, NL, b1, b2)
            line = B[b1][0] if b1 < len(B) else (B[-1][0] if B else 1)
            before = B[b1 - 1][1][-160:] if b1 > 0 else ''
            out.append({'id': hid(file, o, n), 'file': file, 'line': line, 'old': o, 'new': n, 'before': before})
    return out


def git_show(path):
    return subprocess.run(['git', '-C', REPO, 'show', f'{BASE}:{path}'], capture_output=True, check=True).stdout.decode('utf8')


def all_hunks():
    res = []
    for key, fn, title in FILES:
        old = git_show(fn).replace('\r\n', '\n')
        new = open(os.path.join(REPO, fn), 'rb').read().decode('utf8').replace('\r\n', '\n')
        for h in hunks(old, new, fn):
            h['doc'] = key; h['title'] = title
            res.append(h)
    return res


def apply(decisions):
    """Apply reject / edit decisions to the working tree; returns a report."""
    by_file = {}
    for h in all_hunks():
        d = decisions.get(h['id'])
        if d and d.get('status') in ('reject', 'edit'):
            by_file.setdefault(h['file'], []).append((h, d))
    report = []
    for fn, items in by_file.items():
        p = os.path.join(REPO, fn)
        raw = open(p, 'rb').read().decode('utf8'); crlf = '\r\n' in raw
        s = raw.replace('\r\n', '\n')
        for h, d in items:
            repl = h['old'] if d['status'] == 'reject' else d.get('text', h['new']).replace('\r\n', '\n')
            if not h['new']:            # a pure deletion being rejected: re-insert after the preceding paragraph
                anchor = h['before']
                if anchor and s.count(anchor) == 1:
                    s = s.replace(anchor, anchor + '\n\n' + repl, 1); report.append(f"{h['id']} {d['status']}: re-inserted"); continue
                report.append(f"{h['id']} {d['status']}: SKIPPED (no unique anchor)"); continue
            if s.count(h['new']) != 1:
                report.append(f"{h['id']} {d['status']}: SKIPPED (new text not unique / not found)"); continue
            if not repl:                # rejecting a pure insertion: drop the paragraph and one blank line
                s = s.replace('\n\n' + h['new'], '', 1) if ('\n\n' + h['new']) in s else s.replace(h['new'], '', 1)
            else:
                s = s.replace(h['new'], repl, 1)
            report.append(f"{h['id']} {d['status']}: applied ({fn}:{h['line']})")
        open(p, 'wb').write((s.replace('\n', '\r\n') if crlf else s).encode('utf8'))
    return report


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'pull':
        # decisions + comments from the review app on the VPS -> .review/decisions.json, .review/comments.json
        for what in ('decisions', 'comments'):
            out = subprocess.run(CFG['ssh'] + [f"curl -s http://127.0.0.1:{CFG['port']}/api/{what}"],
                                 capture_output=True, check=True).stdout
            json.loads(out)
            open(os.path.join(REPO, '.review', f'{what}.json'), 'wb').write(out)
            print(what, len(json.loads(out)))
    elif len(sys.argv) >= 3 and sys.argv[1] == 'apply':
        rows = json.load(open(sys.argv[2], encoding='utf8'))
        # accept {id: {...}} or an ArtifactData listing [{id, data}] / [{doc_id, data}]
        if isinstance(rows, list):
            rows = {r.get('id') or r.get('doc_id'): r.get('data', r) for r in rows}
        print('\n'.join(apply(rows)))
    else:
        hs = all_hunks()
        print(len(hs), 'hunks:', ', '.join(f"{h['doc']}:{h['line']}" for h in hs))
