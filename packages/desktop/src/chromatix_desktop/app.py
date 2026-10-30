"""ChromaTix desktop: browse a store, import and process injections, review results.

Native Qt widgets only (PySide6-Essentials, no QtWebEngine); plotting with
pyqtgraph.  Two ways to work:

* **Store mode** - open a personal store (SQLite file) or connect to a shared
  one (PostgreSQL).  Projects, sample sets and injections are browsed in a tree;
  results are versioned and every change lands in the audit trail.
* **Quick look** - File > Open file… shows and integrates a single AIA file
  without storing anything.

Every action is a plain method (``create_project``, ``import_files``,
``process_injection`` …) so it can be scripted and tested; menus and dialogs
only collect input for them.
"""

from __future__ import annotations

import functools
import getpass
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import URL
from sqlalchemy.exc import SQLAlchemyError

from chromatix_core import Chromatogram, IntegrationParams, integrate
from chromatix_core.io import read_aia
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram
from chromatix_desktop import dialogs
from chromatix_desktop.browser import ProjectTree
from chromatix_desktop.plot import ChromatogramPlot, PeakTable
from chromatix_store import (
    ProtectionMissingError,
    RawDataIntegrityError,
    ReasonRequiredError,
    Store,
    StoreVersionError,
)

AUDIT_COLUMNS = ("Time (UTC)", "User", "Action", "Entity", "ID", "Reason", "Details")
DEFAULT_METHOD = "Default"
_EXPECTED_ERRORS = (
    ReasonRequiredError,
    PermissionError,
    ProtectionMissingError,
    RawDataIntegrityError,
    StoreVersionError,
    SQLAlchemyError,
    OSError,
    ValueError,
    KeyError,
)


def _demo_chromatogram() -> Chromatogram:
    return gaussian_chromatogram(
        [
            GaussianPeak(2.0, 100, 0.05),
            GaussianPeak(4.0, 40, 0.04),
            GaussianPeak(5.0, 60, 0.06),
            GaussianPeak(5.25, 30, 0.06),
            GaussianPeak(7.5, 10, 0.08),
        ],
        offset=2.0,
        drift=0.5,
        noise=0.3,
    )


