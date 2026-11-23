#!/usr/bin/env bash
# Renders docs/manuals/manual.html to docs/manuals/database-ha-lab-manual.pdf with headless Chrome/Chromium.
set -euo pipefail
dir="$(cd "$(dirname "$0")" && pwd)"
for c in "${CHROME:-}" "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" google-chrome chromium chromium-browser; do
  [[ -n "$c" ]] && command -v "$c" >/dev/null 2>&1 && { chrome="$c"; break; }
done
[[ -n "${chrome:-}" ]] || { echo "Chrome/Chromium not found; set CHROME=/path/to/chrome" >&2; exit 1; }
"$chrome" --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$dir/database-ha-lab-manual.pdf" "file://$dir/manual.html" 2>/dev/null
echo "$dir/database-ha-lab-manual.pdf"
