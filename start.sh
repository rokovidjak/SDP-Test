#!/usr/bin/env bash
# Entry point required by the test runbook: `bash start.sh`.
# Delegates to run.sh, which creates .venv, installs dependencies,
# and starts the dashboard server.
cd "$(dirname "$0")"
exec bash run.sh "$@"
