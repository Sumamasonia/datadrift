"""
DataDrift - Automatic Data Quality Monitoring for Analytics Pipelines.

Core (must-have) scope implemented here, per the project documentation:
  - Multi-dialect DB connector layer (PostgreSQL, MySQL, SQLite, DuckDB via SQLAlchemy)
  - Metric collection (row volume, null rate, freshness)
  - Baseline learning + rolling z-score anomaly detection
  - Alerting (email SMTP, Slack incoming webhook)
  - FastAPI API layer for the dashboard
  - Click CLI: init, add-table, check, history
  - Synthetic data generator for demoing without a real warehouse
  - Evaluation harness (precision / recall / detection latency) - see evaluation/

Stretch scope implemented (best-effort, documented as such):
  - Schema drift detection (information_schema snapshot diffing) - datadrift/schema_drift.py
  - Basic two-table root cause suggestion - datadrift/root_cause.py
"""

__version__ = "0.1.0"
