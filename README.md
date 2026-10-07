# ShiftWise Suite

ShiftWise is a Flask and PostgreSQL staff scheduling and shift-management application for hospitality and service teams with Front-of-House (FOH) and Back-of-House (BOH) operations.

Employees submit ranked preferences for weekly shifts, and the auto-scheduler generates conflict-free schedules honoring weekly hour caps, minimum days off, and strict house boundaries. Managers retain full administrative oversight with sticky assignment overrides, conflict review, and request management.

---

## Quickstart (local development)

Use Docker Engine or Docker Desktop, Docker Compose 2.24.4+, and OpenSSL.
The dev stack runs the app, PostgreSQL 16 and Caddy; no manual database setup is needed.

```sh
git clone https://github.com/clain1016/shiftwise-suite.git
cd shiftwise-suite

# New clone only: create a session secret and opt into demo data.
# Skip this if .env already exists — the redirect overwrites the file.
printf 'SHIFTWISE_SECRET_KEY=%s\nSHIFTWISE_DEMO_SEED=1\n' "$(openssl rand -hex 32)" > .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait
```

Open [https://localhost](https://localhost) and sign in with `manager` / `manager`.
On first use, export Caddy's local root certificate and import it into your host
or browser's trusted certificate authorities:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt /tmp/shiftwise-local-root.crt
```

See the [development guide](docs/DEVELOPMENT.md#local-https-trust) for platform-specific
trust steps. Caddy manages local certificates, but its container cannot install
trust in your host browser. Use HTTPS for sign-in; `http://127.0.0.1:5000/healthz`
is available for direct app diagnostics.

Edit Python routes or Jinja templates, save, then refresh. The checkout is mounted
into the app container, and Flask reloads changes with its interactive debugger
disabled. Rebuild only when dependencies or the Dockerfile change. Production
still uses Gunicorn; the dev override is not for deployment.

Reset and seed a fresh local database with one command:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v && \
  SHIFTWISE_DEMO_SEED=1 docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait
```

This deletes the project's named volumes, including database data and Caddy's
local CA. Export and trust the new root afterward. Use `down` without `-v` to
preserve data. Demo seeding is opt-in and only seeds an empty roster.
The [development guide](docs/DEVELOPMENT.md) covers the full loop and the optional
host Python workflow against PostgreSQL.

---

## Email and text schedule links

The manager can save employee email addresses and E.164 phone numbers on the Roster page, then send the employee a link to sign in and submit schedule preferences. Set `SHIFTWISE_PUBLIC_URL` to the app's externally reachable HTTPS address. Email uses SMTP (`SHIFTWISE_SMTP_HOST`, `SHIFTWISE_SMTP_FROM`, and optionally `SHIFTWISE_SMTP_PORT`, `SHIFTWISE_SMTP_USER`, `SHIFTWISE_SMTP_PASSWORD`). SMS uses Twilio (`SHIFTWISE_TWILIO_ACCOUNT_SID`, `SHIFTWISE_TWILIO_AUTH_TOKEN`, `SHIFTWISE_TWILIO_FROM`). Configure whichever channel you plan to use in `.env` or the deployment secrets file, then restart the app. The message contains the sign-in link, not a password; employees still sign in with their existing account.

---

## Quickstart (Container Deployment)

ShiftWise includes production-grade container orchestration with Gunicorn behind a Caddy reverse proxy providing automatic TLS and security headers:

```sh
# Copy environment configuration
cp .env.example .env

# Edit .env with your domain and secrets, then build and run:
docker compose up -d --build

# View container status
docker compose ps
```

---

## Documentation Directory

Comprehensive documentation is available in the [`docs/`](docs/) directory:

- 📐 **[Architecture & System Design](docs/ARCHITECTURE.md):** High-level architecture, scheduling algorithms, priority sorting, coverage planning, and house boundaries.
- [Data Model & Storage Schema](docs/DATA_MODEL.md): PostgreSQL schema, table relationships, assignment lifecycle, and request state machines.
- 💻 **[Development Guide](docs/DEVELOPMENT.md):** Local setup, running unit/integration tests, and using the gauntlet/chaos simulation harnesses.
- 🚀 **[Production Deployment](docs/DEPLOYMENT.md):** Multi-container Docker Compose deployment, Caddy TLS setup, and security hardening.
- [Go-live readiness review](docs/GO_LIVE_READINESS_REVIEW.md): Reproduced release blockers, recommended fixes, verification evidence, and staging/cutover acceptance checks.
- ⚙️ **[Environment Variables](docs/ENVIRONMENT.md):** Complete reference of all `SHIFTWISE_*` configuration parameters.
- ⚠️ **[Known Issues & Concurrency Backlog](docs/KNOWN_ISSUES.md):** Catalog of concurrency, database locking, and integrity issues surfaced by chaos/load testing, prioritized with reproduction seeds and fix approaches.
- 🐘 **[PostgreSQL Migration Plan & Handoff](docs/POSTGRES_MIGRATION_PLAN.md):** Completed 5-phase migration record from SQLite to PostgreSQL; PostgreSQL is the only supported runtime.

---

## Running Tests & Simulation Harnesses

Tests require a dedicated PostgreSQL database and reset its application tables between cases. Never point the test suite at working or production data. Simulation tools create separate retained schemas; their database role needs `CREATE` permission.

```sh
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/shiftwise_test
# Run entire test suite with pytest
.venv/bin/pytest tests/

# Run the 13-phase stress gauntlet
.venv/bin/python tools/gauntlet.py

# Run the sequential conflict scenario simulation
.venv/bin/python tools/scenario_demo.py

# Run the randomized house-chaos simulation
.venv/bin/python tools/scenario_random.py

# Run the live-week concurrency gauntlet with the mock roster, extra employees,
# and two managers driving
# the app over real HTTP against an isolated PostgreSQL schema. Writes issues,
# logs, snapshots, and reports to tools/liveweek-artifacts/run-*/
.venv/bin/python tools/liveweek.py                 # default 120s ambient phase
.venv/bin/python tools/liveweek.py --strict        # exit non-zero on findings

# Run all tests and simulations in one command
./tools/run_all.sh
```

---

## License

Internal proprietary software — all rights reserved.
