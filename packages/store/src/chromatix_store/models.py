"""Database schema.

The object model follows Empower's (see the Empower3 knowledge graph in the
vault): Project -> Sample Set -> Injection -> raw data file, Processing Method
-> immutable versions, and Results that are versioned, never overwritten.

Tables in :data:`APPEND_ONLY_TABLES` are protected by database triggers so
that rows can be inserted but never updated or deleted, whatever code talks to
the database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    # All timestamps are UTC.  PostgreSQL stores them as timestamptz; SQLite has
    # no time-zone type and returns them naive.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSON().with_variant(JSONB(), "postgresql"),
        datetime: DateTime(timezone=True),
    }


class User(Base):
    __tablename__ = "user_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Project(Base):
    __tablename__ = "project"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[int] = mapped_column(ForeignKey("user_account.id"))


class RawFile(Base):
    """A raw data file, stored once and addressed by the SHA-256 of its bytes."""

    __tablename__ = "raw_file"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int]
    original_name: Mapped[str] = mapped_column(String(500))
    file_format: Mapped[str] = mapped_column(String(32))
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)
    imported_by: Mapped[int] = mapped_column(ForeignKey("user_account.id"))


class SampleSet(Base):
    __tablename__ = "sample_set"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[int] = mapped_column(ForeignKey("user_account.id"))


class Injection(Base):
    __tablename__ = "injection"
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_set_id: Mapped[int] = mapped_column(ForeignKey("sample_set.id"))
    position: Mapped[int]
    sample_name: Mapped[str] = mapped_column(String(200))
    sample_type: Mapped[str] = mapped_column(String(32), default="unknown")
    vial: Mapped[str | None] = mapped_column(String(32))
    amount: Mapped[float | None]
    raw_file_sha256: Mapped[str] = mapped_column(ForeignKey("raw_file.sha256"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[int] = mapped_column(ForeignKey("user_account.id"))
    __table_args__ = (UniqueConstraint("sample_set_id", "position"),)


class ProcessingMethod(Base):
    __tablename__ = "processing_method"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    name: Mapped[str] = mapped_column(String(200))
    __table_args__ = (UniqueConstraint("project_id", "name"),)


class ProcessingMethodVersion(Base):
    """Immutable snapshot of a processing method's parameters."""

    __tablename__ = "processing_method_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("processing_method.id"))
    version: Mapped[int]
    params: Mapped[dict[str, Any]]
    reason: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[int] = mapped_column(ForeignKey("user_account.id"))
    __table_args__ = (UniqueConstraint("method_id", "version"),)


class Result(Base):
    """One processing of one injection.  Reprocessing adds a new version."""

    __tablename__ = "result"
    id: Mapped[int] = mapped_column(primary_key=True)
    injection_id: Mapped[int] = mapped_column(ForeignKey("injection.id"))
    version: Mapped[int]
    method_version_id: Mapped[int] = mapped_column(ForeignKey("processing_method_version.id"))
    reason: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[int] = mapped_column(ForeignKey("user_account.id"))
    __table_args__ = (UniqueConstraint("injection_id", "version"),)


class ResultPeak(Base):
    __tablename__ = "result_peak"
    id: Mapped[int] = mapped_column(primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("result.id"))
    peak_number: Mapped[int]
    retention_time: Mapped[float]
    start_time: Mapped[float]
    end_time: Mapped[float]
    height: Mapped[float]
    area: Mapped[float]
    width_50: Mapped[float | None]
    boundary: Mapped[str] = mapped_column(String(2))


class AuditEvent(Base):
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(default="")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


APPEND_ONLY_TABLES = (
    "raw_file",
    "processing_method_version",
    "result",
    "result_peak",
    "audit_event",
)
