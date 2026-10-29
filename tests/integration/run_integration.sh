#!/bin/bash
# Runs the end-to-end test of the coding-members system (docs/coding-members.md) on THIS machine, on loopback.
#
#   tests/integration/run_integration.sh [pytest arguments]        e.g.  -k sticky   or   -x -q
#
# What it does, in order:
#   1. compiles the VS Code extension (the real TypeScript client is part of the run);
#   2. waits its turn in the CPU lane of the machine's heavy-job lock (~/valheim-testbed/heavy-cpu.lock, which is not the
#      lock the GPU bake-off holds), under a 6 GB memory cap with no swap, and a timeout;
#   3. runs tests/integration with DLNE_INTEGRATION=1: a coordinator, one to three coding members (each with its own
#      llama-server, CPU only, 2 threads) and the compiled client, all bound to 127.0.0.x.
#
# Needs: the pinned test model in ~/.cache/dlne-test (see tests/integration/models.py), a llama.cpp build in ~/.cache,
# node, and a Python with the project's requirements (default ~/.venvs/dlne; DLNE_IT_PYTHON picks another).
# It never touches TheWall, never publishes or deploys anything, and writes only under ~/.cache/dlne-it (DLNE_IT_DIR).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${DLNE_IT_PYTHON:-$HOME/.venvs/dlne/bin/python}"
TIMEOUT="${DLNE_IT_TIMEOUT:-1800}"
HEAVY="${DLNE_HEAVY:-$HOME/valheim-testbed/heavy.sh}"

cd "$REPO"
[ -x "$PY" ] || { echo "run_integration: no Python at $PY (set DLNE_IT_PYTHON)"; exit 2; }
command -v node >/dev/null || { echo "run_integration: node is not installed"; exit 2; }
[ -e vscode-extension/node_modules ] || { echo "run_integration: vscode-extension/node_modules is missing (link it from the main checkout, never commit it)"; exit 2; }

echo "== compiling the extension"
(cd vscode-extension && npm run compile --silent)

echo "== MemAvailable before: $(awk '/^MemAvailable:/{printf "%d MB", $2/1024}' /proc/meminfo)"
export DLNE_INTEGRATION=1
export CUDA_VISIBLE_DEVICES=""
export GGML_VK_VISIBLE_DEVICES=""
export HEAVY_LOCK="${HEAVY_LOCK:-$HOME/valheim-testbed/heavy-cpu.lock}"
export HEAVY_MARGIN_GB="${HEAVY_MARGIN_GB:-4}"

set +e
"$HEAVY" 6G dlne-integration timeout -k 30 "$TIMEOUT" "$PY" -m pytest tests/integration -p no:cacheprovider -v -s "$@"
CODE=$?
set -e
echo "== MemAvailable after: $(awk '/^MemAvailable:/{printf "%d MB", $2/1024}' /proc/meminfo); pytest exit code $CODE"
exit $CODE
