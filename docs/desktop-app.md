# Idea: a desktop app instead of (or next to) the website

Status: idea, to revisit. The web app keeps running meanwhile. Noted 2026-11-15.

## Why

The website works, but for daily writing a local app would be quicker and less awkward:

- no network round trip (today about 1.5 of the ~2.3 s from Ctrl+Enter to new PDF is upload, download and painting);
- no sync step between the laptop files and a server copy: the app edits the working tree directly;
- discussing a selection with Claude fits naturally in a desktop side panel.

## What it should do

1. **Edit through the PDF view.** Click a paragraph in the rendered PDF to edit its LaTeX (synctex), save, and see
   the page update in about a second. Same paragraph editor as the web Edit tab: highlight in the PDF, caret on the
   clicked word, previous/next paragraph, history and undo.
   **Overleaf-style two-way sync** (now in the web app, keep it): the caret in the source shows as a blinking cursor
   in the PDF, a selection in the source is highlighted in the PDF, and a selection in the PDF is marked in the
   source. In the web app this works at character level inside the open paragraph by aligning the normalized text of
   both sides (`srcIndex` / `pdfIndex` / `locate` in `template.html`); synctex only finds the paragraph.
2. **Select and discuss with Claude.** Select text in the PDF (or source), open a chat about it. Claude gets the
   selection, the surrounding source paragraph(s) and the diff of that region. Its proposed edits show up as a diff
   to accept or reject.
3. **Git diffs as a first-class view.** Show the working tree against HEAD, against a chosen commit or tag (e.g. the
   submitted version), or against the last co-author round. Inline change marking on the PDF (latexdiff, as in the
   web Tracked view) plus a source-level diff list with accept / reject per change.
4. **Fast local LaTeX.** Reuse what made the server fast: a precompiled preamble (mylatexformat), figures compressed
   once per file hash, one pdfLaTeX pass per save, bibtex / a second pass only when citations or labels change.

## Proposed stack: C# (.NET 8+), in this repository

C# rather than Rust because it is faster to build and iterate on here (.NET is already installed on the laptop).

- **UI shell:** WPF or WinUI 3 with **WebView2** hosting the existing pdf.js viewer from `template.html` (page
  rendering, text layer, selection, highlights). That reuses the current front end instead of writing a PDF renderer.
  Avalonia is the option if it ever needs to run on macOS / Linux.
- **LaTeX:** local TeX Live or MiKTeX (pdflatex is NOT installed on the laptop yet), run as a background process per
  document. Watch the .tex files so edits made in other editors (or by Claude Code) also trigger a recompile.
- **synctex:** call the `synctex` CLI, or parse `.synctex.gz` directly in C# (simple line-based format).
- **Git:** LibGit2Sharp, or the `git` CLI, for diffs, history and per-change revert. latexdiff (Perl, ships with TeX
  Live) for the marked-up PDF.
- **Claude:** a chat panel using the Claude API, with the selection plus source context; proposed edits applied as a
  patch to the working tree after review.
- **Co-authors:** the website stays for people without the app: the app could push to the server with the existing
  `edit_sync.py` protocol (`/api/sync/state`, `/api/sync/push`), so web edits and app edits still merge 3-way.

## Code from the web app to reuse

- `template.html`: PDF view, click-to-edit, highlight boxes, history dialog (move into a WebView2-hosted page).
- `vps/editor.py`: the fast compile loop (format caching, figure cache, pass / bibtex logic, log parsing) and the
  synctex helpers: port to C#.
- `edit_sync.py`: the 3-way merge (line level, then word level).
- `remote/mark.py`, `vps/diffdata.py`: latexdiff + change markers for the tracked view.

## Open questions

- Install TeX Live locally (full is ~8 GB; a scheme-medium plus the paper's packages is enough), or keep compiling
  on the VPS and only make the viewer local?
- One window per document, or main text and SI side by side?
- Where Claude's conversation history lives (per selection, per paragraph, per document).
