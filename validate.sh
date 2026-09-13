#!/bin/bash
set -e

# mise
eval "$(mise activate bash)"
mise fmt
mise install

# Python
uv sync
uv run pip-licenses --partial-match --allow-only="Apache;BSD;CNRI-Python;ISC;MIT;MPL;PSF;Python Software Foundation"
uv audit
ruff check --fix
ruff format
ty check --error-on-warning
if [[ -n "$CI" ]]; then
  uv run pytest --cov --cov-report=term --cov-report=xml
else
  uv run pytest --cov --cov-report=term
fi
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
