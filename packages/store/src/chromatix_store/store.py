"""The storage API used by the desktop app (and later a server)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import (
    URL,
    Connection,
    Engine,
    Select,
    create_engine,
    event,
    func,
    make_url,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from chromatix_core import CalibrationCurve, Chromatogram, IntegrationParams, Peak, integrate
from chromatix_core.io import read_aia
from chromatix_core.quant import CalibrationPoint, Component, calibrate, identify
from chromatix_core.quant import ProcessingMethod as QuantMethod
from chromatix_store import migrate, pg_functions
from chromatix_store.models import (
    APPEND_ONLY_TABLES,
    AuditEvent,
    Base,
    Calibration,
    Injection,
    ProcessingMethod,
    ProcessingMethodVersion,
    Project,
    RawFile,
    Result,
    ResultPeak,
    SampleSet,
    User,
)

_READERS = {"aia": read_aia}
_SUFFIX_FORMAT = {".cdf": "aia", ".nc": "aia"}


class RawDataIntegrityError(RuntimeError):
    """A stored raw file no longer matches the hash recorded at import."""


class ReasonRequiredError(ValueError):
    """Changing existing data (e.g. reprocessing) needs a documented reason."""


class ProtectionMissingError(RuntimeError):
    """The database is uninitialised, or its append-only triggers are missing or disabled."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()


