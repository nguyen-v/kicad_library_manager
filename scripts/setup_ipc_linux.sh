#!/usr/bin/env bash
# Back-compat wrapper: full Linux host setup lives in setup_linux.sh.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/setup_linux.sh" "$@"
