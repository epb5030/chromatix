# 0003 — Audit-first data model; SQLite now, PostgreSQL next

- Status: accepted (SQLite implemented; PostgreSQL planned)
- Date: 2026-10-29

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

## Status of PostgreSQL

Not implemented yet. `Store.create()` raises `NotImplementedError` for any
non-SQLite engine rather than ship untested triggers. Next steps: PL/pgSQL
trigger equivalents, database roles (the app role gets INSERT/SELECT only on the
append-only tables), and running the store test suite against a real PostgreSQL
in CI.

## Not yet covered (known gaps)

User authentication (the store trusts the username it is given), electronic
signatures and sign-off levels, time-zone and clock policy, and validation
documentation. These are needed before any GxP use.
