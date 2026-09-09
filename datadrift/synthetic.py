"""
Synthetic data generator (section 8, weeks 3-4 deliverable / section 10.1).

Produces a demo source database (SQLite by default, so it needs zero setup)
containing an `orders` table with realistic weekly-seasonal daily volume,
plus a matching set of pre-computed daily metric snapshots that can be
injected directly into DataDrift's own metric_snapshots table. This lets the
system be demoed and evaluated without waiting for the 14-28 day real-time
learning period.

Two things are produced:
  1. A source SQLite DB with an `orders` table (id, customer_email, amount,
     created_at, shipped_at) - realistic table DataDrift could monitor.
  2. A day-by-day series of {row_count, null_rate_*, freshness_minutes}
     values with known injected anomalies at known days/magnitudes, for
     the evaluation harness (see evaluation/run_evaluation.py).
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class InjectedAnomaly:
    day_index: int
    kind: str  # "volume_drop" | "null_spike" | "freshness_delay"
    magnitude: float  # e.g. 0.5 for a 50% volume drop, 0.2 for a 20% null rate
    metric_name: str
    tier: str = "moderate"  # "mild" | "moderate" | "severe" - see generate_anomaly_plan


_TIER_MAGNITUDES = {
    # volume_drop: fraction of baseline volume removed
    ("volume_drop", "mild"): (0.25, 0.40),
    ("volume_drop", "moderate"): (0.45, 0.70),
    ("volume_drop", "severe"): (0.75, 0.97),
    # null_spike: absolute null rate the column jumps to (baseline is ~0.01)
    ("null_spike", "mild"): (0.06, 0.12),
    ("null_spike", "moderate"): (0.15, 0.35),
    ("null_spike", "severe"): (0.40, 0.85),
    # freshness_delay: multiplier on the expected ~30-minute update interval
    ("freshness_delay", "mild"): (1.8, 3.0),
    ("freshness_delay", "moderate"): (3.5, 8.0),
    ("freshness_delay", "severe"): (10.0, 30.0),
}


def generate_anomaly_plan(
    num_anomalies: int,
    num_days: int,
    seed: int,
    learning_period_days: int = 14,
    min_gap_days: int = 6,
) -> list[InjectedAnomaly]:
    """
    Build a plan of `num_anomalies` injected anomalies spread evenly across
    the three metric types (row_count/volume_drop, null_rate/null_spike,
    freshness/freshness_delay) and three severity tiers (mild/moderate/severe),
    each on its own day, with at least `min_gap_days` between two anomalies
    on the *same* metric so one injected anomaly's effect on the rolling
    baseline has mostly settled before the next one on that metric arrives.

    Anomalies are only placed after `learning_period_days` (matching the
    detection engine's own learning period) and at least 2 days before the
    end of the series (so there's room to observe whether it gets detected).
    """
    rng = random.Random(seed)
    metric_kinds = [
        ("row_count", "volume_drop"),
        ("null_rate_customer_email", "null_spike"),
        ("freshness_minutes", "freshness_delay"),
    ]
    tiers = ["mild", "moderate", "severe"]

    # Distribute anomalies as evenly as possible across the 3 metrics.
    base_count, remainder = divmod(num_anomalies, len(metric_kinds))
    counts = [base_count + (1 if i < remainder else 0) for i in range(len(metric_kinds))]

    earliest_day = learning_period_days + 3
    latest_day = num_days - 2
    usable_range = latest_day - earliest_day

    anomalies: list[InjectedAnomaly] = []
    for (metric_name, kind), count in zip(metric_kinds, counts):
        if count == 0:
            continue
        step = usable_range / count
        if step < min_gap_days:
            raise ValueError(
                f"num_days={num_days} gives only {usable_range} usable days for {count} {metric_name} "
                f"anomalies at min_gap_days={min_gap_days} (need >= {count * min_gap_days}) - increase "
                "num_days or decrease num_anomalies/min_gap_days."
            )
        # Stratified placement: one anomaly per equal-width slice of the usable
        # range, with jitter inside a slice small enough to always preserve
        # min_gap_days between consecutive anomalies on this metric. This is
        # both deterministic-given-seed and guaranteed to succeed (unlike
        # pure rejection sampling, which fails unpredictably at high counts).
        jitter_max = max(0.0, (step - min_gap_days) / 2)
        for i in range(count):
            center = earliest_day + step * (i + 0.5)
            jitter = rng.uniform(-jitter_max, jitter_max) if jitter_max > 0 else 0.0
            day = int(round(center + jitter))
            day = min(max(day, earliest_day), latest_day)
            tier = tiers[i % len(tiers)]  # cycle tiers for an even spread
            lo, hi = _TIER_MAGNITUDES[(kind, tier)]
            magnitude = rng.uniform(lo, hi)
            anomalies.append(InjectedAnomaly(day, kind, magnitude, metric_name, tier))

    return sorted(anomalies, key=lambda a: a.day_index)


def _seasonal_base_volume(day_index: int, base: int = 500, weekend_factor: float = 0.6) -> float:
    """Weekly seasonality: lower volume on weekends (day_index 5,6 of each 7)."""
    weekday = day_index % 7
    factor = weekend_factor if weekday in (5, 6) else 1.0
    # gentle upward trend over time to exercise the moving-window adaptation
    trend = 1.0 + 0.002 * day_index
    return base * factor * trend


def generate_daily_series(
    num_days: int = 45,
    seed: int = 42,
    inject_anomalies: bool = True,
    anomaly_plan: list[InjectedAnomaly] | None = None,
) -> tuple[list[dict], list[InjectedAnomaly]]:
    """
    Returns (daily_records, injected_anomalies).
    daily_records[i] = {"day_index", "date", "row_count", "null_rate_email",
                         "freshness_minutes"}

    If `anomaly_plan` is given (see generate_anomaly_plan), it's used as-is -
    this is how the large-scale (e.g. 50-anomaly) validation experiment
    injects anomalies. Otherwise falls back to the small fixed demo set used
    by the quick `evaluation/run_evaluation.py` default run.
    """
    rng = random.Random(seed)
    records = []
    injected: list[InjectedAnomaly] = []
    start = datetime.now(timezone.utc) - timedelta(days=num_days)

    anomaly_days: dict[int, InjectedAnomaly] = {}
    if anomaly_plan is not None:
        injected = list(anomaly_plan)
        for a in anomaly_plan:
            anomaly_days[a.day_index] = a
    elif inject_anomalies:
        candidates = [
            (num_days - 10, "volume_drop", 0.5, "row_count"),
            (num_days - 8, "volume_drop", 0.8, "row_count"),
            (num_days - 6, "volume_drop", 0.95, "row_count"),
            (num_days - 5, "null_spike", 0.2, "null_rate_customer_email"),
            (num_days - 4, "null_spike", 0.5, "null_rate_customer_email"),
            (num_days - 3, "null_spike", 0.8, "null_rate_customer_email"),
            (num_days - 2, "freshness_delay", 2.0, "freshness_minutes"),
            (num_days - 1, "freshness_delay", 10.0, "freshness_minutes"),
        ]
        for day_index, kind, magnitude, metric_name in candidates:
            if 0 <= day_index < num_days:
                anomaly = InjectedAnomaly(day_index, kind, magnitude, metric_name)
                anomaly_days[day_index] = anomaly
                injected.append(anomaly)

    for day_index in range(num_days):
        base_volume = _seasonal_base_volume(day_index)
        row_count = max(0, int(rng.gauss(base_volume, base_volume * 0.05)))
        null_rate = max(0.0, rng.gauss(0.01, 0.003))
        freshness = max(0.0, rng.gauss(30, 5))  # expected update interval ~30 min

        anomaly = anomaly_days.get(day_index)
        if anomaly:
            if anomaly.kind == "volume_drop":
                row_count = int(base_volume * (1 - anomaly.magnitude))
            elif anomaly.kind == "null_spike":
                null_rate = anomaly.magnitude
            elif anomaly.kind == "freshness_delay":
                freshness = 30 * anomaly.magnitude

        records.append(
            {
                "day_index": day_index,
                "date": (start + timedelta(days=day_index)).date().isoformat(),
                "row_count": float(row_count),
                "null_rate_customer_email": float(null_rate),
                "freshness_minutes": float(freshness),
            }
        )

    return records, injected


def build_source_sqlite(path: str | Path, num_days: int = 45, seed: int = 42) -> Path:
    """
    Build an actual SQLite `orders` table on disk whose final day's data
    matches the last record of generate_daily_series - useful for a live CLI
    demo (`datadrift add-table`) against a real (if synthetic) database file,
    as opposed to the pre-computed series used by the evaluation harness.
    """
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_email TEXT,
            amount REAL,
            created_at TEXT,
            shipped_at TEXT
        )
        """
    )
    records, _ = generate_daily_series(num_days=num_days, seed=seed, inject_anomalies=True)
    rng = random.Random(seed)
    row_id = 1
    for rec in records:
        n = int(rec["row_count"])
        null_rate = rec["null_rate_customer_email"]
        day = rec["date"]
        for _ in range(n):
            email = None if rng.random() < null_rate else f"user{row_id}@example.com"
            amount = round(rng.uniform(5, 500), 2)
            created_at = f"{day}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:00"
            conn.execute(
                "INSERT INTO orders (id, customer_email, amount, created_at, shipped_at) VALUES (?,?,?,?,?)",
                (row_id, email, amount, created_at, created_at),
            )
            row_id += 1
    conn.commit()
    conn.close()
    return path
