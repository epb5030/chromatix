"""Dialogs: processing-method editor, import details, shared-store connection, reason prompt."""

from __future__ import annotations

import dataclasses
import getpass
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import URL, make_url

from chromatix_core import IntegrationParams
from chromatix_core.quant import Component
from chromatix_core.quant import ProcessingMethod as QuantMethod

# Per-field spin-box settings: (minimum, maximum, step, decimals, label).
_PARAM_UI: dict[str, tuple[float, float, float, int, str]] = {
    "threshold_snr": (1.0, 1000.0, 1.0, 1, "Detection threshold (× noise)"),
    "smoothing_window": (0.001, 5.0, 0.005, 3, "Smoothing window (min)"),
    "smoothing_fraction": (0.05, 2.0, 0.05, 2, "Auto window (× narrowest FWHM)"),
    "boundary_extension": (0.0, 5.0, 0.25, 2, "Boundary extension (× σ)"),
    "slope_sensitivity": (0.5, 20.0, 0.5, 1, "Slope sensitivity (× slope noise)"),
    "valley_snr": (0.5, 100.0, 0.5, 1, "Valley threshold (× noise)"),
    "min_area": (0.0, 1e9, 0.1, 4, "Minimum area"),
    "min_height": (0.0, 1e9, 0.1, 4, "Minimum height"),
}


def ask_reason(parent: QWidget, title: str, prompt: str) -> str | None:
    """Ask for a non-empty reason; ``None`` if the user cancels."""
    while True:
        text, ok = QInputDialog.getText(parent, title, prompt)
        if not ok:
            return None
        if text.strip():
            return text.strip()


COMPONENT_COLUMNS = ("Name", "RT (min)", "Window ± (min)", "Model", "Weighting", "Origin", "Unit")
SAMPLE_TYPES = ("unknown", "standard", "blank", "control")


class MethodDialog(QDialog):
    """Edit a processing method: integration parameters and the component table."""

    def __init__(
        self, parent: QWidget | None, name: str, method: QuantMethod, name_editable: bool
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Processing method")
        self.resize(720, 460)
        outer = QVBoxLayout(self)
        top = QFormLayout()
        self.name = QLineEdit(name)
        self.name.setReadOnly(not name_editable)
        top.addRow("Name", self.name)
        outer.addLayout(top)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        integration_page = QWidget()
        form = QFormLayout(integration_page)
        tabs.addTab(integration_page, "Integration")
        self.spins: dict[str, QDoubleSpinBox] = {}
        values = dataclasses.asdict(method.integration)
        for field in dataclasses.fields(IntegrationParams):
            low, high, step, decimals, label = _PARAM_UI[field.name]
            spin = QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
            value = values[field.name]
            if field.name == "smoothing_window":
                self.auto_window = QCheckBox("automatic")
                self.auto_window.setChecked(value is None)
                spin.setEnabled(value is not None)
                spin.setValue(value if value is not None else 0.03)
                self.auto_window.toggled.connect(lambda on, s=spin: s.setEnabled(not on))
                row = QHBoxLayout()
                row.addWidget(spin)
                row.addWidget(self.auto_window)
                form.addRow(label, row)
            else:
                spin.setValue(float(value))
                form.addRow(label, spin)
            self.spins[field.name] = spin

        components_page = QWidget()
        page = QVBoxLayout(components_page)
        tabs.addTab(components_page, "Components")
        self.components = QTableWidget(0, len(COMPONENT_COLUMNS))
        self.components.setHorizontalHeaderLabels(COMPONENT_COLUMNS)
        self.components.horizontalHeader().setStretchLastSection(True)
        page.addWidget(self.components)
        row_buttons = QHBoxLayout()
        add = QPushButton("Add component")
        add.clicked.connect(lambda: self.add_component(Component("New", 1.0)))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_selected)
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        row_buttons.addStretch(1)
        page.addLayout(row_buttons)
        for component in method.components:
            self.add_component(component)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def add_component(self, component: Component) -> None:
        row = self.components.rowCount()
        self.components.insertRow(row)
        self.components.setItem(row, 0, QTableWidgetItem(component.name))
        for col, (value, step, decimals) in enumerate(
            ((component.retention_time, 0.01, 3), (component.window, 0.01, 3)), start=1
        ):
            spin = QDoubleSpinBox()
            spin.setRange(0.001, 1000.0)
            spin.setDecimals(decimals)
            spin.setSingleStep(step)
            spin.setValue(value)
            self.components.setCellWidget(row, col, spin)
        for col, choices, current in (
            (3, ("linear", "quadratic"), component.model),
            (4, ("none", "1/x", "1/x^2"), component.weighting),
        ):
            combo = QComboBox()
            combo.addItems(choices)
            combo.setCurrentText(current)
            self.components.setCellWidget(row, col, combo)
        origin = QCheckBox()
        origin.setChecked(component.through_origin)
        self.components.setCellWidget(row, 5, origin)
        self.components.setItem(row, 6, QTableWidgetItem(component.unit))
        self.components.resizeColumnsToContents()

    def _remove_selected(self) -> None:
        for row in sorted({i.row() for i in self.components.selectedIndexes()}, reverse=True):
            self.components.removeRow(row)

    def method(self) -> QuantMethod:
        """The edited method.  Raises ValueError for invalid components."""
        values: dict[str, Any] = {name: spin.value() for name, spin in self.spins.items()}
        if self.auto_window.isChecked():
            values["smoothing_window"] = None
        table = self.components
        components = []
        for row in range(table.rowCount()):
            name_item, unit_item = table.item(row, 0), table.item(row, 6)
            rt, window = (table.cellWidget(row, c) for c in (1, 2))
            model, weighting = (table.cellWidget(row, c) for c in (3, 4))
            origin = table.cellWidget(row, 5)
            assert isinstance(rt, QDoubleSpinBox) and isinstance(window, QDoubleSpinBox)
            assert isinstance(model, QComboBox) and isinstance(weighting, QComboBox)
            assert isinstance(origin, QCheckBox)
            components.append(
                Component(
                    name=(name_item.text() if name_item else "").strip(),
                    retention_time=rt.value(),
                    window=window.value(),
                    model=model.currentText(),  # type: ignore[arg-type]
                    weighting=weighting.currentText(),  # type: ignore[arg-type]
                    through_origin=origin.isChecked(),
                    unit=(unit_item.text() if unit_item else "").strip(),
                )
            )
        return QuantMethod(IntegrationParams(**values), tuple(components))


