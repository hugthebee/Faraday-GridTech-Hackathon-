#!/bin/bash
# One command: model + website on http://127.0.0.1:8770  (builds the site first if needed)
cd "$(dirname "$0")"
export PATH="$PWD/.tools/node-v22.23.4-darwin-arm64/bin:$PATH"
[ -d dist ] || { [ -d node_modules ] || npm install --ignore-scripts --no-audit --no-fund; npx vite build; }
exec ../.venv/bin/python ../team_app/faraday_server.py "$@"
