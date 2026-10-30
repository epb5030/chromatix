import importlib.util
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from chromatix_core import IntegrationParams
from chromatix_core.io import write_aia
from chromatix_core.quant import Component, ProcessingMethod
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram
from chromatix_desktop import dialogs
from chromatix_desktop.app import MainWindow
from chromatix_desktop.plot import AMOUNT_COLUMN, COMPONENT_COLUMN, TYPE_COLUMN

pytestmark = pytest.mark.gui

PEAKS = [GaussianPeak(2.0, 100, 0.05), GaussianPeak(5.0, 60, 0.06), GaussianPeak(5.25, 30, 0.06)]


@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path_factory.mktemp("settings")),
    )
    return QApplication.instance() or QApplication([])


@pytest.fixture
def parent(qapp):
    """Parent widget for dialogs, destroyed while the QApplication still exists.

    Unparented dialogs (with widgets inside table cells) otherwise get torn down
    during interpreter exit, after QApplication, which crashes intermittently.
    """
    widget = QWidget()
    yield widget
    shiboken6.delete(widget)


@pytest.fixture
def errors(monkeypatch):
    """Capture error boxes instead of showing them."""
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda parent, title, text: shown.append(text))
    )
    return shown


@pytest.fixture
def reasons(monkeypatch):
    """Queue answers for the reason prompt (None = cancel)."""
    queue: list[str | None] = []
    monkeypatch.setattr(dialogs, "ask_reason", lambda *args: queue.pop(0))
    return queue


@pytest.fixture
def window(qapp):
    w = MainWindow()
    yield w
    w.close_store()
    w.close()


@pytest.fixture
def aia_files(tmp_path):
    paths = []
    for i, seed in enumerate((1, 2)):
        chrom = gaussian_chromatogram(PEAKS, offset=2, drift=0.5, noise=0.3, seed=seed)
        if i == 0:
            chrom.metadata["sample_name"] = "Std 1"
        path = tmp_path / f"inj{i + 1}.cdf"
        write_aia(chrom, path)
        paths.append(path)
    return paths


def test_no_web_engine_installed():
    """ADR 0002: the desktop app must not ship a browser engine."""
    assert importlib.util.find_spec("PySide6.QtWebEngineCore") is None
    assert importlib.util.find_spec("PySide6.QtWebEngineWidgets") is None


def test_quick_look(window, aia_files):
    window.open_file(aia_files[0])
    window.integrate_quick_look()
    table = window.peak_table
    assert table.rowCount() == 3
    assert [table.item(r, TYPE_COLUMN).text() for r in range(3)] == ["BB", "BV", "VB"]


def test_store_workflow(window, tmp_path, aia_files, reasons, errors):
    window.open_personal_store(tmp_path / "lab", "tester")
    assert window.store is not None
    project = window.create_project("Assay dev")
    assert window.method_combo.currentText() == "Default v1"
    sample_set = window.create_sample_set(project, "SS-001")
    injections = window.import_files(sample_set, aia_files)
    assert len(injections) == 2
    store = window.store
    assert [i.sample_name for i in store.injections(sample_set)] == ["Std 1", "inj2"]

    window.select(("injection", injections[0]))
    assert window.injection_id == injections[0]
    assert window.chromatogram is not None
    assert window.result_combo.count() == 0

    window._process_injection_action()  # first result: no reason asked
    assert window.result_combo.count() == 1
    assert window.peak_table.rowCount() == 3

    reasons.append("baseline check")
    window._process_injection_action()  # reprocessing asks for a reason
    assert window.result_combo.count() == 2
    assert window.result_combo.currentText().startswith("v2")
    assert "baseline check" in window.result_combo.currentText()

    # Older versions stay viewable.
    window.result_combo.setCurrentIndex(1)
    assert window.result_combo.currentText().startswith("v1")
    assert window.peak_table.rowCount() == 3

    reasons.append("batch rerun")
    window.select(("sample_set", sample_set))
    window._process_sample_set_action()
    assert [len(store.results(i)) for i in injections] == [3, 1]
    # One processing run, one reason: it is recorded on every result it writes.
    assert [r.reason for r in store.results(injections[1])] == ["batch rerun"]

    actions = [window.audit_table.item(r, 2).text() for r in range(window.audit_table.rowCount())]
    assert actions[0] == "result.create"  # newest first
    assert actions[-1] == "user.create"
    assert actions.count("result.create") == 4
    assert errors == []


def test_cancelled_reason_does_not_reprocess(window, tmp_path, aia_files, reasons):
    window.open_personal_store(tmp_path / "lab", "tester")
    project = window.create_project("P")
    sample_set = window.create_sample_set(project, "SS")
    (injection,) = window.import_files(sample_set, aia_files[:1])
    window.select(("injection", injection))
    window._process_injection_action()
    reasons.append(None)
    window._process_injection_action()
    assert len(window.store.results(injection)) == 1


def test_method_versions(window, tmp_path, aia_files, errors):
    window.open_personal_store(tmp_path / "lab", "tester")
    project = window.create_project("P")
    window.select(("project", project))
    window.save_method(project, "Default", IntegrationParams(threshold_snr=25), reason="raise")
    assert window.method_combo.currentText() == "Default v2"
    window.save_method(project, "Default", IntegrationParams(threshold_snr=30))
    assert any("reason" in e for e in errors)  # ReasonRequiredError shown, not raised
    assert window.method_combo.currentText() == "Default v2"


