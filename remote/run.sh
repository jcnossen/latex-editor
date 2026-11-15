#!/bin/bash
# Runs on the server in the staging directory: latexdiff (baseline -> current), change markers, pdfLaTeX + BibTeX,
# then a smaller PDF for the review page (ghostscript /ebook, text stays selectable).
cd "$(dirname "$0")"
for pair in $(python3 -c "import prconfig; print(' '.join(k + ':' + f for k, f in prconfig.DOCS.items()))"); do
  d=${pair%%:*}; new=${pair#*:}
  # graphics markup new-only (the default): a replaced figure is framed; 'both' also shows the old one and the float
  # then overflows the page
  latexdiff --type=UNDERLINE --graphics-markup=new-only --encoding=utf8 old_${d}.tex "$new" > ${d}_diff.tex 2> ${d}_latexdiff.log
  python3 mark.py ${d}_diff.tex
  latexmk -pdf -interaction=nonstopmode -f ${d}_diff.tex > ${d}_build.log 2>&1
  if [ -f ${d}_diff.pdf ]; then
    gs -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook -dCompatibilityLevel=1.6 -dNOPAUSE -dBATCH -dQUIET \
       -sOutputFile=${d}_review.pdf ${d}_diff.pdf
    echo "$d: $(grep -c . ${d}_diff.difmarks 2>/dev/null) markers, $(stat -c %s ${d}_diff.pdf) -> $(stat -c %s ${d}_review.pdf) bytes, \
$(grep -c '^!' ${d}_diff.log) LaTeX errors"
  else
    echo "$d: NO PDF"; tail -30 ${d}_build.log
  fi
done
python3 figures.py
