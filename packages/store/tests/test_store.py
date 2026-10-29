import os
import stat

import pytest
from sqlalchemy import create_mock_engine, text
from sqlalchemy.exc import DBAPIError

from chromatix_core import IntegrationParams
from chromatix_core.io import write_aia
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram
from chromatix_store import RawDataIntegrityError, ReasonRequiredError, Store

PEAKS = [GaussianPeak(2.0, 100, 0.05), GaussianPeak(4.0, 40, 0.04), GaussianPeak(7.5, 10, 0.08)]


@pytest.fixture
def aia_file(tmp_path):
    path = tmp_path / "inj1.cdf"
    write_aia(gaussian_chromatogram(PEAKS, offset=2, drift=0.5, noise=0.3, seed=1), path)
    return path


def _setup_injection(store, aia_file):
    project = store.create_project("Assay dev")
    sample_set = store.create_sample_set(project, "SS-001")
    sha = store.import_raw_file(aia_file)
    injection = store.add_injection(sample_set, "Std 1", sha, sample_type="standard")
    method = store.save_method_version(project, "Default", IntegrationParams())
    return project, injection, method


def test_raw_file_is_content_addressed_and_read_only(store, aia_file):
    sha = store.import_raw_file(aia_file)
    stored = store.data_dir / "objects" / sha[:2] / sha
    assert stored.exists()
    assert not stored.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert store.import_raw_file(aia_file) == sha  # same bytes, same object
    imports = [e for e in store.audit_trail("raw_file") if e.action == "raw_file.import"]
    assert len(imports) == 1


def test_tampered_raw_file_is_detected(store, aia_file):
    sha = store.import_raw_file(aia_file)
    stored = store.data_dir / "objects" / sha[:2] / sha
    os.chmod(stored, 0o644)
    with stored.open("r+b") as f:
        f.seek(-8, os.SEEK_END)
        f.write(b"\x00" * 8)
    with pytest.raises(RawDataIntegrityError):
        store.load_chromatogram(sha)


def test_process_and_reprocess_keeps_history(store, aia_file):
    project, injection, method_v1 = _setup_injection(store, aia_file)

    first = store.process_injection(injection, method_v1)
    peaks_v1 = store.peaks(first)
    assert [round(p.retention_time, 1) for p in peaks_v1] == [2.0, 4.0, 7.5]
    assert peaks_v1[0].area == pytest.approx(PEAKS[0].area, rel=0.011)

    with pytest.raises(ReasonRequiredError):
        store.save_method_version(project, "Default", IntegrationParams(threshold_snr=20))
    method_v2 = store.save_method_version(
        project, "Default", IntegrationParams(threshold_snr=20), reason="raise threshold"
    )
    with pytest.raises(ReasonRequiredError):
        store.process_injection(injection, method_v2)
    second = store.process_injection(injection, method_v2, reason="method v2")

    versions = store.results(injection)
    assert [(r.version, r.method_version_id) for r in versions] == [(1, method_v1), (2, method_v2)]
    # Version 1 is untouched by reprocessing.
    assert [p.area for p in store.peaks(first)] == [p.area for p in peaks_v1]
    assert second != first

    actions = [e.action for e in store.audit_trail()]
    assert actions == [
        "user.create",
        "project.create",
        "sample_set.create",
        "raw_file.import",
        "injection.create",
        "method.save",
        "result.create",
        "method.save",
        "result.create",
    ]
    reasons = [e.reason for e in store.audit_trail("result")]
    assert reasons == ["", "method v2"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_event SET username = 'someone'",
        "DELETE FROM audit_event",
        "UPDATE result_peak SET area = area * 2",
        "DELETE FROM result",
        "UPDATE processing_method_version SET params = '{}'",
        "DELETE FROM raw_file",
    ],
)
def test_append_only_tables_reject_changes(store, owner_engine, aia_file, statement):
    """The triggers stop even the owner (clients are already stopped by privileges)."""
    _, injection, method = _setup_injection(store, aia_file)
    store.process_injection(injection, method)
    with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as conn:
        conn.execute(text(statement))


def test_truncate_is_rejected_on_postgres(store, owner_engine, backend):
    if backend != "postgresql":
        pytest.skip("SQLite has no TRUNCATE")
    with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_event CASCADE"))


def test_reinitialising_is_idempotent(store, owner_engine):
    store.create_project("P")
    Store.initialize(owner_engine)
    assert [e.action for e in store.audit_trail()] == ["user.create", "project.create"]
    assert store.create_project("Q") > 0


def test_unknown_user_cannot_write(make_store):
    s = make_store("nobody", create_account=False)
    with pytest.raises(PermissionError):
        s.create_project("x")


def test_audit_records_the_acting_user(store):
    store.create_project("P")
    assert {e.username for e in store.audit_trail("project")} == {store.username}


def test_unsupported_database_is_refused(tmp_path):
    engine = create_mock_engine("mysql://", executor=lambda *args, **kwargs: None)
    with pytest.raises(NotImplementedError, match="SQLite or PostgreSQL"):
        Store.create(engine, tmp_path, "analyst")


def test_read_api(store, aia_file):
    project, injection, method_v1 = _setup_injection(store, aia_file)
    other = store.create_project("Another")
    method_v2 = store.save_method_version(
        project, "Default", IntegrationParams(threshold_snr=20), reason="tighter"
    )
    store.save_method_version(project, "Fast", IntegrationParams())
    result = store.process_injection(injection, method_v2)

    assert [p.name for p in store.projects()] == ["Another", "Assay dev"]
    assert [u.username for u in store.users()] == [store.username]
    (sample_set,) = store.sample_sets(project)
    assert store.sample_sets(other) == []
    assert [i.sample_name for i in store.injections(sample_set.id)] == ["Std 1"]
    latest = {m.name: v.version for m, v in store.methods(project)}
    assert latest == {"Default": 2, "Fast": 1}
    method, mv = store.method_version(method_v2)
    assert (method.name, mv.version, mv.params["threshold_snr"]) == ("Default", 2, 20)
    assert [v.id for v in store.method_versions(method.id)] == [method_v1, method_v2]
    peak = store.peaks(result)[0]
    # The synthetic baseline is 2 + 0.5 t (+ noise).
    assert peak.baseline_start == pytest.approx(2.0 + 0.5 * peak.start_time, abs=0.5)
    assert [e.action for e in store.audit_trail("result", result)] == ["result.create"]
