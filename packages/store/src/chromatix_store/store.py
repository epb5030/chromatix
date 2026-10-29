"""The storage API used by the desktop app (and later a server)."""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session

from chromatix_core import Chromatogram, IntegrationParams, integrate
from chromatix_core.io import read_aia
from chromatix_store.models import (
    APPEND_ONLY_TABLES,
    AuditEvent,
    Base,
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
        """Open (or create) a personal store: ``<path>.sqlite`` + ``<path>.rawdata/``."""
        base = Path(path)
        engine = create_engine(f"sqlite:///{base.with_suffix('.sqlite')}")
        return cls.create(engine, base.with_suffix(".rawdata"), username)

    @classmethod
    def create(cls, engine: Engine, data_dir: Path, username: str) -> Store:
        if engine.dialect.name != "sqlite":
            raise NotImplementedError(
                "Only SQLite is implemented so far; PostgreSQL is planned (docs/adr/0003)."
            )
        event.listen(engine, "connect", _sqlite_pragmas)
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            for table in APPEND_ONLY_TABLES:
                for op in ("UPDATE", "DELETE"):
                    conn.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} "
                            f"BEFORE {op} ON {table} BEGIN "
                            f"SELECT RAISE(ABORT, '{table} is append-only'); END"
                        )
                    )
        (data_dir / "objects").mkdir(parents=True, exist_ok=True)
        return cls(engine, data_dir, username)

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
        with self._tx() as (s, user):
            project = Project(name=name, description=description, created_by=user.id)
            s.add(project)
            s.flush()
            self._audit(s, "project.create", "project", project.id, name=name)
            return project.id

    def create_sample_set(self, project_id: int, name: str) -> int:
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
    ) -> int:
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
            )
            return inj.id

    # ----------------------------------------------------- processing methods
    def save_method_version(
        self, project_id: int, name: str, params: IntegrationParams, reason: str = ""
    ) -> int:
        """Save ``params`` as the next version of method ``name``; returns the version id."""
        with self._tx() as (s, user):
            method = s.scalar(
                select(ProcessingMethod).where(
                    ProcessingMethod.project_id == project_id, ProcessingMethod.name == name
                )
            )
            if method is None:
                method = ProcessingMethod(project_id=project_id, name=name)
                s.add(method)
                s.flush()
            latest = s.scalar(
                select(func.max(ProcessingMethodVersion.version)).where(
                    ProcessingMethodVersion.method_id == method.id
                )
            )
            if latest and not reason:
                raise ReasonRequiredError("changing a processing method needs a reason")
            mv = ProcessingMethodVersion(
                method_id=method.id,
                version=(latest or 0) + 1,
                params=dataclasses.asdict(params),
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
    def process_injection(self, injection_id: int, method_version_id: int, reason: str = "") -> int:
        """Integrate an injection with a method version; returns the new result id.

        The first result needs no reason; any reprocessing does, and earlier
        results are kept unchanged.
        """
        with Session(self.engine) as s:
            inj = s.get(Injection, injection_id)
            mv = s.get(ProcessingMethodVersion, method_version_id)
            if inj is None or mv is None:
                raise KeyError("unknown injection or method version")
            sha, params = inj.raw_file_sha256, IntegrationParams(**mv.params)
        peaks = integrate(self.load_chromatogram(sha), params)

        with self._tx() as (s, user):
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
                created_by=user.id,
            )
            s.add(result)
            s.flush()
            for number, pk in enumerate(peaks, start=1):
                s.add(
                    ResultPeak(
                        result_id=result.id,
                        peak_number=number,
                        retention_time=pk.retention_time,
                        start_time=pk.start_time,
                        end_time=pk.end_time,
                        height=pk.height,
                        area=pk.area,
                        width_50=None if math.isnan(pk.width_50) else pk.width_50,
                        boundary=pk.boundary,
                    )
                )
            self._audit(
                s,
                "result.create",
                "result",
                result.id,
                reason,
                injection_id=injection_id,
                version=result.version,
                method_version_id=method_version_id,
                peaks=len(peaks),
            )
            return result.id

    def results(self, injection_id: int) -> list[Result]:
        with Session(self.engine) as s:
            return list(
                s.scalars(
                    select(Result)
                    .where(Result.injection_id == injection_id)
                    .order_by(Result.version)
                )
            )

    def peaks(self, result_id: int) -> list[ResultPeak]:
        with Session(self.engine) as s:
            return list(
                s.scalars(
                    select(ResultPeak)
                    .where(ResultPeak.result_id == result_id)
                    .order_by(ResultPeak.peak_number)
                )
            )

    def audit_trail(self, entity: str | None = None) -> list[AuditEvent]:
        with Session(self.engine) as s:
            query = select(AuditEvent).order_by(AuditEvent.id)
            if entity:
                query = query.where(AuditEvent.entity == entity)
            return list(s.scalars(query))
