#!/bin/bash
set -euo pipefail

eval "$(mise activate bash)"
mise install aqua:astral-sh/uv

uv build --wheel --out-dir dist
uv run --isolated --no-project --with ./dist/*.whl sfnx --version
uv run --isolated --no-project --with ./dist/*.whl sfnx compile examples/orders.py -o dist/fulfill.asl.json
# sfnx.testing comes with the testing extra, and without it says to install it.
wheel=$(echo dist/*.whl)
uv run --isolated --no-project --with "${wheel}[testing]" python -c "import sfnx.testing"
if uv run --isolated --no-project --with "$wheel" python -c "import sfnx.testing" 2>dist/import.err; then
  exit 1
fi
grep -q 'install sfnx\[testing\]' dist/import.err
# The compiler reads Python's own syntax tree, which changes between versions.
uv run --isolated --group dev pytest -q -p no:cacheprovider
