"""Post-process a latexdiff output: give every change block a numbered position marker and list the blocks.

Each \\DIFaddbegin / \\DIFdelbegin (and the FL variants used in floats) becomes \\difmark{a|d}{N}, which records the page
and position of the change in <job>.difmarks at shipout (pdfTeX \\pdfsavepos). The text of each block (up to its
\\DIF...end) is written, roughly de-LaTeXed, to <job>.changes.json for the review page's change list.
Usage: python3 mark.py main_diff.tex      (plain() is also used by figures.py)
"""
import json, re, sys

MACROS = r"""
%DIF REVIEW MARKERS (position of every change block, for the review page)
\makeatletter
\newwrite\difout
\AtBeginDocument{\immediate\openout\difout=\jobname.difmarks\immediate\write\difout{H \the\pdfpageheight\space\the\pdfpagewidth}}
\DeclareRobustCommand\difmark[2]{\pdfsavepos\edef\dif@x{\noexpand\write\difout{#2\space#1\space\noexpand\thepage\space\noexpand\the\pdflastxpos\space\noexpand\the\pdflastypos}}\dif@x}
\makeatother
% red additions / grey struck-out deletions (the first revision's own blue / red markup is flattened before the diff)
\definecolor{difadd}{RGB}{204,20,20}\definecolor{difdel}{RGB}{140,140,140}
\renewcommand{\DIFadd}[1]{{\protect\color{difadd}\uwave{#1}}}
\renewcommand{\DIFdel}[1]{{\protect\color{difdel}\sout{#1}}}
"""

SYMBOLS = {'sigma': 'σ', 'mu': 'μ', 'lambda': 'λ', 'theta': 'θ', 'phi': 'φ', 'psi': 'ψ', 'epsilon': 'ε', 'Sigma': 'Σ',
           'geq': '≥', 'leq': '≤', 'times': '×', 'pm': '±', 'approx': '≈', 'sim': '~', 'circ': '°', 'chi': 'χ', 'gamma': 'γ'}


def strip_sout(t):
    """Drop \\sout{...} (text struck out in the first revision) including its argument."""
    while True:
        i = t.find('\\sout{')
        if i < 0:
            return t
        j, depth = i + 6, 1
        while j < len(t) and depth:
            depth += {'{': 1, '}': -1}.get(t[j], 0)
            j += 1
        t = t[:i] + t[j:]


def plain(t, drop_sout=False):
    if drop_sout:
        t = strip_sout(t)
    t = re.sub(r'%DIF[^\n]*', '', t)
    t = re.sub(r'(?<!\\)%[^\n]*', '', t)
    t = re.sub(r'\\DIF(add|del)(FL)?\{', '{', t)
    t = re.sub(r'\\cite\{[^}]*\}', '[ref]', t)
    t = re.sub(r'\\(auto)?ref\{[^}]*\}', '[ref]', t)
    t = re.sub(r'\\label\{[^}]*\}', '', t)
    t = re.sub(r'\\includegraphics(\[[^\]]*\])?\{([^}]*)\}', r'[figure \2]', t)
    t = re.sub(r'\\(begin|end)\{[^}]*\}', ' ', t)
    t = re.sub(r'\\textcolor\{[^}]*\}', '', t)
    t = re.sub(r'\\(textbf|textit|emph|mathrm|text|sout|uline|mbox)\b', '', t)
    t = re.sub(r'\\([A-Za-z]+)', lambda m: SYMBOLS.get(m.group(1), ' '), t)
    t = t.replace('{', '').replace('}', '').replace('~', ' ').replace('$', '').replace('--', '–').replace('\\', '')
    return re.sub(r'\s+', ' ', t).strip()


def main(path):
    s = open(path, encoding='utf8').read()
    anchor = '%DIF END PREAMBLE EXTENSION ADDED BY LATEXDIFF'
    assert anchor in s, 'latexdiff preamble marker not found'
    s = s.replace(anchor, anchor + MACROS, 1)
    # the paper loads ulem itself (without options): drop latexdiff's \RequirePackage[normalem]{ulem} (option clash)
    s = re.sub(r'^\\RequirePackage\[normalem\]\{ulem\}.*$', '%DIF ulem is loaded by the document', s, flags=re.M)
    body = s.index('\\begin{document}')
    begin = re.compile(r'\\DIF(add|del)begin(FL)?(?![A-Za-z])')
    out, changes, pos, n = [], [], 0, 0
    for m in begin.finditer(s, body):
        n += 1
        kind = 'a' if m.group(1) == 'add' else 'd'
        out.append(s[pos:m.start()])
        out.append('\\difmark{%s}{%d}' % (kind, n) + m.group(0))   # keep latexdiff's own begin macro
        pos = m.end()
        end = re.compile(r'\\DIF%send(FL)?(?![A-Za-z])' % m.group(1)).search(s, m.end())
        block = s[m.end(): end.start() if end else m.end() + 400]
        after = s[end.end(): end.end() + 600] if end else ''
        changes.append({'id': n, 'type': kind, 'text': block, 'after': after})
    out.append(s[pos:])
    open(path, 'w', encoding='utf8').write(''.join(out))
    for c in changes:
        c['text'] = plain(c['text'])[:400]
        c['after'] = plain(c['after'])[:120]     # the text that follows: lets the page find short changes ("kept")
    json.dump(changes, open(path.replace('.tex', '.changes.json'), 'w'), indent=0)
    print(f'{path}: {n} change blocks marked')


if __name__ == '__main__':
    main(sys.argv[1])
