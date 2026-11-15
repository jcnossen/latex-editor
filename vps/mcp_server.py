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
caused. When the user refers to "this", "here", "what I selected / circled / drew", call get_user_focus first: it
shows their current selection, unsaved drawing and editor caret (with an image). list_comments shows notes and voice
notes (transcribed); view_comment shows a note's drawing on the page (resolve_comment once addressed). get_changes shows the
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


def note_text(c):
    """One comment / note as text: where, kind, source lines, quote, text and voice transcript."""
    kind = 'drawing' if c.get('strokes') else 'selection' if c.get('rect') else (c.get('kind') or 'text')
    head = (f"#{c['id']}{' (resolved)' if c.get('resolved') else ''} [{c.get('where') or ''}] {kind}"
            + (f", {c['doc']} page {c['page']}" if c.get('doc') and c.get('page') else '')
            + (f", source lines {c['src_from']}-{c['src_to']}" if c.get('src_from') else '')
            + (f", by {c['author']}" if c.get('author') else ''))
    body = []
    if c.get('quote'):
        body.append(f"  quoted: \"{c['quote'][:500]}\"")
    if c.get('text'):
        body.append('  text: ' + c['text'])
    if c.get('audio_id'):
        tr = c.get('transcript')
        if tr and tr[:40] not in (c.get('text') or ''):
            body.append('  voice note (transcribed): ' + tr)
        elif not tr:
            body.append(f"  voice note: {c.get('audio_state') or 'pending'} (no transcript yet)")
    return '\n'.join([head] + body)


def page_png(doc, page, width):
    pdf = os.path.join(DATA, 'live', DOCS[doc][:-4] + '.pdf')
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 'p')
        subprocess.run(['pdftoppm', '-png', '-singlefile', '-f', str(page), '-l', str(page), '-scale-to', str(width), pdf, out],
                       check=True, capture_output=True, timeout=60)
        return open(out + '.png', 'rb').read()


def marked_image(doc, page, rect=None, strokes=None, crop=True, width=1400):
    """The page with a region (dashed box) and / or a drawing (red strokes) drawn on it, cropped around them."""
    import io
    from PIL import Image as PImage, ImageDraw
    im = PImage.open(io.BytesIO(page_png(doc, page, width))).convert('RGB')
    W, H = im.size
    dr = ImageDraw.Draw(im)
    lw = max(3, W // 300)
    for s in strokes or []:
        pts = [(x * W, y * H) for x, y in s]
        if len(pts) > 1:
            dr.line(pts, fill=(214, 51, 108), width=lw, joint='curve')
    if rect and not strokes:
        x0, y0, x1, y1 = rect['x'] * W, rect['y'] * H, (rect['x'] + rect['w']) * W, (rect['y'] + rect['h']) * H
        dr.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline=(214, 51, 108), width=lw)
    if crop and (rect or strokes):
        r = rect or {}
        if strokes:
            xs = [x for s in strokes for x, _ in s]; ys = [y for s in strokes for _, y in s]
            r = {'x': min(xs), 'y': min(ys), 'w': max(xs) - min(xs), 'h': max(ys) - min(ys)}
        top = max(0, (r['y'] - 0.12) * H); bot = min(H, (r['y'] + r['h'] + 0.12) * H)
        im = im.crop((0, int(top), W, int(bot)))      # full width: the text around it stays readable
    buf = io.BytesIO(); im.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def region_text(doc, page, rect=None, strokes=None, margin=0.01):
    """The PDF text inside a region of a page (pdftotext on a rectangle in points), or '' if none."""
    if strokes and not rect:
        xs = [x for s in strokes for x, _ in s]; ys = [y for s in strokes for _, y in s]
        rect = {'x': min(xs), 'y': min(ys), 'w': max(xs) - min(xs), 'h': max(ys) - min(ys)}
    if not rect:
        return ''
    pdf = os.path.join(DATA, 'live', DOCS[doc][:-4] + '.pdf')
    info = subprocess.run(['pdfinfo', '-f', str(page), '-l', str(page), pdf], capture_output=True, timeout=30).stdout.decode()
    m = re.search(r'Page\s+\d+\s+size:\s+([\d.]+)\s+x\s+([\d.]+)', info) or re.search(r'Page size:\s+([\d.]+)\s+x\s+([\d.]+)', info)
    if not m:
        return ''
    W, H = float(m.group(1)), float(m.group(2))
    x, y = max(0, rect['x'] - margin), max(0, rect['y'] - margin)
    w, h = min(1 - x, rect['w'] + 2 * margin), min(1 - y, rect['h'] + 2 * margin)
    r = subprocess.run(['pdftotext', '-f', str(page), '-l', str(page), '-r', '72', '-x', str(int(x * W)), '-y', str(int(y * H)),
                        '-W', str(int(w * W) + 1), '-H', str(int(h * H) + 1), '-layout', pdf, '-'], capture_output=True, timeout=30)
    return re.sub(r'[ \t]+', ' ', r.stdout.decode('utf8', errors='replace')).strip()


async def source_excerpt(doc, a, b, limit=4000):
    _, L = await source(doc)
    a, b = max(1, a), min(len(L), b)
    s = '\n'.join(f'{i}\t{L[i - 1]}' for i in range(a, b + 1))
    return s if len(s) <= limit else s[:limit] + ' …'


