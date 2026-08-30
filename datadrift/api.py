"""
API layer: serves data to the React dashboard and accepts configuration
changes. Endpoint shapes follow the "API Design" section of the product
documentation as closely as practical.

Run with:  uvicorn datadrift.api:app --reload --port 8000
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select

from datadrift.alerting import send_email, send_slack, send_webhook
from datadrift.checks import run_check
from datadrift.db import (
    AlertRule,
    Anomaly,
    MetricBaseline,
    MetricSnapshot,
    MonitoredTable,
    ReferentialCheck,
    get_session,
    init_db,
)
from datadrift.schema_drift import diff_schema

app = FastAPI(title="DataDrift API", version="0.1.0")

# Dashboard runs on a different dev-server port (Vite default 5173); allow it in dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------- schemas ----------


class MonitoredTableIn(BaseModel):
    connection_name: str
    connection_url: str
    table_name: str
    schema_name: str | None = None
    timestamp_column: str | None = None
    check_interval: str = "hourly"
    learning_period_days: int | None = None
    zscore_threshold: float | None = None
    upstream_table_id: str | None = None
    enabled_metrics: list[str] | None = None


class MonitoredTablePatch(BaseModel):
    zscore_threshold: float | None = None
    check_interval: str | None = None
    enabled_metrics: list[str] | None = None
    baseline_status: str | None = None  # set to "paused" / "learning" to pause/resume


class AlertRuleIn(BaseModel):
    table_id: str | None = None
    min_severity: str = "low"
    channel: str  # email / slack / webhook
    destination: str


class AlertTestIn(BaseModel):
    channel: str
    destination: str


class AnomalyStatusIn(BaseModel):
    status: str  # open / acknowledged / resolved


class ReferentialCheckIn(BaseModel):
    source_column: str
    ref_table_name: str
    ref_schema_name: str | None = None
    ref_column: str


def _table_to_dict(t: MonitoredTable) -> dict:
    return {
        "id": t.id,
        "connection_name": t.connection_name,
        "full_name": t.full_name,
        "table_name": t.table_name,
        "schema_name": t.schema_name,
        "check_interval": t.check_interval,
        "learning_period_days": t.learning_period_days,
        "sensitivity_multiplier": t.sensitivity_multiplier,
        "enabled_metrics": t.enabled_metrics,
        "baseline_status": t.baseline_status,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat(),
    }


def _anomaly_to_dict(session, a: Anomaly) -> dict:
    table = session.get(MonitoredTable, a.table_id)
    return {
        "id": a.id,
        "table_id": a.table_id,
        "table_name": table.full_name if table else a.table_id,
        "metric_name": a.metric_name,
        "observed_value": a.observed_value,
        "expected_range": a.expected_range,
        "z_score": a.z_score,
        "severity": a.severity,
        "diagnosis": a.diagnosis,
        "diagnostic_results": a.diagnostic_results,
        "detected_at": a.detected_at.isoformat(),
        "last_seen_at": a.last_seen_at.isoformat(),
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        "status": a.status,
    }


# ---------- Table Management ----------


@app.get("/api/tables")
def list_tables():
    session = get_session()
    tables = session.execute(select(MonitoredTable)).scalars().all()
    out = [_table_to_dict(t) for t in tables]
    session.close()
    return out


@app.post("/api/tables")
def add_table(payload: MonitoredTableIn):
    session = get_session()
    data = payload.model_dump()
    enabled_metrics = data.pop("enabled_metrics", None)
    table = MonitoredTable(**{k: v for k, v in data.items() if v is not None})
    if enabled_metrics:
        table.enabled_metrics_json = json.dumps(enabled_metrics)
    session.add(table)
    session.commit()
    result = _table_to_dict(table)
    session.close()
    return result


@app.get("/api/tables/{table_id}")
def get_table(table_id: str):
    session = get_session()
    table = session.get(MonitoredTable, table_id)
    if not table:
        session.close()
        raise HTTPException(404, "table not found")
    open_count = len(
        session.execute(
            select(Anomaly).where(Anomaly.table_id == table_id, Anomaly.status == "open")
        ).scalars().all()
    )
    result = {**_table_to_dict(table), "open_anomaly_count": open_count}
    session.close()
    return result


@app.patch("/api/tables/{table_id}")
def update_table(table_id: str, payload: MonitoredTablePatch):
    session = get_session()
    table = session.get(MonitoredTable, table_id)
    if not table:
        session.close()
        raise HTTPException(404, "table not found")
    if payload.zscore_threshold is not None:
        table.zscore_threshold = payload.zscore_threshold
    if payload.check_interval is not None:
        table.check_interval = payload.check_interval
    if payload.enabled_metrics is not None:
        table.enabled_metrics_json = json.dumps(payload.enabled_metrics)
    if payload.baseline_status is not None:
        table.baseline_status = payload.baseline_status
    session.commit()
    result = _table_to_dict(table)
    session.close()
    return result


@app.delete("/api/tables/{table_id}")
def delete_table(table_id: str):
    session = get_session()
    table = session.get(MonitoredTable, table_id)
    if not table:
        session.close()
        raise HTTPException(404, "table not found")
    session.delete(table)  # cascades to snapshots/baselines/anomalies/alert_rules/referential_checks
    session.commit()
    session.close()
    return {"deleted": True}


@app.post("/api/tables/{table_id}/check")
def trigger_check(table_id: str):
    session = get_session()
    table = session.get(MonitoredTable, table_id)
    if not table:
        session.close()
        raise HTTPException(404, "table not found")
    anomalies = run_check(table, session=session)
    out = [{"id": a.id, "metric_name": a.metric_name, "severity": a.severity, "status": a.status} for a in anomalies]
    session.close()
    return {"changed_anomalies": out}


# ---------- Metrics and Snapshots ----------


@app.get("/api/tables/{table_id}/metrics")
def table_metrics(table_id: str):
    """Most recent snapshot for every metric on a table (table health view)."""
    session = get_session()
    table = session.get(MonitoredTable, table_id)
    if not table:
        session.close()
        raise HTTPException(404, "table not found")

    metric_names = (
        session.execute(
            select(MetricSnapshot.metric_name).where(MetricSnapshot.table_id == table_id).distinct()
        )
        .scalars()
        .all()
    )
    metrics = []
    for name in metric_names:
        latest = (
            session.execute(
                select(MetricSnapshot)
                .where(MetricSnapshot.table_id == table_id, MetricSnapshot.metric_name == name)
                .order_by(MetricSnapshot.recorded_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        baseline = session.execute(
            select(MetricBaseline).where(
                MetricBaseline.table_id == table_id, MetricBaseline.metric_name == name
            )
        ).scalar_one_or_none()
        metrics.append(
            {
                "metric_name": name,
                "latest_value": latest.value if latest else None,
                "recorded_at": latest.recorded_at.isoformat() if latest else None,
                "baseline_mean": baseline.rolling_mean if baseline else None,
                "baseline_stddev": baseline.rolling_stddev if baseline else None,
            }
        )
    session.close()
    return metrics


@app.get("/api/tables/{table_id}/history")
def table_history(table_id: str, metric_name: str | None = None, metric_type: str | None = None, days: int = 30):
    """Time series for metric charts. `metric_type` is an alias some clients may
    pass (e.g. 'volume'); `metric_name` (e.g. 'row_count') takes precedence."""
    session = get_session()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(MetricSnapshot).where(
        MetricSnapshot.table_id == table_id, MetricSnapshot.recorded_at >= cutoff
    )
    if metric_name:
        q = q.where(MetricSnapshot.metric_name == metric_name)
    rows = session.execute(q.order_by(MetricSnapshot.recorded_at.asc())).scalars().all()
    out = [
        {"metric_name": r.metric_name, "value": r.value, "recorded_at": r.recorded_at.isoformat()}
        for r in rows
    ]
    session.close()
    return out


@app.get("/api/tables/{table_id}/schema")
def table_schema(table_id: str):
    """Current schema snapshot (and, if present, a diff against the prior one is
    already applied at check time - anomalies of metric_name='schema_drift' hold
    the historical diff text)."""
    session = get_session()
    table = session.get(MonitoredTable, table_id)
    if not table:
        session.close()
        raise HTTPException(404, "table not found")
    result = {
        "schema_snapshot": table.schema_snapshot,
        "schema_snapshot_updated_at": (
            table.schema_snapshot_updated_at.isoformat() if table.schema_snapshot_updated_at else None
        ),
    }
    session.close()
    return result


@app.get("/api/tables/{table_id}/baseline")
def table_baseline(table_id: str):
    session = get_session()
    rows = session.execute(select(MetricBaseline).where(MetricBaseline.table_id == table_id)).scalars().all()
    out = [
        {
            "metric_name": b.metric_name,
            "rolling_mean": b.rolling_mean,
            "rolling_stddev": b.rolling_stddev,
            "window_size": b.window_size,
            "last_updated": b.last_updated.isoformat(),
        }
        for b in rows
    ]
    session.close()
    return out


# ---------- Referential integrity config ----------


@app.post("/api/tables/{table_id}/referential-checks")
def add_referential_check(table_id: str, payload: ReferentialCheckIn):
    session = get_session()
    table = session.get(MonitoredTable, table_id)
    if not table:
        session.close()
        raise HTTPException(404, "table not found")
    rc = ReferentialCheck(table_id=table_id, **payload.model_dump())
    session.add(rc)
    session.commit()
    result = {"id": rc.id}
    session.close()
    return result


@app.get("/api/tables/{table_id}/referential-checks")
def list_referential_checks(table_id: str):
    session = get_session()
    rows = session.execute(select(ReferentialCheck).where(ReferentialCheck.table_id == table_id)).scalars().all()
    out = [
        {
            "id": r.id,
            "source_column": r.source_column,
            "ref_table_name": r.ref_table_name,
            "ref_schema_name": r.ref_schema_name,
            "ref_column": r.ref_column,
            "is_active": r.is_active,
        }
        for r in rows
    ]
    session.close()
    return out


# ---------- Anomalies ----------


@app.get("/api/anomalies")
def list_anomalies(status: str | None = None, severity: str | None = None, table_id: str | None = None, days: int | None = None, limit: int = 200):
    session = get_session()
    q = select(Anomaly)
    if status:
        q = q.where(Anomaly.status == status)
    if severity:
        q = q.where(Anomaly.severity == severity)
    if table_id:
        q = q.where(Anomaly.table_id == table_id)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        q = q.where(Anomaly.detected_at >= cutoff)
    rows = session.execute(q.order_by(Anomaly.detected_at.desc()).limit(limit)).scalars().all()
    out = [_anomaly_to_dict(session, a) for a in rows]
    session.close()
    return out


@app.get("/api/anomalies/active")
def active_anomalies():
    session = get_session()
    rows = session.execute(select(Anomaly).where(Anomaly.status == "open")).scalars().all()
    out = [_anomaly_to_dict(session, a) for a in rows]
    session.close()
    return out


@app.get("/api/anomalies/summary")
def anomalies_summary():
    """Count of anomalies by severity and table, for the dashboard overview."""
    session = get_session()
    rows = session.execute(select(Anomaly).where(Anomaly.status == "open")).scalars().all()
    by_severity: dict[str, int] = {}
    by_table: dict[str, int] = {}
    for a in rows:
        by_severity[a.severity] = by_severity.get(a.severity, 0) + 1
        table = session.get(MonitoredTable, a.table_id)
        name = table.full_name if table else a.table_id
        by_table[name] = by_table.get(name, 0) + 1
    session.close()
    return {"total_open": len(rows), "by_severity": by_severity, "by_table": by_table}


@app.get("/api/anomalies/{anomaly_id}")
def get_anomaly(anomaly_id: str):
    session = get_session()
    a = session.get(Anomaly, anomaly_id)
    if not a:
        session.close()
        raise HTTPException(404, "anomaly not found")
    result = _anomaly_to_dict(session, a)
    session.close()
    return result


@app.post("/api/anomalies/{anomaly_id}/acknowledge")
def acknowledge_anomaly(anomaly_id: str):
    session = get_session()
    a = session.get(Anomaly, anomaly_id)
    if not a:
        session.close()
        raise HTTPException(404, "anomaly not found")
    a.status = "acknowledged"
    session.commit()
    session.close()
    return {"ok": True}


@app.patch("/api/anomalies/{anomaly_id}")
def update_anomaly_status(anomaly_id: str, payload: AnomalyStatusIn):
    session = get_session()
    anomaly = session.get(Anomaly, anomaly_id)
    if not anomaly:
        session.close()
        raise HTTPException(404, "anomaly not found")
    anomaly.status = payload.status
    if payload.status == "resolved" and not anomaly.resolved_at:
        anomaly.resolved_at = datetime.now(timezone.utc)
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Alert Rules ----------


@app.get("/api/tables/{table_id}/alerts")
def list_table_alerts(table_id: str):
    session = get_session()
    rows = session.execute(select(AlertRule).where(AlertRule.table_id == table_id)).scalars().all()
    out = [
        {
            "id": r.id,
            "table_id": r.table_id,
            "min_severity": r.min_severity,
            "channel": r.channel,
            "destination": r.destination,
            "is_active": r.is_active,
        }
        for r in rows
    ]
    session.close()
    return out


@app.post("/api/tables/{table_id}/alerts")
def add_table_alert(table_id: str, payload: AlertRuleIn):
    session = get_session()
    rule = AlertRule(table_id=table_id, min_severity=payload.min_severity, channel=payload.channel, destination=payload.destination)
    session.add(rule)
    session.commit()
    result = {"id": rule.id}
    session.close()
    return result


@app.get("/api/alert-rules")
def list_alert_rules():
    """All alert rules, including global (table_id=null) ones."""
    session = get_session()
    rows = session.execute(select(AlertRule)).scalars().all()
    out = [
        {
            "id": r.id,
            "table_id": r.table_id,
            "min_severity": r.min_severity,
            "channel": r.channel,
            "destination": r.destination,
            "is_active": r.is_active,
        }
        for r in rows
    ]
    session.close()
    return out


@app.post("/api/alert-rules")
def add_global_alert_rule(payload: AlertRuleIn):
    session = get_session()
    rule = AlertRule(**payload.model_dump())
    session.add(rule)
    session.commit()
    result = {"id": rule.id}
    session.close()
    return result


@app.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: str):
    session = get_session()
    rule = session.get(AlertRule, alert_id)
    if not rule:
        session.close()
        raise HTTPException(404, "alert rule not found")
    session.delete(rule)
    session.commit()
    session.close()
    return {"deleted": True}


@app.post("/api/alerts/test")
def test_alert(payload: AlertTestIn):
    """Send a test alert to verify delivery, per the documented endpoint."""
    subject = "DataDrift test alert"
    body = "This is a test alert from DataDrift to verify this destination is reachable."
    if payload.channel == "email":
        sent = send_email(payload.destination, subject, body)
    elif payload.channel == "slack":
        sent = send_slack(payload.destination, body)
    elif payload.channel == "webhook":
        sent = send_webhook(payload.destination, {"event": "test", "message": body})
    else:
        raise HTTPException(400, f"unknown channel {payload.channel}")
    return {"sent": sent}


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
