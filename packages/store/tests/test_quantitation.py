"""Calibration and quantitation through the store (SQLite and PostgreSQL)."""

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from chromatix_core import IntegrationParams
from chromatix_core.io import write_aia
from chromatix_core.quant import Component, ProcessingMethod
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram
from chromatix_store import ReasonRequiredError

# Response (peak height) per unit amount; area = height * sigma * sqrt(2 pi).
RESPONSE = {"Caffeine": 40.0, "Theobromine": 25.0}
RT = {"Caffeine": 2.0, "Theobromine": 4.0}
METHOD = ProcessingMethod(
    IntegrationParams(),
    (
        Component("Caffeine", 2.0, window=0.1, unit="ug/mL"),
        Component("Theobromine", 4.0, window=0.1, unit="ug/mL"),
    ),
)


def _write(path, amounts, seed):
    peaks = [GaussianPeak(RT[c], RESPONSE[c] * a, 0.05) for c, a in amounts.items() if a]
    write_aia(gaussian_chromatogram(peaks, offset=2.0, drift=0.5, noise=0.3, seed=seed), path)
    return path


@pytest.fixture
def lab(store, tmp_path):
    """A project with a 5-level standard series, two unknowns and a blank."""
    project = store.create_project("Assay")
    sample_set = store.create_sample_set(project, "SS-1")
    method = store.save_method_version(project, "Quant", METHOD)
    injections = {}
    levels = (0.5, 1.0, 2.0, 4.0, 8.0)
    for i, level in enumerate(levels):
        amounts = {"Caffeine": level, "Theobromine": level / 2}
        path = _write(tmp_path / f"std{i}.cdf", amounts, seed=i)
        injections[f"std{i}"] = store.add_injection(
            sample_set, f"Std {level}", store.import_raw_file(path), "standard", amounts=amounts
        )
    for name, amounts, seed in (
        ("unk1", {"Caffeine": 3.0, "Theobromine": 1.5}, 50),
        ("unk2", {"Caffeine": 6.5, "Theobromine": 0.8}, 51),
        ("blank", {}, 52),
    ):
        path = _write(tmp_path / f"{name}.cdf", amounts, seed=seed)
        injections[name] = store.add_injection(
            sample_set, name, store.import_raw_file(path), "blank" if name == "blank" else "unknown"
        )
    return {
        "store": store,
        "sample_set": sample_set,
        "method": method,
        "injections": injections,
        "tmp": tmp_path,
    }


def _amounts(store, result_id):
    return {p.component: p.amount for p in store.peaks(result_id) if p.component}


def test_calibrate_and_quantitate_sample_set(lab):
    store, inj = lab["store"], lab["injections"]
    out = store.process_sample_set(lab["sample_set"], lab["method"])
    assert out.skipped == {}
    assert set(out.calibrations) == {"Caffeine", "Theobromine"}
    assert set(out.results) == set(inj.values())

    cals = {c.component: c for c in store.calibrations(lab["sample_set"])}
    assert all(c.r_squared > 0.999 for c in cals.values())
    assert [len(c.points) for c in cals.values()] == [5, 5]

    unk1 = _amounts(store, out.results[inj["unk1"]])
    assert unk1["Caffeine"] == pytest.approx(3.0, rel=0.02)
    assert unk1["Theobromine"] == pytest.approx(1.5, rel=0.03)
    unk2 = _amounts(store, out.results[inj["unk2"]])
    assert unk2["Caffeine"] == pytest.approx(6.5, rel=0.02)
    # Standards are quantitated too (back-calculated), which shows curve fit quality.
    assert _amounts(store, out.results[inj["std3"]])["Caffeine"] == pytest.approx(4.0, rel=0.02)
    assert store.peaks(out.results[inj["blank"]]) == []
    for peak in store.peaks(out.results[inj["unk1"]]):
        if peak.component:
            assert peak.calibration_id == out.calibrations[peak.component]

    actions = [e.action for e in store.audit_trail()]
    assert actions.count("calibration.create") == 2
    assert actions.count("result.create") == len(inj)


def test_calibrations_are_append_only(lab, owner_engine):
    lab["store"].process_sample_set(lab["sample_set"], lab["method"])
    for statement in ("UPDATE calibration SET r_squared = 1", "DELETE FROM calibration"):
        with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as conn:
            conn.execute(text(statement))


