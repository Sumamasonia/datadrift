"""
Statistical engine: baseline learning + rolling z-score anomaly detection,
with seasonal decomposition for weekly-seasonal metrics.

Implements the baseline engine from the product documentation:
  - During the learning period (default 14 days, configurable), metrics are
    recorded but never flagged as anomalies.
  - Once enough history exists (>= 2 full periods, i.e. >=14 points for
    weekly seasonality), the engine runs statsmodels seasonal_decompose
    (period=7) to separate the weekly pattern from the trend, and computes
    the z-score of the new observation against the seasonally-adjusted
    baseline (observed - seasonal_mean) / residual_std. This avoids flagging
    e.g. "Sunday is always quieter than Friday" as an anomaly every week.
  - With too little history for seasonal decomposition, the engine falls
    back to a plain rolling mean/stddev z-score over the trailing window -
    documented as a graceful degradation, not silent failure.
  - A minimum stddev floor avoids division-by-zero / hair-trigger alerts on
    near-constant metrics.
  - Baselines are a moving window, not fixed, so they adapt to legitimate
    long-term shifts (e.g. organic growth) rather than treating growth as a
    permanent anomaly.

This module is intentionally free of any DB/network I/O so it can be unit
tested in isolation (see tests/test_detection.py) and reused by both the
live scheduler and the offline evaluation harness.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from datadrift.config import settings

SEASONAL_PERIOD = 7  # weekly seasonality on daily-granularity checks


def _as_aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip even for DateTime(timezone=True) columns;
    normalize any naive datetime to UTC so subtraction never raises."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass
class DetectionResult:
    metric_name: str
    value: float
    mean: float
    stddev: float
    z_score: float
    is_anomaly: bool
    severity: str | None  # low / medium / high, or None if not anomalous
    confidence: str  # "low" (<28d history) or "high" (>=28d history)
    method: str = "rolling_zscore"  # "rolling_zscore" or "seasonal_zscore"


def severity_for_zscore(z: float) -> str:
    az = abs(z)
    if az >= 5:
        return "high"
    if az >= 4:
        return "medium"
    return "low"


def rolling_baseline(values: list[float], window_size: int | None = None) -> tuple[float, float]:
    """Compute (mean, stddev) over the trailing `window_size` values."""
    window_size = window_size or settings.rolling_window_size
    windowed = values[-window_size:] if window_size else values
    n = len(windowed)
    if n == 0:
        return 0.0, 0.0
    mean = sum(windowed) / n
    if n < 2:
        return mean, 0.0
    variance = sum((v - mean) ** 2 for v in windowed) / (n - 1)
    return mean, variance ** 0.5


def seasonal_baseline(
    history: list[float], period: int = SEASONAL_PERIOD
) -> tuple[float, float] | None:
    """
    Run statsmodels seasonal_decompose over `history` and return
    (expected_value_for_next_point, residual_stddev), or None if there isn't
    enough history (needs at least 2 full periods) or decomposition fails
    (e.g. a metric that is exactly constant, which statsmodels can reject).
    """
    if len(history) < period * 2:
        return None
    try:
        import pandas as pd
        from statsmodels.tsa.seasonal import seasonal_decompose

        series = pd.Series(history)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = seasonal_decompose(
                series, period=period, model="additive", extrapolate_trend="freq"
            )

        seasonal = result.seasonal
        trend = result.trend.dropna()
        resid = result.resid.dropna()

        if trend.empty:
            return None

        # Position in the seasonal cycle that the *next* (new) observation falls on.
        next_position = len(history) % period
        seasonal_component = float(seasonal.iloc[-period:].reset_index(drop=True).iloc[next_position])
        expected_trend = float(trend.iloc[-1])
        expected_value = expected_trend + seasonal_component
        residual_std = float(resid.std()) if len(resid) > 1 else 0.0

        return expected_value, residual_std
    except Exception:
        # Decomposition can fail on short, constant, or degenerate series -
        # fall back to the plain rolling baseline rather than raising.
        return None


def in_learning_period(
    first_seen_at: datetime,
    now: datetime | None = None,
    learning_period_days: int | None = None,
) -> bool:
    first_seen_at = _as_aware(first_seen_at)
    now = _as_aware(now) if now else datetime.now(timezone.utc)
    # NB: `learning_period_days or default` would silently replace an explicit 0
    # (e.g. a demo table configured to skip the learning period) with the 14-day
    # default, since 0 is falsy - use an explicit None check instead.
    if learning_period_days is None:
        learning_period_days = settings.default_learning_period_days
    return (now - first_seen_at) < timedelta(days=learning_period_days)


def confidence_level(
    first_seen_at: datetime,
    now: datetime | None = None,
) -> str:
    first_seen_at = _as_aware(first_seen_at)
    now = _as_aware(now) if now else datetime.now(timezone.utc)
    return "high" if (now - first_seen_at) >= timedelta(days=settings.high_confidence_days) else "low"


def detect(
    metric_name: str,
    new_value: float,
    history: list[float],
    first_seen_at: datetime,
    now: datetime | None = None,
    threshold: float | None = None,
    learning_period_days: int | None = None,
    window_size: int | None = None,
    use_seasonal: bool = True,
) -> DetectionResult:
    """
    Evaluate a single new metric reading against its rolling baseline.

    `history` should be prior readings for this metric, oldest first, NOT
    including `new_value`.
    """
    now = _as_aware(now) if now else datetime.now(timezone.utc)
    threshold = threshold if threshold is not None else settings.default_zscore_threshold

    method = "rolling_zscore"
    seasonal = seasonal_baseline(history) if use_seasonal else None
    if seasonal is not None:
        mean, stddev = seasonal
        method = "seasonal_zscore"
    else:
        mean, stddev = rolling_baseline(history, window_size)

    stddev = max(stddev, settings.min_stddev_floor)
    z = (new_value - mean) / stddev if history else 0.0

    still_learning = in_learning_period(first_seen_at, now, learning_period_days)
    is_anomaly = (not still_learning) and abs(z) >= threshold and len(history) > 0

    return DetectionResult(
        metric_name=metric_name,
        value=new_value,
        mean=mean,
        stddev=stddev,
        z_score=z,
        is_anomaly=is_anomaly,
        severity=severity_for_zscore(z) if is_anomaly else None,
        confidence=confidence_level(first_seen_at, now),
        method=method,
    )
