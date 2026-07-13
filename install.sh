#!/usr/bin/env bash
# Bootstrap entrypoint for the local-search stack.
#
# Delegates to scripts/local-search install. Designed to be invoked by
# consumers (pi-shared, server/home-automation) or run directly after cloning
# this repo on a fresh machine.
#
# Usage:
#   ./install.sh                 # bootstrap + start + verify
#   ./install.sh --no-start      # bootstrap only (no service start)
#   LOCAL_SEARCH_LOG_DIR=... ./install.sh
#   LOCAL_SEARCH_DATA_DIR=... ./install.sh
#
# Prerequisites: macOS, Homebrew uv, git, python3, curl.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/scripts/local-search" install "$@"
