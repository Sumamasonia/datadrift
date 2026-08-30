"""
Root cause suggestion engine.

When a volume anomaly is detected, DataDrift runs a set of diagnostic checks
designed to suggest the most likely cause. Each check returns evidence, not
certainty - suggestions are labeled "possible cause" and ranked by how
strongly they point to a single explanation, per the product documentation.

Diagnostic checks implemented:
  1. Source table volume     - does the configured upstream table also show a drop?
  2. Freshness cross-check   - is the most recent row old, or is volume merely low today?
  3. Null-rate spike         - did a normally non-null column suddenly start being null?
  4. Schema change           - did the table's schema change recently?
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DiagnosticCheck:
    name: str
    triggered: bool
    evidence: str
    weight: int  # higher = stronger signal, used only to rank suggestions


@dataclass
class RootCauseSuggestion:
    checks: list[DiagnosticCheck] = field(default_factory=list)

    @property
    def triggered_checks(self) -> list[DiagnosticCheck]:
        return sorted((c for c in self.checks if c.triggered), key=lambda c: -c.weight)

    def summary(self) -> str | None:
        """Ranked, human-readable summary of possible causes, or None if nothing fired."""
        triggered = self.triggered_checks
        if not triggered:
            return None
        lines = [f"Possible cause: {c.evidence}" for c in triggered]
        return "\n".join(lines)

    def to_dict(self) -> list[dict]:
        return [
            {"name": c.name, "triggered": c.triggered, "evidence": c.evidence, "weight": c.weight}
            for c in self.checks
        ]


def check_source_volume(
    downstream_row_count_change_pct: float,
    upstream_row_count_change_pct: float | None,
    drop_threshold_pct: float = 20.0,
) -> DiagnosticCheck:
    """Check 1: does the configured upstream table also show a matching drop?"""
    if upstream_row_count_change_pct is None:
        return DiagnosticCheck("source_volume", False, "No upstream table configured.", 0)
    upstream_dropped = upstream_row_count_change_pct <= -drop_threshold_pct
    if upstream_dropped:
        return DiagnosticCheck(
            "source_volume",
            True,
            f"Upstream source failure: the configured upstream table also dropped "
            f"{upstream_row_count_change_pct:.1f}% over the same window, suggesting the failure "
            "originates upstream of this table.",
            weight=3,
        )
    return DiagnosticCheck(
        "source_volume",
        True,
        f"Transformation/loading failure: the upstream table is healthy "
        f"({upstream_row_count_change_pct:+.1f}% vs. baseline) while this table dropped "
        f"{downstream_row_count_change_pct:.1f}%, suggesting the break is between source and destination.",
        weight=3,
    )


def check_freshness(freshness_minutes: float | None, expected_interval_minutes: float) -> DiagnosticCheck:
    """Check 2: is the most recent row old (ingestion stopped), or just today's volume low?"""
    if freshness_minutes is None:
        return DiagnosticCheck("freshness", False, "No timestamp column configured.", 0)
    if freshness_minutes > expected_interval_minutes * 2:
        return DiagnosticCheck(
            "freshness",
            True,
            f"Ingestion appears to have stopped entirely: the most recent row is "
            f"{freshness_minutes:.0f} minutes old, more than double the expected "
            f"{expected_interval_minutes:.0f}-minute update interval.",
            weight=3,
        )
    return DiagnosticCheck(
        "freshness",
        True,
        "New rows are still arriving on schedule, so low volume is likely rows failing "
        "validation and being dropped rather than ingestion stopping outright.",
        weight=1,
    )


def check_null_rate_spike(
    critical_column: str | None,
    current_null_rate: float | None,
    baseline_null_rate: float | None,
    spike_threshold: float = 0.15,
) -> DiagnosticCheck:
    """Check 3: did a normally non-null column suddenly start being null in recent rows?"""
    if critical_column is None or current_null_rate is None or baseline_null_rate is None:
        return DiagnosticCheck("null_rate_spike", False, "No comparable null-rate history.", 0)
    if current_null_rate - baseline_null_rate >= spike_threshold:
        return DiagnosticCheck(
            "null_rate_spike",
            True,
            f"Column '{critical_column}' null rate jumped from {baseline_null_rate:.1%} to "
            f"{current_null_rate:.1%} - an upstream system may have stopped sending this field, "
            "causing rows to fail validation and be dropped.",
            weight=2,
        )
    return DiagnosticCheck("null_rate_spike", False, "No unusual null-rate spike found.", 0)


def check_schema_change(schema_diff_has_drift: bool, schema_diff_summary: str | None) -> DiagnosticCheck:
    """Check 4: did the table's schema change (a common cause of pipeline breaks)?"""
    if not schema_diff_has_drift:
        return DiagnosticCheck("schema_change", False, "No schema drift detected.", 0)
    return DiagnosticCheck(
        "schema_change",
        True,
        f"Schema drift detected: {schema_diff_summary} - an upstream schema change breaking "
        "a transformation is a common cause of volume drops.",
        weight=3,
    )


def suggest_root_cause(
    *,
    downstream_row_count_change_pct: float | None = None,
    upstream_row_count_change_pct: float | None = None,
    freshness_minutes: float | None = None,
    expected_interval_minutes: float = 60.0,
    critical_column: str | None = None,
    current_null_rate: float | None = None,
    baseline_null_rate: float | None = None,
    schema_diff_has_drift: bool = False,
    schema_diff_summary: str | None = None,
) -> RootCauseSuggestion:
    """Run all four diagnostic checks and return a ranked RootCauseSuggestion."""
    checks = [
        check_source_volume(downstream_row_count_change_pct or 0.0, upstream_row_count_change_pct),
        check_freshness(freshness_minutes, expected_interval_minutes),
        check_null_rate_spike(critical_column, current_null_rate, baseline_null_rate),
        check_schema_change(schema_diff_has_drift, schema_diff_summary),
    ]
    return RootCauseSuggestion(checks=checks)
