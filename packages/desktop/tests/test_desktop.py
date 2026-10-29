import importlib.util
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from chromatix_core import IntegrationParams
from chromatix_core.io import write_aia
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram
from chromatix_desktop import dialogs
from chromatix_desktop.app import MainWindow
from chromatix_desktop.plot import TYPE_COLUMN

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
    assert [r.reason for r in store.results(injections[1])] == [""]

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


def test_shared_dialog_uses_login_as_user(qapp, tmp_path):
    dialog = dialogs.SharedStoreDialog(None, "postgresql://dbhost:5432/chromatix", str(tmp_path))
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
