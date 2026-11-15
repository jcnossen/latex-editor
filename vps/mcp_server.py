"""MCP endpoint for the review server: lets an AI client (Claude Code, Claude Desktop / claude.ai connectors, others)
read, search and edit the manuscript, check the compile result, see the changes, handle comments and look at pages.

It is a thin layer over the review app's HTTP API on 127.0.0.1 (vps/app.py), so every edit goes through the same
path as an edit in the browser: one git commit on the `web` branch (undoable in the History list), a recompile in about
a second, and later the 3-way merge into the laptop copy (edit_sync.py).

Runs with the official MCP Python SDK (v2) over streamable HTTP, stateless, on 127.0.0.1:<mcp_port> (config.json,
default 8803); nginx publishes it under the secret path as https://<host>/<token>/mcp (see deploy/).
    /srv/paperreview/mcpvenv/bin/python mcp_server.py
"""
import difflib, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request

import anyio
import uvicorn
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prconfig import CFG, DOCS, TITLES  # noqa: E402
from hunks import hunks  # noqa: E402

API = f"http://127.0.0.1:{CFG['port']}"
DATA = CFG['server_root'] + '/data'
WHO = 'mcp'

INSTRUCTIONS = f"""Tools for the LaTeX manuscript "{CFG['title']}" on its review server. Documents: """ + \
    ', '.join(f'{k} = {TITLES[k]} ({f})' for k, f in DOCS.items()) + """.
The text is a git working copy: every edit is one undoable commit, the PDF recompiles in about a second, and the
authors merge the edits into their own copy later. A paragraph is usually one long source line.
Typical flow: search_source or read_source to find the text; replace_text for an exact phrase (preferred for small
edits) or edit_lines for whole lines; then read the compile result returned by the edit and fix any LaTeX error you
caused. list_comments shows what reviewers asked for (resolve_comment once addressed). get_changes shows the
revision so far; render_page shows a typeset page.
Keep edits minimal and in the authors' wording and style; never reformat or rewrap text you were not asked to change."""

mcp = MCPServer('latex-review', instructions=INSTRUCTIONS)


# ------------------------------------------------------------------------------------------------------- app access
def _call(method, path, body=None, timeout=40):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except Exception:
            return e.code, {'error': str(e)}


async def call(method, path, body=None, timeout=40):
    return await anyio.to_thread.run_sync(lambda: _call(method, path, body, timeout))


def _doc(doc):
    if doc in DOCS:
        return doc
    for k, f in DOCS.items():       # also accept the file name or the title
        if doc in (f, TITLES[k]) or doc.lower() == k.lower():
            return k
    raise ToolError(f'unknown document {doc!r}; use one of: ' + ', '.join(DOCS))


async def source(doc):
    st, r = await call('GET', f'/api/src/{doc}')
    if st != 200:
        raise ToolError(r.get('error', f'HTTP {st}'))
    return r['rev'], r['text'].split('\n')


def words(a, b, ctx=6, limit=600):
    """Word-level change a -> b with some context: '… kept [-old-]{+new+} kept …'."""
    A, B = a.split(), b.split()
    ops = difflib.SequenceMatcher(a=A, b=B, autojunk=False).get_opcodes()
    parts = []
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


def fmt_build(s):
    st = s.get('state')
    if st == 'idle' and s.get('seq'):       # after an app restart: the last compile's result
        st = 'error' if s.get('log_tail') else 'ok'
    if st == 'running' and not s.get('message'):
        return 'compile: running (a new compile is in progress; ask again in a few seconds)'
    out = [f"compile: {st}" + (f" ({s.get('message')})" if s.get('message') else '') +
           (f", last pass {s['pass_times'][-1]} s" if s.get('pass_times') else '')]
    for e in s.get('errors') or []:
        out.append(f"  LaTeX error" + (f" at line {e['line']}" if e.get('line') else '') + f": {e['msg']}" +
                   (f" | {e['context']}" if e.get('context') else ''))
    if s.get('undefined'):
        out.append('  undefined references: ' + ', '.join(s['undefined']))
    if s.get('missing'):
        out.append('  missing figure files: ' + ', '.join(s['missing']))
    if st == 'error':
        out.append('  The PDF was NOT updated (the last good version is still shown). Fix the error with another edit.')
    return '\n'.join(out)