@mcp.tool()
async def get_user_focus(include_image: bool = True) -> list:
    """What the user is pointing at in the review page right now: the text selected in the PDF (with its page,
    region and the matching LaTeX source lines), a drawing they made but did not save yet, the paragraph open in the
    source editor (with the caret position and any selected source), and the page in view. Call this when the user
    says "this", "here", "what I selected", "what I circled" and similar. With include_image, the page region is
    returned as an image with the selection / drawing marked."""
    st, f = await call('GET', '/api/focus')
    if not f:
        return ['The review page has not reported anything yet (is it open in a browser?).']
    out = [f"(reported {f.get('age', '?')} s ago; view: {f.get('view')}"
           + (f"; page in view: {f['doc']} page {f['page']}" if f.get('doc') else '') + ')']
    img = None
    sel = f.get('selection') or None
    if not sel and f.get('last_selection'):
        sel = f['last_selection']
        out.append('No text is selected right now; the last selection was:')
    if sel:
        out.append(f"Selected in the PDF ({sel.get('doc')} page {sel.get('page')}, {sel.get('view')} view): \"{sel.get('text', '')[:1500]}\"")
        src = sel.get('source')
        if src:
            out.append(f"Matching source, lines {src.get('from_line')}-{src.get('to_line')}: \"{src.get('text', '')[:1500]}\"")
        if include_image and sel.get('view') == 'edit' and sel.get('rect') and sel.get('doc') in DOCS:
            img = (sel['doc'], sel['page'], sel['rect'], None)
    dw = f.get('drawing')
    if dw and dw.get('doc') in DOCS:
        out.append(f"Unsaved drawing on {dw['doc']} page {dw['page']} ({len(dw.get('strokes') or [])} stroke(s)) around "
                   f"x {dw['rect']['x']:.2f}-{dw['rect']['x'] + dw['rect']['w']:.2f}, y {dw['rect']['y']:.2f}-{dw['rect']['y'] + dw['rect']['h']:.2f} of the page.")
        t = await anyio.to_thread.run_sync(lambda: region_text(dw['doc'], dw['page'], dw.get('rect'), None))
        if t:
            out.append('PDF text in the drawn region: ' + t[:2000])
        if include_image:
            img = (dw['doc'], dw['page'], dw['rect'], dw.get('strokes'))
    ed = f.get('editor')
    if ed and ed.get('doc') in DOCS:
        out.append(f"Source editor open on {ed['doc']} lines {ed['start_line']}-{ed['end_line']}, caret at line {ed.get('caret_line')}"
                   + (' (unsaved changes in the editor!)' if ed.get('unsaved_changes') else '') + f": …{ed.get('caret_context', '')}…")
        if ed.get('selected_source'):
            out.append(f"Selected in the source editor: \"{ed['selected_source'][:1500]}\"")
    res = ['\n'.join(out)]
    if img:
        try:
            res.append(Image(data=await anyio.to_thread.run_sync(lambda: marked_image(img[0], img[1], img[2], img[3])), format='png'))
        except Exception as ex:
            res[0] += f'\n(image unavailable: {ex})'
    return res


@mcp.tool()
async def list_comments(include_resolved: bool = False) -> str:
    """Reviewer comments and notes on the paper: id, where (document / page), kind (text, selection, drawing, voice),
    the source lines it points at, the quoted text, the comment text and the transcript of a voice note. Use
    view_comment to see a drawing or selection on the page."""
    st, cs = await call('GET', '/api/comments')
    out = [note_text(c) for c in cs if include_resolved or not c.get('resolved')]
    return '\n'.join(out) or 'no open comments'


@mcp.tool()
async def view_comment(comment_id: int) -> list:
    """One note in full: its text and voice transcript, the LaTeX source lines it points at, and an image of the page
    with the user's drawing or selected region marked."""
    st, c = await call('GET', f'/api/comments/{comment_id}')
    if st != 200:
        raise ToolError(c.get('error', f'HTTP {st}'))
    parts = [note_text(c)]
    if c.get('doc') in DOCS and c.get('page') and (c.get('rect') or c.get('strokes')):
        t = await anyio.to_thread.run_sync(lambda: region_text(c['doc'], c['page'], c.get('rect'), c.get('strokes')))
        if t:
            parts.append('PDF text in the marked region (search_source finds it in the source):\n' + t[:3000])
    if c.get('src_from') and c.get('doc') in DOCS:
        ex = await source_excerpt(c['doc'], c['src_from'], c['src_to'])
        if re.sub(r'^\d+\t', '', ex, flags=re.M).strip():
            parts.append('Source lines under it (synctex):\n' + ex)
    res = ['\n\n'.join(parts)]
    if c.get('doc') in DOCS and c.get('page') and (c.get('rect') or c.get('strokes')):
        try:
            res.append(Image(data=await anyio.to_thread.run_sync(
                lambda: marked_image(c['doc'], c['page'], c.get('rect'), c.get('strokes'))), format='png'))
        except Exception as ex:
            res[0] += f'\n(image unavailable: {ex})'
    return res


@mcp.tool()
async def add_comment(text: str, where: str = '', quote: str = '') -> str:
    """Add a comment for the authors (e.g. a question or a suggestion you did not apply). where: a location such as
    'Main text, line 120'; quote: the text it is about."""
    st, r = await call('POST', '/api/comments', {'text': text, 'where': where, 'quote': quote, 'author': 'AI (MCP)'})
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
