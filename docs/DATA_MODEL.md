# Data Model & Storage Schema
> Status: current · Last verified: 2026-10-04

## 1. Storage Configuration

ShiftWise uses PostgreSQL through a process-local `psycopg_pool.ConnectionPool`
(4 to 20 connections). `DATABASE_URL` is required outside Compose; Compose assembles
its default from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`.
Startup checks connectivity before serving requests and applies
`shiftwise.db.POSTGRES_SCHEMA` during initialization.

Foreign keys delete dependent picks, assignments, coverage preferences, requests,
and notifications atomically. Optional request/notification shift references and
request invitee references use `ON DELETE SET NULL` to preserve history.
The connection facade translates `?` parameters and normalizes temporal results
to the string shapes used by routes and templates.

---

## 2. Relational Schema

### `users`
Represents staff members and administrative accounts.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Unique user identifier. |
| `username` | `TEXT` | `UNIQUE NOT NULL` | Login username. |
| `password` | `TEXT` | `NOT NULL` | Password hash (`scrypt` or `pbkdf2`). |
| `name` | `TEXT` | `NOT NULL` | Display name of employee/manager. |
| `role` | `TEXT` | `NOT NULL DEFAULT 'employee'` | Role: `'employee'` or `'manager'`. |
| `weekly_hours` | `INTEGER` | `DEFAULT 40` | Maximum weekly hours cap. |
| `employment_type` | `TEXT` | `NOT NULL DEFAULT 'part_time'` | `'full_time'` or `'part_time'` (influences priority). |
| `hired_on` | `DATE` | `NULL` | Hiring date establishing seniority. |
| `station` | `TEXT` | `NOT NULL DEFAULT 'front'` | House station: `'front'` (FOH) or `'back'` (BOH). |

---

### `shifts`
Represents scheduled work blocks within a specified week.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Unique shift identifier. |
| `week_start` | `DATE` | `NOT NULL` | Monday of the schedule week. |
| `day` | `TEXT` | `NOT NULL` | Weekday: `'Mon'`, `'Tue'`, `'Wed'`, `'Thu'`, `'Fri'`, `'Sat'`, `'Sun'`. |
| `start_time` | `TIME` | `NOT NULL` | Shift start time (`HH:MM`, 24-hour). |
| `end_time` | `TIME` | `NOT NULL` | Shift end time (`HH:MM`, 24-hour). |
| `slots` | `INTEGER` | `NOT NULL DEFAULT 1 CHECK (slots >= 1)` | Number of concurrent workers required. |
| `note` | `TEXT` | `NULL` | Optional description (e.g., "Dinner prep"). |
| `area` | `TEXT` | `NOT NULL DEFAULT 'front'` | Operational area: `'front'` (FOH) or `'back'` (BOH). |

---

### `picks`
Records an employee's ranked preference for a specific shift.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Unique preference identifier. |
| `user_id` | `BIGINT` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Employee identifier. |
| `shift_id` | `BIGINT` | `NOT NULL REFERENCES shifts(id) ON DELETE CASCADE` | Shift identifier. |
| `rank` | `INTEGER` | `NOT NULL` | Preference rank (`1` = highest choice). |

*Constraint:* `UNIQUE(user_id, shift_id)` guarantees one rank per shift per user.

---

### `coverage_preferences`
Stores the employee's yes/no willingness to cover each shift. Missing rows default to willing.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `user_id` | `BIGINT` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Employee identifier. |
| `shift_id` | `BIGINT` | `NOT NULL REFERENCES shifts(id) ON DELETE CASCADE` | Shift identifier. |
| `willing` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | `TRUE` = willing to cover; `FALSE` = opt out. |

*Constraint:* `PRIMARY KEY(user_id, shift_id)` keeps one coverage preference per employee and shift.

---

### `assignments`
Connects an employee to a shift slot and tracks its scheduling state.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Unique assignment identifier. |
| `shift_id` | `BIGINT` | `NOT NULL REFERENCES shifts(id) ON DELETE CASCADE` | Shift identifier. |
| `user_id` | `BIGINT` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Employee identifier. |
| `status` | `TEXT` | `NOT NULL DEFAULT 'proposed'` | Scheduling lifecycle state. |

*Constraint:* `UNIQUE(shift_id, user_id)` guarantees an employee cannot be double-assigned to the same shift.

#### Assignment Statuses
- `'proposed'`: Assigned by the auto-scheduler; subject to displacement or recalculation on rebuild.
- `'notified'`: Finalized automated assignment; notification sent to employee.
- `'confirmed'`: Explicitly confirmed assignment; preserved across auto-rebuilds.
- `'manager_fixed'`: Placed via manual manager override; permanently preserved across rebuilds.
- `'sick'`: Vacated due to sick call; row retained for tracking, slot opened for coverage.
- `'swap_requested'`: Employee requested coverage; slot available for coverage assignment.
- `'swap_invited'`: An employee→coworker swap invitation is pending on this shift; the holder keeps the shift (still counts as staffed and toward hours) until the invited coworker accepts.

---

### `requests`
Tracks employee absence, vacation, sick, swap, and switch requests.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Unique request identifier. |
| `user_id` | `BIGINT` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Employee identifier. |
| `kind` | `TEXT` | `NOT NULL` | Request category: `day_off`, `vacation`, `sick`, `swap`, `switch`, or `manager_unassign`. |
| `shift_id` | `BIGINT` | `REFERENCES shifts(id) ON DELETE SET NULL` | Target shift (sick shift, or shift being surrendered). |
| `day` | `TEXT` | `NULL` | Weekday for `day_off` request. |
| `target_shift_id` | `BIGINT` | `REFERENCES shifts(id) ON DELETE SET NULL` | Desired shift for `switch` request. |
| `target_user_id` | `BIGINT` | `REFERENCES users(id) ON DELETE SET NULL` | Coworker invited to a direct employee-to-employee swap. |
| `vacation_start` | `DATE` | `NULL` | Vacation start (inclusive). |
| `vacation_end` | `DATE` | `NULL` | Vacation end (inclusive). |
| `week_start` | `DATE` | `NULL` | Week for `day_off` request. |
| `reason` | `TEXT` | `NULL` | Decision or rejection explanation shown in request history. |
| `status` | `TEXT` | `NOT NULL DEFAULT 'approved'` | Request status: `pending`, `approved`, `approved_ok`, `denied`. |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Request submission timestamp. |

---

### `notifications`
In-app communication delivery queue.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Unique notification identifier. |
| `user_id` | `BIGINT` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Recipient identifier. |
| `shift_id` | `BIGINT` | `REFERENCES shifts(id) ON DELETE SET NULL` | Associated shift (if applicable). |
| `kind` | `TEXT` | `NOT NULL` | Notification category: `'assignment'` or `'conflict'`. |
| `message` | `TEXT` | `NOT NULL` | Text content of notification. |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Notification creation timestamp. |
| `read` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Read flag (`FALSE` = unread, `TRUE` = read). |
