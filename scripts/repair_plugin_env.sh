#!/usr/bin/env bash
# Recreate this plugin's KiCad IPC Python environment and install requirements.txt.
# Use when the toolbar button never appears (empty/broken venv, missing pip, etc.).
set -euo pipefail

PLUGIN_ID="com.github.nguyen-v.kicad-library-manager"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQ="${ROOT}/requirements.txt"

CACHE_ROOT="${XDG_CACHE_HOME:-${HOME}/.cache}"
ENV_DIR="${CACHE_ROOT}/kicad/9.0/python-environments/${PLUGIN_ID}"

if [[ ! -f "${REQ}" ]]; then
  echo "Error: missing ${REQ}" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found on PATH" >&2
  exit 1
fi

# Prefer distro packages that make venv+pip+wx+previews+ODBC work on Debian/Ubuntu.
if command -v apt-get >/dev/null 2>&1; then
  echo "Ensuring Linux host packages (IPC + librsvg + ODBC) ..."
  "${SCRIPT_DIR}/setup_linux.sh"
fi

echo "Removing old env (if any): ${ENV_DIR}"
rm -rf "${ENV_DIR}"

echo "Creating venv with --system-site-packages ..."
python3 -m venv --system-site-packages "${ENV_DIR}"

PY="${ENV_DIR}/bin/python"
if ! "${PY}" -m pip --version >/dev/null 2>&1; then
  echo "Bootstrapping pip into the venv ..."
  "${PY}" -m ensurepip --upgrade || true
fi
if ! "${PY}" -m pip --version >/dev/null 2>&1; then
  echo "Error: venv has no pip. Install python3-pip and re-run." >&2
  exit 1
fi

"${PY}" -m pip install -U pip
"${PY}" -m pip install -r "${REQ}"

echo
"${PY}" -c "import kipy, rapidfuzz, wx; print('OK: kipy + rapidfuzz + wx', wx.version())"
echo
echo "Restart the PCB editor. The plugin button should appear under Tools → External Plugins."
