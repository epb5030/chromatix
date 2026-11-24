# ChromaTix

An open-source chromatography data system (CDS) for Linux: a native desktop app
with a tested processing engine and an audit-first data model.

> **Status: early development (restarted 2026-10-29).** Not validated. Do not use
> for GxP or regulated work.

## What works today

- **`chromatix-core`**: reads AIA/ANDI files (ASTM E1947, the usual
  vendor-neutral export from Empower, Chromeleon and OpenLab). Detects and
  integrates peaks with baseline (`BB`) and drop-line (`BV`/`VB`/`VV`) splits, and
  fits linear or quadratic calibrations with optional 1/x or 1/x² weighting.
  Processing methods carry a **component table**: expected retention time,
  window, calibration model, weighting and unit. Peaks are identified as the
  closest match within each window. Accuracy is tested against synthetic
  chromatograms whose true areas are known exactly (see
  `packages/core/tests/test_peaks_accuracy.py`).
- **`chromatix-store`**: Project → Sample Set → Injection; content-addressed,
  read-only raw files checked against their hash on every load; versioned
  processing methods and results; and an append-only audit trail enforced by
  database triggers. SQLite for one user, PostgreSQL for a shared lab.
  - **Calibration and quantitation.** Injections record a sample type and, for
    standards, known component amounts. Processing a sample set integrates
    every injection, fits one curve per component from the standards, and
    writes the curves and every quantitated result in one transaction.
    Calibrations are append-only like results. A single injection processed
    later uses the latest curves of its sample set.
- **`chromatix-desktop`**: a native Qt app with no embedded browser engine.
  - Open a personal store (SQLite file) or connect to a shared one
    (PostgreSQL). The password is asked for and never saved.
  - Browse projects → sample sets → injections, and import AIA files into a
    sample set. The import table sets each file's sample name, sample type and
    standard amounts.
  - Edit processing methods: integration settings and components. Each saved
    change is a new version, and a reason is required after the first.
  - Process one injection or a whole sample set (calibrate and quantitate).
    Reprocessing asks for a reason and keeps every earlier result version
    viewable.
  - Peak table with component and amount columns. The Calibration tab shows
    each component's standard points, fitted curve, equation, R² and n.
  - Review the audit trail.
  - **Quick look** (File → Open file) integrates a single file without storing
    anything.

## Run it

```bash
uv sync --all-packages
uv run chromatix                 # then File → Open personal store…
uv run chromatix ~/lab.sqlite    # open a personal store directly (created if missing)
uv run chromatix run.cdf         # quick look at one file
uv run pytest                    # all tests (GUI test runs offscreen)
# PostgreSQL store tests: point at any server you can create databases on
CHROMATIX_TEST_POSTGRES_URL=postgresql://user:pass@localhost:5432/postgres uv run pytest
uv run ruff check . && uv run mypy
uv run chromatix-admin new-migration "describe the change"   # after changing models.py
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

Stores are versioned. A personal store upgrades itself when opened, after
saving a backup copy next to it. A shared store is upgraded by its owner with
`chromatix-admin init-postgres`. Use `chromatix-admin status --url …` to see a
store's version.

## Shared lab database (PostgreSQL)

Run these as an **owner** role that owns the database and has CREATEROLE. It
doesn't need to be a superuser.

Each person then logs in with **their own** PostgreSQL role, which is a member
of a client group. The group can read data and call ChromaTix's write
functions, nothing else. Every change is recorded under the login that made it,
and nothing can be edited or deleted afterwards.

```bash
createdb -O lab_owner chromatix
uv run chromatix-admin init-postgres --url postgresql://lab_owner@dbhost/chromatix \
    --group-role chromatix_users --create-role
CHROMATIX_USER_PASSWORD=... uv run chromatix-admin add-user \
    --url postgresql://lab_owner@dbhost/chromatix --group-role chromatix_users \
    --username jdoe --full-name "J. Doe"
```

- **Passwords:** pass a new user's password through `CHROMATIX_USER_PASSWORD` or
  the prompt, never on the command line. If the login role already exists (for
  example from LDAP), `add-user` just adds it to the group.
- **Desktop:** in the app, *Connect to shared store* asks for your login and
  password.
- **Raw data:** lives in a directory every client can reach (an NFS or SMB
  share).

The details and known gaps are in [ADR 0003](docs/adr/0003-audit-first-storage.md).

## Design decisions

See [`docs/adr/`](docs/adr/):

1. [Python for the core](docs/adr/0001-python-core.md)
2. [Native Qt, no web engine](docs/adr/0002-native-qt-no-web-engine.md)
3. [Audit-first storage](docs/adr/0003-audit-first-storage.md)

## Layout

```
packages/core/      chromatix-core     file I/O, peaks, calibration (pure functions)
packages/store/     chromatix-store    schema, raw-data store, audit trail
packages/desktop/   chromatix-desktop  PySide6 + pyqtgraph app
docs/adr/           architecture decision records
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
