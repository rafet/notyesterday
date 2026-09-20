#!/usr/bin/env bash
# Locate a python3 and run notyesterday.py with the hook payload on stdin.
# No interpreter, no output: the session continues as if nothing happened.
mode="$1"
shift || true
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$root/notyesterday.py" "$mode" "$@"
elif command -v py >/dev/null 2>&1; then
  exec py -3 "$root/notyesterday.py" "$mode" "$@"
fi
cat >/dev/null
exit 0