async def wait_build(doc, ver, t0, timeout=45):
    """The build status after an edit: wait for the compile that started after t0."""
    end = time.time() + timeout
    s = {}
    while time.time() < end:
        st, s = await call('GET', f'/api/build/{doc}?since={ver}&timeout=20', timeout=30)
        if st != 200:
            return {'state': 'unknown', 'message': s.get('error', f'HTTP {st}')}
        ver = s.get('ver', ver)
        if s.get('state') in ('ok', 'error') and (s.get('finished') or 0) >= t0:
            return s
    return {**s, 'state': s.get('state', 'unknown'), 'message': 'still compiling after 45 s'}


async def apply_edit(doc, start, old, new):
    t0 = time.time()
    st, r = await call('POST', f'/api/edit/{doc}', {'rev': '', 'start': start, 'old': old, 'new': new, 'who': WHO})
    if st == 409:
        return None, 'Conflict: that text changed in the meantime (someone else edited it). Read it again and retry.'
    if st != 200:
        return None, f"Edit refused: {r.get('error', 'HTTP %d' % st)}"
    if not r.get('changed'):
        return r, 'No change (the new text equals the old text).'
    b = await wait_build(doc, r.get('ver', -1), t0 - 0.5)
    return r, fmt_build(b)


# ------------------------------------------------------------------------------------------------------------ tools
@mcp.tool()
async def list_documents() -> str:
    """List the manuscript documents with their size and compile state. Use the `key` as `doc` in the other tools."""
    st, e = await call('GET', '/api/editor')
    out = []
    for k, f in DOCS.items():
        try:
            rev, lines = await source(k)
        except Exception as ex:
            out.append(f'{k}: {TITLES[k]} ({f}): unavailable ({ex})'); continue
        b = (e.get('docs') or {}).get(k, {})
        out.append(f"{k}: {TITLES[k]} ({f}), {len(lines)} lines; " + fmt_build(b).splitlines()[0]
                   + (f", {len(b.get('errors') or [])} LaTeX errors" if b.get('errors') else ''))
    return '\n'.join(out) + f"\ncurrent version: {(e.get('rev') or '')[:10]}"


