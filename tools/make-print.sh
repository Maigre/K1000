#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# make-print.sh: render the two pages that go in the gig case to PDF, one A4 page each.
#
#   tools/make-print.sh [OUTDIR]     default OUTDIR: print/ at the repository root (gitignored)
#
#   docs/04-operator-card.md  -> OUTDIR/04-operator-card.pdf   (for the operator)
#   docs/13-quick-start.md    -> OUTDIR/13-quick-start.pdf     (for someone new to the box)
#
# Renderer, the first one found:
#   1. md_to_pdf.sh (pandoc + LuaLaTeX, 9 pt, 1 cm margins), at $K1000_MD_TO_PDF or
#      ~/.local/share/md-to-pdf/md_to_pdf.sh. Run with --no-toc (else it adds a contents page),
#      and given the H1 as --title with the H1 itself left out (else the title prints twice).
#   2. plain pandoc with lualatex or xelatex, the same page settings.
#   3. neither: it says what to install and exits 3.
# Then each PDF is checked with pdfinfo (poppler-utils), when present: one page, A4. A page that
# spills is reported and the exit status is 1: shorten the wording, not the font.
# Runs on the laptop, never on the box (no TeX there).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
PAGES=(docs/04-operator-card.md docs/13-quick-start.md)
MD2PDF=${K1000_MD_TO_PDF:-$HOME/.local/share/md-to-pdf/md_to_pdf.sh}

case "${1:-}" in
  -h|--help) sed -n '3,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  -*) echo "make-print.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
esac
OUT=${1:-$ROOT/print}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd -P)

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# The renderer.
if [ -f "$MD2PDF" ] && command -v pandoc >/dev/null 2>&1; then
  MODE=md2pdf
elif command -v pandoc >/dev/null 2>&1 && command -v lualatex >/dev/null 2>&1; then
  MODE=pandoc ENGINE=lualatex
elif command -v pandoc >/dev/null 2>&1 && command -v xelatex >/dev/null 2>&1; then
  MODE=pandoc ENGINE=xelatex
else
  cat >&2 <<'EOT'
make-print.sh: nothing to render PDFs with.
  Needs pandoc and a Unicode LaTeX engine (lualatex or xelatex), e.g. on Debian/Ubuntu:
    sudo apt-get install pandoc texlive-luatex texlive-latex-extra fonts-dejavu poppler-utils
  (md_to_pdf.sh, if you have it, is used first: K1000_MD_TO_PDF=/path/to/md_to_pdf.sh.)
  Or open docs/04-operator-card.md and docs/13-quick-start.md in any Markdown viewer and print
  each to one A4 page.
EOT
  exit 3
fi

status=0
for page in "${PAGES[@]}"; do
  src=$ROOT/$page
  [ -f "$src" ] || { echo "make-print.sh: $page is missing" >&2; exit 1; }
  name=$(basename "$page" .md)
  pdf=$OUT/$name.pdf
  log=$TMP/$name.log
  title=$(sed -n 's/^# \{1,\}//p' "$src" | head -n 1)
  rm -f "$pdf"
  if [ "$MODE" = md2pdf ]; then
    # The body without its first H1 (md_to_pdf.sh prints --title itself). md_to_pdf.sh typesets
    # in French (babel), which puts a space before every ':' and ';': switched off for English.
    {
      printf '%s\n' '```{=latex}' '\ifdefined\NoAutoSpacing\NoAutoSpacing\fi' '```' ''
      awk '!done && /^# / { done = 1; next } { print }' "$src"
    } > "$TMP/$name.md"
    ok=0
    bash "$MD2PDF" "$TMP/$name.md" "$pdf" --title "$title" --no-toc > "$log" 2>&1 || ok=$?
  else
    ok=0
    (cd "$ROOT/docs" && pandoc "$src" -o "$pdf" --pdf-engine="$ENGINE" \
       -V papersize=a4 -V geometry:margin=1cm -V fontsize=9pt -V documentclass=extarticle \
       -V mainfont="DejaVu Sans" -V monofont="DejaVu Sans Mono" -V pagestyle=empty \
       -V colorlinks=true) > "$log" 2>&1 || ok=$?
  fi
  if [ "$ok" != 0 ] || [ ! -s "$pdf" ]; then
    echo "make-print.sh: $page did not render ($MODE):" >&2
    tail -n 20 "$log" >&2
    exit 1
  fi
  if command -v pdfinfo >/dev/null 2>&1; then
    pages=$(pdfinfo "$pdf" | awk '/^Pages:/ { print $2 }')
    size=$(pdfinfo "$pdf" | sed -n 's/^Page size: *//p')
    case "$size" in *A4*) a4=yes ;; *) a4=no ;; esac
    if [ "$pages" = 1 ] && [ "$a4" = yes ]; then
      echo "$pdf: 1 page, A4"
    else
      echo "$pdf: $pages page(s), $size: NOT one A4 page. Shorten $page (the wording, not the font)" >&2
      status=1
    fi
  else
    echo "$pdf (page count not checked: no pdfinfo, from poppler-utils)"
  fi
done
exit $status
