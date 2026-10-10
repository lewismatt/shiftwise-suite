#!/usr/bin/env bash
set -euo pipefail

# Fail fast on a missing or placeholder session secret: without a stable
# SHIFTWISE_SECRET_KEY every container restart invalidates all sessions.
if [[ -z "${SHIFTWISE_SECRET_KEY:-}" || "${SHIFTWISE_SECRET_KEY}" == "replace_with_32_byte_hex_token" ]]; then
  echo "ERROR: set a stable SHIFTWISE_SECRET_KEY (32-byte hex) in your .env" >&2
  exit 1
fi

# Run database schema initialization and migrations prior to starting workers.
# Demo seeding (10-employee mock roster) is strictly opt-in: set
# SHIFTWISE_DEMO_SEED=1 for a demo; production boots with an empty database
# and requires SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD on first start.
python3 - <<'PY'
from datetime import timedelta
import os

try:
    import app

    if os.environ.get("SHIFTWISE_DEMO_SEED", "0") in ("1", "8", "mock"):
        import mock_seed

        app.init_db(mock_roster=True)
        week0 = app.monday_of(app.date.today())
        for offset in (0, 7):
            app.run_scheduler((week0 + timedelta(days=offset)).isoformat())
    else:
        app.init_db()
except RuntimeError as exc:
    raise SystemExit(str(exc)) from None
PY


exec "$@"
