#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs the controller until the scenario finishes. No manual steps.
# Restarting with the same command resumes from state/controller.db.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="$(command -v python3 || command -v python)"
"$PY" -c "import httpx" 2>/dev/null || "$PY" -m pip install -q httpx
exec "$PY" -m controller --url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}"
