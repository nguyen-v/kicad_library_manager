#!/usr/bin/env bash
# One-shot Linux host setup for KiCad Library Manager (Debian/Ubuntu).
# Installs IPC Python packages, SVG preview tools, and SQLite ODBC for KiCad DBL.
# Safe to re-run. Requires root (sudo/pkexec).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

as_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

if ! command -v apt-get >/dev/null 2>&1; then
  echo "Error: this script supports Debian/Ubuntu (apt-get)." >&2
  echo "Install equivalents for:" >&2
  echo "  python3-pip python3-venv python3-wxgtk4.0 librsvg2-bin unixodbc libsqliteodbc" >&2
  exit 1
fi

echo "Installing system packages..."
as_root apt-get update -y
as_root apt-get install -y \
  python3-pip \
  python3-venv \
  python3-wxgtk4.0 \
  librsvg2-bin \
  unixodbc \
  libsqliteodbc

echo
echo "Registering SQLite3 ODBC Driver..."
# Re-use the dedicated ODBC registrar (idempotent; packages already installed).
bash "${SCRIPT_DIR}/setup_odbc_linux.sh"

echo
echo "OK: Linux host dependencies are installed."
echo "  - python3-pip / python3-venv / python3-wxgtk4.0  (KiCad IPC plugin env)"
echo "  - librsvg2-bin                                   (symbol/footprint previews)"
echo "  - unixodbc + libsqliteodbc                       (KiCad database libraries)"
echo
echo "If the plugin toolbar button is still missing, run:"
echo "  ${SCRIPT_DIR}/repair_plugin_env.sh"
echo "Then restart the PCB editor."
