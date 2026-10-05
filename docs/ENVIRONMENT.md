# Environment Variables Reference
> Status: current · Last verified: 2026-09-29

All ShiftWise configuration is driven through environment variables prefixed with `SHIFTWISE_`.

## Core Application Variables

| Variable | Type | Default Value | Used In | Description & Recommendations |
|---|---|---|---|---|
| `SHIFTWISE_SECRET_KEY` | String | *Ephemeral (Generated)* | `app.py`, `docker-entrypoint.sh` | Cryptographic secret key used to sign session cookies. **Must be fixed and persistent** in production (32-byte hex); an ephemeral key logs a warning and invalidates all user logins on restart. |
| `SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD` | String | *None* | `app.py`, `docker-entrypoint.sh` | Password used to create the initial `manager` account on a fresh database, or upgrade legacy plain-text `manager` passwords. Must be at least 12 characters. |
| `SHIFTWISE_HOST` | String | `127.0.0.1` | `app.py` | Bind interface for the Flask development server. Defaults to loopback (`127.0.0.1`); set to `0.0.0.0` for LAN access. |
| `SHIFTWISE_PORT` | Integer | `5000` | `app.py`, `Dockerfile` | HTTP listening port for the application server. (Mock demo defaults to `5001`). |
| `SHIFTWISE_BEHIND_PROXY` | Boolean | `""` (False) | `app.py` | When set to `1`, `true`, or `yes`, wraps the WSGI application with Werkzeug's `ProxyFix` middleware to trust `X-Forwarded-*` headers from Caddy or Nginx. |
| `SHIFTWISE_SESSION_COOKIE_SECURE` | Boolean | `""` (False) | `app.py` | When set to `1`, `true`, or `yes`, marks the session cookie with the `Secure` flag (browser will only send it over HTTPS). |
| `SHIFTWISE_SESSION_COOKIE_SAMESITE` | String | `Lax` | `app.py` | `SameSite` attribute for the session cookie (`Lax`, `Strict`, or `None`). |
| `SHIFTWISE_DEMO_SEED` | String | `""` (0) | `app.py`, `docker-entrypoint.sh` | When set to `1`, `8`, or `mock`, populates a fresh database with the demo roster and shift schedule. Keep `0` for production deployments. |

---

## Database Backend Variables

| Variable | Type | Default Value | Used In | Description & Recommendations |
|---|---|---|---|---|
| `DATABASE_URL` | Connection string | *(compose default: bundled postgres; otherwise required)* | `shiftwise/db.py`, `docker-compose.yml` | Required PostgreSQL URL (`postgresql://` or `postgres://`) or libpq connection string. Startup validates configuration and connectivity before serving requests. Empty, invalid, or unreachable values stop startup; Compose assembles its default from `POSTGRES_*`. |
| `SHIFTWISE_DB_ENGINE` | String | `postgres` | `shiftwise/db.py` | Optional explicit selection: `postgres` or `postgresql`. `sqlite` errors with "SQLite support removed — configure PostgreSQL via DATABASE_URL". |
| `POSTGRES_DB` | String | `shiftwise` | `docker-compose.yml` | Database name created by the bundled postgres service. If changed, update `DATABASE_URL` to match. |
| `POSTGRES_USER` | String | `shiftwise` | `docker-compose.yml` | Database user for the bundled postgres service. If changed, update `DATABASE_URL` to match. |
| `POSTGRES_PASSWORD` | String | `shiftwise` | `docker-compose.yml` | Database password for the bundled postgres service. **Change this in `.env` for any non-local deployment**; the default is a development convenience only. If changed, update `DATABASE_URL` to match. |

---

## Container & Proxy Variables

| Variable | Type | Default Value | Used In | Description & Recommendations |
|---|---|---|---|---|
| `SHIFTWISE_PUBLIC_HOSTNAME` | String | `localhost` | `Caddyfile`, `docker-compose.yml` | Public domain name for the service. Caddy uses this hostname to provision automatic TLS certificates. |
| `SHIFTWISE_ACME_EMAIL` | String | `operator@cysys.dev` | `Caddyfile`, `docker-compose.yml` | Email address registered with Let's Encrypt / ZeroSSL for certificate renewal and expiration notices. |
| `SHIFTWISE_HTTP_PORT` | Integer | `80` | `docker-compose.yml` | External host port mapped to Caddy's HTTP listener. |
| `SHIFTWISE_HTTPS_PORT` | Integer | `443` | `docker-compose.yml` | External host port mapped to Caddy's HTTPS listener. |
| `SHIFTWISE_ENV_FILE` | Path | `/srv/cyberdyne/secrets/...` | `docker-compose.yml` | Optional host path to a secondary machine-local secrets file. |

---

## Security & Login Throttling Variables

| Variable | Type | Default Value | Used In | Description & Recommendations |
|---|---|---|---|---|
| `SHIFTWISE_LOGIN_MAX_FAILURES` | Integer | `5` | `shiftwise/auth.py` | Consecutive failed sign-ins allowed for one username before that username is locked out. `0` disables the username lockout. |
| `SHIFTWISE_LOGIN_IP_MAX_FAILURES` | Integer | `25` | `shiftwise/auth.py` | Consecutive failed sign-ins allowed from one client address, which catches username spraying. `0` disables the address lockout. |
| `SHIFTWISE_LOGIN_LOCKOUT_SECONDS` | Integer | `900` | `shiftwise/auth.py` | How long a locked username or address stays locked. |

---

## Test & Simulation Variables

| Variable | Type | Default Value | Used In | Description |
|---|---|---|---|---|
| `TEST_DATABASE_URL` | URL | `DATABASE_URL` | `tools/run_all.sh`, PostgreSQL tests | Dedicated test database. Tests reset application tables; never use working or production data. |
| `SHIFTWISE_SCENARIO_DATABASE_URL` | URL | `DATABASE_URL` | `gauntlet.py`, `scenario_demo.py`, `scenario_random.py` | Simulation database; each run creates a unique retained schema. Gauntlet/demo also accept `--database-url`. The role needs `CREATE` on the database. |
| `SHIFTWISE_LIVEWEEK_DATABASE_URL` | URL | `DATABASE_URL` | `liveweek.py` | Liveweek database; `--database-url` takes precedence. Each run creates a unique retained schema. |