def test_reprocessing_needs_reason_and_keeps_history(lab):
    store, ss, mv = lab["store"], lab["sample_set"], lab["method"]
    first = store.process_sample_set(ss, mv)
    with pytest.raises(ReasonRequiredError):
        store.process_sample_set(ss, mv)
    assert len(store.calibrations(ss)) == 2  # nothing written by the refused run
    second = store.process_sample_set(ss, mv, reason="re-run after review")
    assert len(store.calibrations(ss)) == 4
    current = store.current_curves(ss, mv)
    assert {name: cal_id for name, (cal_id, _) in current.items()} == second.calibrations
    assert first.calibrations != second.calibrations
    unk = lab["injections"]["unk1"]
    assert [r.version for r in store.results(unk)] == [1, 2]
    assert store.results(unk)[1].reason == "re-run after review"


def test_single_injection_uses_latest_calibration(lab):
    store = lab["store"]
    store.process_sample_set(lab["sample_set"], lab["method"])
    path = _write(lab["tmp"] / "late.cdf", {"Caffeine": 5.0, "Theobromine": 2.0}, seed=77)
    late = store.add_injection(lab["sample_set"], "late", store.import_raw_file(path))
    result = store.process_injection(late, lab["method"])
    amounts = _amounts(store, result)
    assert amounts["Caffeine"] == pytest.approx(5.0, rel=0.02)
    assert amounts["Theobromine"] == pytest.approx(2.0, rel=0.03)


def test_too_few_standards_are_skipped(store, tmp_path):
    project = store.create_project("P")
    ss = store.create_sample_set(project, "SS")
    mv = store.save_method_version(project, "Quant", METHOD)
    for i, level in enumerate((1.0, 2.0)):  # linear with intercept needs 3 points
        amounts = {"Caffeine": level}
        path = _write(tmp_path / f"s{i}.cdf", amounts, seed=i)
        store.add_injection(
            ss, f"Std {level}", store.import_raw_file(path), "standard", amounts=amounts
        )
    out = store.process_sample_set(ss, mv)
    assert set(out.skipped) == {"Caffeine", "Theobromine"}
    assert "at least" in out.skipped["Caffeine"]
    assert "no calibration points" in out.skipped["Theobromine"]
    assert out.calibrations == {}
    for result_id in out.results.values():
        peaks = store.peaks(result_id)
        assert [p.component for p in peaks] == ["Caffeine"]
        assert peaks[0].amount is None


def test_injection_records_amounts_and_type(lab):
    store = lab["store"]
    injection = store.injection(lab["injections"]["std2"])
    assert injection.sample_type == "standard"
    assert injection.component_amounts == {"Caffeine": 2.0, "Theobromine": 1.0}
    event = store.audit_trail("injection", injection.id)[0]
    assert event.details["component_amounts"] == {"Caffeine": 2.0, "Theobromine": 1.0}


def test_calibration_holds_sample_set_lock_through_writes(lab, backend, owner_engine):
    if backend != "postgresql":
        pytest.skip("PostgreSQL row lock")
    store, ss, mv = lab["store"], lab["sample_set"], lab["method"]
    locked_after = []

    def check_lock(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith("SELECT chromatix_save_calibration("):
            return
        with pytest.raises(DBAPIError) as raised, owner_engine.begin() as probe:
            # Compatible with the weaker KEY SHARE lock taken by the foreign key.
            probe.execute(
                text("SELECT id FROM sample_set WHERE id = :id FOR NO KEY UPDATE NOWAIT"),
                {"id": ss},
            )
        assert raised.value.orig.sqlstate == "55P03"
        locked_after.append(statement)

    event.listen(store.engine, "after_cursor_execute", check_lock)
    try:
        out = store.process_sample_set(ss, mv)
    finally:
        event.remove(store.engine, "after_cursor_execute", check_lock)
    assert len(locked_after) == len(out.calibrations) == 2
    with owner_engine.begin() as probe:
        assert (
            probe.execute(
                text("SELECT id FROM sample_set WHERE id = :id FOR UPDATE NOWAIT"), {"id": ss}
            ).scalar_one()
            == ss
        )
