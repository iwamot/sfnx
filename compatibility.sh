#!/bin/bash
set -euo pipefail

eval "$(mise activate bash)"
mise install aqua:astral-sh/uv

uv build --wheel --out-dir dist
uv run --isolated --no-project --with ./dist/*.whl sfnx --version
uv run --isolated --no-project --with ./dist/*.whl sfnx compile examples/orders.py -o dist/fulfill.asl.json
# sfnx.testing comes with sfnx, and the testing extra, kept for projects that
# name it, still installs.
wheel=$(echo dist/*.whl)
uv run --isolated --no-project --with "$wheel" python -c "import sfnx.testing"
uv run --isolated --no-project --with "${wheel}[testing]" python -c "import sfnx.testing"
# The compiler reads Python's own syntax tree, which changes between versions.
uv run --isolated --group dev pytest -q -p no:cacheprovider
