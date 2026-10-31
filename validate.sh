#!/bin/bash
set -e

# mise
eval "$(mise activate bash)"
mise fmt
mise install

# Python
uv sync
mise run licenses python
uv audit
ruff check --fix
ruff format
ty check --error-on-warning
# Coverage is for the unit tests. The tests that compare what definitions do
# over generated programs run apart from it, as it would slow them down for
# lines the unit tests reach already.
if [[ -n "$CI" ]]; then
  uv run pytest --cov --cov-report=term --cov-report=xml --ignore=tests/test_differential.py
else
  uv run pytest --cov --cov-report=term --ignore=tests/test_differential.py
fi
uv run pytest tests/test_differential.py
trap 'rm -rf dist' EXIT
rm -rf dist
uv build
# --token is a placeholder to skip the interactive prompt; --dry-run never uploads.
uv publish --dry-run --trusted-publishing never --token dry-run

# Shared lint tasks
mise run gha-lint
mise run shell-lint

# Check for uncommitted changes
git diff --exit-code
