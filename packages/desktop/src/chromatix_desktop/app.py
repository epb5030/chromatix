"""ChromaTix desktop: open a chromatogram, integrate it, inspect the peaks.

Native Qt widgets only (PySide6-Essentials, no QtWebEngine); plotting with
pyqtgraph.  This first window works on single files through chromatix-core;
project/sample-set browsing on top of chromatix-store comes next.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
)

from chromatix_core import Chromatogram, IntegrationParams, Peak, estimate_noise, integrate
from chromatix_core.io import read_aia
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram

PEAK_COLUMNS = ("#", "RT (min)", "Area", "Height", "Width ½ (min)", "Type", "Start", "End")


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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ChromaTix")
        self.resize(1200, 760)
        self.chromatogram: Chromatogram | None = None
        self.peaks: list[Peak] = []
        self.params = IntegrationParams()

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "Time", units="min")
        self.plot.setMouseEnabled(x=True, y=True)
        self.table = QTableWidget(0, len(PEAK_COLUMNS))
        self.table.setHorizontalHeaderLabels(PEAK_COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self._zoom_to_selected)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.plot)
        splitter.addWidget(self.table)
        splitter.setSizes([520, 240])
        self.setCentralWidget(splitter)
        self._build_actions()
        self.statusBar().showMessage("Open an AIA (.cdf) file or load demo data")

    def _build_actions(self) -> None:
        toolbar = self.addToolBar("Main")
        menu = self.menuBar().addMenu("&File")
        for label, shortcut, slot in (
            ("&Open…", QKeySequence.StandardKey.Open, self._open_dialog),
            ("&Demo data", None, lambda: self.set_chromatogram(_demo_chromatogram(), "demo")),
            ("&Integrate", "Ctrl+I", self.integrate),
        ):
            action = QAction(label, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            menu.addAction(action)
            toolbar.addAction(action)
        menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

    # ------------------------------------------------------------------ data
    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open chromatogram", "", "AIA / ANDI (*.cdf *.CDF *.nc);;All files (*)"
        )
        if path:
            self.open_file(Path(path))

    def open_file(self, path: Path) -> None:
        try:
            chrom = read_aia(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Cannot open file", f"{path}\n\n{exc}")
            return
        self.set_chromatogram(chrom, path.name)

    def set_chromatogram(self, chrom: Chromatogram, name: str) -> None:
        self.chromatogram = chrom
        self.peaks = []
        self.table.setRowCount(0)
        self.plot.clear()
        self.plot.plot(chrom.time, chrom.signal, pen=pg.mkPen(width=1))
        self.plot.setLabel("left", "Signal", units=chrom.signal_unit or None)
        self.plot.autoRange()
        noise = estimate_noise(chrom.signal)
        self.setWindowTitle(f"ChromaTix — {name}")
        self.statusBar().showMessage(
            f"{name}: {len(chrom)} points, {chrom.time[0]:.2f}–{chrom.time[-1]:.2f} min, "
            f"noise ≈ {noise:.3g} {chrom.signal_unit}"
        )

    # -------------------------------------------------------------- analysis
    def integrate(self) -> None:
        if self.chromatogram is None:
            return
        chrom = self.chromatogram
        self.peaks = integrate(chrom, self.params)
        self.plot.clear()
        self.plot.plot(chrom.time, chrom.signal, pen=pg.mkPen(width=1))
        baseline_pen = pg.mkPen("#e15759", width=1.5)
        drop_pen = pg.mkPen("#e15759", width=1, style=Qt.PenStyle.DashLine)
        for number, pk in enumerate(self.peaks, start=1):
            self.plot.plot(
                [pk.start_time, pk.end_time], [pk.baseline_start, pk.baseline_end], pen=baseline_pen
            )
            for code, time_, base, idx in (
                (pk.boundary[0], pk.start_time, pk.baseline_start, pk.start_index),
                (pk.boundary[1], pk.end_time, pk.baseline_end, pk.end_index),
            ):
                if code == "V":
                    self.plot.plot([time_, time_], [base, chrom.signal[idx]], pen=drop_pen)
            label = pg.TextItem(f"{number}\n{pk.retention_time:.2f}", anchor=(0.5, 1.0))
            label.setPos(pk.retention_time, pk.baseline_start + pk.height)
            self.plot.addItem(label)
        # Leave headroom so the apex labels are not clipped.
        low, high = float(chrom.signal.min()), float(chrom.signal.max())
        self.plot.setYRange(low, high + 0.15 * (high - low), padding=0.02)
        self._fill_table()
        self.statusBar().showMessage(f"{len(self.peaks)} peaks integrated")

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.peaks))
        for row, pk in enumerate(self.peaks):
            values = (
                str(row + 1),
                f"{pk.retention_time:.3f}",
                f"{pk.area:.4g}",
                f"{pk.height:.4g}",
                "—" if math.isnan(pk.width_50) else f"{pk.width_50:.4f}",
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
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()

    def _zoom_to_selected(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        if len(rows) != 1:
            return
        pk = self.peaks[rows.pop()]
        margin = (pk.end_time - pk.start_time) * 0.5
        self.plot.setXRange(pk.start_time - margin, pk.end_time + margin)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    pg.setConfigOptions(antialias=True)
    app = QApplication.instance() or QApplication(args)
    window = MainWindow()
    if len(args) > 1:
        window.open_file(Path(args[1]))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
