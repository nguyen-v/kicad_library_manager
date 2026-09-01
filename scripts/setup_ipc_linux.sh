#!/usr/bin/env bash
# Install Linux system packages required for KiCad 9 IPC Python plugins.
# Run once per machine (PCM or manual install). Safe to re-run.
set -euo pipefail

as_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

if ! command -v apt-get >/dev/null 2>&1; then
  echo "Error: this script supports Debian/Ubuntu (apt-get)." >&2
  echo "Install equivalents for: python3-pip python3-venv python3-wxgtk4.0" >&2
  exit 1
fi

as_root apt-get update -y
as_root apt-get install -y python3-pip python3-venv python3-wxgtk4.0

echo
echo "OK: IPC Python prerequisites installed."
echo "Next: enable Preferences → Plugins → Enable KiCad API, then restart the PCB editor."
echo "The Library Manager button appears after KiCad finishes creating the plugin venv"
echo "(usually within ~30–60s on first launch)."
