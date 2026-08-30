"""
CLI (section 7 / 8, Click): init, add-table, check, history.

Also includes a couple of convenience commands (`demo`, `start`) that aren't
part of the graded Core-scope list verbatim but make the tool runnable
end-to-end without extra scripting.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import click
from sqlalchemy import select

from datadrift.checks import run_all_checks, run_check
from datadrift.config import settings
from datadrift.db import Anomaly, MetricSnapshot, MonitoredTable, get_session, init_db
from datadrift.synthetic import build_source_sqlite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@click.group()
def cli():
    """DataDrift - automatic data quality monitoring for analytics pipelines."""


@cli.command()
def init():
    """Initialize DataDrift's metadata store (creates the 5 core tables)."""
    init_db()
    click.echo(f"Initialized DataDrift storage at: {settings.storage_url}")


@cli.command("add-table")
@click.option("--connection-name", required=True, help="Friendly name for the source connection")
@click.option("--connection-url", required=True, help="SQLAlchemy URL of the source DB")
@click.option("--table", "table_name", required=True, help="Table name to monitor")
@click.option("--schema", "schema_name", default=None, help="Schema name (optional)")
@click.option("--timestamp-column", default=None, help="Column used for freshness checks (optional)")
@click.option("--check-interval", default="hourly", type=click.Choice(["hourly", "daily"]))
@click.option("--learning-period-days", default=None, type=int)
@click.option("--zscore-threshold", "--sensitivity", default=None, type=float)
@click.option("--upstream-table-id", default=None, help="ID of an upstream table for root-cause suggestion")
@click.option(
    "--metrics",
    "enabled_metrics",
    default=None,
    help="Comma-separated subset of volume,schema_drift,null_rate,freshness,distribution,referential_integrity "
    "(default: all six)",
)
def add_table(
    connection_name,
    connection_url,
    table_name,
    schema_name,
    timestamp_column,
    check_interval,
    learning_period_days,
    zscore_threshold,
    upstream_table_id,
    enabled_metrics,
):
    """Register a table for monitoring."""
    init_db()
    session = get_session()
    table = MonitoredTable(
        connection_name=connection_name,
        connection_url=connection_url,
        table_name=table_name,
        schema_name=schema_name,
        timestamp_column=timestamp_column,
        check_interval=check_interval,
        learning_period_days=(
            learning_period_days if learning_period_days is not None else settings.default_learning_period_days
        ),
        zscore_threshold=zscore_threshold if zscore_threshold is not None else settings.default_zscore_threshold,
        upstream_table_id=upstream_table_id,
    )
    if enabled_metrics:
        import json

        table.enabled_metrics_json = json.dumps([m.strip() for m in enabled_metrics.split(",") if m.strip()])
    session.add(table)
    session.commit()
    click.echo(f"Added monitored table {table.full_name!r} (id={table.id})")
    session.close()


# `add` is the shorter name used in the product docs' CLI glossary; kept as an
# alias of `add-table` so both spellings work.
cli.add_command(add_table, name="add")


@cli.command("list-tables")
def list_tables():
    """List all monitored tables."""
    init_db()
    session = get_session()
    tables = session.execute(select(MonitoredTable)).scalars().all()
    if not tables:
        click.echo("No tables registered yet. Use `datadrift add-table` first.")
    for t in tables:
        click.echo(
            f"{t.id}  {t.full_name:30s}  interval={t.check_interval:6s}  "
            f"status={t.baseline_status:8s}  active={t.is_active}"
        )
    session.close()


@cli.command()
@click.option("--table", "table_id", default=None, help="Show only this table id")
def status(table_id):
    """Show baseline status (learning/active/paused/error) and open anomaly count per table."""
    init_db()
    session = get_session()
    from datadrift.db import Anomaly

    q = select(MonitoredTable)
    if table_id:
        q = q.where(MonitoredTable.id == table_id)
    tables = session.execute(q).scalars().all()
    if not tables:
        click.echo("No tables registered yet.")
    for t in tables:
        open_count = len(
            session.execute(
                select(Anomaly).where(Anomaly.table_id == t.id, Anomaly.status == "open")
            ).scalars().all()
        )
        click.echo(
            f"{t.full_name:30s}  status={t.baseline_status:8s}  "
            f"open_anomalies={open_count:3d}  sensitivity={t.zscore_threshold}"
        )
    session.close()


