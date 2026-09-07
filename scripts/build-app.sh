#!/usr/bin/env bash
# Build AgentFeed.app (run on macOS). Output: dist/AgentFeed.app
set -euo pipefail
cd "$(dirname "$0")/.."
uv pip install -q -e '.[desktop]'
rm -rf build dist
.venv/bin/pyinstaller --noconfirm --clean packaging/agentfeed.spec
echo
echo "Built: dist/AgentFeed.app"
echo "Open:  open dist/AgentFeed.app"
echo "Ship:  ditto -c -k --keepParent dist/AgentFeed.app dist/AgentFeed-macos.zip"
