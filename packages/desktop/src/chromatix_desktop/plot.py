"""Chromatogram plot, peak table and calibration view."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from chromatix_core import Chromatogram
from chromatix_store.models import Calibration
from chromatix_store.store import curve_of

PEAK_COLUMNS = (
    "#",
    "Component",
    "RT (min)",
    "Area",
    "Amount",
    "Height",
    "Width ½ (min)",
    "Type",
    "Start",
    "End",
)
TYPE_COLUMN = PEAK_COLUMNS.index("Type")
COMPONENT_COLUMN = PEAK_COLUMNS.index("Component")
AMOUNT_COLUMN = PEAK_COLUMNS.index("Amount")


class PeakLike(Protocol):
    """Fields shared by a freshly integrated ``Peak`` and a stored ``ResultPeak``."""

    @property
    def retention_time(self) -> float: ...
    @property
    def start_time(self) -> float: ...
    @property
    def end_time(self) -> float: ...
    @property
    def height(self) -> float: ...
    @property
    def area(self) -> float: ...
    @property
    def width_50(self) -> float | None: ...
    @property
    def boundary(self) -> str: ...
    @property
    def baseline_start(self) -> float: ...
    @property
    def baseline_end(self) -> float: ...


class ChromatogramPlot(pg.PlotWidget):
    def __init__(self) -> None:
        super().__init__()
        self.showGrid(x=True, y=True, alpha=0.2)
        self.setLabel("bottom", "Time", units="min")

    def display(self, chrom: Chromatogram | None, peaks: Sequence[PeakLike] = ()) -> None:
        self.clear()
        if chrom is None:
            return
        self.plot(chrom.time, chrom.signal, pen=pg.mkPen(width=1))
        self.setLabel("left", "Signal", units=chrom.signal_unit or None)
        baseline_pen = pg.mkPen("#e15759", width=1.5)
        drop_pen = pg.mkPen("#e15759", width=1, style=Qt.PenStyle.DashLine)
        for number, pk in enumerate(peaks, start=1):
            self.plot(
                [pk.start_time, pk.end_time], [pk.baseline_start, pk.baseline_end], pen=baseline_pen
            )
            for code, time_, base in (
                (pk.boundary[0], pk.start_time, pk.baseline_start),
                (pk.boundary[1], pk.end_time, pk.baseline_end),
            ):
                if code == "V":
                    top = float(np.interp(time_, chrom.time, chrom.signal))
                    self.plot([time_, time_], [base, top], pen=drop_pen)
            label = pg.TextItem(f"{number}\n{pk.retention_time:.2f}", anchor=(0.5, 1.0))
            label.setPos(pk.retention_time, pk.baseline_start + pk.height)
            self.addItem(label)
        low, high = float(chrom.signal.min()), float(chrom.signal.max())
        self.setXRange(float(chrom.time[0]), float(chrom.time[-1]), padding=0.01)
        self.setYRange(low, high + 0.15 * (high - low), padding=0.02)

    def zoom_to(self, start: float, end: float) -> None:
        margin = (end - start) * 0.5
        self.setXRange(start - margin, end + margin)


class PeakTable(QTableWidget):
    def __init__(self) -> None:
        super().__init__(0, len(PEAK_COLUMNS))
        self.setHorizontalHeaderLabels(PEAK_COLUMNS)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.peaks: list[PeakLike] = []

    def set_peaks(self, peaks: Sequence[PeakLike]) -> None:
        self.peaks = list(peaks)
        self.setRowCount(len(self.peaks))
        for row, pk in enumerate(self.peaks):
            width = pk.width_50
            # Stored result peaks carry identification; quick-look peaks do not.
            component = getattr(pk, "component", None)
            amount = getattr(pk, "amount", None)
            values = (
                str(row + 1),
                component or "",
                f"{pk.retention_time:.3f}",
                f"{pk.area:.4g}",
                "" if amount is None else f"{amount:.4g}",
                f"{pk.height:.4g}",
                "—" if width is None or math.isnan(width) else f"{width:.4f}",
                pk.boundary,
                f"{pk.start_time:.3f}",
                f"{pk.end_time:.3f}",
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col not in (COMPONENT_COLUMN, TYPE_COLUMN):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.setItem(row, col, item)
        self.resizeColumnsToContents()

    def selected_peak(self) -> PeakLike | None:
        rows = {index.row() for index in self.selectedIndexes()}
        return self.peaks[rows.pop()] if len(rows) == 1 else None


class CalibrationView(QWidget):
    """Calibration curve of one component: standard points, fitted curve, statistics."""

    def __init__(self) -> None:
        super().__init__()
        self.component = QComboBox()
        self.component.currentIndexChanged.connect(self._draw)
        self.summary = QLabel("No calibration for this sample set and method yet.")
        self.summary.setWordWrap(True)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("left", "Response (area)")
        top = QHBoxLayout()
        top.addWidget(QLabel("Component"))
        top.addWidget(self.component, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.plot, 1)
        self.calibrations: dict[str, Calibration] = {}
        self.units: dict[str, str] = {}

    def set_calibrations(self, calibrations: Sequence[Calibration], units: dict[str, str]) -> None:
        """Show the latest calibration per component."""
        current = self.component.currentText()
        self.calibrations = {c.component: c for c in calibrations}
        self.units = units
        self.component.blockSignals(True)
        self.component.clear()
        self.component.addItems(sorted(self.calibrations))
        index = self.component.findText(current)
        self.component.setCurrentIndex(max(index, 0))
        self.component.blockSignals(False)
        self._draw()

    def _draw(self, *_: object) -> None:
        self.plot.clear()
        cal = self.calibrations.get(self.component.currentText())
        if cal is None:
            self.summary.setText("No calibration for this sample set and method yet.")
            return
        curve = curve_of(cal)
        unit = self.units.get(cal.component, "")
        amounts = np.array([float(p["amount"]) for p in cal.points])
        responses = np.array([float(p["response"]) for p in cal.points])
        self.plot.plot(
            amounts, responses, pen=None, symbol="o", symbolSize=8, symbolBrush="#4e79a7"
        )
        upper = float(amounts.max()) * 1.1 if amounts.size else 1.0
        xs = np.linspace(0.0, upper, 200)
        self.plot.plot(xs, np.polyval(curve.coefficients, xs), pen=pg.mkPen("#e15759", width=1.5))
        self.plot.setLabel("bottom", f"Amount ({unit})" if unit else "Amount")
        terms = " ".join(
            f"{c:+.5g}·x^{len(curve.coefficients) - 1 - i}"
            for i, c in enumerate(curve.coefficients)
        )
        terms = terms.replace("·x^0", "").replace("x^1", "x")
        r2 = "—" if cal.r_squared is None else f"{cal.r_squared:.5f}"
        self.summary.setText(
            f"{cal.model}, weighting {cal.weighting}"
            f"{', through origin' if cal.through_origin else ''}:  y = {terms}   "
            f"R² = {r2}   n = {len(cal.points)}   "
            f"(calibration #{cal.id}, {cal.created_at:%Y-%m-%d %H:%M})"
        )
