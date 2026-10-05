#!/usr/bin/env bash
# Reseed utility for the ShiftWise mock environment.
# Note: Source code and templates are now shared directly with the root
# application; this script provides an optional database reseed utility.
set -euo pipefail

cd "$(dirname "$0")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--reseed" ) ]]; then
    echo "usage: $0 [--reseed]" >&2
    exit 2
fi

python_bin="$(cd .. && pwd)/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    python_bin="python3"
fi

if [[ "${1:-}" == "--reseed" ]]; then
    export DATABASE_URL="${DATABASE_URL-postgresql://shiftwise:shiftwise@localhost:5432/shiftwise_mock}"
    export SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD="${SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD:-testbootstrap123}"
    cd ..
    "$python_bin" - <<'PY'
from datetime import date

try:
    import app
    import mock_seed

    app.init_db(seed_demo=True)
    employees, days, picks = mock_seed.seed(app, force=True)
    week = mock_seed.monday_of(date.today()).isoformat()
    app.run_scheduler(week)
except RuntimeError as exc:
    raise SystemExit(str(exc)) from None
print(f"mock database seeded: {employees} employees (FOH+BOH), "
      f"{days} days/house, {picks} pre-seeded picks")
PY
    echo "mock database reseeded successfully"
else
    echo "Mock environment directly uses root app and templates (no file copying required)."
    echo "To reseed the mock database, run: ./sync.sh --reseed"
fi
