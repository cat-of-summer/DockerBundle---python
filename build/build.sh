#!/usr/bin/env bash
# Build the dockerbundle binary into dist/.
#
#   SKIP_TESTS=true   build without running the test suite
#   PYTHON=python3.12 use a specific interpreter
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

python="${PYTHON:-}"
if [ -z "$python" ]; then
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1; then
            python="$candidate"
            break
        fi
    done
fi
[ -n "$python" ] || { echo "Python not found. Install Python 3.10+ and retry." >&2; exit 1; }

echo "Python: $("$python" --version)"

scratch="$root/build/__pycache__"
export PYTHONPYCACHEPREFIX="$scratch"

# Editable so PyInstaller and pytest both work against this source tree rather than an
# installed copy. Dependencies come from pyproject.toml, the single place they live.
"$python" -m pip install --upgrade --quiet -e ".[dev]"

if [ "${SKIP_TESTS:-false}" != "true" ]; then
    # Docker-backed tests are excluded: a release runner has no daemon.
    "$python" -m pytest -m "not docker" -q
fi

"$python" -m PyInstaller --clean --noconfirm \
    --distpath dist --workpath "$scratch" build/dockerbundle.spec

echo
echo "Artifacts in $root/dist:"
ls -la dist/
