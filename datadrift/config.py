"""
Central configuration for DataDrift.

Settings are read from environment variables with sensible defaults so the
project can be demoed with zero external services (SQLite storage, no SMTP,
no Slack webhook -> alerts are just logged).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATADRIFT_HOME", PROJECT_ROOT / ".datadrift"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    # Where DataDrift stores its own metadata (monitored_tables, baselines,
    # snapshots, anomalies, alert_rules). Defaults to a local SQLite file so
    # the whole project runs with zero external infrastructure. Point this at
    # a PostgreSQL URL in production, e.g. postgresql+psycopg2://user:pass@host/db
    storage_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATADRIFT_STORAGE_URL", f"sqlite:///{DATA_DIR / 'datadrift.db'}"
        )
    )

    # Default z-score threshold used when a table doesn't override it.
    default_zscore_threshold: float = float(
        os.environ.get("DATADRIFT_ZSCORE_THRESHOLD", 3.0)
    )

    # Learning period (days) before anomalies are raised for a newly added table.
    default_learning_period_days: int = int(
        os.environ.get("DATADRIFT_LEARNING_PERIOD_DAYS", 14)
    )

    # Recommended minimum history (days) before results are treated as
    # "high confidence" (see docs section 6.2).
    high_confidence_days: int = int(
        os.environ.get("DATADRIFT_HIGH_CONFIDENCE_DAYS", 28)
    )

    # Rolling window size (number of historical points) used to (re)compute
    # the baseline mean/stddev.
    rolling_window_size: int = int(os.environ.get("DATADRIFT_WINDOW_SIZE", 30))

    # Minimum standard deviation floor to avoid division-by-zero / hair-trigger
    # alerts on near-constant metrics.
    min_stddev_floor: float = float(os.environ.get("DATADRIFT_MIN_STDDEV", 1e-6))

    # Minimum number of prior readings required before a metric can be flagged
    # as anomalous. With fewer points than this, any tiny deviation gets
    # divided by the near-zero stddev floor and looks like an astronomical
    # z-score (a single-point "baseline" has no real variance estimate at
    # all) - so anomaly detection is deferred, not just made less confident,
    # until there's enough history to compute a meaningful stddev.
    min_history_points: int = int(os.environ.get("DATADRIFT_MIN_HISTORY_POINTS", 3))

    # Email (SMTP) alerting - all optional, alert is skipped (and logged) if unset.
    smtp_host: str | None = os.environ.get("DATADRIFT_SMTP_HOST")
    smtp_port: int = int(os.environ.get("DATADRIFT_SMTP_PORT", 587))
    smtp_user: str | None = os.environ.get("DATADRIFT_SMTP_USER")
    smtp_password: str | None = os.environ.get("DATADRIFT_SMTP_PASSWORD")
    smtp_from: str | None = os.environ.get("DATADRIFT_SMTP_FROM")

    # Scheduler default interval (used by `datadrift start`), in minutes.
    default_check_interval_minutes: int = int(
        os.environ.get("DATADRIFT_CHECK_INTERVAL_MINUTES", 60)
    )


settings = Settings()
