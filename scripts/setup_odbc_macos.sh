#!/usr/bin/env bash
set -euo pipefail

# Registers the SQLite ODBC driver under the name used by our KiCad DBL:
#   "SQLite3 ODBC Driver"
#
# macOS install path is typically managed by Homebrew:
#   brew install sqliteodbc
#
# Reference:
# - Homebrew sqliteodbc: https://formulae.brew.sh/formula/sqliteodbc
# - unixODBC: https://formulae.brew.sh/formula/unixodbc

SECTION_NAME="SQLite3 ODBC Driver"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Error: required command not found: $1" >&2
    exit 1
  }
}

as_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

need_cmd brew

echo "Installing dependencies via Homebrew (sqliteodbc includes unixODBC)..."
brew install sqliteodbc

need_cmd odbcinst

echo
echo "unixODBC config locations:"
odbcinst -j || true

# Locate the driver library installed by Homebrew.
prefix="$(brew --prefix sqliteodbc 2>/dev/null || true)"
if [[ -z "${prefix:-}" ]]; then
  echo "Error: could not determine Homebrew prefix for sqliteodbc." >&2
  exit 1
fi

driver_path=""
for cand in \
  "$prefix/lib/libsqlite3odbc.dylib" \
  "$prefix/lib/libsqlite3odbc.so" \
  "$prefix/lib/libsqliteodbc.dylib" \
  "$prefix/lib/libsqliteodbc.so" \
  "$prefix/lib/"*sqlite3odbc*.dylib \
  "$prefix/lib/"*sqliteodbc*.dylib
do
  if [[ -f "$cand" ]]; then
    driver_path="$cand"
    break
  fi
done

if [[ -z "${driver_path:-}" ]]; then
  echo "Error: could not locate sqlite ODBC driver library under: $prefix/lib" >&2
  echo "Try: brew info sqliteodbc" >&2
  exit 1
fi

setup_path="$driver_path"
echo
echo "Using Driver: $driver_path"
echo "Using Setup : $setup_path"

# Determine odbcinst.ini file location from odbcinst -j output.
ODBCINST_INI="$(odbcinst -j 2>/dev/null | awk -F': ' '/odbcinst.ini/ { print $2; exit }' | tr -d '\r' || true)"
if [[ -z "${ODBCINST_INI:-}" ]]; then
  # Reasonable defaults:
  if [[ -f "/opt/homebrew/etc/odbcinst.ini" ]]; then
    ODBCINST_INI="/opt/homebrew/etc/odbcinst.ini"
  elif [[ -f "/usr/local/etc/odbcinst.ini" ]]; then
    ODBCINST_INI="/usr/local/etc/odbcinst.ini"
  else
    ODBCINST_INI="/opt/homebrew/etc/odbcinst.ini"
  fi
fi

echo
echo "Target odbcinst.ini: $ODBCINST_INI"

if [[ -w "$ODBCINST_INI" ]] || [[ ! -e "$ODBCINST_INI" && -w "$(dirname "$ODBCINST_INI")" ]]; then
  mkdir -p "$(dirname "$ODBCINST_INI")"
  touch "$ODBCINST_INI"
  port_needs_root=false
else
  as_root mkdir -p "$(dirname "$ODBCINST_INI")"
  as_root touch "$ODBCINST_INI"
  port_needs_root=true
fi

tmpfile="$(mktemp)"
trap 'rm -f "$tmpfile"' EXIT

# Remove an existing [SQLite3 ODBC Driver] block (until next [Section] or EOF)
awk -v section="[$SECTION_NAME]" '
  BEGIN { skip=0 }
  $0 == section { skip=1; next }
  skip && $0 ~ /^\[/ { skip=0 }
  !skip { print }
' "$ODBCINST_INI" > "$tmpfile"

cat >> "$tmpfile" <<EOF

[$SECTION_NAME]
Description=SQLite3 ODBC Driver
Driver=$driver_path
Setup=$setup_path
Threading=2
EOF

if [[ "$port_needs_root" == true ]]; then
  as_root cp "$tmpfile" "$ODBCINST_INI"
else
  cp "$tmpfile" "$ODBCINST_INI"
fi

echo
echo "Registered drivers now:"
odbcinst -q -d || true

echo
echo "Done."
echo "KiCad DBL connection string should work if the driver name matches exactly:"
echo "  DRIVER={$SECTION_NAME};DATABASE=\${CWD}/parts.sqlite"

