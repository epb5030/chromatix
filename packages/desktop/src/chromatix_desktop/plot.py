"""Chromatogram plot and peak table, shared by single-file and store modes."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

from chromatix_core import Chromatogram

PEAK_COLUMNS = ("#", "RT (min)", "Area", "Height", "Width ½ (min)", "Type", "Start", "End")


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
            values = (
                str(row + 1),
                f"{pk.retention_time:.3f}",
                f"{pk.area:.4g}",
                f"{pk.height:.4g}",
                "—" if width is None or math.isnan(width) else f"{width:.4f}",
                pk.boundary,
                f"{pk.start_time:.3f}",
                f"{pk.end_time:.3f}",
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col != 5:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.setItem(row, col, item)
        self.resizeColumnsToContents()

    def selected_peak(self) -> PeakLike | None:
        rows = {index.row() for index in self.selectedIndexes()}
        return self.peaks[rows.pop()] if len(rows) == 1 else None
