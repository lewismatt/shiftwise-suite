#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Tiered verification runner.
#
#   ./tools/run_all.sh                       full gate (required before opening a PR
#                                            or merging: AGENTS.md section 4)
#   ./tools/run_all.sh --fast                 unit + integration suite only; use this
#                                            while writing code instead of the full gate
#   ./tools/run_all.sh --no-liveweek          full gate minus the live-week gauntlet
#   ./tools/run_all.sh --liveweek-seconds N   override the live-week duration (default 60)
#
# SHIFTWISE_LIVEWEEK_SECONDS also overrides the live-week duration.
#
# DATABASE_URL=<postgres-url> (or SHIFTWISE_DB_ENGINE=postgres) runs the gate
# against PostgreSQL: the pytest step runs the Postgres regression modules and
# liveweek targets the given database (see AGENTS.md section 4).

MODE="full"
RUN_LIVEWEEK=1
LIVEWEEK_SECONDS="${SHIFTWISE_LIVEWEEK_SECONDS:-60}"
usage() {
    cat <<'EOF'
Usage: tools/run_all.sh [options]

  (no options)              full gate: pytest + gauntlet + chaos
                                + liveweek + compose
  --fast                    pytest suite only (inner development loop)
  --no-liveweek             full gate without the live-week concurrency gauntlet
  --liveweek-seconds N      override the live-week duration (default 60;
                            SHIFTWISE_LIVEWEEK_SECONDS also works)
  -h, --help                show this message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fast) MODE="fast" ;;
        --no-liveweek) RUN_LIVEWEEK=0 ;;
        --liveweek-seconds)
            LIVEWEEK_SECONDS="${2:?--liveweek-seconds requires a value}"
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ! [[ "${LIVEWEEK_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "Error: live-week duration must be a non-negative integer seconds (got '${LIVEWEEK_SECONDS}')" >&2
    exit 2
fi

CLEAN="${ROOT_DIR}/tools/clean_artifacts.sh"

# Start from a clean artifact baseline, and clean again on exit -- including
# aborted runs -- so a killed gate cannot leave test databases or caches behind.
# tools/liveweek-artifacts/ is preserved: AGENTS.md section 4 keeps those
# findings for fixing agents.
"${CLEAN}" --quiet
trap '"${CLEAN}" --quiet || true' EXIT

PYTHON="${ROOT_DIR}/.venv/bin/python"
PYTEST="${ROOT_DIR}/.venv/bin/pytest"

if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="python3"
fi

if [[ ! -x "${PYTEST}" ]]; then
    PYTEST="pytest"
fi

echo "=========================================================="
echo " ShiftWise Suite: Test & Verification Suite (${MODE})"
echo "=========================================================="

echo "--> [0] Checking entrypoint executability (tools/check_entrypoints.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/check_entrypoints.py"

echo ""
if [[ "${MODE}" == "fast" ]]; then
    echo "--> FAST MODE: unit & integration suite only."
    echo "    The full gate must still pass before a PR or merge: ./tools/run_all.sh"
    echo ""
    "${PYTEST}" "${ROOT_DIR}/tests/"
    echo ""
    echo "FAST MODE OK"
    exit 0
fi

if ! "${PYTHON}" -c "import pglast, pytest_timeout" >/dev/null 2>&1; then
    echo "    NOTE: dev extras missing (pglast/pytest-timeout)."
    echo "          Install with: .venv/bin/pip install -r requirements-dev.txt"
    echo "          Without pglast the PostgreSQL DDL parser test is skipped."
    echo ""
fi

# Detect the backend the app will actually use, mirroring shiftwise/db.py:
# SHIFTWISE_DB_ENGINE wins; otherwise infer from DATABASE_URL.
EFFECTIVE_ENGINE="${SHIFTWISE_DB_ENGINE:-}"
if [[ -z "${EFFECTIVE_ENGINE}" ]]; then
    case "${DATABASE_URL:-}" in
        postgres://*|postgresql://*) EFFECTIVE_ENGINE="postgres" ;;
        *) EFFECTIVE_ENGINE="sqlite" ;;
    esac
fi
IS_POSTGRES=0
if [[ "${EFFECTIVE_ENGINE}" == "postgres" || "${EFFECTIVE_ENGINE}" == "postgresql" ]]; then
    IS_POSTGRES=1
    if [[ -z "${DATABASE_URL:-}" ]]; then
        echo "ERROR: PostgreSQL backend selected but DATABASE_URL is empty." >&2
        exit 1
    fi
    # The Postgres regression modules isolate via TEST_DATABASE_URL; default it
    # to the gate's database when unset (mirrors the CI postgres job).
    export TEST_DATABASE_URL="${TEST_DATABASE_URL:-${DATABASE_URL}}"
fi

STEP=0
TOTAL=$((4 + RUN_LIVEWEEK))

STEP=$((STEP + 1))
if [[ "${IS_POSTGRES}" -eq 1 ]]; then
    # The full suite is not yet Postgres-safe (issue #39): run the Postgres
    # regression modules instead, mirroring the CI postgres job.
    echo "--> [${STEP}/${TOTAL}] Running Postgres Regression Tests (pytest)..."
    "${PYTEST}" "${ROOT_DIR}/tests/test_postgres_phase3.py" "${ROOT_DIR}/tests/test_postgres_phase4.py"
else
    echo "--> [${STEP}/${TOTAL}] Running Unit & Integration Test Suite (pytest)..."
    "${PYTEST}" "${ROOT_DIR}/tests/"
fi

echo ""
STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Running Conflict Gauntlet (tools/gauntlet.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/gauntlet.py"

echo ""
STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Running Chaos Simulation (tools/scenario_random.py, pinned seed)..."
# Pinned seed keeps the gate deterministic; re-run manually with other seeds
# (tools/scenario_random.py <seed>) for wider exploration.
"${PYTHON}" "${ROOT_DIR}/tools/scenario_random.py" 42

if [[ "${RUN_LIVEWEEK}" -eq 1 ]]; then
    echo ""
    STEP=$((STEP + 1))
    echo "--> [${STEP}/${TOTAL}] Running Live-Week Concurrency Gauntlet (tools/liveweek.py, ${LIVEWEEK_SECONDS}s)..."
    LIVEWEEK_ARGS=(--duration "${LIVEWEEK_SECONDS}")
    if [[ "${IS_POSTGRES}" -eq 1 ]]; then
        LIVEWEEK_ARGS+=(--database-url "${DATABASE_URL}")
    fi
    "${PYTHON}" "${ROOT_DIR}/tools/liveweek.py" "${LIVEWEEK_ARGS[@]}"
else
    echo ""
    echo "--> SKIPPED: live-week concurrency gauntlet (--no-liveweek)."
    echo "    Run the full gate before a PR or merge: ./tools/run_all.sh"
fi

echo ""
STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Validating Docker Compose configuration..."
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    (cd "${ROOT_DIR}" && docker compose config >/dev/null)
    echo "    docker compose config: OK"
else
    echo "    SKIPPED: docker compose is not available in this environment"
fi

echo ""
echo "=========================================================="
echo " ALL TESTS & SIMULATIONS PASSED (100% GREEN)"
echo "=========================================================="
