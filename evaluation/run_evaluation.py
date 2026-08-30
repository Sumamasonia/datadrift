"""
Evaluation harness (section 10 of the project documentation).

Uses datadrift.synthetic.generate_daily_series to produce ~45 days of
realistic, weekly-seasonal "normal" data with a controlled set of injected
anomalies (known day, type, and magnitude). Replays that series day-by-day
through the real detection engine (datadrift.detection.detect) and measures:

  - Precision: of all flagged anomalies, what fraction are true injected anomalies?
  - Recall: of all injected anomalies, what fraction were detected?
  - Detection latency: how many check cycles after injection until flagged?

Run across a small threshold sweep (2sigma, 3sigma, 4sigma) to show the
precision/recall trade-off explicitly, per section 10.3, instead of picking
one threshold and presenting it as universally correct.

Usage:
    python evaluation/run_evaluation.py
    python evaluation/run_evaluation.py --days 60 --thresholds 2,3,4
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datadrift.detection import detect  # noqa: E402
from datadrift.synthetic import generate_daily_series  # noqa: E402


@dataclass
class EvalOutcome:
    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    mean_latency_cycles: float | None


def evaluate(days: int, threshold: float, learning_period_days: int = 10, seed: int = 42) -> EvalOutcome:
    records, injected = generate_daily_series(num_days=days, seed=seed, inject_anomalies=True)
    metric_names = ["row_count", "null_rate_customer_email", "freshness_minutes"]

    histories: dict[str, list[float]] = {m: [] for m in metric_names}
    now0 = datetime.now(timezone.utc) - timedelta(days=days)

    # ground truth: (day_index, metric_name) -> anomaly
    injected_lookup = {(a.day_index, a.metric_name): a for a in injected}
    detected_true_positive_days: dict[tuple[int, str], int | None] = {k: None for k in injected_lookup}

    true_positives = 0
    false_positives = 0
    latencies = []

    for rec in records:
        day_index = rec["day_index"]
        now = now0 + timedelta(days=day_index)
        for metric_name in metric_names:
            value = rec[metric_name]
            history = histories[metric_name]
            result = detect(
                metric_name=metric_name,
                new_value=value,
                history=history,
                first_seen_at=now0,
                now=now,
                threshold=threshold,
                learning_period_days=learning_period_days,
            )
            history.append(value)

            if result.is_anomaly:
                key = (day_index, metric_name)
                # a flagged anomaly is a true positive if an anomaly of the same
                # metric was injected on this day OR within the prior 2 days
                # (allows a little slack for the injected anomaly to propagate)
                matched = False
                for offset in range(0, 3):
                    lookup_key = (day_index - offset, metric_name)
                    if lookup_key in injected_lookup and detected_true_positive_days.get(lookup_key) is None:
                        detected_true_positive_days[lookup_key] = offset
                        matched = True
                        break
                if matched:
                    true_positives += 1
                    latencies.append(offset)
                else:
                    false_positives += 1

    false_negatives = sum(1 for v in detected_true_positive_days.values() if v is None)
    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) else 0.0
    recall = true_positives / len(injected_lookup) if injected_lookup else 0.0
    mean_latency = sum(latencies) / len(latencies) if latencies else None

    return EvalOutcome(
        threshold=threshold,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        mean_latency_cycles=mean_latency,
    )


def main():
    parser = argparse.ArgumentParser(description="DataDrift detection evaluation harness")
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--thresholds", type=str, default="2,3,4")
    parser.add_argument("--learning-period-days", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    thresholds = [float(t) for t in args.thresholds.split(",")]

    print(f"DataDrift evaluation - {args.days} days, seed={args.seed}\n")
    print(f"{'Threshold':>10} {'TP':>4} {'FP':>4} {'FN':>4} {'Precision':>10} {'Recall':>8} {'Avg latency (cycles)':>22}")
    print("-" * 70)
    for t in thresholds:
        outcome = evaluate(args.days, t, args.learning_period_days, args.seed)
        latency_str = f"{outcome.mean_latency_cycles:.2f}" if outcome.mean_latency_cycles is not None else "n/a"
        print(
            f"{outcome.threshold:>10.1f} {outcome.true_positives:>4} {outcome.false_positives:>4} "
            f"{outcome.false_negatives:>4} {outcome.precision:>10.2%} {outcome.recall:>8.2%} {latency_str:>22}"
        )

    print(
        "\nNote: precision/recall are measured against a small, deliberately-injected set of "
        "volume-drop, null-rate-spike, and freshness-delay anomalies (see section 10.1 of the "
        "project documentation). Lower thresholds catch more anomalies (higher recall) at the "
        "cost of more false positives on normal variation (lower precision), matching the "
        "expected trade-off documented in section 10.3."
    )


if __name__ == "__main__":
    main()
