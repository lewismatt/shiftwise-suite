# Go-live readiness review

Reviewed on 2026-10-06 against commit `1505820cdeba70e5afa99d50dfe5083d47556d77`.
This report records findings and recommended fixes. It does not implement them.
Line references describe that reviewed revision.

## Release decision

Do not expose this revision to unrestricted public test traffic or replace the existing live service yet.
The app, PostgreSQL database, and Caddy proxy work together. Reproduced security and connection-lifecycle defects remain.
The live-data migration and recovery procedure also needs a rehearsal before cutover.

After the public-traffic blockers below are fixed, use a separate staging hostname and database with disposable data and enrolled testers.
Keep the existing live service unchanged until the data-preserving cutover and rollback checks pass.

### Evidence labels

- **Reproduced:** observed through the running Compose stack, Chromium, real HTTP requests, or a disposable-database script using the application.
- **Code evidence:** the implementation/configuration contains the stated behavior; the failure scenario was not separately exercised.
- **[INFERENCE]:** a predicted outcome or reconstructed cause, not directly observed.
- **Unverified deployment condition:** the actual live host, environment, or datastore was not inspected.

All destructive probes used disposable databases. Review containers, volumes, temporary credentials, and Python caches were removed.
No live service or data was changed. The original review made no application changes.

## Blockers before public test traffic

### R01: Employee-controlled stored JavaScript executes in the manager roster

**Severity:** High. **Evidence:** Reproduced.

An employee can change their display name through `POST /settings`, `action=profile`.
The manager roster inserts that name into a single-quoted JavaScript string in an inline `onsubmit` handler.
HTML escaping does not protect this JavaScript context because the browser decodes entities before compiling the handler.
Execution requires the manager to click that employee's Delete button.

Sources: `shiftwise/routes/employee.py:40-64`, `shiftwise/routes/roster.py:265-270`, `templates/roster.html:66-69`.

The review exercised the employee-controlled profile write separately from the browser execution proof.
A stored name containing `'+(localStorage.setItem('shiftwise-xss-proof','executed'),'')+'` produced the persistent marker in Chromium when Delete was clicked.
The proof changed only disposable data. [INFERENCE] An attacker can instead read the page's CSRF tokens and send authenticated manager requests from the same origin.

Recommended fix: remove employee names from executable JavaScript. Use a static confirmation message or an HTML-escaped data attribute read by an event listener.
Do not address this by rejecting apostrophes in names.

Acceptance: an employee-supplied name containing quotes and script syntax renders as text, executes no script, and cannot issue manager actions.
Normal employee names still work.

### R02: Password changes and resets do not revoke existing sessions

**Severity:** High. **Evidence:** Administrative reset reproduced; other password paths have code evidence.

Sessions contain `uid`, `role`, and `name`, but no authentication generation.
Protected requests check the current user and role, not whether a password changed after login.
Self-service password changes clear the current browser's session only. Administrative resets leave other cookies valid.

Sources: `shiftwise/routes/auth.py:45-51,82-89`, `shiftwise/auth.py:126-138`, `shiftwise/routes/employee.py:85-92`, `shiftwise/routes/roster.py:143-165`.

After a manager reset an employee's password through the real HTTP endpoint, the employee's old cookie still accessed `/calendar` with HTTP 200.
[INFERENCE] A copied cookie or a session obtained with compromised credentials remains usable after a recovery reset.

Recommended fix: add a per-user authentication generation, capture it at login, compare it on every protected request, and increment it atomically in all password-update paths.

Acceptance: cookies issued before an administrative reset or either self-service password change are rejected on their next protected request.
A login with the new password succeeds. Deleted-user and manager-role checks remain enforced.

### R03: Malformed switch requests leak database leases

**Severity:** High. **Evidence:** Reproduced.

`target_shift=²` passes `str.isdigit()` but fails `int()` after `conn = db()`.
The exception bypasses the route's connection cleanup.

Source: `shiftwise/routes/employee.py:493-499`.

A real route probe returned HTTP 500 and reduced `pool_available` from 4 to 3.
The pool allows at most 20 connections per process. [INFERENCE] Repeated authenticated requests can exhaust a worker's pool and make unrelated requests wait or fail.

Recommended fix: parse and validate the target before leasing a connection, then guarantee lease return with `try/finally` for every subsequent exit.
Use the existing adapter's rollback-and-return behavior rather than suppressing the exception.

Acceptance: malformed targets, missing targets, invalid ownership, and database exceptions return the intended response without reducing available pool capacity.
Valid switch requests still follow the current approval workflow.

### R04: Failed health probes leak database leases

