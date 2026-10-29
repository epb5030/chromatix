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
  Accuracy is tested against synthetic chromatograms whose true areas are known
  exactly (see `packages/core/tests/test_peaks_accuracy.py`).
- **`chromatix-store`**: Project → Sample Set → Injection; content-addressed,
  read-only raw files checked against their hash on every load; versioned
  processing methods and results; and an append-only audit trail enforced by
  database triggers. SQLite now, PostgreSQL planned.
- **`chromatix-desktop`**: a native Qt window. Open a file (or demo data),
  integrate it, and inspect baselines, drop lines and the peak table. No embedded
  browser engine.

## Run it

```bash
uv sync --all-packages
uv run chromatix                 # or: uv run chromatix path/to/file.cdf
uv run pytest                    # all tests (GUI test runs offscreen)
uv run ruff check . && uv run mypy
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

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
