#!/usr/bin/env sh
# RouteLLM launcher (macOS / Linux).
#
#   ./start.sh                  # http://127.0.0.1:8000
#   ./start.sh --dev            # backend reload + Vite dev server
#   ./start.sh --port 8123      # different port
#   ./start.sh --help           # all options
#
# Everything real happens in run.py (stdlib only); this wrapper exists so the
# obvious filename works and so a missing Python is reported in one line.

set -eu

cd "$(dirname "$0")"

for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" run.py "$@"
    fi
done

echo "Python 3.11+ was not found on PATH." >&2
echo "  macOS: brew install python@3.12" >&2
echo "  Linux: sudo apt install python3 python3-venv" >&2
echo "Then run ./start.sh again." >&2
exit 1