**Severity:** High. **Evidence:** Reproduced.

`/healthz` returns its lease only when the users-table query succeeds.
Its exception path returns 503 without closing the facade.

Sources: `shiftwise/routes/calendar.py:22-30`, `shiftwise/db.py:458-475`.

In a separate application process against a disposable restored database, the users table was temporarily renamed.
Four real health requests returned 503. Pool statistics changed from `pool_size=4, pool_available=4` to `pool_size=4, pool_available=0`.
The table was restored in `finally`; a subsequent probe returned 200, but the failed requests had not returned their leases.
[INFERENCE] Repeated database-visible health failures can exhaust the pool and prevent recovery without a process restart.

Recommended fix: close the acquired lease in `finally`, including query failures. Keep the unauthenticated response generic.
Audit other route exception paths for the same ownership defect as part of this fix.

Acceptance: repeated failing probes return 503 without leaking leases or holding aborted transactions.
After the underlying failure is repaired, health and normal requests recover without restarting the app.

### R05: Published bootstrap credentials can be accepted on a fresh install

**Severity:** High if deployed with copied credentials. **Evidence:** Code/configuration evidence; actual live credentials unverified.

The entrypoint rejects the session-secret placeholder but does not reject the bootstrap-password placeholder.
`replace_with_strong_manager_password` satisfies the 12-character check.
The deployment guide also contains fixed example credentials, including a session-signing key.

Sources: `.env.example:4-8`, `docker-entrypoint.sh:4-9`, `shiftwise/db.py:645-670`, `docs/DEPLOYMENT.md:59-63`.

Recommended fix: reject known placeholder bootstrap passwords before first boot and replace published fixed credentials with generation instructions.
Require a generated, persistent session secret and a unique manager password in the deployment checklist.
Do not impose a bootstrap-password requirement on an already initialized deployment solely to reject an unused value.

Acceptance: fresh startup rejects known placeholders; generated credentials bootstrap successfully; copied example secrets are not used on staging or production.
Demo seeding stays disabled for real deployments.

## Blockers before live-data cutover

### R06: The test database setting does not protect the working database

**Severity:** High operational/data-loss risk. **Evidence:** Reproduced.

Shared pytest isolation truncates through `appmod.db()`, which resolves `DATABASE_URL`.
Setting a different `TEST_DATABASE_URL` does not redirect those shared fixtures.
Some dedicated PostgreSQL tests do use `TEST_DATABASE_URL`, so a mixed configuration can touch both databases.

Sources: `tests/conftest.py:30-54`, `tests/test_support.py`, `shiftwise/db.py:238,543-547`, `tools/run_all.sh:105`, `docs/ENVIRONMENT.md:60`.

With `DATABASE_URL` pointing at a disposable database containing two users and `TEST_DATABASE_URL` pointing at another disposable database, `tests/test_priority.py::test_priority` passed while the first database's user count changed from 2 to 0.

Recommended fix: select the disposable test URL before importing the application, or reject mismatched targets before initialization/truncation.
Update the documentation to match the implemented contract.
Until fixed, set both URLs to the same fresh disposable database. Never run the gate with `DATABASE_URL` pointing at working or production data.

Acceptance: different URL settings cannot silently truncate the application database.
Prove the safety check with sentinel data in a separate database, and keep CI's disposable isolation intact.

### R07: The documented restore can return success despite SQL errors

**Severity:** High recovery risk. **Evidence:** Reproduced.

The guide dumps without a clean-target restore procedure and restores into the existing database without `ON_ERROR_STOP`.
Schema/object collisions and data conflicts therefore need not stop `psql` or produce a failing exit code.

Source: `docs/DEPLOYMENT.md:179-188`.

A dump restored into a new empty database succeeded and preserved the manager and employee rows.
Repeating the documented-style restore into the populated target emitted SQL errors but exited 0.
[INFERENCE] A recovery following that recipe can leave a mixture of backup and current state rather than the intended backup state.

Recommended fix: stop application writers, restore into a new empty database using fail-on-error settings, validate the restored data, then switch the application deliberately.
Use `docker compose exec -T` for scripted dumps. Make database/user names match deployment configuration rather than assuming defaults.

Acceptance: a complete restore drill preserves counts, relationships, passwords, and sequence behavior.
An injected restore error fails the command. The app can sign in and schedule against the restored database.

### R08: Existing-data migration and rollback are not specified

**Severity:** High cutover prerequisite. **Evidence:** Documentation/tooling evidence; live datastore unverified.

The PostgreSQL migration record describes runtime/schema work and seeding, not importing an existing live datastore.
No SQLite-to-PostgreSQL importer was found in the repository review.
Current startup does not read a legacy SQLite store.
The deployment guide has no write-freeze, migration validation, or rollback procedure.

