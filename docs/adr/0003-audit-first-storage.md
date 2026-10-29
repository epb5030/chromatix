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

### Roles

A shared deployment uses two PostgreSQL roles (`chromatix-admin`,
`chromatix_store.admin`):

- **Owner.** Owns the database, tables and triggers; runs `init-postgres` and
  `add-user`. It does not need to be a superuser: owning the database plus
  CREATEROLE (to create the app role) is enough, and this setup was checked with
  such a role. Its credentials are for administrators only, because an owner can
  drop triggers.
- **Application role.** What clients connect as. It gets SELECT on everything,
  INSERT on the data tables, SELECT only on `user_account`, and USAGE on the
  ID sequences. It has no UPDATE, DELETE or TRUNCATE privilege and owns
  nothing, so it cannot ALTER or DROP tables or triggers. It also cannot CREATE
  in the schema (revoked from PUBLIC as well).
- **Start-up check.** `Store.open_postgres()` refuses to run
  (`ProtectionMissingError`) unless every table exists and every append-only
  trigger is present and enabled.

Tests cover each of these against PostgreSQL 17, including attempts by the app
role to update, delete, truncate, drop or disable triggers, alter tables,
create tables and add users.

## Not yet covered (known gaps)

- **Direct inserts with the app credentials.** Someone holding the application
  role's password could connect outside ChromaTix and INSERT rows directly,
  including made-up audit events. They still cannot change or delete anything.
  Closing this needs writes to go through SECURITY DEFINER functions that write
  their own audit rows, or through a server that holds the credentials. Until
  then, the application password should be held by the deployment, not by end
  users.
- **User authentication.** The store trusts the username it is given.
- **Electronic signatures and sign-off levels.**
- **Clock policy** (trusted time source) and **validation documentation.**

All of these are needed before any GxP use.
