from datadrift.root_cause import suggest_root_cause
from datadrift.schema_drift import diff_schema
from datadrift.synthetic import generate_anomaly_plan, generate_daily_series


def test_generate_anomaly_plan_produces_requested_count():
    plan = generate_anomaly_plan(num_anomalies=50, num_days=250, seed=1, learning_period_days=14)
    assert len(plan) == 50


def test_generate_anomaly_plan_spreads_evenly_across_metrics():
    plan = generate_anomaly_plan(num_anomalies=51, num_days=260, seed=2, learning_period_days=14)
    counts = {}
    for a in plan:
        counts[a.metric_name] = counts.get(a.metric_name, 0) + 1
    assert len(counts) == 3
    assert max(counts.values()) - min(counts.values()) <= 1  # evenly split (51/3=17 each)


def test_generate_anomaly_plan_respects_min_gap_per_metric():
    plan = generate_anomaly_plan(num_anomalies=30, num_days=200, seed=3, learning_period_days=14, min_gap_days=6)
    by_metric = {}
    for a in plan:
        by_metric.setdefault(a.metric_name, []).append(a.day_index)
    for metric, days in by_metric.items():
        days = sorted(days)
        for a, b in zip(days, days[1:]):
            assert b - a >= 6


def test_generate_anomaly_plan_deterministic_given_seed():
    plan1 = generate_anomaly_plan(20, 150, seed=7, learning_period_days=14)
    plan2 = generate_anomaly_plan(20, 150, seed=7, learning_period_days=14)
    assert [(a.day_index, a.metric_name, a.tier) for a in plan1] == [(a.day_index, a.metric_name, a.tier) for a in plan2]


def test_generate_daily_series_with_explicit_plan():
    plan = generate_anomaly_plan(9, 100, seed=4, learning_period_days=14)
    records, injected = generate_daily_series(num_days=100, seed=4, anomaly_plan=plan)
    assert len(records) == 100
    assert injected == plan


def test_root_cause_upstream_failure():
    result = suggest_root_cause(
        downstream_row_count_change_pct=-60, upstream_row_count_change_pct=-55
    )
    assert "upstream" in result.summary().lower()


def test_root_cause_transformation_failure():
    result = suggest_root_cause(
        downstream_row_count_change_pct=-60, upstream_row_count_change_pct=2
    )
    assert "transformation" in result.summary().lower()


def test_root_cause_no_upstream_configured():
    result = suggest_root_cause(downstream_row_count_change_pct=1, upstream_row_count_change_pct=None)
    assert result.checks[0].triggered is False


def test_root_cause_freshness_check_detects_stopped_ingestion():
    result = suggest_root_cause(freshness_minutes=200, expected_interval_minutes=60)
    freshness_check = next(c for c in result.checks if c.name == "freshness")
    assert freshness_check.triggered
    assert "stopped" in freshness_check.evidence.lower()


def test_root_cause_null_spike_check():
    result = suggest_root_cause(
        critical_column="email", current_null_rate=0.4, baseline_null_rate=0.01
    )
    null_check = next(c for c in result.checks if c.name == "null_rate_spike")
    assert null_check.triggered


def test_root_cause_schema_change_check():
    result = suggest_root_cause(schema_diff_has_drift=True, schema_diff_summary="removed columns: email")
    schema_check = next(c for c in result.checks if c.name == "schema_change")
    assert schema_check.triggered


def test_root_cause_ranks_by_weight():
    result = suggest_root_cause(
        downstream_row_count_change_pct=-60,
        upstream_row_count_change_pct=-55,  # weight 3
        critical_column="email",
        current_null_rate=0.4,
        baseline_null_rate=0.01,  # weight 2
    )
    triggered = result.triggered_checks
    assert triggered[0].weight >= triggered[-1].weight


def test_schema_diff_detects_added_and_removed():
    old = {"id": "INTEGER", "email": "VARCHAR"}
    new = {"id": "INTEGER", "phone": "VARCHAR"}
    diff = diff_schema(old, new)
    assert diff.added_columns == ["phone"]
    assert diff.removed_columns == ["email"]
    assert diff.has_drift


def test_schema_diff_detects_type_change():
    old = {"id": "INTEGER"}
    new = {"id": "BIGINT"}
    diff = diff_schema(old, new)
    assert diff.type_changes == {"id": ("INTEGER", "BIGINT")}


def test_schema_drift_diagnostics_flags_removed_column_as_high_weight():
    from datadrift.checks import _schema_drift_diagnostics

    diff = diff_schema({"id": "INTEGER", "email": "VARCHAR"}, {"id": "INTEGER"})
    summary, checks = _schema_drift_diagnostics(diff)
    assert "email" in summary
    assert checks[0]["name"] == "column_removed"
    assert checks[0]["weight"] == 3


def test_schema_drift_diagnostics_ranks_removal_above_addition():
    from datadrift.checks import _schema_drift_diagnostics

    diff = diff_schema({"id": "INTEGER"}, {"phone": "VARCHAR"})  # id removed, phone added
    summary, checks = _schema_drift_diagnostics(diff)
    assert checks[0]["name"] == "column_removed"
    assert checks[-1]["name"] == "column_added"
