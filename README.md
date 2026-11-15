# latex-editor

A small self-hosted review and editing site for a LaTeX manuscript that lives in a git repository. You edit the
paper on a laptop as usual, and co-authors or you yourself review and edit it in the browser:

- **Edit**: the current text as a PDF that recompiles about a second after each save. Click any paragraph in the PDF
  to open its LaTeX source next to it (synctex), change it, press Ctrl+Enter. Saved edits are versioned (git) and can
  be undone one by one.
- **Tracked**: latexdiff of the current text against a baseline git tag, with change bars and a list of changes,
  rebuilt on the server in the background after every change.
- **Changes**: every changed source paragraph with Accept / Reject / Edit (Reject and Edit change the text).
- **Figures**: each figure before / after, with a word diff of its caption. **Comments** on selected text.
- **Sync** (`edit_sync.py`): a 3-way merge between the laptop working tree and the web text, both ways. It refuses
  on conflicts instead of overwriting anything.

The server side is Python 3 standard library plus TeX Live; the page uses pdf.js from a CDN. No accounts: the site is
served under an unguessable path (a personal link).

## How it is fast

- The preamble (up to the last `\usepackage`) is precompiled into a format with `mylatexformat` and rebuilt only when
  it changes.
- Figures are compressed once per file content (ghostscript `/ebook`; PNG/JPEG converted to PDF), so a pass embeds a
  few MB instead of tens. On a 4-core VPS a 14-page paper with 10 MB of figures compiles in about 0.8 s per pass; the
  browser shows the new PDF about 2 s after Ctrl+Enter.
- A second pass / bibtex runs only when labels or citations changed, after the first-pass PDF is already shown.
- latexdiff + pdfLaTeX for the tracked view run in a persistent directory, so a rebuild after a small edit takes
  seconds (the first one about a minute).

## Layout

| path | runs on | what |
|---|---|---|
| `config.json` | both | your project: documents, baseline tag, support files, ssh command, server paths (copy `config.example.json`; not in git) |
| `prconfig.py` | both | loads `config.json` |
| `vps/app.py` | server | HTTP app (static site, editor API, decisions / comments in SQLite) |
| `vps/editor.py` | server | git working copy, fast compile, synctex, laptop sync, background latexdiff |
| `vps/diffdata.py` | server | the latexdiff rebuild |
| `remote/mark.py`, `remote/figures.py`, `remote/run.sh` | server | latexdiff change markers, figure before / after, the laptop-driven build |
| `template.html` | browser | the single-page app |
| `edit_sync.py` | laptop | sync laptop <-> web (3-way merge) |
| `build_review.py` | laptop | sync + server rebuild; or a full build from the laptop files; `--page` uploads the page |
| `deploy.py` | laptop | copies the server code + `config.json` to the server and restarts the service |
| `hunks.py` | both | source-level change paragraphs (the Changes tab) |

## Setup

Requirements on the server: Debian/Ubuntu with `texlive-full` (or at least pdfLaTeX, latexdiff, latexmk, bibtex,
synctex, the `mylatexformat` package), `ghostscript`, `mupdf-tools` (mutool), `poppler-utils` (pdftoppm), `git`,
Python 3.9+ with Pillow, nginx. On the laptop: Python 3.9+, git, and an ssh command that runs a shell command on the
server (OpenSSH `ssh host`, or PuTTY `plink -batch -load session`).

1. Put this repo somewhere next to (or inside, git-ignored) your manuscript repo and write `config.json`
   (see `config.example.json`; `repo` is the manuscript repo relative to the config file).
2. On the server, as root:
   ```
   mkdir -p /srv/paperreview/{app,data,site,build} && chown www-data:www-data /srv/paperreview/data
   python3 -c "import secrets; print('review-' + secrets.token_hex(12))" > /srv/paperreview/token
   ```
   Install `deploy/paperreview.service.example` as `/etc/systemd/system/paperreview.service` and
   `systemctl enable paperreview`. Add the nginx location from `deploy/nginx-site.conf.example` with your token in
   place of `<TOKEN>`, run `nginx -t`, reload nginx.
3. From the laptop: `python deploy.py` (code + config), `python edit_sync.py` (first upload of the text, figures and
   baseline), `python build_review.py --laptop-build` (first full build of the page and site).