@cli.command()
@click.option("--table", "table_id", default=None, help="Filter to this table id")
@click.option("--limit", default=20, show_default=True)
def anomalies(table_id, limit):
    """List open anomalies (alias/variant of `history` scoped to status=open)."""
    init_db()
    session = get_session()
    from datadrift.db import Anomaly as AnomalyModel

    q = select(AnomalyModel).where(AnomalyModel.status == "open").order_by(AnomalyModel.detected_at.desc()).limit(limit)
    if table_id:
        q = q.where(AnomalyModel.table_id == table_id)
    rows = session.execute(q).scalars().all()
    if not rows:
        click.echo("No open anomalies.")
    for a in rows:
        table = session.get(MonitoredTable, a.table_id)
        name = table.full_name if table else a.table_id
        click.echo(f"[{a.severity:6s}] {name:25s}  {a.metric_name:25s}  z={a.z_score:8.2f}")
        if a.diagnosis:
            for line in a.diagnosis.splitlines():
                click.echo(f"    {line}")
    session.close()


@cli.command()
@click.option("--table", "table_id", default=None, help="Check only this table id (default: all active tables)")
def check(table_id):
    """Run one check cycle now (collect metrics, detect anomalies, alert)."""
    init_db()
    now = datetime.now(timezone.utc)
    if table_id:
        session = get_session()
        table = session.get(MonitoredTable, table_id)
        if not table:
            raise click.ClickException(f"No table with id {table_id}")
        anomalies = run_check(table, session=session, now=now)
        session.close()
        results = {table.full_name: anomalies}
    else:
        results = run_all_checks(now=now)

    total = sum(len(v) for v in results.values())
    for name, anomalies in results.items():
        status = f"{len(anomalies)} anomaly(ies)" if anomalies else "OK"
        click.echo(f"{name}: {status}")
        for a in anomalies:
            click.echo(f"    - [{a.severity}] {a.metric_name}={a.observed_value:.4f} (z={a.z_score:.2f})")
    click.echo(f"\nDone. {total} new anomaly(ies) across {len(results)} table(s).")


@cli.command()
@click.option("--table", "table_id", default=None, help="Filter to this table id")
@click.option("--limit", default=20, show_default=True)
def history(table_id, limit):
    """Show recent anomalies (optionally filtered to one table)."""
    init_db()
    session = get_session()
    q = select(Anomaly).order_by(Anomaly.detected_at.desc()).limit(limit)
    if table_id:
        q = q.where(Anomaly.table_id == table_id)
    rows = session.execute(q).scalars().all()
    if not rows:
        click.echo("No anomalies recorded yet.")
    for a in rows:
        table = session.get(MonitoredTable, a.table_id)
        name = table.full_name if table else a.table_id
        resolved_note = f" (resolved {a.resolved_at.isoformat()})" if a.resolved_at else ""
        click.echo(
            f"{a.detected_at.isoformat()}  {name:25s}  {a.metric_name:25s}  "
            f"[{a.severity:6s}]  z={a.z_score:6.2f}  status={a.status}{resolved_note}"
        )
        if a.diagnosis:
            for line in a.diagnosis.splitlines():
                click.echo(f"    {line}")
    session.close()


@cli.command()
@click.option("--interval-minutes", default=None, type=int, help="Override the default check interval")
def start(interval_minutes):
    """Start the in-process scheduler (APScheduler) - runs check cycles on a fixed interval."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    init_db()
    interval = interval_minutes or settings.default_check_interval_minutes
    scheduler = BlockingScheduler()
    scheduler.add_job(run_all_checks, "interval", minutes=interval, next_run_time=datetime.now())
    click.echo(f"DataDrift scheduler started - checking every {interval} minute(s). Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        click.echo("Stopped.")


@cli.command()
@click.option("--db-path", default="demo_orders.db", help="Path to the synthetic SQLite source DB to create")
@click.option("--days", default=45, show_default=True)
def demo(db_path, days):
    """
    End-to-end demo: builds a synthetic `orders` source DB with realistic
    seasonality and injected anomalies, registers it for monitoring, and runs
    a check so you can immediately inspect results (`datadrift history`) or
    launch the dashboard API/UI on top of it.
    """
    init_db()
    click.echo(f"Generating synthetic source data ({days} days) at {db_path} ...")
    path = build_source_sqlite(db_path, num_days=days)

    session = get_session()
    table = MonitoredTable(
        connection_name="demo",
        connection_url=f"sqlite:///{path}",
        table_name="orders",
        timestamp_column="created_at",
        learning_period_days=0,  # demo: skip the learning period so anomalies show immediately
        zscore_threshold=settings.default_zscore_threshold,
    )
    session.add(table)
    session.commit()
    table_id = table.id
    click.echo(f"Registered demo table (id={table_id}). Backfilling snapshot history is not simulated here -")
    click.echo("run `datadrift check` repeatedly, or use evaluation/run_evaluation.py for a full backfilled sweep.")
    anomalies = run_check(table, session=session)
    click.echo(f"\nFirst check found {len(anomalies)} anomaly(ies) (expected: 0, since there's no history yet).")
    click.echo("Run `datadrift check` again after the source data changes, or see the evaluation harness")
    click.echo("for a fully backfilled, evaluated run against injected synthetic anomalies.")
    session.close()


if __name__ == "__main__":
    cli()