def test_unknown_user_is_refused(window, tmp_path, errors):
    window.open_personal_store(tmp_path / "lab", "tester")
    window.close_store()
    window.open_personal_store(tmp_path / "lab", "intruder")
    assert window.store is None
    assert any("intruder" in e for e in errors)


def test_tampered_raw_file_is_reported(window, tmp_path, aia_files, errors):
    window.open_personal_store(tmp_path / "lab", "tester")
    project = window.create_project("P")
    sample_set = window.create_sample_set(project, "SS")
    (injection,) = window.import_files(sample_set, aia_files[:1])
    sha = window.store.injection(injection).raw_file_sha256
    stored = window.store.data_dir / "objects" / sha[:2] / sha
    stored.chmod(0o644)
    with stored.open("r+b") as f:
        f.seek(-8, os.SEEK_END)
        f.write(b"\x00" * 8)
    window.select(("sample_set", sample_set))
    window.select(("injection", injection))
    assert any("RawDataIntegrityError" in e for e in errors)


def test_shared_dialog_uses_login_as_user(parent, tmp_path):
    dialog = dialogs.SharedStoreDialog(parent, "postgresql://dbhost:5432/chromatix", str(tmp_path))
    dialog.username.setText("jdoe")
    dialog.password.setText("s3cret")
    url, data_dir, username = dialog.connection()
    assert (url.username, url.password, url.host, url.database) == (
        "jdoe",
        "s3cret",
        "dbhost",
        "chromatix",
    )
    assert username == "jdoe"
    assert data_dir == tmp_path


QUANT = ProcessingMethod(
    IntegrationParams(),
    (
        Component("Caffeine", 2.0, window=0.1, unit="ug/mL"),
        Component("Theobromine", 5.0, window=0.1, unit="ug/mL"),
    ),
)


def _quant_file(path, caffeine, theobromine, seed):
    peaks = [
        GaussianPeak(2.0, 40 * caffeine, 0.05),
        GaussianPeak(5.0, 25 * theobromine, 0.06),
    ]
    write_aia(gaussian_chromatogram(peaks, offset=2, drift=0.5, noise=0.3, seed=seed), path)
    return path


def test_calibration_workflow(window, tmp_path, errors):
    window.open_personal_store(tmp_path / "lab", "tester")
    project = window.create_project("Assay")
    window.select(("project", project))
    method = window.save_method(project, "Quant", QUANT)
    sample_set = window.create_sample_set(project, "SS")
    paths, details = [], []
    for i, level in enumerate((1.0, 2.0, 4.0, 8.0)):
        paths.append(_quant_file(tmp_path / f"std{i}.cdf", level, level / 2, seed=i))
        details.append(
            {
                "sample_name": f"Std {level}",
                "sample_type": "standard",
                "amounts": {"Caffeine": level, "Theobromine": level / 2},
            }
        )
    paths.append(_quant_file(tmp_path / "unk.cdf", 3.0, 1.5, seed=40))
    details.append({"sample_name": "Sample A", "sample_type": "unknown", "amounts": None})
    injections = window.import_files(sample_set, paths, details)
    assert errors == []

    window.select(("sample_set", sample_set))
    window.method_combo.setCurrentIndex(window.method_combo.findData(method))
    window._process_sample_set_action()
    assert errors == []
    assert "calibrated Caffeine, Theobromine" in window.statusBar().currentMessage()

    window.select(("injection", injections[-1]))
    table = window.peak_table
    by_component = {
        table.item(r, COMPONENT_COLUMN).text(): float(table.item(r, AMOUNT_COLUMN).text())
        for r in range(table.rowCount())
        if table.item(r, COMPONENT_COLUMN).text()
    }
    assert by_component["Caffeine"] == pytest.approx(3.0, rel=0.03)
    assert by_component["Theobromine"] == pytest.approx(1.5, rel=0.03)

    view = window.calibration_view
    assert [view.component.itemText(i) for i in range(view.component.count())] == [
        "Caffeine",
        "Theobromine",
    ]
    summary = view.summary.text()
    r_squared = float(summary.split("R² = ")[1].split()[0])
    assert r_squared >= 0.999
    assert "n = 4" in summary


def test_method_dialog_round_trip(parent):
    dialog = dialogs.MethodDialog(parent, "Quant", QUANT, False)
    assert dialog.method() == QUANT
    dialog.add_component(Component("Caffeine", 3.0))
    with pytest.raises(ValueError, match="unique"):
        dialog.method()


def test_import_dialog_details(parent, tmp_path):
    files = [(tmp_path / "a.cdf", "Std 1"), (tmp_path / "b.cdf", "Sample")]
    dialog = dialogs.ImportDialog(parent, files, QUANT.components)
    dialog.table.item(0, 3).setText("1.5")
    details = dialog.details()
    assert details[0] == {
        "sample_name": "Std 1",
        "sample_type": "standard",
        "amounts": {"Caffeine": 1.5},
    }
    assert details[1]["sample_type"] == "unknown"
    assert details[1]["amounts"] is None
    dialog.table.item(1, 4).setText("lots")
    with pytest.raises(ValueError, match="not a number"):
        dialog.details()
