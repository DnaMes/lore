#!/usr/bin/env bash
# Rebuild the precompiled Tailwind stylesheet for the web UI (issue #129).
#
# The Play-CDN JIT runtime (tailwind-3.4.16.min.js) is gone; the UI ships a
# build-time compiled, minified sheet instead. Run this after template
# changes that introduce new Tailwind classes, then commit the output:
#
#     scripts/build_tailwind.sh
#
# Requires node/npx (build time only — the running app stays Python-only
# and air-gapped: the compiled CSS is checked into lore/interfaces/static/).
set -euo pipefail
cd "$(dirname "$0")/.."

npx --yes tailwindcss@3.4.16 \
  -c tailwind.config.js \
  -i tailwind.input.css \
  -o lore/interfaces/static/tailwind-compiled.min.css \
  --minify

echo "wrote lore/interfaces/static/tailwind-compiled.min.css"
