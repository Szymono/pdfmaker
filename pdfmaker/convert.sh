#!/usr/bin/env bash
# Wrapper: runs convert.py inside the local venv.  See README.md.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LLAMA_CPP_BINARY="${LLAMA_CPP_BINARY:-$HERE/llama/llama-b11738/llama-server}"
exec "$HERE/venv/bin/python" "$HERE/convert.py" "$@"
