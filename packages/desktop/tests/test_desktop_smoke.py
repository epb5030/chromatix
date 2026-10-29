import importlib.util
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from chromatix_core.io import write_aia
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram
from chromatix_desktop.app import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_no_web_engine_installed():
    """ADR 0002: the desktop app must not ship a browser engine."""
    assert importlib.util.find_spec("PySide6.QtWebEngineCore") is None
    assert importlib.util.find_spec("PySide6.QtWebEngineWidgets") is None


def test_open_and_integrate(app, tmp_path):
    path = tmp_path / "run.cdf"
    write_aia(
        gaussian_chromatogram(
            [
                GaussianPeak(2.0, 100, 0.05),
                GaussianPeak(5.0, 60, 0.06),
                GaussianPeak(5.25, 30, 0.06),
            ],
            noise=0.3,
        ),
        path,
    )
    window = MainWindow()
    window.open_file(path)
    window.integrate()
    assert window.table.rowCount() == 3
    assert [window.table.item(r, 5).text() for r in range(3)] == ["BB", "BV", "VB"]
    window.table.selectRow(1)
    window.close()