class Store:
    """A ChromaTix database plus its content-addressed raw-data directory.

    Every mutating call runs in one transaction together with its audit-trail
    entry, attributed to ``username``.
    """

    def __init__(self, engine: Engine, data_dir: Path, username: str) -> None:
        self.engine = engine
        self.data_dir = data_dir
        self.username = username

    # ------------------------------------------------------------------ setup
    @classmethod
    def open_sqlite(cls, path: str | os.PathLike[str], username: str) -> Store:
        """Open (or create) a personal store: ``<path>.sqlite`` + ``<path>.rawdata/``.

        An existing store that needs a schema upgrade is copied to
        ``<path>.sqlite.bak-<revision>-<UTC time>`` first.
        """
        base = Path(path)
        db_file = base.with_suffix(".sqlite")
        engine = create_engine(f"sqlite:///{db_file}")
        if db_file.exists() and migrate.needs_upgrade(engine):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            revision = migrate.current_revision(engine) or "unversioned"
            shutil.copy2(db_file, db_file.with_name(f"{db_file.name}.bak-{revision}-{stamp}"))
        return cls.create(engine, base.with_suffix(".rawdata"), username)

    @classmethod
    def open_postgres(
        cls,
        url: str | URL,
        data_dir: str | os.PathLike[str],
        username: str,
        *,
        initialize: bool = False,
    ) -> Store:
        """Open a shared store in PostgreSQL.

        By default this only connects, and refuses to run unless the schema and
        every append-only trigger are in place (see :meth:`verify_protection`).
        The application should connect as a restricted role (see
        :mod:`chromatix_store.admin`); ``initialize=True`` creates the schema and
        must be run by the owner role.

        ``data_dir`` holds the raw files and must be storage every client can
        reach (e.g. an NFS or SMB share).  The psycopg 3 driver is used whatever
        driver the URL names.
        """
        engine = create_engine(make_url(url).set(drivername="postgresql+psycopg"))
        if initialize:
            cls.initialize(engine)
        else:
            cls.verify_protection(engine)
        with engine.connect() as conn:
            session_user = conn.scalar(text("SELECT session_user"))
        if session_user != username:
            engine.dispose()
            raise PermissionError(
                f"connected to PostgreSQL as {session_user!r} but working as {username!r}; "
                "each ChromaTix user logs in with their own database role"
            )
        path = Path(data_dir)
        (path / "objects").mkdir(parents=True, exist_ok=True)
        return cls(engine, path, username)

    @classmethod
    def create(cls, engine: Engine, data_dir: Path, username: str) -> Store:
        """Initialise the schema (idempotent) and open the store."""
        cls.initialize(engine)
        (data_dir / "objects").mkdir(parents=True, exist_ok=True)
        return cls(engine, data_dir, username)

    @staticmethod
    def initialize(engine: Engine) -> None:
        """Create or upgrade the schema to the current revision (see :mod:`.migrate`).

        Tables, append-only triggers and PostgreSQL write functions all come from
        the migrations.  Safe to run again.
        """
        dialect = engine.dialect.name
        if dialect not in ("sqlite", "postgresql"):
            raise NotImplementedError(
                f"unsupported database {dialect!r}: use SQLite or PostgreSQL (docs/adr/0003)"
            )
        if dialect == "sqlite":
            event.listen(engine, "connect", _sqlite_pragmas)
        migrate.upgrade(engine)

    @staticmethod
    def verify_protection(engine: Engine) -> None:
        """Refuse a shared store unless it is at this version's schema revision, every
        table exists, every append-only trigger is enabled and every write function
        is present (PostgreSQL)."""
        current, head = migrate.current_revision(engine), migrate.head_revision()
        if current is not None and current != head:
            raise migrate.StoreVersionError(
                f"store schema is at revision {current}, this ChromaTix expects {head}; "
                "the owner must run `chromatix-admin init-postgres` (or update ChromaTix)"
            )
        with engine.connect() as conn:
            tables = set(
                conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                )
            )
            missing_tables = sorted(set(Base.metadata.tables) - tables)
            if missing_tables:
                raise ProtectionMissingError(
                    f"database is not initialised (missing tables: {', '.join(missing_tables)}); "
                    "run `chromatix-admin init-postgres` as the owner role"
                )
            enabled = set(
                conn.scalars(
                    text(
                        "SELECT t.tgname FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = current_schema() AND NOT t.tgisinternal "
                        "AND t.tgenabled <> 'D'"
                    )
                )
            )
            definer = set(
                conn.scalars(
                    text(
                        "SELECT p.proname FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = current_schema() AND p.prosecdef"
                    )
                )
            )
        functions = {
            sig.split("(")[0]
            for sig in pg_functions.PUBLIC_FUNCTIONS + pg_functions.INTERNAL_FUNCTIONS
        }
        missing_functions = sorted(functions - definer)
        if missing_functions:
            raise ProtectionMissingError(
                f"write functions are missing: {', '.join(missing_functions)}; "
                "run `chromatix-admin init-postgres` as the owner role"
            )
        expected = {
            f"{table}_{suffix}"
            for table in APPEND_ONLY_TABLES
            for suffix in ("append_only", "no_truncate")
        }
        missing = sorted(expected - enabled)
        if missing:
            raise ProtectionMissingError(
                f"append-only protection is missing or disabled: {', '.join(missing)}"
            )

    # ---------------------------------------------------------------- helpers
    @contextmanager
    def _tx(self) -> Iterator[tuple[Session, User]]:
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            user = session.scalar(select(User).where(User.username == self.username))
            if user is None or not user.active:
                raise PermissionError(f"unknown or inactive user {self.username!r}")
            yield session, user

    def _audit(
        self,
        session: Session,
        action: str,
        entity: str,
        entity_id: object,
        reason: str = "",
        **details: Any,
    ) -> None:
        session.add(
            AuditEvent(
                username=self.username,
                action=action,
                entity=entity,
                entity_id=str(entity_id),
                reason=reason,
                details=details,
            )
        )

    def _object_path(self, sha256: str) -> Path:
        return self.data_dir / "objects" / sha256[:2] / sha256

    @property
    def _pg(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    @contextmanager
    def _pg_tx(self) -> Iterator[Connection]:
        """A PostgreSQL transaction whose write-function errors are mapped back to the
        same exceptions the SQLite path raises (CXR01 reason, CXU01 unknown user)."""
        try:
            with self.engine.begin() as conn:
                yield conn
        except DBAPIError as exc:
            orig = exc.orig
            code = getattr(orig, "sqlstate", None)
            diag = getattr(orig, "diag", None)
            message = getattr(diag, "message_primary", None) or str(orig)
            if code == "CXR01":
                raise ReasonRequiredError(message) from exc
            if code == "CXU01":
                raise PermissionError(message) from exc
            raise

    def _call(self, sql: str, **params: Any) -> Any:
        """Run one PostgreSQL write function in its own transaction."""
        with self._pg_tx() as conn:
            return conn.execute(text(sql), params).scalar()

    @contextmanager
    def _writer(self) -> Iterator[_Writer]:
        """One transaction for several result/calibration writes, on either backend."""
        if self._pg:
            with self._pg_tx() as conn:
                yield _PgWriter(conn)
        else:
            with self._tx() as (session, user):
                yield _SqliteWriter(self, session, user)

    # ------------------------------------------------------------------ users
    @staticmethod
    def add_user(engine: Engine, username: str, full_name: str = "") -> None:
        """Bootstrap a user account (recorded in the audit trail as 'system')."""
        with Session(engine) as session, session.begin():
            session.add(User(username=username, full_name=full_name))
            session.add(
                AuditEvent(
                    username="system",
                    action="user.create",
                    entity="user",
                    entity_id=username,
                    details={"full_name": full_name},
                )
            )

    # -------------------------------------------------------------- projects
    def create_project(self, name: str, description: str = "") -> int:
        if self._pg:
            return int(self._call("SELECT chromatix_create_project(:n, :d)", n=name, d=description))
        with self._tx() as (s, user):
            project = Project(name=name, description=description, created_by=user.id)
            s.add(project)
            s.flush()
            self._audit(s, "project.create", "project", project.id, name=name)
            return project.id

    def create_sample_set(self, project_id: int, name: str) -> int:
        if self._pg:
            return int(
                self._call("SELECT chromatix_create_sample_set(:p, :n)", p=project_id, n=name)
            )
        with self._tx() as (s, user):
            ss = SampleSet(project_id=project_id, name=name, created_by=user.id)
            s.add(ss)
            s.flush()
            self._audit(s, "sample_set.create", "sample_set", ss.id, name=name)
            return ss.id

    # ---------------------------------------------------------------- raw data
    def import_raw_file(self, path: str | os.PathLike[str]) -> str:
        """Copy a raw file into the store (once) and return its SHA-256."""
        src = Path(path)
        fmt = _SUFFIX_FORMAT.get(src.suffix.lower())
        if fmt is None:
            raise ValueError(f"unsupported raw file type: {src.suffix}")
        sha = _sha256(src)
        dest = self._object_path(sha)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
                tmp_path = Path(tmp.name)
            shutil.copyfile(src, tmp_path)
            if _sha256(tmp_path) != sha:
                tmp_path.unlink()
                raise RawDataIntegrityError(f"copy of {src} does not match its hash")
            tmp_path.chmod(0o444)
            tmp_path.replace(dest)
        if self._pg:
            self._call(
                "SELECT chromatix_register_raw_file(:sha, :size, :name, :fmt)",
                sha=sha,
                size=src.stat().st_size,
                name=src.name,
                fmt=fmt,
            )
            return sha
        with self._tx() as (s, user):
            if s.get(RawFile, sha) is None:
                s.add(
                    RawFile(
                        sha256=sha,
                        size=src.stat().st_size,
                        original_name=src.name,
                        file_format=fmt,
                        imported_by=user.id,
                    )
                )
                self._audit(s, "raw_file.import", "raw_file", sha, original_name=src.name)
        return sha

    def load_chromatogram(self, sha256: str) -> Chromatogram:
        """Read a stored raw file, verifying it against its recorded hash first."""
        with Session(self.engine) as s:
            raw = s.get(RawFile, sha256)
            if raw is None:
                raise KeyError(sha256)
            fmt = raw.file_format
        path = self._object_path(sha256)
        if _sha256(path) != sha256:
            raise RawDataIntegrityError(f"raw file {sha256} has been altered on disk")
        return _READERS[fmt](path)

    def add_injection(
        self,
        sample_set_id: int,
        sample_name: str,
        raw_file_sha256: str,
        sample_type: str = "unknown",
        vial: str | None = None,
        amount: float | None = None,
        amounts: Mapping[str, float] | None = None,
    ) -> int:
        """Add an injection.  ``amounts`` are known component amounts (standards)."""
        component_amounts = {k: float(v) for k, v in amounts.items()} if amounts else None
        if self._pg:
            return int(
                self._call(
                    "SELECT chromatix_add_injection(:ss, :name, :sha, :type, :vial, :amount, "
                    "CAST(:amounts AS jsonb))",
                    ss=sample_set_id,
                    name=sample_name,
                    sha=raw_file_sha256,
                    type=sample_type,
                    vial=vial,
                    amount=amount,
                    amounts=None if component_amounts is None else json.dumps(component_amounts),
                )
            )
        with self._tx() as (s, user):
            position = 1 + (
                s.scalar(
                    select(func.max(Injection.position)).where(
                        Injection.sample_set_id == sample_set_id
                    )
                )
                or 0
            )
            inj = Injection(
                sample_set_id=sample_set_id,
                position=position,
                sample_name=sample_name,
                sample_type=sample_type,
                vial=vial,
                amount=amount,
                component_amounts=component_amounts,
                raw_file_sha256=raw_file_sha256,
                created_by=user.id,
            )
            s.add(inj)
            s.flush()
            self._audit(
                s,
                "injection.create",
                "injection",
                inj.id,
                sample_name=sample_name,
                raw_file=raw_file_sha256,
                sample_type=sample_type,
                component_amounts=component_amounts,
            )
            return inj.id

    # ----------------------------------------------------- processing methods
    def save_method_version(
        self,
        project_id: int,
        name: str,
        method: QuantMethod | IntegrationParams,
        reason: str = "",
    ) -> int:
        """Save ``method`` as the next version of method ``name``; returns the version id.

        Plain :class:`IntegrationParams` are saved as a method without components.
        """
        if isinstance(method, IntegrationParams):
            method = QuantMethod(integration=method)
        params = method.to_dict()
        if self._pg:
            return int(
                self._call(
                    "SELECT chromatix_save_method_version(:p, :n, CAST(:params AS jsonb), :r)",
                    p=project_id,
                    n=name,
                    params=json.dumps(params),
                    r=reason,
                )
            )
        with self._tx() as (s, user):
            method_row = s.scalar(
                select(ProcessingMethod).where(
                    ProcessingMethod.project_id == project_id, ProcessingMethod.name == name
                )
            )
            if method_row is None:
                method_row = ProcessingMethod(project_id=project_id, name=name)
                s.add(method_row)
                s.flush()
            latest = s.scalar(
                select(func.max(ProcessingMethodVersion.version)).where(
                    ProcessingMethodVersion.method_id == method_row.id
                )
            )
            if latest and not reason:
                raise ReasonRequiredError("changing a processing method needs a reason")
            mv = ProcessingMethodVersion(
                method_id=method_row.id,
                version=(latest or 0) + 1,
                params=params,
                reason=reason,
                created_by=user.id,
            )
            s.add(mv)
            s.flush()
            self._audit(
                s,
                "method.save",
                "processing_method_version",
                mv.id,
                reason,
                method=name,
                version=mv.version,
                params=mv.params,
            )
            return mv.id

    # ---------------------------------------------------------------- results
    def _load_method(self, method_version_id: int) -> QuantMethod:
        with Session(self.engine) as s:
            mv = s.get(ProcessingMethodVersion, method_version_id)
            if mv is None:
                raise KeyError(f"unknown method version {method_version_id}")
            return QuantMethod.from_dict(mv.params)

    def process_injection(self, injection_id: int, method_version_id: int, reason: str = "") -> int:
        """Integrate one injection and quantitate it; returns the new result id.

        Identified peaks get amounts from the latest calibration of the same
        sample set and method version, if there is one.  The first result needs
        no reason; any reprocessing does, and earlier results are kept unchanged.
        """
        injection = self.injection(injection_id)
        method = self._load_method(method_version_id)
        peaks = integrate(self.load_chromatogram(injection.raw_file_sha256), method.integration)
        curves = self.current_curves(injection.sample_set_id, method_version_id)
        with self._writer() as writer:
            return writer.save_result(
                injection_id, method_version_id, reason, _peak_rows(peaks, method, curves)
            )

    def process_sample_set(
        self, sample_set_id: int, method_version_id: int, reason: str = ""
    ) -> SampleSetProcessing:
        """Integrate every injection, calibrate from the standards, quantitate everything.

        Calibration points are the identified peak areas of injections whose
        sample type is ``standard`` and that have a known amount for the
        component.  Components that cannot be calibrated (e.g. too few
        standards) are reported in ``skipped`` and get no amounts.  All curves
        and results are written in a single transaction.  A reason is needed
        if any injection already has a result.
        """
        method = self._load_method(method_version_id)
        injections = self.injections(sample_set_id)
        peaks = {
            inj.id: integrate(self.load_chromatogram(inj.raw_file_sha256), method.integration)
            for inj in injections
        }
        points: dict[str, list[CalibrationPoint]] = {c.name: [] for c in method.components}
        for inj in injections:
            if inj.sample_type != "standard" or not inj.component_amounts:
                continue
            for index, name in identify(peaks[inj.id], method.components).items():
                amount = inj.component_amounts.get(name)
                if amount is not None:
                    points[name].append(
                        CalibrationPoint(float(amount), peaks[inj.id][index].area, inj.id)
                    )
        fitted: dict[str, tuple[CalibrationCurve, list[CalibrationPoint]]] = {}
        skipped: dict[str, str] = {}
        for component in method.components:
            try:
                fitted[component.name] = (
                    calibrate(component, points[component.name]),
                    points[component.name],
                )
            except ValueError as exc:
                skipped[component.name] = str(exc)
        if not reason and any(self.results(inj.id) for inj in injections):
            raise ReasonRequiredError("reprocessing a sample set needs a reason")

        with self._writer() as writer:
            curves = {
                name: (
                    writer.save_calibration(
                        sample_set_id, method_version_id, method.component(name), curve, pts
                    ),
                    curve,
                )
                for name, (curve, pts) in fitted.items()
            }
            results = {
                inj.id: writer.save_result(
                    inj.id, method_version_id, reason, _peak_rows(peaks[inj.id], method, curves)
                )
                for inj in injections
            }
        return SampleSetProcessing(
            results=results,
            calibrations={name: cal_id for name, (cal_id, _) in curves.items()},
            skipped=skipped,
        )

    def calibrations(
        self, sample_set_id: int, method_version_id: int | None = None
    ) -> list[Calibration]:
        query = select(Calibration).where(Calibration.sample_set_id == sample_set_id)
        if method_version_id is not None:
            query = query.where(Calibration.method_version_id == method_version_id)
        return self._all(query.order_by(Calibration.id))

    def current_curves(
        self, sample_set_id: int, method_version_id: int
    ) -> dict[str, tuple[int, CalibrationCurve]]:
        """Latest calibration per component for a sample set and method version."""
        latest: dict[str, tuple[int, CalibrationCurve]] = {}
        for cal in self.calibrations(sample_set_id, method_version_id):
            latest[cal.component] = (cal.id, curve_of(cal))
        return latest

    # ------------------------------------------------------------------ reads
    def _all(self, query: Select[Any]) -> list[Any]:
        with Session(self.engine) as s:
            return list(s.scalars(query))

    def users(self) -> list[User]:
        return self._all(select(User).order_by(User.username))

    def projects(self) -> list[Project]:
        return self._all(select(Project).order_by(Project.name))

    def sample_sets(self, project_id: int) -> list[SampleSet]:
        return self._all(
            select(SampleSet).where(SampleSet.project_id == project_id).order_by(SampleSet.id)
        )

    def injections(self, sample_set_id: int) -> list[Injection]:
        return self._all(
            select(Injection)
            .where(Injection.sample_set_id == sample_set_id)
            .order_by(Injection.position)
        )

    def injection(self, injection_id: int) -> Injection:
        with Session(self.engine) as s:
            injection = s.get(Injection, injection_id)
            if injection is None:
                raise KeyError(injection_id)
            return injection

    def methods(self, project_id: int) -> list[tuple[ProcessingMethod, ProcessingMethodVersion]]:
        """Each processing method of a project with its latest version."""
        latest = (
            select(
                ProcessingMethodVersion.method_id,
                func.max(ProcessingMethodVersion.version).label("version"),
            )
            .group_by(ProcessingMethodVersion.method_id)
            .subquery()
        )
        query = (
            select(ProcessingMethod, ProcessingMethodVersion)
            .join(ProcessingMethodVersion, ProcessingMethodVersion.method_id == ProcessingMethod.id)
            .join(
                latest,
                (latest.c.method_id == ProcessingMethodVersion.method_id)
                & (latest.c.version == ProcessingMethodVersion.version),
            )
            .where(ProcessingMethod.project_id == project_id)
            .order_by(ProcessingMethod.name)
        )
        with Session(self.engine) as s:
            return [(m, v) for m, v in s.execute(query)]

    def method_versions(self, method_id: int) -> list[ProcessingMethodVersion]:
        return self._all(
            select(ProcessingMethodVersion)
            .where(ProcessingMethodVersion.method_id == method_id)
            .order_by(ProcessingMethodVersion.version)
        )

    def method_version(
        self, method_version_id: int
    ) -> tuple[ProcessingMethod, ProcessingMethodVersion]:
        with Session(self.engine) as s:
            mv = s.get(ProcessingMethodVersion, method_version_id)
            if mv is None:
                raise KeyError(method_version_id)
            method = s.get(ProcessingMethod, mv.method_id)
            assert method is not None
            return method, mv

    def results(self, injection_id: int) -> list[Result]:
        return self._all(
            select(Result).where(Result.injection_id == injection_id).order_by(Result.version)
        )

    def peaks(self, result_id: int) -> list[ResultPeak]:
        return self._all(
            select(ResultPeak)
            .where(ResultPeak.result_id == result_id)
            .order_by(ResultPeak.peak_number)
        )

    def audit_trail(
        self, entity: str | None = None, entity_id: object | None = None
    ) -> list[AuditEvent]:
        query = select(AuditEvent).order_by(AuditEvent.id)
        if entity:
            query = query.where(AuditEvent.entity == entity)
        if entity_id is not None:
            query = query.where(AuditEvent.entity_id == str(entity_id))
        return self._all(query)


# ---------------------------------------------------------------- quantitation
@dataclass(frozen=True)
class SampleSetProcessing:
    results: dict[int, int]
    """injection id -> new result id"""
    calibrations: dict[str, int]
    """component -> new calibration id"""
    skipped: dict[str, str]
    """component -> why it could not be calibrated"""


def curve_of(cal: Calibration) -> CalibrationCurve:
    """Rebuild the fitted curve stored in a calibration row."""
    amounts = [float(p["amount"]) for p in cal.points] or [0.0]
    return CalibrationCurve(
        model=cal.model,  # type: ignore[arg-type]
        coefficients=tuple(float(c) for c in cal.coefficients),
        r_squared=math.nan if cal.r_squared is None else cal.r_squared,
        weighting=cal.weighting,  # type: ignore[arg-type]
        through_origin=cal.through_origin,
        amount_range=(min(amounts), max(amounts)),
    )


def _peak_rows(
    peaks: Sequence[Peak],
    method: QuantMethod,
    curves: Mapping[str, tuple[int, CalibrationCurve]],
) -> list[dict[str, Any]]:
    """Stored form of a result's peaks, with identification and amounts."""
    names = identify(peaks, method.components)
    rows = []
    for index, pk in enumerate(peaks):
        component = names.get(index)
        amount: float | None = None
        calibration_id: int | None = None
        if component is not None and component in curves:
            calibration_id, curve = curves[component]
            try:
                amount = curve.amount(pk.area)
            except ValueError:
                amount = None  # response outside a quadratic curve
        rows.append(
            {
                "peak_number": index + 1,
                "retention_time": pk.retention_time,
                "start_time": pk.start_time,
                "end_time": pk.end_time,
                "height": pk.height,
                "area": pk.area,
                "width_50": None if math.isnan(pk.width_50) else pk.width_50,
                "boundary": pk.boundary,
                "baseline_start": pk.baseline_start,
                "baseline_end": pk.baseline_end,
                "component": component,
                "amount": amount,
                "calibration_id": calibration_id,
            }
        )
    return rows


def _points_json(points: Sequence[CalibrationPoint]) -> list[dict[str, Any]]:
    return [
        {"injection_id": p.injection_id, "amount": p.amount, "response": p.response} for p in points
    ]


class _Writer(Protocol):
    def save_result(
        self, injection_id: int, method_version_id: int, reason: str, rows: list[dict[str, Any]]
    ) -> int: ...

    def save_calibration(
        self,
        sample_set_id: int,
        method_version_id: int,
        component: Component,
        curve: CalibrationCurve,
        points: Sequence[CalibrationPoint],
    ) -> int: ...


class _PgWriter:
    """Writes through the SECURITY DEFINER functions, inside the caller's transaction."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def save_result(
        self, injection_id: int, method_version_id: int, reason: str, rows: list[dict[str, Any]]
    ) -> int:
        return int(
            self.conn.execute(
                text("SELECT chromatix_save_result(:inj, :mv, :r, CAST(:peaks AS jsonb))"),
                {
                    "inj": injection_id,
                    "mv": method_version_id,
                    "r": reason,
                    "peaks": json.dumps(rows),
                },
            ).scalar_one()
        )

    def save_calibration(
        self,
        sample_set_id: int,
        method_version_id: int,
        component: Component,
        curve: CalibrationCurve,
        points: Sequence[CalibrationPoint],
    ) -> int:
        return int(
            self.conn.execute(
                text(
                    "SELECT chromatix_save_calibration(:ss, :mv, :c, :model, :w, :o, "
                    "CAST(:coef AS jsonb), :r2, CAST(:pts AS jsonb))"
                ),
                {
                    "ss": sample_set_id,
                    "mv": method_version_id,
                    "c": component.name,
                    "model": curve.model,
                    "w": curve.weighting,
                    "o": curve.through_origin,
                    "coef": json.dumps(list(curve.coefficients)),
                    "r2": None if math.isnan(curve.r_squared) else curve.r_squared,
                    "pts": json.dumps(_points_json(points)),
                },
            ).scalar_one()
        )


class _SqliteWriter:
    """The same writes through the ORM, with the same audit events as the PG functions."""

    def __init__(self, store: Store, session: Session, user: User) -> None:
        self.store, self.session, self.user = store, session, user

    def save_result(
        self, injection_id: int, method_version_id: int, reason: str, rows: list[dict[str, Any]]
    ) -> int:
        s = self.session
        latest = s.scalar(
            select(func.max(Result.version)).where(Result.injection_id == injection_id)
        )
        if latest and not reason:
            raise ReasonRequiredError("reprocessing an injection needs a reason")
        result = Result(
            injection_id=injection_id,
            version=(latest or 0) + 1,
            method_version_id=method_version_id,
            reason=reason,
            created_by=self.user.id,
        )
        s.add(result)
        s.flush()
        for row in rows:
            s.add(ResultPeak(result_id=result.id, **row))
        self.store._audit(
            s,
            "result.create",
            "result",
            result.id,
            reason,
            injection_id=injection_id,
            version=result.version,
            method_version_id=method_version_id,
            peaks=len(rows),
        )
        return result.id

    def save_calibration(
        self,
        sample_set_id: int,
        method_version_id: int,
        component: Component,
        curve: CalibrationCurve,
        points: Sequence[CalibrationPoint],
    ) -> int:
        s = self.session
        r_squared = None if math.isnan(curve.r_squared) else curve.r_squared
        cal = Calibration(
            sample_set_id=sample_set_id,
            method_version_id=method_version_id,
            component=component.name,
            model=curve.model,
            weighting=curve.weighting,
            through_origin=curve.through_origin,
            coefficients=list(curve.coefficients),
            r_squared=r_squared,
            points=_points_json(points),
            created_by=self.user.id,
        )
        s.add(cal)
        s.flush()
        self.store._audit(
            s,
            "calibration.create",
            "calibration",
            cal.id,
            sample_set_id=sample_set_id,
            method_version_id=method_version_id,
            component=component.name,
            model=curve.model,
            coefficients=list(curve.coefficients),
            r_squared=r_squared,
            points=len(points),
        )
        return cal.id
