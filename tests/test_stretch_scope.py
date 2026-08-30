from datadrift.root_cause import suggest_root_cause
from datadrift.schema_drift import diff_schema


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