4. Open `https://<your host>/<token>/`.

Keep the token out of commits and logs: the path is the only access control.

## Daily workflow

```
python edit_sync.py --dry-run   # what would come in from the web / go out from the laptop
python edit_sync.py             # sync both ways (run before and after each batch of laptop edits)
python edit_sync.py --check     # exit 0 if laptop text == web text, 1 if not
python build_review.py          # sync, then rebuild the tracked view on the server and wait
python build_review.py --page   # after changing template.html
python deploy.py                # after changing the server code or config.json
```

`edit_sync.py` merges per file with base = the last synced laptop text (the server's `laptop` branch), laptop = the
working tree, web = the server's `web` branch: first line by line, then word by word inside a paragraph. Edits to
the same or adjacent words on both sides are a conflict: nothing is written or pushed, the merge with markers goes to
`conflicts/<file>`, exit code 2. Before writing a laptop file it keeps a copy in `sync-backups/` and checks that the
file did not change during the sync. Line endings of the laptop files are kept.

## Visual feedback, voice notes, authorship

- **Point at things:** the page reports what you select in the PDF (text, page, region, matching source lines), an
  unsaved drawing, and the paragraph and caret in the source editor (`POST /api/focus`, sent again whenever the page
  loses focus). An AI client reads it with the MCP tool `get_user_focus`, including an image of the marked region.
- **Draw:** toolbar button or **D** in the Edit view; draw circles or arrows on the PDF, then **Enter** to attach a
  note. Notes stay on the page as numbered marks; `view_comment` shows them to the AI client as an image, with the PDF
  text inside the region and the source lines under it.
- **Voice notes:** the microphone button in the note dialog, or **V**. The recording is uploaded and transcribed on
  the server by `vps/transcribe.py` (faster-whisper on the CPU; `whisper_model` in `config.json`, default `base`,
  about 7 s for an 11-s clip on a 4-core VPS; `whisper_prompt` gives it the paper's vocabulary). The transcript goes
  into the note text for you to check; the recording is kept for playback. Install: `pip install faster-whisper
  pillow` in the MCP venv.
- **Who changed what:** set "Your name" in the Edit sidebar; edits are committed with that author (`AI (MCP)` for the
  MCP endpoint, `laptop` for syncs). `GET /api/blame/<doc>` gives the last author per source line; hovering over a
  change in the Tracked view (or a card in the Changes tab, or the open paragraph) shows who changed it and when.

## AI tools (MCP)

`vps/mcp_server.py` publishes the manuscript to AI clients over MCP (streamable HTTP) at `https://<host>/<token>/mcp`.
It goes through the app's own API, so an AI edit is handled exactly like a browser edit: one undoable commit
(`mcp: ...` in the history), a recompile, conflict checks, and later the 3-way merge into the laptop copy.

Tools: `list_documents`, `read_source`, `search_source`, `replace_text` (exact phrase; preferred for small edits),
`edit_lines`, `compile_status` (LaTeX errors with line numbers), `get_changes` (word diff against the baseline, the
last laptop sync, or any version), `list_comments` / `add_comment` / `resolve_comment`, `history` / `show_edit` /
`undo_edit`, `page_of_lines`, `render_page` (a PDF page as an image).

Connect:
- Claude Code: `claude mcp add --transport http paper https://<host>/<token>/mcp`
- Claude Desktop / claude.ai: Settings > Connectors > Add custom connector, with the same URL.

Setup on the server: create the venv and the unit from `deploy/paperreview-mcp.service.example` (MCP Python SDK 2.x),
add the `/mcp` location from `deploy/nginx-site.conf.example`, then `python deploy.py` (which also restarts the
`-mcp` unit). Port: `mcp_port` in `config.json` (default 8803). Whoever has the URL can edit, as with the web page.

## Server data

Everything the app writes is under `<server_root>/data`: `repo/` (git: branch `web` = browser edits, one commit per
save; `laptop` = last synced laptop text), `assets/` (support files, figures, baseline), `figcache/`, `compile/`,
`live/` (the last good PDF per document), `diff/` (tracked-changes build), `review.db` (decisions, comments).
Run git there as the service user, or with `git -c safe.directory=...`.
