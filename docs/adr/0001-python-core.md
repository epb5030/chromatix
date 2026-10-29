# 0001 — Python for the processing core and application

- Status: accepted
- Date: 2026-10-29

## Context

ChromaTix was restarted from scratch after an audit of the first scaffold
(gitlab.com/epb5030/chromatix, June 2026). That code was never wired end to end:
peak areas came out 35–45 % low, `integrate()` crashed, the AIA reader rejected
standard ASTM E1947 files, and a NumPy 2 change broke it on a fresh install.
None of these failures came from the language. They came from code that was
never run against known answers.

Rust and Go were considered.

## Decision

Python (3.12+) for the processing engine (`chromatix-core`), the storage layer
(`chromatix-store`) and the desktop app (`chromatix-desktop`).

- The numerical building blocks already exist and are well tested: NumPy and
  SciPy (Savitzky–Golay, peak finding, least squares), netCDF4 for AIA files.
  In Rust some of these would have to be written; in Go almost all of them.
- Chromatograms are small (10⁴–10⁵ points, one dimension), so NumPy is fast
  enough. Large MS data sets are the only place Python is likely to be slow.
- The maintainer and most scientist contributors already work in Python.

Go is rejected: no scientific computing ecosystem.

Rust is kept for two specific cases, if profiling or reliability calls for it:
1. Hot loops in binary vendor parsers or MS processing, exposed to Python with
   PyO3/maturin. To keep this option open, `chromatix-core` is written as pure
   functions (arrays in, results out) with no framework inside it.
2. A long-running acquisition service (instrument control) in a later phase.

## Consequences

- Dependencies are locked (`uv.lock`), with lower bounds in each package.
  CI runs the accuracy tests on every change.
- Type checking with `mypy --strict` covers the core and store packages.
