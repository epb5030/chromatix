"""Dialogs: processing-method editor, shared-store connection, reason prompt."""

from __future__ import annotations

import dataclasses
import getpass
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QPushButton,
    QWidget,
)
from sqlalchemy import URL, make_url

from chromatix_core import IntegrationParams

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


class MethodDialog(QDialog):
    """Edit the integration parameters of a processing method."""

    def __init__(
        self, parent: QWidget | None, name: str, params: IntegrationParams, name_editable: bool
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Processing method")
        form = QFormLayout(self)
        self.name = QLineEdit(name)
        self.name.setReadOnly(not name_editable)
        form.addRow("Name", self.name)
        self.spins: dict[str, QDoubleSpinBox] = {}
        values = dataclasses.asdict(params)
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
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def params(self) -> IntegrationParams:
        values: dict[str, Any] = {name: spin.value() for name, spin in self.spins.items()}
        if self.auto_window.isChecked():
            values["smoothing_window"] = None
        return IntegrationParams(**values)


class SharedStoreDialog(QDialog):
    """Connection details for a shared PostgreSQL store.  The password is never saved."""

    def __init__(self, parent: QWidget | None, url: str = "", data_dir: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to shared store")
        form = QFormLayout(self)
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("postgresql://chromatix_app@dbhost/chromatix")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.data_dir = QLineEdit(data_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.data_dir)
        row.addWidget(browse)
        self.username = QLineEdit(getpass.getuser())
        form.addRow("Database URL", self.url)
        form.addRow("Database password", self.password)
        form.addRow("Raw-data folder", row)
        form.addRow("ChromaTix user", self.username)
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
        url = make_url(self.url.text().strip())
        if self.password.text():
            url = url.set(password=self.password.text())
        return url, Path(self.data_dir.text().strip()), self.username.text().strip()
