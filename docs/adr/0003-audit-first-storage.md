# 0003 — Audit-first data model on SQLite and PostgreSQL

- Status: accepted (SQLite and PostgreSQL implemented, with owner/application roles)
- Date: 2026-10-29 (PostgreSQL added the same day)

## Context

The first scaffold left compliance for "Phase 4". In practice, audit trails,
versioned results and immutable raw data define the data model, so adding them
later means rewriting it. Waters Empower is the reference for what users and
inspectors expect (see the Empower3 knowledge graph in the maintainer's notes).

## Decision

The schema follows Empower's object model and enforces data integrity in the
database itself:

- **Project → Sample Set → Injection → raw data file.**
- **Raw files are content-addressed.** They are stored once under their SHA-256,
  written read-only, and re-hashed every time they are loaded. A mismatch raises
  `RawDataIntegrityError` (the equivalent of Empower's project integrity check).
- **Processing methods are versioned.** Each saved change is a new, immutable
  `processing_method_version`, and a reason is required after the first version.
- **Results are versioned, never overwritten.** Reprocessing creates a new
  `result` version that records the exact method version used; earlier versions
  are kept. Reprocessing requires a reason.
- **Audit trail.** Every change writes an `audit_event` (user, action, entity,
  reason, details) in the same transaction.
- **Append-only tables.** Database triggers reject `UPDATE` and `DELETE` on
  `raw_file`, `processing_method_version`, `result`, `result_peak` and
  `audit_event`, no matter which code talks to the database.
- **Two tiers, like Empower Personal vs Workgroup.** SQLite plus a local
  raw-data directory for one user. PostgreSQL plus shared storage for a lab,
  with the same schema.

## PostgreSQL

`Store.open_postgres(url, data_dir, username)` uses the psycopg 3 driver.

- **Triggers.** The PL/pgSQL function `chromatix_append_only()` backs a
  `BEFORE UPDATE OR DELETE … FOR EACH ROW` trigger and a `BEFORE TRUNCATE`
  trigger on every append-only table. TRUNCATE has no SQLite equivalent.
- **Types.** Timestamps are `timestamptz`, always written in UTC. JSON fields
  are `jsonb`.
- **Raw data** for a shared store lives in `data_dir`, which must be storage
  every client can reach (NFS or SMB). Its integrity still rests on the SHA-256
  check at load time.
- **Tests.** The whole store suite runs against SQLite and a real PostgreSQL 17,
  with a throwaway database per test. CI uses a `postgres:17` service container
  and sets `CHROMATIX_REQUIRE_POSTGRES=1`, so a missing server fails the build
  instead of skipping the tests.

Concurrency: version numbers are allocated as `max + 1` inside the
transaction. Two clients saving the same method or reprocessing the same
injection at once will collide on the unique constraint and one gets an error;
nothing is overwritten. Retry or locking can be added if that becomes an issue.

### Why no Redis

A task queue, cache or message bus is not needed. Background work runs in the
desktop app's worker threads. If a shared server comes later, PostgreSQL itself
can serve as the job queue (`SELECT … FOR UPDATE SKIP LOCKED`) and notify other
clients (`LISTEN/NOTIFY`), inside the same transaction as the audit entry. An
in-memory store adds a system to validate and back up, and cannot be the record
of truth. Revisit only if live acquisition needs high-rate streaming across
machines, and even then raw data must reach disk first.

### Roles and write path

A shared deployment uses three kinds of PostgreSQL role, managed with
`chromatix-admin` (`chromatix_store.admin`):

- **Owner.** Owns the database, tables, triggers and write functions; runs
  `init-postgres` and `add-user`. It does not need to be a superuser: owning the
  database plus CREATEROLE is enough, and this setup was checked with such a
  role. Its credentials are for administrators only, because an owner can drop
  triggers.
- **Client group** (NOLOGIN, e.g. `chromatix_users`). It gets SELECT on every
  table and EXECUTE on the six write functions, and nothing else: no INSERT,
  UPDATE, DELETE or TRUNCATE on any table, no ownership (so no ALTER or DROP),
  and no CREATE in the schema. Re-running `init-postgres` also revokes INSERT
  left over from the earlier single-app-role setup.
- **Personal login per user.** Each ChromaTix user has a login role of the same
  name that belongs to the group. PostgreSQL authenticates the person (password,
  or LDAP/Kerberos configured on the server). `add-user` either creates the
  login or adds an existing one, such as an LDAP-provisioned role, to the group.

**All writes go through SECURITY DEFINER functions**
(`chromatix_store.pg_functions`):

- `create_project`, `create_sample_set`, `register_raw_file`, `add_injection`,
  `save_method_version` and `save_result`.
- Each function takes the user from `session_user`, enforces the reason rules
  (SQLSTATE `CXR01`), numbers versions under a row lock, and writes the data row
  together with its audit event.
- The audit helper itself cannot be called by clients, and EXECUTE is revoked
  from PUBLIC.
- So on a shared store there is no way to add data without its audit entry,
  add an audit entry without data, or record an action under someone else's
  name.
- The SQLite (personal) store keeps the equivalent Python write path. The same
  test suite runs on both backends, so they cannot drift apart.

**Start-up check.** `Store.open_postgres()` refuses to run
(`ProtectionMissingError`) unless every table exists, every append-only trigger
is present and enabled, and every write function exists as SECURITY DEFINER.
It also refuses a ChromaTix username that differs from the database login.

Tests cover all of this against PostgreSQL 17. They include 13 tampering
attempts by a user (direct INSERT into data or audit tables, UPDATE, DELETE,
TRUNCATE, calling the internal audit helper, dropping or disabling triggers or
functions, creating tables), a login outside the group, the database rejecting
a reprocess without a reason when the function is called directly, and upgrading
from the INSERT-based grants.

## Not yet covered (known gaps)

- **Result content is computed on the client.** The database guarantees who
  saved a result, when, with which method version and reason, but not that the
  peaks really came from integrating the raw file. A user with a database login
  could call `save_result` with numbers of their own. They would be recorded
  under their own name, and earlier versions still cannot be changed. Closing
  this needs integration to run server-side.
- **Personal stores (SQLite)** have no authentication. The file belongs to one
  operating-system user, and the store trusts the name it is given.
- **Electronic signatures and sign-off levels.**
- **Clock policy** (trusted time source) and **validation documentation.**

All of these are needed before any GxP use.