@mcp.tool()
async def read_source(doc: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read LaTeX source lines (1-based, inclusive), prefixed with their line numbers. end_line 0 = up to 200 lines
    from start_line. Lines are paragraphs, so they can be long."""
    doc = _doc(doc)
    rev, L = await source(doc)
    a = max(1, start_line)
    b = min(len(L), end_line if end_line > 0 else a + 199)
    out, size = [], 0
    for i in range(a, b + 1):
        line = f'{i}\t{L[i - 1]}'
        size += len(line)
        if size > 120_000:
            out.append(f'… (stopped at line {i - 1}: output limit; continue with start_line={i})'); break
        out.append(line)
    return f'{TITLES[doc]}, lines {a}-{min(b, i)} of {len(L)}:\n' + '\n'.join(out)


@mcp.tool()
async def search_source(query: str, doc: str = '', regex: bool = False, max_results: int = 30) -> str:
    """Find text in the LaTeX source (case-insensitive). Returns doc, line number and the surrounding text of each
    match. Set regex=true for a Python regular expression."""
    docs = [_doc(doc)] if doc else list(DOCS)
    pat = re.compile(query if regex else re.escape(query), re.I)
    out = []
    for d in docs:
        _, L = await source(d)
        for i, line in enumerate(L, 1):
            for m in pat.finditer(line):
                a, b = max(0, m.start() - 100), min(len(line), m.end() + 100)
                out.append(f'{d}:{i}: …{line[a:m.start()]}>>{m.group(0)}<<{line[m.end():b]}…')
                if len(out) >= max_results:
                    return '\n'.join(out) + f'\n(stopped at {max_results} matches)'
    return '\n'.join(out) or 'no matches'


@mcp.tool()
async def replace_text(doc: str, find: str, replace: str, occurrence: int = 0) -> str:
    """Replace an exact phrase in the source (within one line/paragraph) and recompile. `find` must match the source
    exactly, including LaTeX markup; it must occur once in the document, or give `occurrence` (1-based, in document
    order) when it occurs several times. Returns the word-level change and the compile result."""
    doc = _doc(doc)
    if not find:
        raise ToolError('find is empty')
    if '\n' in find:
        raise ToolError('find spans several lines: use edit_lines for that')
    _, L = await source(doc)
    hits = [(i, m.start()) for i, line in enumerate(L) for m in re.finditer(re.escape(find), line)]
    if not hits:
        return 'Not found. Check the exact source text with search_source (LaTeX markup, ~, \\ commands).'
    if len(hits) > 1 and not occurrence:
        where = ', '.join(f'line {i + 1}' for i, _ in hits[:10])
        return f'Found {len(hits)} times ({where}); pass occurrence=N to choose one.'
    if occurrence and not 1 <= occurrence <= len(hits):
        return f'occurrence must be 1..{len(hits)}'
    i, pos = hits[(occurrence or 1) - 1]
    old = L[i]
    new = old[:pos] + replace + old[pos + len(find):]
    r, msg = await apply_edit(doc, i + 1, old, new)
    if r is None:
        return msg
    return f"{TITLES[doc]} line {r.get('start', i + 1)}: {words(old, new)}\n{msg}"


@mcp.tool()
async def edit_lines(doc: str, start_line: int, end_line: int, new_text: str, expected_old: str = '') -> str:
    """Replace source lines start_line..end_line (1-based, inclusive) by new_text (may contain newlines; an empty
    string deletes the lines) and recompile. Pass expected_old (the current text of those lines, as read) to make
    sure nothing changed since you read it. Returns the change and the compile result."""
    doc = _doc(doc)
    _, L = await source(doc)
    if not (1 <= start_line <= end_line <= len(L)):
        raise ToolError(f'line range must be within 1..{len(L)}')
    old = '\n'.join(L[start_line - 1:end_line])
    if expected_old and expected_old.replace('\r\n', '\n') != old:
        return 'Conflict: those lines differ from expected_old (changed since you read them). Read them again.'
    if not old.strip():
        return 'Those lines are empty; edit a non-empty range that includes the neighbouring text instead.'
    r, msg = await apply_edit(doc, start_line, old, new_text.replace('\r\n', '\n'))
    if r is None:
        return msg
    return f"{TITLES[doc]} lines {start_line}-{end_line}: {words(old, new_text)}\n{msg}"


@mcp.tool()
async def compile_status(doc: str = '') -> str:
    """The latest compile of each document (or one): state, LaTeX errors with line numbers, undefined references,
    missing figures."""
    st, e = await call('GET', '/api/editor')
    out = []
    for k in ([_doc(doc)] if doc else DOCS):
        b = (e.get('docs') or {}).get(k, {})
        when = time.strftime('%H:%M:%S', time.localtime(b['finished'])) if b.get('finished') else '?'
        out.append(f'{TITLES[k]} (compiled {when}): ' + fmt_build(b))
    d = e.get('diff') or {}
    out.append(f"tracked-changes rebuild: {d.get('state', '?')}" + (f" ({d.get('message')})" if d.get('message') else ''))
    return '\n'.join(out)


def _git(*a):
    r = subprocess.run(['git', '-C', DATA + '/repo', *a], capture_output=True)
    if r.returncode:
        raise ToolError(r.stderr.decode(errors='replace').strip())
    return r.stdout.decode('utf8', errors='replace')


@mcp.tool()
async def get_changes(doc: str, against: str = 'baseline', max_changes: int = 60) -> str:
    """The changes in a document as a list of changed paragraphs with a word-level diff ([-removed-]{+added+}).
    against: 'baseline' (the reviewed-against version, i.e. the whole revision), 'last_sync' (what was edited on the
    server since the authors last synced their copy), or a version id from history."""
    doc = _doc(doc)
    fn = DOCS[doc]
    _, L = await source(doc)
    new = '\n'.join(L)
    if against == 'baseline':
        p = os.path.join(DATA, 'assets', 'baseline', fn)
        old = open(p, encoding='utf8').read().replace('\r\n', '\n')
    elif against == 'last_sync':
        old = await anyio.to_thread.run_sync(lambda: _git('show', f'laptop:{fn}'))
    elif re.fullmatch(r'[0-9a-f]{7,40}', against):
        old = await anyio.to_thread.run_sync(lambda: _git('show', f'{against}:{fn}'))
    else:
        raise ToolError("against must be 'baseline', 'last_sync' or a version id")
    hs = hunks(old, new, fn)
    if not hs:
        return 'no changes'
    out = [f'{len(hs)} changed paragraph(s) in {TITLES[doc]} against {against}:']
    for h in hs[:max_changes]:
        kind = 'added' if not h['old'].strip() else 'deleted' if not h['new'].strip() else 'changed'
        out.append(f"- line {h['line']} ({kind}): {words(h['old'], h['new'], limit=400)}")
    if len(hs) > max_changes:
        out.append(f'… {len(hs) - max_changes} more (raise max_changes)')
    return '\n'.join(out)


@mcp.tool()
async def list_comments(include_resolved: bool = False) -> str:
    """Reviewer comments on the paper: id, where (document / page / figure), the quoted text and the comment."""
    st, cs = await call('GET', '/api/comments')
    out = []
    for c in cs:
        if c.get('resolved') and not include_resolved:
            continue
        out.append(f"#{c['id']}{' (resolved)' if c.get('resolved') else ''} [{c.get('where') or ''}] "
                   + (f"on \"{c['quote'][:300]}\": " if c.get('quote') else '') + c['text'])
    return '\n'.join(out) or 'no open comments'


@mcp.tool()
async def add_comment(text: str, where: str = '', quote: str = '') -> str:
    """Add a comment for the authors (e.g. a question or a suggestion you did not apply). where: a location such as
    'Main text, line 120'; quote: the text it is about."""
    st, r = await call('POST', '/api/comments', {'text': f'[{WHO}] {text}', 'where': where, 'quote': quote})
    return f"comment #{r.get('id')} added" if st == 200 else f"failed: {r.get('error')}"


@mcp.tool()
async def resolve_comment(comment_id: int, resolved: bool = True) -> str:
    """Mark a comment as resolved (after addressing it), or reopen it with resolved=false."""
    st, r = await call('PUT', f'/api/comments/{comment_id}', {'resolved': resolved})
    return 'ok' if st == 200 else f"failed: {r.get('error')}"


@mcp.tool()
async def history(n: int = 20) -> str:
    """The latest saved versions (newest first): version id, time and description. Web edits, edits through this
    tool ('mcp: ...') and laptop syncs all appear here."""
    st, h = await call('GET', f'/api/history?n={max(1, min(n, 200))}')
    return '\n'.join(f"{c['rev'][:10]}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['time']))}  {c['msg']}"
                     for c in h) or 'no history'


@mcp.tool()
async def show_edit(version: str) -> str:
    """The word-level diff of one saved version (from history)."""
    st, r = await call('GET', f'/api/history/{version}')
    return r.get('patch', r.get('error', ''))[:60_000]


@mcp.tool()
async def undo_edit(version: str) -> str:
    """Undo one saved edit (a version id from history) by adding a new version that reverts it. Refused when later
    edits changed the same text."""
    st, r = await call('POST', f'/api/revert/{version}', {})
    return f"undone; new version {r.get('rev', '')[:10]}" if st == 200 else f"refused: {r.get('error')}"


@mcp.tool()
async def page_of_lines(doc: str, start_line: int, end_line: int = 0) -> str:
    """On which PDF page(s) the given source lines are typeset (synctex), with their position on the page."""
    doc = _doc(doc)
    st, boxes = await call('GET', f'/api/boxes/{doc}?from={start_line}&to={end_line or start_line}')
    if st != 200 or not boxes:
        return 'not found on any page (not typeset, or the PDF is being rebuilt)'
    return '\n'.join(f"page {b['page']}: {b['y']:.0f}-{b['y'] + b['h']:.0f} pt from the top of the page" for b in boxes)


@mcp.tool()
async def render_page(doc: str, page: int, width: int = 1100) -> Image:
    """A typeset page of the current PDF as a PNG image (to check layout, figures, equations, overfull lines)."""
    doc = _doc(doc)
    pdf = os.path.join(DATA, 'live', DOCS[doc][:-4] + '.pdf')
    width = max(400, min(int(width), 2000))

    def run():
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'p')
            subprocess.run(['pdftoppm', '-png', '-singlefile', '-f', str(page), '-l', str(page), '-scale-to', str(width),
                            pdf, out], check=True, capture_output=True, timeout=60)
            return open(out + '.png', 'rb').read()
    try:
        data = await anyio.to_thread.run_sync(run)
    except subprocess.CalledProcessError:
        raise ToolError(f'page {page} does not exist in {TITLES[doc]}')
    return Image(data=data, format='png')


def main():
    port = int(CFG.get('mcp_port', 8803))
    app = mcp.streamable_http_app(
        streamable_http_path='/mcp', stateless_http=True, json_response=True,
        # nginx forwards with Host 127.0.0.1:<port>; requests from browsers (an Origin header) are refused
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=['127.0.0.1:*', 'localhost:*'],
                                                     allowed_origins=list(CFG.get('mcp_allowed_origins', []))))
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning', proxy_headers=False)


if __name__ == '__main__':
    main()