class ImportDialog(QDialog):
    """Per-file sample name, sample type and known component amounts (for standards)."""

    def __init__(
        self,
        parent: QWidget | None,
        files: Sequence[tuple[Path, str]],
        components: Sequence[Component],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import AIA files")
        self.resize(760, 400)
        self.components = list(components)
        columns = ["File", "Sample name", "Type"] + [
            f"{c.name} ({c.unit})" if c.unit else c.name for c in self.components
        ]
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Enter known amounts for standards; leave blank for unknowns. "
            "Injections cannot be edited after import."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(len(files), len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.paths = [path for path, _ in files]
        for row, (path, sample_name) in enumerate(files):
            file_item = QTableWidgetItem(path.name)
            file_item.setFlags(file_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, file_item)
            self.table.setItem(row, 1, QTableWidgetItem(sample_name))
            kind = QComboBox()
            kind.addItems(SAMPLE_TYPES)
            if "std" in sample_name.lower() or "standard" in sample_name.lower():
                kind.setCurrentText("standard")
            self.table.setCellWidget(row, 2, kind)
            for col in range(3, len(columns)):
                self.table.setItem(row, col, QTableWidgetItem(""))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def details(self) -> list[dict[str, Any]]:
        """One dict per file: sample_name, sample_type, amounts.  Raises ValueError on a
        non-numeric amount."""
        out = []
        for row, path in enumerate(self.paths):
            name_item = self.table.item(row, 1)
            kind = self.table.cellWidget(row, 2)
            assert isinstance(kind, QComboBox)
            amounts: dict[str, float] = {}
            for offset, component in enumerate(self.components):
                item = self.table.item(row, 3 + offset)
                text = item.text().strip() if item else ""
                if text:
                    try:
                        amounts[component.name] = float(text)
                    except ValueError:
                        raise ValueError(
                            f"{path.name}: amount for {component.name} is not a number: {text!r}"
                        ) from None
            out.append(
                {
                    "sample_name": (name_item.text() if name_item else "").strip() or path.stem,
                    "sample_type": kind.currentText(),
                    "amounts": amounts or None,
                }
            )
        return out


class SharedStoreDialog(QDialog):
    """Connection details for a shared PostgreSQL store.  The password is never saved."""

    def __init__(self, parent: QWidget | None, url: str = "", data_dir: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to shared store")
        form = QFormLayout(self)
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("postgresql://dbhost/chromatix")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.data_dir = QLineEdit(data_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.data_dir)
        row.addWidget(browse)
        # Each ChromaTix user logs in with their own database role of the same name.
        self.username = QLineEdit(getpass.getuser())
        form.addRow("Database URL", self.url)
        form.addRow("Your login", self.username)
        form.addRow("Password", self.password)
        form.addRow("Raw-data folder", row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Raw-data folder", self.data_dir.text())
        if folder:
            self.data_dir.setText(folder)

    def connection(self) -> tuple[URL, Path, str]:
        """(URL with login and password, raw-data folder, ChromaTix username = login)."""
        url = make_url(self.url.text().strip())
        login = self.username.text().strip() or url.username or ""
        url = url.set(username=login)
        if self.password.text():
            url = url.set(password=self.password.text())
        return url, Path(self.data_dir.text().strip()), login
