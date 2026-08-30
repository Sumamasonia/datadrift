"""
DataDrift's internal metadata store.

Implements the five core tables from the project documentation (section 5):
  monitored_tables, metric_baselines, metric_snapshots, anomalies, alert_rules

This store is separate from the target database(s) being monitored - DataDrift
only ever issues read-only diagnostic queries against those; all of its own
state lives here (SQLite by default, PostgreSQL in production - see config.py).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from datadrift.config import settings


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MonitoredTable(Base):
    __tablename__ = "monitored_tables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_name: Mapped[str] = mapped_column(String(255))
    connection_url: Mapped[str] = mapped_column(Text)  # SQLAlchemy URL of the source DB
    schema_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    table_name: Mapped[str] = mapped_column(String(255))
    timestamp_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    check_interval: Mapped[str] = mapped_column(String(20), default="hourly")
    learning_period_days: Mapped[int] = mapped_column(
        Integer, default=settings.default_learning_period_days
    )
    # "sensitivity multiplier" in the product docs; kept as zscore_threshold internally
    # since that's what the detection engine reads. See the sensitivity_multiplier
    # property below for the doc-facing name.
    zscore_threshold: Mapped[float] = mapped_column(
        Float, default=settings.default_zscore_threshold
    )
    upstream_table_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("monitored_tables.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Baseline status state machine: learning -> active, or paused / error at any time.
    baseline_status: Mapped[str] = mapped_column(String(20), default="learning")

    # JSON list of which of the six metric types are enabled for this table,
    # e.g. ["volume", "schema_drift", "null_rate", "freshness", "distribution", "referential_integrity"]
    enabled_metrics_json: Mapped[str] = mapped_column(
        Text, default=lambda: json.dumps(
            ["volume", "schema_drift", "null_rate", "freshness", "distribution", "referential_integrity"]
        )
    )

    # Most recently captured {column_name: column_type} snapshot, used for schema-drift diffing.
    schema_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_snapshot_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    baselines: Mapped[list["MetricBaseline"]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    anomalies: Mapped[list["Anomaly"]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    alert_rules: Mapped[list["AlertRule"]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    referential_checks: Mapped[list["ReferentialCheck"]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )

    @property
    def full_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name

    @property
    def sensitivity_multiplier(self) -> float:
        return self.zscore_threshold

    @property
    def enabled_metrics(self) -> list[str]:
        try:
            return json.loads(self.enabled_metrics_json)
        except (TypeError, ValueError):
            return ["volume", "schema_drift", "null_rate", "freshness", "distribution", "referential_integrity"]

    @property
    def schema_snapshot(self) -> dict[str, str] | None:
        if not self.schema_snapshot_json:
            return None
        return json.loads(self.schema_snapshot_json)


class MetricBaseline(Base):
    __tablename__ = "metric_baselines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    table_id: Mapped[str] = mapped_column(String(36), ForeignKey("monitored_tables.id"))
    metric_name: Mapped[str] = mapped_column(String(255))
    rolling_mean: Mapped[float] = mapped_column(Float, default=0.0)
    rolling_stddev: Mapped[float] = mapped_column(Float, default=0.0)
    window_size: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    table: Mapped[MonitoredTable] = relationship(back_populates="baselines")


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    table_id: Mapped[str] = mapped_column(String(36), ForeignKey("monitored_tables.id"))
    metric_name: Mapped[str] = mapped_column(String(255))
    value: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    table: Mapped[MonitoredTable] = relationship(back_populates="snapshots")


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    table_id: Mapped[str] = mapped_column(String(36), ForeignKey("monitored_tables.id"))
    metric_name: Mapped[str] = mapped_column(String(255))
    observed_value: Mapped[float] = mapped_column(Float)
    expected_range: Mapped[str] = mapped_column(String(255))
    z_score: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(20))  # low / medium / high
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)  # ranked, human-readable summary
    diagnostic_results_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # supporting evidence
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open/acknowledged/resolved

    table: Mapped[MonitoredTable] = relationship(back_populates="anomalies")

    @property
    def diagnostic_results(self) -> list[dict]:
        if not self.diagnostic_results_json:
            return []
        return json.loads(self.diagnostic_results_json)

    @diagnostic_results.setter
    def diagnostic_results(self, value: list[dict]) -> None:
        self.diagnostic_results_json = json.dumps(value)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    table_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("monitored_tables.id"), nullable=True
    )  # NULL = applies globally
    min_severity: Mapped[str] = mapped_column(String(20), default="low")
    channel: Mapped[str] = mapped_column(String(20))  # email / slack / webhook
    destination: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    table: Mapped[MonitoredTable | None] = relationship(back_populates="alert_rules")


class ReferentialCheck(Base):
    """
    Stretch/Core metric 5: a configured foreign-key relationship to check for
    orphaned rows, e.g. orders.customer_id -> customers.id.
    """
    __tablename__ = "referential_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    table_id: Mapped[str] = mapped_column(String(36), ForeignKey("monitored_tables.id"))
    source_column: Mapped[str] = mapped_column(String(255))
    ref_table_name: Mapped[str] = mapped_column(String(255))
    ref_schema_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ref_column: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    table: Mapped[MonitoredTable] = relationship(back_populates="referential_checks")

    @property
    def metric_name(self) -> str:
        return f"orphan_rate_{self.source_column}"


_engine = None
_SessionLocal = None


def get_engine(url: str | None = None):
    global _engine
    if _engine is None or url is not None:
        _engine = create_engine(url or settings.storage_url, future=True)
    return _engine


def init_db(url: str | None = None) -> None:
    """Create all tables in the storage backend if they don't already exist."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)


def get_session():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal()