Sources: `docs/POSTGRES_MIGRATION_PLAN.md:9-12,204-206`, `docs/DEPLOYMENT.md:140-188`, `docker-entrypoint.sh:11-30`.

Recommended fix: first establish the live revision and database format.
If it is SQLite, either implement and rehearse a data-preserving import or explicitly approve a fresh-start cutover with a list of records to re-enter.
If it is PostgreSQL, rehearse the supported upgrade/restore path instead.
Preserve the old image, configuration, and store; freeze writes; validate the new data; define backout triggers.
Document what happens to writes made after cutover. Rolling back an image alone does not reconcile stores.

Acceptance: demonstrate the chosen cutover on a copy of live-format data, compare table counts and relationships, validate generated IDs, and exercise employee/manager workflows.
Rehearse rollback and state any write loss explicitly before the maintenance window.

## Scheduling defects to fix before using real schedules

### R09: Reassignment is silently undone by a rebuild

**Severity:** Medium. **Evidence:** Reproduced.

Unassigning creates an active `manager_unassign` exclusion.
Assigning the same employee back does not retire it, so the rebuild removes the new `manager_fixed` assignment.

Sources: `shiftwise/routes/conflicts.py:134-141,227-242`, `shiftwise/domain/rules.py:27-30`, `shiftwise/scheduler/engine.py:110-118`.

Probe result: the assignment was `manager_fixed` after reassignment and absent after rebuilding.
Recommended fix: supersede the matching active unassignment exclusion inside the assignment transaction.
Acceptance: unassign, reassign, and rebuild preserves the deliberate assignment without removing unrelated absence records.

### R10: A day-off override affects unrelated weeks

**Severity:** Medium. **Evidence:** Reproduced.

The manager's day-off check and update filter by weekday but not `week_start`.
Assigning a previous-week Monday changed the current-week Monday request to `overridden_by_manager`.

Source: `shiftwise/routes/conflicts.py:199-208`.

Recommended fix: scope both statements to the selected shift's week.
Acceptance: overriding one week's day off leaves other weeks' requests and scheduling exclusions unchanged.

## Other security and deployment findings

| ID | Priority/evidence | Finding and recommended fix |
|---|---|---|
| R11 | Medium; reproduced | The roster's hidden input named `confirm` shadows the global function. Chromium threw `TypeError: confirm is not a function` and submitted deletion without confirmation. Use `window.confirm` or an event listener outside the form's named-control scope. Verify cancel preserves the employee and confirm deletes only the selected employee. `templates/roster.html:66-68`. |
| R12 | Medium; code evidence | Login failures use an unlocked read followed by a computed replacement value. [INFERENCE] Concurrent attempts lose increments and weaken lockout. Use an atomic increment and derive the lock deadline from the resulting count. Verify parallel failures cannot lower or undercount the stored count. `shiftwise/auth.py:74-92`. |
| R13 | Medium; code evidence | Successful login deletes both username and shared-IP failure history. [INFERENCE] One known credential lets an attacker reset aggregate spraying history between batches. Clear only the successful username's failures; expire IP history independently in a bounded window. Keep the individual username limit. `shiftwise/auth.py:49-53,95-99`, `shiftwise/routes/auth.py:44-47`. |
| R14 | Medium; code evidence | Day-off submissions lack the employee-row serialization used by vacation requests. [INFERENCE] Simultaneous requests can pass the same absence check and create duplicates. Lock the employee before checking/inserting. Verify one active request is created and denial does not leave a duplicate exclusion. `shiftwise/routes/employee.py:452-464`. |
| R15 | Low; code evidence | GET `/logout` clears state without the unsafe-method CSRF check. [INFERENCE] A cross-site top-level navigation can force logout with default Lax cookies. Make logout a CSRF-protected POST and update both navigation controls. `shiftwise/routes/auth.py:61-64`, `templates/base.html:858,881`. |
| R16 | Medium; reproduced container configuration | Caddy receives the complete `.env`, including the session-signing secret, bootstrap password, and database password. The secrets overlay blanks database values but not the other application credentials. Pass only hostname/ACME configuration to Caddy. Verify those credentials are absent from its environment. `docker-compose.yml:68-73`, `docker-compose.secrets.yml:55-60`. |
| R17 | Medium configuration risk; code evidence | The secondary `SHIFTWISE_ENV_FILE` is an `env_file`, but explicit interpolated `DATABASE_URL`/`POSTGRES_*` entries take precedence. PostgreSQL does not load that file. [INFERENCE] An operator can unintentionally use the bundled database/default password while expecting the secondary file to configure a different target. Document or reject unsupported entries and test effective configuration. `docker-compose.yml:5-11,29-43`. |
| R18 | Medium operational prerequisite; configuration evidence | Compose declares no service log-rotation limits. The host's daemon rotation is unverified. Confirm or configure bounded logs, scheduled off-host backups, retention, restore drills, and alerts for availability, 5xx, database failures, and stalled rebuilds. `docker-compose.yml`, `docs/DEPLOYMENT.md:153-188`. |
| R19 | Low observability gap; code evidence | Caddy logs/forwards a request ID but the default Gunicorn access format does not record it. Add the forwarded ID to application/access logs and prove one request can be traced across proxy and app logs. `Caddyfile:29,48,60`, `Dockerfile:32`. |
| R20 | Low protocol/documentation gap; configuration evidence | Caddy advertises HTTP/3, but Compose publishes TCP 443 only. Publish UDP 443 if HTTP/3 is intended, or correct the advertised/documented expectation. HTTP/2 worked and this does not block launch. `docker-compose.yml:74-76`, `docs/DEPLOYMENT.md:9`. |

