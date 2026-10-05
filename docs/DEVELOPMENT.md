# Development Guide
> Status: current · Last verified: 2026-10-04

## 1. Compose development loop

Use Docker Engine (or Docker Desktop), Docker Compose 2.24.4 or newer, and
OpenSSL. No host Python installation or manual PostgreSQL setup is needed.
Run these commands from a new clone:

```sh
git clone https://github.com/clain1016/shiftwise-suite.git
cd shiftwise-suite

# Create a local session secret and explicitly opt into demo seeding.
# Run once in a new clone; do not overwrite an existing .env.
printf 'SHIFTWISE_SECRET_KEY=%s\nSHIFTWISE_DEMO_SEED=1\n' "$(openssl rand -hex 32)" > .env

docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait
```

Open **https://localhost** through Caddy. Sign in with `manager` / `manager`;
demo employees use their username as their password, for example `maria` / `maria`.
The existing entrypoint initializes PostgreSQL, seeds the roster and shifts on an
empty database, and runs the scheduler. `SHIFTWISE_DEMO_SEED=1` is opt-in:
the override defaults to `0`. To start without demo data, set it to `0` in `.env`
and set `SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD` to a password of at least 12 characters
before starting with an empty database. Never use demo credentials for real deployments.

### Local HTTPS trust

Caddy issues the localhost certificate using its internal certificate authority
(CA) and attempts to install its root certificate in the container's trust store.
It cannot install that root in your host or browser from inside Docker.
If the browser does not already trust this CA, export its public root:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt /tmp/shiftwise-local-root.crt
```

Import `/tmp/shiftwise-local-root.crt` into your operating system or browser's
trusted certificate authorities. On Debian/Ubuntu:

```sh
sudo install -m 0644 /tmp/shiftwise-local-root.crt /usr/local/share/ca-certificates/shiftwise-local.crt
sudo update-ca-certificates
```

Firefox may require a separate import under Settings → Privacy & Security →
Certificates → View Certificates → Authorities. On macOS, import it into Keychain
Access and trust it for SSL; on Windows, import it into Trusted Root Certification
Authorities. Restart the browser if needed. Caddy manages certificate issuance and
renewal; do not generate a separate localhost server certificate.

HTTP at `http://localhost` redirects to HTTPS. The app also listens directly at
`http://127.0.0.1:5000` for diagnostics such as `/healthz`. Use HTTPS through Caddy
for the UI and sign-in: the dev override keeps secure session cookies and proxy
handling enabled. All published dev ports bind to loopback, not the LAN.

### Edit and refresh

The override mounts the checkout at `/app` and replaces the image's command with
the Flask development server. Its reloader restarts on Python changes, and debug
mode reloads Jinja templates on the next request. The interactive debugger is
disabled. Flask was chosen over `gunicorn --reload` because it also reloads
templates without listing template files individually.

Edit a route in `shiftwise/routes/` or a file in `templates/`, save it, then refresh
the browser. No image rebuild is needed for source or template edits. Rebuild after
changing `requirements.txt` or `Dockerfile`:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f app
```

The stack uses the same app image, PostgreSQL 16 and Caddy as production. The
override retains the production entrypoint, health checks, networks and named
volumes, including `postgres-data`. It pins the app to the bundled PostgreSQL
service, sets local HTTPS/proxy settings, and excludes the optional production
machine-local secrets file. `docker-compose.yml` and the production Gunicorn CMD
are deployment settings. Never include the dev override in a deployment.

### Stop or reset the database

Stopping without `-v` preserves the database and Caddy CA:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

To delete this Compose project's data and start with a fresh demo database, run
this single chained command from the checkout:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v && \
  SHIFTWISE_DEMO_SEED=1 docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait
```

This deletes **all named volumes in this project**, including PostgreSQL data and
Caddy's local CA. It is only for disposable local development data. The entrypoint
seeds the empty database during `up`; there is no separate seed script to run.
Export and trust the new Caddy root after every `down -v`. Ordinary `down` / `up`
keeps existing data, and seeding never overwrites a populated roster.

Validate either configuration without starting containers:

```sh
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.dev.yml config --quiet
```

## 2. Optional host Python workflow

PostgreSQL is required for host runs, tests, and simulation tools.
Install Python 3.12 or newer and the development dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

To run outside Compose on `127.0.0.1:5000`, stop the dev stack first to free the port:

```sh
export SHIFTWISE_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD="your-strong-manager-password"
export DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/shiftwise"
.venv/bin/python app.py
```

Open `http://127.0.0.1:5000` and sign in as `manager` with the password above.
For an empty database with demo data, set `SHIFTWISE_DEMO_SEED=1` before launching.

### Mock / LAN demo environment

Provision a dedicated PostgreSQL database named `shiftwise_mock` on a reachable
server. For a host PostgreSQL installation, `createdb -h HOST -U USER shiftwise_mock`
creates it. Compose's PostgreSQL service is not published to the host by default;
use an explicit local port mapping or another host-reachable PostgreSQL instance.

The launcher binds `0.0.0.0:5001` so other devices on your LAN can evaluate it.
Its default URL is `postgresql://shiftwise:shiftwise@localhost:5432/shiftwise_mock`.
Override credentials and host with `DATABASE_URL`:

```sh
export DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/shiftwise_mock"
./shiftwise-mock/sync.sh --reseed
.venv/bin/python shiftwise-mock/app.py
```

`--reseed` replaces the target database's roster with the canonical `mock_seed.py`
data. Never point it at production or another working database.

---

## 3. Running the Test Suite

