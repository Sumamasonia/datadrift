"""
Alerting layer.

Dispatches notifications when an anomaly is confirmed, via:
  - email (SMTP, e.g. a Gmail app password)
  - webhook (HTTP POST of the anomaly as JSON to any configured URL)
  - Slack Incoming Webhooks (kept from the original build; the "complete"
    product doc treats Slack as enterprise-tier, but supporting it costs
    nothing extra and doesn't conflict with the documented free-tier set)

Every channel degrades gracefully: if not configured (no SMTP host, or no
destination URL), the alert is logged instead of raising, so the rest of the
pipeline (detection, storage) keeps working during a demo even without real
credentials wired up.

Alert deduplication (Feature #17) happens one layer up, in checks.py: this
module is only ever called once per newly-created or newly-resolved anomaly,
never once per check cycle for an anomaly that's still open.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

import requests

from datadrift.config import settings
from datadrift.db import Anomaly, MonitoredTable

logger = logging.getLogger("datadrift.alerting")


def format_anomaly_message(table: MonitoredTable, anomaly: Anomaly) -> str:
    return (
        f"[DataDrift] {anomaly.severity.upper()} anomaly on {table.full_name}\n"
        f"Metric: {anomaly.metric_name}\n"
        f"Observed: {anomaly.observed_value:.4f}\n"
        f"Expected range: {anomaly.expected_range}\n"
        f"Z-score: {anomaly.z_score:.2f}\n"
        f"Detected at: {anomaly.detected_at.isoformat()}\n"
        + (f"Diagnosis:\n{anomaly.diagnosis}\n" if anomaly.diagnosis else "")
    )


def format_resolution_message(table: MonitoredTable, anomaly: Anomaly) -> str:
    return (
        f"[DataDrift] RESOLVED: {anomaly.metric_name} on {table.full_name} has returned to baseline\n"
        f"Was: {anomaly.severity} severity, observed {anomaly.observed_value:.4f} "
        f"(expected {anomaly.expected_range})\n"
        f"Detected at: {anomaly.detected_at.isoformat()}\n"
        f"Resolved at: {anomaly.resolved_at.isoformat() if anomaly.resolved_at else ''}\n"
    )


def anomaly_to_json(table: MonitoredTable, anomaly: Anomaly) -> dict:
    return {
        "id": anomaly.id,
        "table": table.full_name,
        "table_id": table.id,
        "metric_name": anomaly.metric_name,
        "observed_value": anomaly.observed_value,
        "expected_range": anomaly.expected_range,
        "z_score": anomaly.z_score,
        "severity": anomaly.severity,
        "diagnosis": anomaly.diagnosis,
        "status": anomaly.status,
        "detected_at": anomaly.detected_at.isoformat(),
        "resolved_at": anomaly.resolved_at.isoformat() if anomaly.resolved_at else None,
    }


def send_email(destination: str, subject: str, body: str) -> bool:
    if not settings.smtp_host or not settings.smtp_from:
        logger.info("SMTP not configured; skipping email alert to %s. Body:\n%s", destination, body)
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = destination
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            server.starttls()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_from, [destination], msg.as_string())
        return True
    except Exception:
        logger.exception("Failed to send email alert to %s", destination)
        return False


def send_slack(webhook_url: str, text: str) -> bool:
    if not webhook_url:
        logger.info("Slack webhook not configured; skipping alert. Body:\n%s", text)
        return False
    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Slack alert")
        return False


def send_webhook(url: str, payload: dict) -> bool:
    """Feature #16: POST the anomaly as JSON to any configured URL."""
    if not url:
        logger.info("Webhook URL not configured; skipping alert. Payload:\n%s", payload)
        return False
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to POST webhook alert to %s", url)
        return False


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def rule_matches_severity(min_severity: str, actual_severity: str) -> bool:
    return _SEVERITY_RANK.get(actual_severity, 0) >= _SEVERITY_RANK.get(min_severity, 0)


def _matching_rules(table: MonitoredTable, anomaly: Anomaly, alert_rules: list) -> list:
    return [
        r
        for r in alert_rules
        if r.is_active
        and (r.table_id is None or r.table_id == table.id)
        and rule_matches_severity(r.min_severity, anomaly.severity)
    ]


def _send_via_channel(rule, subject: str, text_body: str, json_payload: dict) -> bool:
    if rule.channel == "email":
        return send_email(rule.destination, subject, text_body)
    if rule.channel == "slack":
        return send_slack(rule.destination, text_body)
    if rule.channel == "webhook":
        return send_webhook(rule.destination, json_payload)
    logger.warning("Unknown alert channel %s on rule %s", rule.channel, rule.id)
    return False


def dispatch_alerts(table: MonitoredTable, anomaly: Anomaly, alert_rules: list) -> list[dict]:
    """Send a new-anomaly notification to every active, severity-matching alert rule.
    Returns a list of {rule_id, channel, destination, sent} dicts for logging/testing.
    Called exactly once per newly-created anomaly (dedup happens in checks.py)."""
    results = []
    message = format_anomaly_message(table, anomaly)
    subject = f"DataDrift alert: {anomaly.severity} anomaly on {table.full_name}"
    payload = {"event": "anomaly_detected", **anomaly_to_json(table, anomaly)}

    for rule in _matching_rules(table, anomaly, alert_rules):
        sent = _send_via_channel(rule, subject, message, payload)
        results.append(
            {"rule_id": rule.id, "channel": rule.channel, "destination": rule.destination, "sent": sent}
        )
    return results


def dispatch_resolution(table: MonitoredTable, anomaly: Anomaly, alert_rules: list) -> list[dict]:
    """Feature #17: send one resolution notification when a metric returns to baseline.
    Called exactly once per anomaly, at the moment it transitions to status="resolved"."""
    results = []
    message = format_resolution_message(table, anomaly)
    subject = f"DataDrift resolved: {anomaly.metric_name} on {table.full_name}"
    payload = {"event": "anomaly_resolved", **anomaly_to_json(table, anomaly)}

    for rule in _matching_rules(table, anomaly, alert_rules):
        sent = _send_via_channel(rule, subject, message, payload)
        results.append(
            {"rule_id": rule.id, "channel": rule.channel, "destination": rule.destination, "sent": sent}
        )
    return results
