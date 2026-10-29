# 0002 — Native Qt desktop app, no embedded browser engine

- Status: accepted
- Date: 2026-10-29

## Context

ChromaTix must run natively on Linux and must not depend on Chromium or any
other embedded browser engine. The first scaffold planned a React web front end
with an optional Tauri wrapper. Tauri still renders through a system webview
(WebKitGTK on Linux), so it does not meet the requirement either.

## Decision

The user interface is a native Qt 6 application:

- **PySide6-Essentials** (LGPL, the official Qt for Python). We depend on the
  *Essentials* wheel, not the `PySide6` meta-package, because the Addons
  wheel brings in QtWebEngine, which is Chromium. A test asserts that
  `PySide6.QtWebEngineCore` cannot be imported.
- **pyqtgraph** for chromatogram plots: fast zoom and pan on long traces, and
  draggable items for manual peak boundaries and baselines later on.
- Runs on Wayland (including Hyprland) and X11 through Qt's own platform plugins.

Alternatives rejected:

| Option | Reason |
|---|---|
| Electron / Tauri / any webview | Embedded browser engine |
| GTK4 + libadwaita | Built around GNOME; weaker scientific plotting |
| Dear PyGui / ImGui | Non-standard controls, poor accessibility |
| Rust UI toolkits | Would only make sense with an all-Rust stack (see 0001) |

## Consequences

- The desktop app calls `chromatix-core` and `chromatix-store` in-process, so
  no web API is needed for single-user work. A headless server mode for
  multi-user work can come later as an option, not a requirement.
- Packaging targets native Linux packages: an AUR package first, then Flatpak
  or AppImage. During development: `uv run chromatix`.