## Verification and report reliability

The full gate passing is not evidence that every load finding is resolved.
`tools/run_all.sh` invokes liveweek in report-only mode; `--strict` is required to fail on HIGH/CRITICAL findings.

### R21: Cross-schema scheduler locks affect concurrent verification

**Priority:** Low tooling concern. **Evidence:** Reproduced mechanism; original failure cause inferred.

Scheduler locks use a fixed namespace plus the week's CRC32, without a schema identifier.
PostgreSQL advisory locks are database-scoped.
Two connections using different schemas in one database contended on the same week lock: the first acquired it and the second did not.

Source: `shiftwise/db.py:569-574`.

An initial full gate ran concurrently with a separate schema-isolated load run in the same database.
It passed 181 tests and failed two phase-4 assertions because processing returned 0 or did not invoke the injected failure.
[INFERENCE] The demonstrated cross-schema contention explains those results, but the competing holder was not recorded at failure time.
The final full gate used a distinct fresh database and passed all 183 tests.

Recommended fix: continue using separate databases for concurrent verification.
If schema-isolated instances are intended to run concurrently, include the logical instance/schema in the lock key without weakening same-instance serialization.
This is not a single-schema deployment blocker.

### R22: Load reports misclassify an override and count old vacation rows

**Priority:** Medium verification quality. **Evidence:** Retained events/database review.

The report-only gate run, seed `228617326`, reported a HIGH days-off finding for morgan.
Events and notifications reconstruct five working days followed by an explicit manager placement on Thursday.
[INFERENCE] The manager override produced the sixth day; no historical assignment snapshot captured the exact audit-time set.
The days-off auditor counts `manager_fixed` without the override distinction already used by its hours auditor.

Sources: `tools/liveweek.py:852-864`, `shiftwise/routes/conflicts.py:157-249`.

Make override reporting explicit, retain the violation evidence, and warn managers when their placement exceeds the days-off policy.
Do not discard automatic violations or weaken the policy to make the check green.

The same run's double-vacation drill counted an older approved request in its collision window.
Retained rows were `2026-10-14..2026-10-17` and `2026-10-18..2026-10-21`; they did not overlap.
Only one of the racing submissions was inserted. The active-request guard worked.

Source: `tools/liveweek.py:1920-1925`, `shiftwise/routes/employee.py:395-405`.

Capture a request-ID watermark before the drill and check newly created requests, or compare actual ranges pairwise.
Verify historical approved requests do not create false duplicate reports while genuinely overlapping new approvals still fail.

### R23: The sick-vs-swap drill can abort before exercising the race

**Priority:** Medium verification quality. **Evidence:** Recorded exception and events.

In seed `228617326`, the drill re-queried assignments after two logins and called `random.choice` on an empty list.
It raised `IndexError` before issuing either race action, so this drill contributed no coverage.

Source: `tools/liveweek.py:1692-1693`.

Recommended fix: handle a changed/empty assignment set explicitly, record a skip or select another eligible donor, and report drill coverage alongside findings.
Acceptance: empty donors never crash the drill; a readiness run contains an exercised sick-vs-swap case, not only a skipped case.

### R24: CI and documentation do not provide the same acceptance gate

**Priority:** Medium release-process concern. **Evidence:** Code/documentation evidence.