def guarded[**P, R](fn: Callable[P, R]) -> Callable[P, R | None]:
    """Show expected store/file errors in a message box instead of crashing."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return fn(*args, **kwargs)
        except _EXPECTED_ERRORS as exc:
            parent = args[0] if args and isinstance(args[0], QWidget) else None
            QMessageBox.critical(parent, "ChromaTix", f"{type(exc).__name__}: {exc}")
            return None

    return wrapper


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1300, 820)
        # INI format so the location can be redirected (tests use QSettings.setPath).
        self.settings = QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "ChromaTix", "ChromaTix"
        )
        self.store: Store | None = None
        self.store_label = ""
        self.injection_id: int | None = None
        self.chromatogram: Chromatogram | None = None

        self.plot = ChromatogramPlot()
        self.peak_table = PeakTable()
        self.peak_table.itemSelectionChanged.connect(self._zoom_to_selected)
        self.audit_table = QTableWidget(0, len(AUDIT_COLUMNS))
        self.audit_table.setHorizontalHeaderLabels(AUDIT_COLUMNS)
        self.audit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.peak_table, "Peaks")
        self.tabs.addTab(self.audit_table, "Audit trail")

        # Processing bar: method + process buttons + result version.
        self.method_combo = QComboBox()
        self.method_combo.setMinimumWidth(180)
        self.result_combo = QComboBox()
        self.result_combo.setMinimumWidth(320)
        self.result_combo.currentIndexChanged.connect(self._show_selected_result)
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Method"))
        method_row.addWidget(self.method_combo, 1)
        for label, slot in (
            ("Edit…", self._edit_method_dialog),
            ("New…", self._new_method_dialog),
            ("Process", self._process_injection_action),
            ("Process sample set", self._process_sample_set_action),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            method_row.addWidget(button)
        result_row = QHBoxLayout()
        result_row.addWidget(QLabel("Result"))
        result_row.addWidget(self.result_combo, 1)
        bar = QVBoxLayout()
        bar.setContentsMargins(4, 4, 4, 0)
        bar.addLayout(method_row)
        bar.addLayout(result_row)
        self.process_bar = QWidget()
        self.process_bar.setLayout(bar)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(self.process_bar)
        lower_layout.addWidget(self.tabs)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.plot)
        splitter.addWidget(lower)
        splitter.setSizes([500, 320])
        self.setCentralWidget(splitter)

        self.tree = ProjectTree()
        self.tree.currentItemChanged.connect(self._tree_selection_changed)
        dock = QDockWidget("Store", self)
        dock.setObjectName("store-dock")
        dock.setWidget(self.tree)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        self._build_menus()
        self._update_title()
        self._update_enabled()
        self.statusBar().showMessage(
            "Open a personal store, connect to a shared one, or open a file"
        )

    # ------------------------------------------------------------------ menus
    def _build_menus(self) -> None:
        def add(menu: Any, label: str, slot: Callable[[], object], shortcut: Any = None) -> QAction:
            action = QAction(label, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            menu.addAction(action)
            return action

        file_menu = self.menuBar().addMenu("&File")
        add(file_menu, "Open &personal store…", self._open_personal_dialog, "Ctrl+Shift+O")
        add(file_menu, "Connect to &shared store…", self._connect_shared_dialog)
        add(file_menu, "&Close store", self.close_store)
        file_menu.addSeparator()
        add(
            file_menu,
            "&Open file (quick look)…",
            self._open_file_dialog,
            QKeySequence.StandardKey.Open,
        )
        add(file_menu, "&Demo data", lambda: self.show_file(_demo_chromatogram(), "demo"))
        add(file_menu, "&Integrate (quick look)", self.integrate_quick_look, "Ctrl+I")
        file_menu.addSeparator()
        add(file_menu, "&Quit", self.close, QKeySequence.StandardKey.Quit)

        store_menu = self.menuBar().addMenu("&Store")
        self.store_actions = [
            add(store_menu, "New &project…", self._new_project_dialog),
            add(store_menu, "New &sample set…", self._new_sample_set_dialog),
            add(store_menu, "&Import AIA files into sample set…", self._import_dialog),
        ]

    # ------------------------------------------------------------ store setup
    @guarded
    def open_personal_store(self, path: str | Path, username: str | None = None) -> None:
        """Open or create a personal (SQLite) store.  The first user is created
        automatically on an empty store (recorded in the audit trail)."""
        username = username or getpass.getuser()
        store = Store.open_sqlite(path, username)
        if not store.users():
            Store.add_user(store.engine, username)
        elif username not in {u.username for u in store.users()}:
            raise PermissionError(f"user {username!r} has no account in this store")
        self.settings.setValue("personal_store", str(path))
        self._attach(store, f"{Path(path).name} (personal)")

    @guarded
    def connect_shared_store(self, url: URL | str, data_dir: str | Path, username: str) -> None:
        store = Store.open_postgres(url, data_dir, username)
        if username not in {u.username for u in store.users()}:
            raise PermissionError(f"user {username!r} has no account in this store")
        safe = store.engine.url.render_as_string(hide_password=True)
        self.settings.setValue("shared_url", store.engine.url.set(password=None).render_as_string())
        self.settings.setValue("shared_data_dir", str(data_dir))
        self._attach(store, f"{safe} (shared)")

    def close_store(self) -> None:
        if self.store is not None:
            self.store.engine.dispose()
        self.store = None
        self.store_label = ""
        self.injection_id = None
        self.tree.populate(None)
        self._clear_view()
        self._update_title()
        self._update_enabled()

    def _attach(self, store: Store, label: str) -> None:
        self.close_store()
        self.store = store
        self.store_label = label
        self.tree.populate(store)
        self.refresh_audit()
        self._update_title()
        self._update_enabled()
        self.statusBar().showMessage(f"Opened {label} as {store.username}")

    # ------------------------------------------------------------- store edits
    @guarded
    def create_project(self, name: str) -> int:
        store = self._require_store()
        project_id = store.create_project(name)
        store.save_method_version(project_id, DEFAULT_METHOD, IntegrationParams())
        self._refresh(("project", project_id))
        return project_id

    @guarded
    def create_sample_set(self, project_id: int, name: str) -> int:
        sample_set_id = self._require_store().create_sample_set(project_id, name)
        self._refresh(("sample_set", sample_set_id))
        return sample_set_id

    @guarded
    def import_files(self, sample_set_id: int, paths: Sequence[str | Path]) -> list[int]:
        """Import AIA files as new injections, named after the file's sample_name."""
        store = self._require_store()
        ids = []
        for path in map(Path, paths):
            sha = store.import_raw_file(path)
            chrom = store.load_chromatogram(sha)
            sample_name = str(chrom.metadata.get("sample_name") or path.stem)
            ids.append(store.add_injection(sample_set_id, sample_name, sha))
        self._refresh(("injection", ids[-1]) if ids else None)
        return ids

    @guarded
    def save_method(
        self, project_id: int, name: str, params: IntegrationParams, reason: str = ""
    ) -> int:
        version_id = self._require_store().save_method_version(project_id, name, params, reason)
        self._load_methods(project_id, select=version_id)
        self.refresh_audit()
        return version_id

    @guarded
    def process_injection(self, injection_id: int, method_version_id: int, reason: str = "") -> int:
        result_id = self._require_store().process_injection(injection_id, method_version_id, reason)
        self.refresh_audit()
        if injection_id == self.injection_id:
            self._load_results(select=result_id)
        return result_id

    # ------------------------------------------------------------- selection
    def select(self, key: tuple[str, int]) -> None:
        self.tree.select(key)

    def _tree_selection_changed(self, *_: object) -> None:
        context = self.tree.context()
        project_id = context.get("project")
        self._load_methods(project_id)
        self.injection_id = context.get("injection")
        if self.injection_id is None:
            self._clear_view()
        else:
            self._load_injection(self.injection_id)
        self._update_enabled()

    @guarded
    def _load_injection(self, injection_id: int) -> None:
        store = self._require_store()
        injection = store.injection(injection_id)
        self.chromatogram = store.load_chromatogram(injection.raw_file_sha256)
        self._load_results()
        self.statusBar().showMessage(
            f"{injection.sample_name}: {len(self.chromatogram)} points, raw file "
            f"{injection.raw_file_sha256[:12]}… verified"
        )

    def _load_methods(self, project_id: int | None, select: int | None = None) -> None:
        current = select if select is not None else self.method_combo.currentData()
        self.method_combo.blockSignals(True)
        self.method_combo.clear()
        if self.store is not None and project_id is not None:
            for method, version in self.store.methods(project_id):
                self.method_combo.addItem(f"{method.name} v{version.version}", version.id)
        index = self.method_combo.findData(current)
        self.method_combo.setCurrentIndex(max(index, 0))
        self.method_combo.blockSignals(False)

    def _load_results(self, select: int | None = None) -> None:
        store = self.store
        self.result_combo.blockSignals(True)
        self.result_combo.clear()
        if store is not None and self.injection_id is not None:
            for result in reversed(store.results(self.injection_id)):
                method, mv = store.method_version(result.method_version_id)
                label = (
                    f"v{result.version} · {method.name} v{mv.version} · "
                    f"{result.created_at:%Y-%m-%d %H:%M}"
                )
                if result.reason:
                    label += f" · {result.reason}"
                self.result_combo.addItem(label, result.id)
        index = self.result_combo.findData(select) if select is not None else 0
        self.result_combo.setCurrentIndex(max(index, 0))
        self.result_combo.blockSignals(False)
        self._show_selected_result()

    def _show_selected_result(self, *_: object) -> None:
        result_id = self.result_combo.currentData()
        peaks = self.store.peaks(result_id) if self.store and result_id is not None else []
        self.plot.display(self.chromatogram, peaks)
        self.peak_table.set_peaks(peaks)

    def refresh_audit(self) -> None:
        events = list(reversed(self.store.audit_trail())) if self.store else []
        self.audit_table.setRowCount(len(events))
        for row, event in enumerate(events):
            values = (
                f"{event.at:%Y-%m-%d %H:%M:%S}",
                event.username,
                event.action,
                event.entity,
                event.entity_id,
                event.reason,
                json.dumps(event.details, default=str) if event.details else "",
            )
            for col, value in enumerate(values):
                self.audit_table.setItem(row, col, QTableWidgetItem(value))
        self.audit_table.resizeColumnsToContents()

    # -------------------------------------------------------------- quick look
    @guarded
    def open_file(self, path: str | Path) -> None:
        self.show_file(read_aia(path), Path(path).name)

    def show_file(self, chrom: Chromatogram, name: str) -> None:
        self.tree.clearSelection()
        self.injection_id = None
        self.chromatogram = chrom
        self.result_combo.clear()
        self.plot.display(chrom)
        self.peak_table.set_peaks([])
        self.statusBar().showMessage(f"Quick look: {name} ({len(chrom)} points, not stored)")

    def integrate_quick_look(self) -> None:
        if self.chromatogram is None:
            return
        peaks = integrate(self.chromatogram)
        self.plot.display(self.chromatogram, peaks)
        self.peak_table.set_peaks(peaks)
        self.statusBar().showMessage(f"{len(peaks)} peaks (quick look, not stored)")

    # ---------------------------------------------------------------- dialogs
    def _open_personal_dialog(self) -> None:
        start = str(self.settings.value("personal_store", str(Path.home())))
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Open or create personal store",
            start,
            "ChromaTix store (*.sqlite)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            self.open_personal_store(Path(path).with_suffix(""))

    def _connect_shared_dialog(self) -> None:
        dialog = dialogs.SharedStoreDialog(
            self,
            str(self.settings.value("shared_url", "")),
            str(self.settings.value("shared_data_dir", "")),
        )
        if dialog.exec():
            self.connect_shared_store(*dialog.connection())

    def _open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open chromatogram", "", "AIA / ANDI (*.cdf *.CDF *.nc);;All files (*)"
        )
        if path:
            self.open_file(path)

    def _new_project_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "New project", "Project name")
        if ok and name.strip():
            self.create_project(name.strip())

    def _new_sample_set_dialog(self) -> None:
        project_id = self.tree.context().get("project")
        if project_id is None:
            QMessageBox.information(self, "New sample set", "Select a project first.")
            return
        name, ok = QInputDialog.getText(self, "New sample set", "Sample set name")
        if ok and name.strip():
            self.create_sample_set(project_id, name.strip())

    def _import_dialog(self) -> None:
        sample_set_id = self.tree.context().get("sample_set")
        if sample_set_id is None:
            QMessageBox.information(self, "Import", "Select a sample set first.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import AIA files", "", "AIA / ANDI (*.cdf *.CDF *.nc)"
        )
        if paths:
            self.import_files(sample_set_id, paths)

    def _edit_method_dialog(self) -> None:
        project_id = self.tree.context().get("project")
        version_id = self.method_combo.currentData()
        if self.store is None or project_id is None or version_id is None:
            return
        method, mv = self.store.method_version(version_id)
        dialog = dialogs.MethodDialog(self, method.name, IntegrationParams(**mv.params), False)
        if not dialog.exec():
            return
        reason = dialogs.ask_reason(self, "Change method", f"Reason for changing {method.name}:")
        if reason is not None:
            self.save_method(project_id, method.name, dialog.params(), reason)

    def _new_method_dialog(self) -> None:
        project_id = self.tree.context().get("project")
        if self.store is None or project_id is None:
            return
        dialog = dialogs.MethodDialog(self, "", IntegrationParams(), True)
        if dialog.exec() and dialog.name.text().strip():
            self.save_method(project_id, dialog.name.text().strip(), dialog.params())

    def _process_injection_action(self) -> None:
        version_id = self.method_combo.currentData()
        if self.store is None or self.injection_id is None or version_id is None:
            return
        reason = ""
        if self.store.results(self.injection_id):
            answer = dialogs.ask_reason(self, "Reprocess", "Reason for reprocessing:")
            if answer is None:
                return
            reason = answer
        self.process_injection(self.injection_id, version_id, reason)

    def _process_sample_set_action(self) -> None:
        sample_set_id = self.tree.context().get("sample_set")
        version_id = self.method_combo.currentData()
        if self.store is None or sample_set_id is None or version_id is None:
            return
        injections = self.store.injections(sample_set_id)
        reason = ""
        if any(self.store.results(i.id) for i in injections):
            answer = dialogs.ask_reason(
                self, "Reprocess sample set", "Some injections already have results. Reason:"
            )
            if answer is None:
                return
            reason = answer
        for injection in injections:
            needs_reason = bool(self.store.results(injection.id))
            self.process_injection(injection.id, version_id, reason if needs_reason else "")
        self.statusBar().showMessage(f"Processed {len(injections)} injections")

    # ----------------------------------------------------------------- helpers
    def _require_store(self) -> Store:
        if self.store is None:
            raise ValueError("no store is open")
        return self.store

    def _refresh(self, select: tuple[str, int] | None) -> None:
        self.tree.populate(self.store)
        if select is not None:
            self.tree.select(select)
        self.refresh_audit()

    def _clear_view(self) -> None:
        self.chromatogram = None
        self.result_combo.clear()
        self.plot.display(None)
        self.peak_table.set_peaks([])

    def _zoom_to_selected(self) -> None:
        peak = self.peak_table.selected_peak()
        if peak is not None:
            self.plot.zoom_to(peak.start_time, peak.end_time)

    def _update_title(self) -> None:
        self.setWindowTitle(f"ChromaTix — {self.store_label}" if self.store_label else "ChromaTix")

    def _update_enabled(self) -> None:
        has_store = self.store is not None
        for action in self.store_actions:
            action.setEnabled(has_store)
        self.process_bar.setEnabled(has_store and self.tree.context().get("project") is not None)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    pg.setConfigOptions(antialias=True)
    app = QApplication.instance() or QApplication(args)
    window = MainWindow()
    if len(args) > 1:
        target = Path(args[1])
        if target.suffix == ".sqlite":
            window.open_personal_store(target.with_suffix(""))
        else:
            window.open_file(target)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
