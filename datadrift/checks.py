"""
Orchestration layer: ties together connectors -> detection -> storage -> alerting
for a single "check cycle" across all six metric types. Used by both the CLI
(`datadrift check`) and the in-process scheduler (`datadrift start`).

Anomaly lifecycle implemented here (Feature #11, "dedup and resolution"):
  - A newly-breaching metric with no existing open anomaly for the same
    table+metric creates a new Anomaly row and triggers an alert.
  - A metric that is still breaching and already has an open anomaly for the
    same table+metric updates that row in place (refreshes observed_value,
    z_score, last_seen_at) instead of creating a duplicate - no repeat alert.
  - A metric that returns to baseline while an anomaly is open marks it
    resolved (status="resolved", resolved_at=now) and sends one resolution
    notification.

Schema drift is structural, not statistical: any diff vs. the stored
schema_snapshot is flagged immediately (no z-score / learning period).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from datadrift.alerting import dispatch_alerts, dispatch_resolution
from datadrift.connectors import ConnectorError, collect_metrics, snapshot_schema
from datadrift.db import (
    AlertRule,
    Anomaly,
    MetricBaseline,
    MetricSnapshot,
    MonitoredTable,
    get_session,
)
from datadrift.detection import detect, in_learning_period
from datadrift.root_cause import suggest_root_cause
from datadrift.schema_drift import diff_schema

logger = logging.getLogger("datadrift.checks")

# A null_rate_* metric is treated as a "critical column" candidate for the
# null-rate-spike root-cause check if its baseline null rate was near zero -
# i.e. it was normally always populated.
_CRITICAL_COLUMN_BASELINE_NULL_RATE = 0.05


def _history_for_metric(session, table_id: str, metric_name: str, limit: int) -> list[float]:
    rows = (
        session.execute(
            select(MetricSnapshot.value)
            .where(MetricSnapshot.table_id == table_id, MetricSnapshot.metric_name == metric_name)
            .order_by(MetricSnapshot.recorded_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))  # oldest first


def _latest_value(session, table_id: str, metric_name: str) -> float | None:
    return session.execute(
        select(MetricSnapshot.value)
        .where(MetricSnapshot.table_id == table_id, MetricSnapshot.metric_name == metric_name)
        .order_by(MetricSnapshot.recorded_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _pct_change(observed: float, mean: float) -> float:
    if mean == 0:
        return 0.0
    return (observed - mean) / mean * 100.0


def _open_anomaly(session, table_id: str, metric_name: str) -> Anomaly | None:
    return session.execute(
        select(Anomaly).where(
            Anomaly.table_id == table_id, Anomaly.metric_name == metric_name, Anomaly.status == "open"
        )
    ).scalar_one_or_none()


def _upsert_baseline(session, table_id: str, metric_name: str, result, history_len: int, now: datetime) -> None:
    baseline = session.execute(
        select(MetricBaseline).where(
            MetricBaseline.table_id == table_id, MetricBaseline.metric_name == metric_name
        )
    ).scalar_one_or_none()
    if baseline is None:
        baseline = MetricBaseline(table_id=table_id, metric_name=metric_name)
        session.add(baseline)
    baseline.rolling_mean = result.mean
    baseline.rolling_stddev = result.stddev
    baseline.window_size = history_len
    baseline.last_updated = now


def _build_root_cause(session, table: MonitoredTable, result, schema_diff, now: datetime):
    """Only run the full root-cause suite for row_count (volume) anomalies, per the docs."""
    if result.metric_name != "row_count":
        return None, []

    downstream_pct = _pct_change(result.value, result.mean)

    upstream_pct = None
    if table.upstream_table_id:
        upstream_history = _history_for_metric(session, table.upstream_table_id, "row_count", limit=60)
        upstream_latest = _latest_value(session, table.upstream_table_id, "row_count")
        if upstream_latest is not None and upstream_history:
            up_mean = sum(upstream_history) / len(upstream_history)
            upstream_pct = _pct_change(upstream_latest, up_mean)

    freshness_value = _latest_value(session, table.id, "freshness_minutes")
    expected_interval = {"hourly": 60.0, "daily": 24 * 60.0}.get(table.check_interval, 60.0)

    critical_column = None
    current_null_rate = None
    baseline_null_rate = None
    null_baselines = (
        session.execute(
            select(MetricBaseline).where(
                MetricBaseline.table_id == table.id, MetricBaseline.metric_name.like("null_rate_%")
            )
        )
        .scalars()
        .all()
    )
    candidates = [b for b in null_baselines if b.rolling_mean <= _CRITICAL_COLUMN_BASELINE_NULL_RATE]
    if candidates:
        best = min(candidates, key=lambda b: b.rolling_mean)
        critical_column = best.metric_name.replace("null_rate_", "", 1)
        baseline_null_rate = best.rolling_mean
        current_null_rate = _latest_value(session, table.id, best.metric_name)

    suggestion = suggest_root_cause(
        downstream_row_count_change_pct=downstream_pct,
        upstream_row_count_change_pct=upstream_pct,
        freshness_minutes=freshness_value,
        expected_interval_minutes=expected_interval,
        critical_column=critical_column,
        current_null_rate=current_null_rate,
        baseline_null_rate=baseline_null_rate,
        schema_diff_has_drift=schema_diff.has_drift if schema_diff else False,
        schema_diff_summary=schema_diff.summary() if schema_diff else None,
    )
    return suggestion.summary(), suggestion.to_dict()


def _alert_rules_for_table(session, table_id: str) -> list[AlertRule]:
    return (
        session.execute(
            select(AlertRule).where((AlertRule.table_id == table_id) | (AlertRule.table_id.is_(None)))
        )
        .scalars()
        .all()
    )


def _schema_drift_diagnostics(diff) -> tuple[str, list[dict]]:
    """
    Feature #13: root-cause style explanation for a standalone schema-drift
    anomaly (i.e. one not accompanying a row_count anomaly). Unlike the
    volume root-cause suite, there's no live data to diagnose against - the
    diff itself *is* the evidence - so this classifies each change by how
    likely it is to break downstream consumers and suggests the probable
    cause of that class of change.
    """
    checks = []
    for col in diff.removed_columns:
        checks.append(
            {
                "name": "column_removed",
                "triggered": True,
                "evidence": (
                    f"Column '{col}' was removed. Likely cause: an upstream migration or ETL "
                    "refactor dropped or renamed this column. Any query, dashboard, or transformation "
                    "that reads it will now fail or silently return NULL."
                ),
                "weight": 3,
            }
        )
    for col, (old_t, new_t) in diff.type_changes.items():
        checks.append(
            {
                "name": "type_changed",
                "triggered": True,
                "evidence": (
                    f"Column '{col}' changed type from {old_t} to {new_t}. Likely cause: a source "
                    "system schema change or a manual ALTER TABLE. Downstream code that assumes the "
                    "old type (e.g. casts, comparisons) may error or produce wrong results."
                ),
                "weight": 3,
            }
        )
    for col in diff.added_columns:
        checks.append(
            {
                "name": "column_added",
                "triggered": True,
                "evidence": (
                    f"Column '{col}' was added. Likely cause: a new field being tracked upstream. "
                    "Low risk to existing consumers, but any strict-schema downstream tooling "
                    "(e.g. schema-validated loaders) may need updating."
                ),
                "weight": 1,
            }
        )
    checks.sort(key=lambda c: -c["weight"])
    summary = "\n".join(f"Possible cause: {c['evidence']}" for c in checks) if checks else None
    return summary, checks


def _run_schema_drift_check(session, table: MonitoredTable, now: datetime):
    """Structural check - immediate flag on any diff, no z-score/learning period. Returns the SchemaDiff (or None)."""
    if "schema_drift" not in table.enabled_metrics:
        return None
    try:
        new_snapshot = snapshot_schema(table.connection_url, table.table_name, table.schema_name)
    except Exception:
        logger.exception("Schema snapshot failed for %s", table.full_name)
        return None

    old_snapshot = table.schema_snapshot
    table.schema_snapshot_json = json.dumps(new_snapshot)
    table.schema_snapshot_updated_at = now

    if old_snapshot is None:
        return None  # first observation - nothing to diff against yet

    diff = diff_schema(old_snapshot, new_snapshot)
    if not diff.has_drift:
        return diff

    # Column removed / type changed = HIGH; column added alone = LOW.
    severity = "high" if (diff.removed_columns or diff.type_changes) else "low"
    metric_name = "schema_drift"
    change_count = float(len(diff.added_columns) + len(diff.removed_columns) + len(diff.type_changes))
    diagnosis_text, diagnostic_results = _schema_drift_diagnostics(diff)
    existing = _open_anomaly(session, table.id, metric_name)
    if existing:
        existing.observed_value = change_count
        existing.diagnosis = diagnosis_text or diff.summary()
        existing.diagnostic_results = diagnostic_results
        existing.last_seen_at = now
        existing.severity = severity
    else:
        anomaly = Anomaly(
            table_id=table.id,
            metric_name=metric_name,
            observed_value=change_count,
            expected_range="no schema change",
            z_score=0.0,
            severity=severity,
            diagnosis=diagnosis_text or diff.summary(),
            detected_at=now,
            last_seen_at=now,
            status="open",
        )
        anomaly.diagnostic_results = diagnostic_results
        session.add(anomaly)
        session.flush()
        dispatch_alerts(table, anomaly, _alert_rules_for_table(session, table.id))
    return diff


def run_check(table: MonitoredTable, session=None, now: datetime | None = None) -> list[Anomaly]:
    """
    Run one full check cycle for a single monitored table across all enabled
    metric types. Returns newly created OR newly resolved Anomaly rows
    (already committed).
    """
    own_session = session is None
    session = session or get_session()
    now = now or datetime.now(timezone.utc)
    changed_anomalies: list[Anomaly] = []

    if table.baseline_status == "paused":
        if own_session:
            session.close()
        return []

    previous_row_count = _latest_value(session, table.id, "row_count")

    try:
        metrics = collect_metrics(
            table.connection_url,
            table.table_name,
            table.schema_name,
            table.timestamp_column,
            enabled_metrics=table.enabled_metrics,
            previous_row_count=previous_row_count,
            referential_checks=table.referential_checks,
        )
    except ConnectorError:
        logger.exception("Connector error while checking %s", table.full_name)
        table.baseline_status = "error"
        session.commit()
        if own_session:
            session.close()
        return []

    # Schema drift: structural, evaluated once per check, separately from the z-score metrics.
    schema_diff = _run_schema_drift_check(session, table, now)

    for metric_name, value in metrics.items():
        if metric_name == "row_delta":
            # Informational only (shown on the dashboard) - row_count's own z-score
            # already covers volume anomalies, so row_delta isn't independently detected.
            session.add(MetricSnapshot(table_id=table.id, metric_name=metric_name, value=value, recorded_at=now))
            continue

        history = _history_for_metric(session, table.id, metric_name, limit=60)
        result = detect(
            metric_name=metric_name,
            new_value=value,
            history=history,
            first_seen_at=table.created_at,
            now=now,
            threshold=table.zscore_threshold,
            learning_period_days=table.learning_period_days,
        )

        session.add(MetricSnapshot(table_id=table.id, metric_name=metric_name, value=value, recorded_at=now))
        _upsert_baseline(session, table.id, metric_name, result, len(history), now)

        existing = _open_anomaly(session, table.id, metric_name)

        if result.is_anomaly:
            if existing:
                # Dedup: update the existing open anomaly in place, don't re-alert.
                existing.observed_value = result.value
                existing.expected_range = f"{result.mean:.4f} \u00b1 {table.zscore_threshold * result.stddev:.4f}"
                existing.z_score = result.z_score
                existing.severity = result.severity
                existing.last_seen_at = now
            else:
                diagnosis, diagnostic_results = _build_root_cause(session, table, result, schema_diff, now)
                anomaly = Anomaly(
                    table_id=table.id,
                    metric_name=metric_name,
                    observed_value=result.value,
                    expected_range=f"{result.mean:.4f} \u00b1 {table.zscore_threshold * result.stddev:.4f}",
                    z_score=result.z_score,
                    severity=result.severity,
                    diagnosis=diagnosis,
                    detected_at=now,
                    last_seen_at=now,
                    status="open",
                )
                anomaly.diagnostic_results = diagnostic_results
                session.add(anomaly)
                session.flush()
                changed_anomalies.append(anomaly)
                dispatch_alerts(table, anomaly, _alert_rules_for_table(session, table.id))
        elif existing:
            # Metric returned to baseline - resolve the open anomaly and notify once.
            existing.status = "resolved"
            existing.resolved_at = now
            changed_anomalies.append(existing)
            dispatch_resolution(table, existing, _alert_rules_for_table(session, table.id))

    # Baseline status: "active" once past the learning window, "learning" otherwise.
    if not in_learning_period(table.created_at, now, table.learning_period_days):
        table.baseline_status = "active"
    else:
        table.baseline_status = "learning"

    session.commit()

    if own_session:
        session.close()
    return changed_anomalies


def run_all_checks(now: datetime | None = None) -> dict[str, list[Anomaly]]:
    """Run a check cycle for every active monitored table (paused tables are skipped inside run_check)."""
    session = get_session()
    tables = (
        session.execute(select(MonitoredTable).where(MonitoredTable.is_active.is_(True)))
        .scalars()
        .all()
    )
    results = {}
    for table in tables:
        anomalies = run_check(table, session=session, now=now)
        results[table.full_name] = anomalies
    session.close()
    return results