CI runs a subset of the local gate and omits liveweek and Compose validation.
Required branch protection was not inspected.
The gate also passes `--database-url "$DATABASE_URL"` to liveweek, overriding `SHIFTWISE_LIVEWEEK_DATABASE_URL`.
The known-issues document mixes historical SQLite evidence with status entries inconsistent with current implementation notes.

Sources: `.github/workflows/ci.yml:1-13`, `tools/run_all.sh:149-162`, `docs/ENVIRONMENT.md:60-62`, `docs/KNOWN_ISSUES.md:36-39,250-303,453-468`.

Recommended fix: document which checks are mandatory/manual, add or require the intended acceptance checks, honor or document the gate's liveweek target precedence, and reconcile backlog status with current PostgreSQL evidence.
Do not treat historical SQLite failures as reproduced current PostgreSQL defects.

## Exercised verification

The original review built a fresh image and launched isolated PostgreSQL, Gunicorn, and Caddy services.
Only the temporary review override published a loopback database port.
Base production Compose exposes the proxy ports, not app or PostgreSQL ports.

Observed checks:

- Fresh PostgreSQL/app/Caddy startup through the health-gated dependency chain.
- Local HTTPS through Caddy, HTTP redirect, HTTP/2, security headers, and Secure/HttpOnly/SameSite=Lax session cookies.
- Browser manager sign-in and employee onboarding.
- Employee `/manager` rejection with 403; missing-CSRF manager POST rejection with 400.
- App restart followed by successful health response.
- Password-file container startup followed by health status 200.
- Dump restoration into an empty database; error reporting on a populated-target restoration.
- The reproduced security, lease, test-target, and scheduling probes described above.
- Final `./tools/run_all.sh`: 183 tests passed, conflict gauntlet passed, chaos seed 42 completed, report-only liveweek completed, Compose validated.
- `ruff check` and `ruff format --check` passed.

| Load check | Requests | HTTP 5xx | Request P95 | Whole-action P95 | Reported findings |
|---|---:|---:|---:|---:|---|
| Strict seed 42; 60 seconds ambient duration; Gunicorn 2 workers x 2 threads | 2,461 | 0 | 747.0 ms | 1,185.6 ms | 0 HIGH/CRITICAL, 2 MEDIUM, 1 INFO |
| Full-gate seed 228617326; 60 seconds ambient duration; default harness server | 2,444 | 0 | 423.2 ms | 785.6 ms | 1 HIGH, 5 MEDIUM, 1 INFO |

The strict run matched the container's worker/thread counts but did not include Compose CPU limits, Caddy, or public network latency.
Its request P95 exceeded the migration plan's 200 ms target. Establish the required staging latency target and measure the actual deployed stack before accepting performance.
The load checks are not exhaustive and do not cover the malicious-input and failure-injection cases above.

Original local artifacts were `tools/liveweek-artifacts/run-20261006-190641/` and `/tmp/shiftwise-readiness-i71jcoxv/liveweek-production-workers/`.
Those paths are not portable evidence links. The results and triage are recorded here; ignored/raw artifacts and credentials are not included in this PR.

## Launch acceptance checklist

### Public domain staging

- [ ] Fix R01-R05 and verify their acceptance criteria.
- [ ] Fix the confirmation defect and restrict Caddy's environment to proxy configuration.
- [ ] Fix R09-R10 before testers depend on real schedules.
- [ ] Use a separate database and hostname, with disposable data and enrolled testers.
- [ ] Replace template/example credentials; keep demo seeding disabled.
- [ ] Verify effective proxy and Secure-cookie settings, including any outer CDN/proxy. Prevent direct untrusted access to the app.
- [ ] Verify public DNS, a publicly trusted certificate, redirects, `/healthz`, login, onboarding, picks, and a completed rebuild through the domain.
- [ ] Run the full gate and strict load acceptance against isolated databases. Record failures, skipped drills, override reports, and actual-stack latency.
- [ ] Configure bounded logs, backup retention, and actionable alerts.

### Existing live-service replacement

- [ ] Fix R06-R07 and rehearse their data-safety/recovery acceptance criteria.
- [ ] Identify the live revision and datastore; implement/rehearse the selected R08 migration path.
- [ ] Preserve the old image/configuration/store and take a verified pre-cutover backup.
- [ ] Define the maintenance window, write freeze, validation checks, observation period, and rollback trigger.
- [ ] Define treatment of post-cutover writes; rehearse rollback without silently discarding them.
- [ ] Restore an off-host backup and exercise sign-in and scheduling against it.
- [ ] Resolve or explicitly accept remaining scheduling/security risks using current evidence, not the historical backlog summary.

The actual live datastore, public certificate issuance, DNS, external proxy topology, host log rotation, backup policy, and alert delivery remain unverified.