Use a dedicated PostgreSQL test database. Tests reset its application tables between cases, including identity sequences. Never point them at working or production data.

### Running with Pytest
```sh
export DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/shiftwise_test"
export TEST_DATABASE_URL="$DATABASE_URL"
.venv/bin/pytest tests/
```

### Running All Verifications via Runner
```sh
./tools/run_all.sh
```

---

## 4. End-to-End Simulation Harnesses

All simulation tools require a PostgreSQL URL. Each run creates a unique schema
without a public-schema fallback, so force-seeding cannot clear regular app tables.
The database role needs `CREATE` permission. Gauntlet and demo accept `--database-url`;
otherwise these tools use `SHIFTWISE_SCENARIO_DATABASE_URL`, then `DATABASE_URL`.
Liveweek uses `--database-url`, then `SHIFTWISE_LIVEWEEK_DATABASE_URL`, then `DATABASE_URL`.

Schemas are retained for inspection. The run prints its schema name; remove it
when finished with `DROP SCHEMA <printed_schema_name> CASCADE` on the simulation
database. This deletes all state from that run.

### 1. Gauntlet (`tools/gauntlet.py`)
A 13-phase deterministic stress test verifying:
- Full mock-roster preference submissions
- Partial pick form rejection
- Multiple sick calls and instant auto-cover
- Swaps, vacation ranges, past-dated vacation rejection
- Full shift switch request auto-denial
- Manager unassignment and instant backfills
- Mid-week roster additions and re-ranking

```sh
.venv/bin/python tools/gauntlet.py
```

### 2. Scenario Demo (`tools/scenario_demo.py`)
A 10-phase sequential simulation illustrating conflict handling step-by-step:
```sh
.venv/bin/python tools/scenario_demo.py
```

To inspect a completed demo on port 5001, launch the mock with the simulation
database URL and its printed schema:

```sh
DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/shiftwise_test?options=-c%20search_path%3DPRINTED_SCHEMA" \
  .venv/bin/python shiftwise-mock/app.py
```

Do not run `sync.sh --reseed` against that schema if you want to keep the
scenario's final state.


### 3. House Chaos Simulation (`tools/scenario_random.py`)
A randomized property-based chaos harness:
- Randomly balances roster between FOH and BOH
- Plants invalid cross-house picks to verify isolation
- Injects 10 random lifecycle events (sick, swap, vacation, day off, station flips, cap changes)
- Audits invariants (no cross-house leaks, no over-cap hours except the documented pending-vacation coverage option)

```sh
# Random run
.venv/bin/python tools/scenario_random.py

# Replay a specific seed
.venv/bin/python tools/scenario_random.py <seed>
```

### 4. Live-Week Concurrency Gauntlet (`tools/liveweek.py`)
A full work-week simulation under real concurrency. Boots a threaded WSGI
server on an isolated PostgreSQL schema and drives it with actual HTTP clients.
The roster is derived from `mock_seed.PREFS` plus extra employee and manager sessions, running the complete action
matrix (ranked picks, coverage preferences, sick calls, self-swaps, switch
requests, coworker swap invites and responses, vacation/day-off requests,
password changes, logout cycles) plus manager triage (rebuilds, shift
add/delete, request approve/deny, unassign/override assign, roster edits,
hires, schedule-link sends). Barrier-synchronized collision drills force
specific races (double manager review, sick-vs-swap, shift-delete-vs-swap
accept, pick storms, roster delete vs. activity, double vacation,
unassign-vs-accept). A background auditor recomputes invariants from the
database (capacity, orphaned rows, status validity, invite consistency,
day-off/vacation honoring, hours caps, days-off rule, rank collisions)
throughout the run.

The gauntlet **reports** issues without fixing them. Artifacts land in
`tools/liveweek-artifacts/run-<ts>/` (gitignored): `REPORT.md`,
`summary.json`, `eventlog.jsonl` (every client request),
`serverlog.jsonl` (server responses + unhandled tracebacks),
`issues/ISSUE-*.md` (evidence + suspected code locations),
`snapshots/` (HTML + PNG). The retained PostgreSQL schema holds the final database
state. Read `issues/` top-down and cross-reference the event logs.

```sh
.venv/bin/python tools/liveweek.py                  # 120s ambient phase
.venv/bin/python tools/liveweek.py --duration 240   # longer soak
.venv/bin/python tools/liveweek.py --seed 42        # reproducible roster/RNG
.venv/bin/python tools/liveweek.py --strict         # CI mode: exit 2 on CRITICAL/HIGH findings
```

See [`docs/KNOWN_ISSUES.md`](KNOWN_ISSUES.md) for the active concurrency and data-integrity backlog compiled from liveweek runs, prioritized from CRITICAL to MEDIUM with root-cause analyses and reproduction seeds.


---

## 5. Development Principles

1. **Keep Invariants Intact:**
   Never bypass house boundaries (`station == area`), weekly hours limits, or the 2-days-off rule. The single exception is documented in `docs/ARCHITECTURE.md` §3: only a slot a *pending vacation* holds open may be backfilled over the limits, so the manager can review that trade-off.
2. **Backward-Compatible Schema:**
   Add new columns with default values and update `init_db()` migration blocks.
3. **Verify Before Submitting:**
   Always run the full verification suite (`./tools/run_all.sh`) before creating PRs.
4. **Protect State-Changing Forms:**
   Every `POST` form MUST carry `<input type="hidden" name="_csrf" value="{{ csrf_token() }}">` (`shiftwise/security.py` enforces the token on all unsafe methods). JSON/fetch callers send the same token in the `X-CSRF-Token` header.
