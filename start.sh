#!/usr/bin/env bash
# Run from any working directory, using this checkout's virtual environment.
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

venv_dir="${OPENHOMEKTV_VENV:-$project_dir/.venv}"
if [[ "$venv_dir" != /* ]]; then
  venv_dir="$project_dir/$venv_dir"
fi

if [[ ! -f "$venv_dir/bin/activate" || ! -x "$venv_dir/bin/python" ]]; then
  printf 'Python virtual environment not found: %s\n' "$venv_dir" >&2
  printf 'Create it first, then run this script again:\n' >&2
  printf '  python3 -m venv "%s"\n' "$venv_dir" >&2
  printf '  "%s/bin/python" -m pip install -r "%s/requirements.txt"\n' "$venv_dir" "$project_dir" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$venv_dir/bin/activate"
exec "$venv_dir/bin/python" "$project_dir/app.py" "$@"
