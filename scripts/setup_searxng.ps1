$ErrorActionPreference = "Stop"

# Shared installer owns the pinned source, dependencies and completion checks.
& python (Join-Path $PSScriptRoot "searxng_install.py") setup
exit $LASTEXITCODE
