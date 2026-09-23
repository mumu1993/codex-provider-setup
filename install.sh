#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$candidate" "$script_dir/provider_setup.py" "$@"
  fi
done
printf '%s\n' 'Python 3.11+ is required. Install it from python.org or your trusted package manager, then retry.' >&2
exit 1
