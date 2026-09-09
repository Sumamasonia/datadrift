"""Unit tests for datadrift.detection - the core rolling z-score algorithm."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from datadrift.detection import detect, rolling_baseline, seasonal_baseline, severity_for_zscore


def test_rolling_baseline_basic():
    mean, stddev = rolling_baseline([10, 10, 10, 10])
    assert mean == 10
    assert stddev == 0


def test_severity_thresholds():
    assert severity_for_zscore(3.1) == "low"
    assert severity_for_zscore(4.2) == "medium"
    assert severity_for_zscore(5.5) == "high"
    assert severity_for_zscore(-5.5) == "high"


def test_no_anomaly_during_learning_period():
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=1)  # well within the default 14-day learning period
    result = detect("row_count", new_value=0, history=[500] * 10, first_seen_at=first_seen, now=now)
    assert result.is_anomaly is False


def test_anomaly_detected_after_learning_period():
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=20)  # past the learning period
    history = [500, 505, 495, 510, 490, 500, 502, 498, 501, 499] * 3
    result = detect("row_count", new_value=50, history=history, first_seen_at=first_seen, now=now)
    assert result.is_anomaly is True
    assert result.severity in {"low", "medium", "high"}


def test_normal_reading_not_flagged():
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=20)
    history = [500, 505, 495, 510, 490, 500, 502, 498, 501, 499] * 3
    result = detect("row_count", new_value=503, history=history, first_seen_at=first_seen, now=now)
    assert result.is_anomaly is False


def test_seasonal_baseline_not_enough_history_returns_none():
    assert seasonal_baseline([1, 2, 3]) is None  # < 2 full weekly periods


def test_seasonal_baseline_distinguishes_weekend_dip_from_anomaly():
    # 4 weeks of data: weekday=500, weekend=200. A weekend reading of ~200
    # should NOT look like an anomaly once seasonality is accounted for.
    history = []
    for i in range(28):
        weekday = i % 7
        history.append(200.0 if weekday in (5, 6) else 500.0)
    result = seasonal_baseline(history)
    assert result is not None
    expected, resid_std = result
    assert resid_std < 5  # near-zero residual noise since the pattern is exact


def test_seasonal_decomposition_used_when_enough_history():
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=40)
    history = [200.0 if i % 7 in (5, 6) else 500.0 for i in range(28)]
    # New value continues the same weekday pattern -> should not be flagged
    # even though a plain (non-seasonal) mean of ~414 would make 200 look anomalous.
    weekday_of_new_point = 28 % 7  # Sunday-equivalent in this synthetic pattern
    new_value = 200.0 if weekday_of_new_point in (5, 6) else 500.0
    result = detect("row_count", new_value=new_value, history=history, first_seen_at=first_seen, now=now)
    assert result.method == "seasonal_zscore"
    assert result.is_anomaly is False


def test_min_stddev_floor_prevents_division_by_zero():
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=20)
    result = detect("null_rate_x", new_value=0.0, history=[0.0] * 10, first_seen_at=first_seen, now=now)
    assert result.is_anomaly is False  # constant history, no deviation
    assert result.stddev > 0  # floor applied, no ZeroDivisionError


def test_single_history_point_never_flagged_despite_huge_zscore():
    # Regression test: with only 1 prior reading, stddev falls back to the
    # floor (1e-6), so ANY different value produces an astronomical z-score.
    # This must NOT be flagged - there's no real variance estimate yet.
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=20)
    result = detect("freshness_minutes", new_value=935.9, history=[935.88], first_seen_at=first_seen, now=now)
    assert abs(result.z_score) > 1000  # confirms the hair-trigger z-score is indeed huge
    assert result.is_anomaly is False  # but it's correctly suppressed


def test_anomaly_flagged_once_min_history_reached():
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=20)
    stable_history = [500, 502, 498, 501, 499]  # 5 points, real variance
    result = detect("row_count", new_value=50, history=stable_history, first_seen_at=first_seen, now=now)
    assert result.is_anomaly is True
