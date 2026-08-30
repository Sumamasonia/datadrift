"""
Connector layer.

Connects to the target database (PostgreSQL, MySQL, SQLite, or DuckDB - any
SQLAlchemy-supported dialect works) and runs read-only diagnostic queries to
collect all six metric types from the product documentation:

  - row_count                       : total row volume (+ row delta since last check)
  - null_rate_<column>              : fraction of NULLs in each column
  - freshness_minutes               : minutes since the latest value in the
                                       configured timestamp column
  - mean_/median_/stddev_/p5_/p95_<column> : distribution shift stats per numeric column
  - orphan_rate_<column>            : referential integrity - % of rows whose FK
                                       value has no match in the referenced table
  - (schema drift is handled separately via snapshot_schema + schema_drift.diff_schema,
     since it's a structural diff rather than a numeric metric)

DataDrift never writes to the monitored table - every query here is a SELECT.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

# Column types treated as numeric for distribution-shift monitoring.
_NUMERIC_TYPE_HINTS = (
    "INT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL", "BIGINT", "SMALLINT",
)


class ConnectorError(RuntimeError):
    """Raised when a diagnostic query against a monitored table fails."""


def get_target_engine(connection_url: str) -> Engine:
    """Return (and cache-free-create) a SQLAlchemy engine for a monitored table's source DB."""
    return create_engine(connection_url, future=True)


def _qualified(schema: str | None, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def _is_numeric_type(type_str: str) -> bool:
    upper = type_str.upper()
    return any(hint in upper for hint in _NUMERIC_TYPE_HINTS)


def collect_metrics(
    connection_url: str,
    table_name: str,
    schema_name: str | None = None,
    timestamp_column: str | None = None,
    enabled_metrics: list[str] | None = None,
    previous_row_count: float | None = None,
    referential_checks: list | None = None,
) -> dict[str, float]:
    """
    Run the diagnostic queries for all enabled metric types against a single
    monitored table.

    enabled_metrics: subset of {"volume","null_rate","freshness","distribution",
        "referential_integrity"} - schema_drift is handled separately via
        snapshot_schema(). Defaults to all.
    previous_row_count: last known row_count, used to also report row_delta.
    referential_checks: list of ReferentialCheck rows (table_id, source_column,
        ref_table_name, ref_schema_name, ref_column) to evaluate.

    Returns a flat dict of metric_name -> value, ready to be written as
    metric_snapshots rows. Any failure raises ConnectorError with a message
    (rather than partial/silent results) so a broken connection itself
    doesn't get mistaken for a data anomaly.
    """
    enabled = set(enabled_metrics) if enabled_metrics else {
        "volume", "null_rate", "freshness", "distribution", "referential_integrity"
    }
    engine = get_target_engine(connection_url)
    metrics: dict[str, float] = {}
    qualified = _qualified(schema_name, table_name)

    try:
        with engine.connect() as conn:
            inspector = inspect(engine)
            columns_info = inspector.get_columns(table_name, schema=schema_name)
            columns = [c["name"] for c in columns_info]

            # --- Metric 1: volume (row count + row delta) ---
            row_count = conn.execute(text(f"SELECT COUNT(*) FROM {qualified}")).scalar()
            total = float(row_count or 0)
            if "volume" in enabled:
                metrics["row_count"] = total
                if previous_row_count is not None:
                    metrics["row_delta"] = total - previous_row_count

            # --- Metric 4: null rate per column ---
            if "null_rate" in enabled:
                for col in columns:
                    if total == 0:
                        metrics[f"null_rate_{col}"] = 0.0
                        continue
                    null_count = conn.execute(
                        text(f"SELECT COUNT(*) FROM {qualified} WHERE {col} IS NULL")
                    ).scalar()
                    metrics[f"null_rate_{col}"] = float(null_count or 0) / total

            # --- Metric 3: freshness ---
            if "freshness" in enabled and timestamp_column:
                latest = conn.execute(
                    text(f"SELECT MAX({timestamp_column}) FROM {qualified}")
                ).scalar()
                if latest is not None:
                    if isinstance(latest, str):
                        latest = datetime.fromisoformat(latest)
                    if latest.tzinfo is None:
                        latest = latest.replace(tzinfo=timezone.utc)
                    delta = datetime.now(timezone.utc) - latest
                    metrics["freshness_minutes"] = delta.total_seconds() / 60.0

            # --- Metric 6: distribution shift (numeric columns only) ---
            if "distribution" in enabled and total > 0:
                numeric_columns = [
                    c["name"] for c in columns_info if _is_numeric_type(str(c["type"]))
                ]
                for col in numeric_columns:
                    row = conn.execute(
                        text(
                            f"SELECT AVG({col}), MIN({col}), MAX({col}) FROM {qualified} "
                            f"WHERE {col} IS NOT NULL"
                        )
                    ).first()
                    if row is None or row[0] is None:
                        continue
                    avg_val, min_val, max_val = row
                    metrics[f"mean_{col}"] = float(avg_val)
                    metrics[f"min_{col}"] = float(min_val)
                    metrics[f"max_{col}"] = float(max_val)
                    # median / p5 / p95 / stddev computed client-side via pandas for
                    # cross-dialect portability (percentile functions differ a lot
                    # between SQLite/MySQL/Postgres/DuckDB).
                    try:
                        import pandas as pd

                        sample = pd.read_sql(
                            text(f"SELECT {col} FROM {qualified} WHERE {col} IS NOT NULL LIMIT 50000"),
                            conn,
                        )[col]
                        if not sample.empty:
                            metrics[f"stddev_{col}"] = float(sample.std() or 0.0)
                            metrics[f"median_{col}"] = float(sample.median())
                            metrics[f"p5_{col}"] = float(sample.quantile(0.05))
                            metrics[f"p95_{col}"] = float(sample.quantile(0.95))
                    except Exception:
                        pass  # distribution stats are best-effort; core mean/min/max still recorded

            # --- Metric 5: referential integrity ---
            if "referential_integrity" in enabled and referential_checks:
                for rc in referential_checks:
                    if not getattr(rc, "is_active", True):
                        continue
                    ref_qualified = _qualified(rc.ref_schema_name, rc.ref_table_name)
                    orphan_count = conn.execute(
                        text(
                            f"SELECT COUNT(*) FROM {qualified} src WHERE src.{rc.source_column} IS NOT NULL "
                            f"AND NOT EXISTS (SELECT 1 FROM {ref_qualified} ref "
                            f"WHERE ref.{rc.ref_column} = src.{rc.source_column})"
                        )
                    ).scalar()
                    metrics[rc.metric_name] = float(orphan_count or 0) / total if total else 0.0

    except Exception as exc:  # noqa: BLE001 - surfaced to caller as ConnectorError
        raise ConnectorError(
            f"Failed to collect metrics for {qualified} via {connection_url}: {exc}"
        ) from exc

    return metrics


def list_columns(connection_url: str, table_name: str, schema_name: str | None = None) -> list[str]:
    engine = get_target_engine(connection_url)
    inspector = inspect(engine)
    return [c["name"] for c in inspector.get_columns(table_name, schema=schema_name)]


def snapshot_schema(
    connection_url: str, table_name: str, schema_name: str | None = None
) -> dict[str, str]:
    """Stretch scope: capture {column_name: column_type} for schema-drift diffing."""
    engine = get_target_engine(connection_url)
    inspector = inspect(engine)
    columns = inspector.get_columns(table_name, schema=schema_name)
    return {c["name"]: str(c["type"]) for c in columns}
