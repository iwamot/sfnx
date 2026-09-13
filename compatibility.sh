#!/bin/bash
set -euo pipefail

eval "$(mise activate bash)"
mise install aqua:astral-sh/uv

uv build --wheel --out-dir dist
uv run --isolated --no-project --with ./dist/*.whl sfnx --version
uv run --isolated --no-project --with ./dist/*.whl sfnx compile examples/orders.py -o dist/fulfill.asl.json
# The compiler reads Python's own syntax tree, which changes between versions.
uv run --isolated --group dev pytest -q -p no:cacheprovider
