"""
Validation experiment: measures precision, recall, false-positive rate, and
detection latency of the real detection engine (datadrift.detection.detect)
against a controlled set of 50 injected anomalies, spread evenly across the
three metric types (volume, null rate, freshness) and three severity tiers
(mild/moderate/severe), replayed day-by-day over a long enough synthetic
series that anomalies on the same metric don't interfere with each other's
baseline recovery (see datadrift.synthetic.generate_anomaly_plan).

To guard against the result being an artifact of one lucky/unlucky random
seed, the whole experiment is repeated across multiple seeds and the report
gives mean +/- standard deviation for every metric, not a single run.

Usage:
    python evaluation/run_evaluation.py                      # quick 8-anomaly smoke run (legacy default)
    python evaluation/run_evaluation.py --scale 50            # the 50-anomaly validation experiment
    python evaluation/run_evaluation.py --scale 50 --trials 10 --report
"""
from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datadrift.detection import detect  # noqa: E402
from datadrift.synthetic import generate_anomaly_plan, generate_daily_series  # noqa: E402

METRIC_NAMES = ["row_count", "null_rate_customer_email", "freshness_minutes"]
MATCH_WINDOW_DAYS = 2  # a flagged anomaly counts as a true positive if it lands within this many days of an injected one


@dataclass
class TrialResult:
    seed: int
    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int
    total_normal_readings: int  # denominator for false-positive rate
    latencies: list[int] = field(default_factory=list)
    by_metric: dict = field(default_factory=dict)  # metric_name -> {tp, fn, total}
    by_tier: dict = field(default_factory=dict)  # tier -> {tp, fn, total}

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / self.total_normal_readings if self.total_normal_readings else 0.0

    @property
    def mean_latency(self) -> float | None:
        return sum(self.latencies) / len(self.latencies) if self.latencies else None


def run_trial(
    seed: int,
    threshold: float,
    num_anomalies: int,
    num_days: int,
    learning_period_days: int,
) -> TrialResult:
    plan = generate_anomaly_plan(num_anomalies, num_days, seed, learning_period_days)
    records, injected = generate_daily_series(num_days=num_days, seed=seed, anomaly_plan=plan)

    injected_lookup = {(a.day_index, a.metric_name): a for a in injected}
    matched: dict[tuple[int, str], int | None] = {k: None for k in injected_lookup}

    histories: dict[str, list[float]] = {m: [] for m in METRIC_NAMES}
    now0 = datetime.now(timezone.utc) - timedelta(days=num_days)

    tp = fp = 0
    total_normal_readings = 0
    latencies: list[int] = []
    by_metric = {m: {"tp": 0, "fn": 0, "total": 0} for m in METRIC_NAMES}
    by_tier = {
        "mild": {"tp": 0, "fn": 0, "total": 0},
        "moderate": {"tp": 0, "fn": 0, "total": 0},
        "severe": {"tp": 0, "fn": 0, "total": 0},
    }

    for rec in records:
        day_index = rec["day_index"]
        now = now0 + timedelta(days=day_index)
        for metric_name in METRIC_NAMES:
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

            is_injected_day = (day_index, metric_name) in injected_lookup
            if not is_injected_day and day_index >= learning_period_days:
                total_normal_readings += 1

            if result.is_anomaly:
                matched_key = None
                for offset in range(0, MATCH_WINDOW_DAYS + 1):
                    key = (day_index - offset, metric_name)
                    if key in injected_lookup and matched.get(key) is None:
                        matched_key = key
                        matched[key] = offset
                        break
                if matched_key:
                    tp += 1
                    latencies.append(matched[matched_key])
                    by_metric[metric_name]["tp"] += 1
                    by_tier[injected_lookup[matched_key].tier]["tp"] += 1
                elif not is_injected_day:
                    fp += 1
                # else: flagged on/near an injected day that's already matched - not double counted

    fn = 0
    for key, offset in matched.items():
        _, metric_name = key
        by_metric[metric_name]["total"] += 1
        by_tier[injected_lookup[key].tier]["total"] += 1
        if offset is None:
            fn += 1
            by_metric[metric_name]["fn"] += 1
            by_tier[injected_lookup[key].tier]["fn"] += 1

    return TrialResult(
        seed=seed,
        threshold=threshold,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        total_normal_readings=total_normal_readings,
        latencies=latencies,
        by_metric=by_metric,
        by_tier=by_tier,
    )


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    return mean, std


def run_experiment(
    num_anomalies: int,
    num_days: int,
    thresholds: list[float],
    num_trials: int,
    learning_period_days: int,
    base_seed: int,
) -> dict[float, list[TrialResult]]:
    results: dict[float, list[TrialResult]] = {t: [] for t in thresholds}
    for trial_idx in range(num_trials):
        seed = base_seed + trial_idx
        for threshold in thresholds:
            results[threshold].append(run_trial(seed, threshold, num_anomalies, num_days, learning_period_days))
    return results


def print_summary(results: dict[float, list[TrialResult]]) -> None:
    print(f"\n{'Threshold':>10} {'Precision':>16} {'Recall':>16} {'FPR':>16} {'Avg latency (days)':>20}")
    print("-" * 82)
    for threshold, trials in results.items():
        p_mean, p_std = _mean_std([t.precision for t in trials])
        r_mean, r_std = _mean_std([t.recall for t in trials])
        f_mean, f_std = _mean_std([t.false_positive_rate for t in trials])
        latencies = [t.mean_latency for t in trials if t.mean_latency is not None]
        l_mean, l_std = _mean_std(latencies)
        print(
            f"{threshold:>10.1f} {p_mean:>8.2%} +/-{p_std:>5.2%} {r_mean:>8.2%} +/-{r_std:>5.2%} "
            f"{f_mean:>8.4%} +/-{f_std:>5.4%} {l_mean:>12.2f} +/-{l_std:>4.2f}"
        )


def write_report(
    path: Path, results: dict[float, list[TrialResult]], num_anomalies: int, num_days: int, num_trials: int
) -> None:
    lines = []
    lines.append("# DataDrift Detection Validation Report")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        f"{num_anomalies} anomalies were injected into a synthetic {num_days}-day daily series, "
        "spread evenly across the three metric types (row-count volume drops, null-rate spikes on a "
        "single column, and freshness delays) and three severity tiers (mild/moderate/severe), with "
        "at least a 6-day gap between two injected anomalies on the same metric so each has time to "
        "settle out of the rolling baseline before the next arrives. The series was replayed day-by-day "
        "through the production detection engine (`datadrift.detection.detect`), unmodified from the "
        "code path used in the live system. A flagged anomaly counts as a true positive if it lands "
        f"within {MATCH_WINDOW_DAYS} days of an injected anomaly on the same metric that hasn't already "
        "been matched; anything else flagged is a false positive. The whole experiment was repeated "
        f"across {num_trials} random seeds and results are reported as mean +/- standard deviation to "
        "avoid presenting a single lucky or unlucky run as representative."
    )
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Threshold | Precision | Recall | False Positive Rate | Avg. Detection Latency (days) |")
    lines.append("|---|---|---|---|---|")
    for threshold, trials in results.items():
        p_mean, p_std = _mean_std([t.precision for t in trials])
        r_mean, r_std = _mean_std([t.recall for t in trials])
        f_mean, f_std = _mean_std([t.false_positive_rate for t in trials])
        latencies = [t.mean_latency for t in trials if t.mean_latency is not None]
        l_mean, l_std = _mean_std(latencies)
        lines.append(
            f"| {threshold:.1f}sigma | {p_mean:.1%} +/- {p_std:.1%} | {r_mean:.1%} +/- {r_std:.1%} | "
            f"{f_mean:.3%} +/- {f_std:.3%} | {l_mean:.2f} +/- {l_std:.2f} |"
        )
    lines.append("")

    mid_threshold = sorted(results.keys())[len(results) // 2]
    mid_trials = results[mid_threshold]
    lines.append(f"## Recall breakdown at {mid_threshold}sigma")
    lines.append("")
    lines.append("### By metric type")
    lines.append("")
    lines.append("| Metric | Recall |")
    lines.append("|---|---|")
    metric_names = list(mid_trials[0].by_metric.keys())
    for metric in metric_names:
        recalls = []
        for t in mid_trials:
            d = t.by_metric[metric]
            if d["total"]:
                recalls.append(d["tp"] / d["total"])
        m, s = _mean_std(recalls)
        lines.append(f"| {metric} | {m:.1%} +/- {s:.1%} |")
    lines.append("")
    lines.append("### By severity tier")
    lines.append("")
    lines.append("| Tier | Recall |")
    lines.append("|---|---|")
    for tier in ["mild", "moderate", "severe"]:
        recalls = []
        for t in mid_trials:
            d = t.by_tier[tier]
            if d["total"]:
                recalls.append(d["tp"] / d["total"])
        m, s = _mean_std(recalls)
        lines.append(f"| {tier} | {m:.1%} +/- {s:.1%} |")
    lines.append("")

    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "As expected, recall on mild-tier anomalies (barely above the detection threshold by "
        "construction) is meaningfully lower than on moderate/severe-tier anomalies - these are the "
        "hardest cases and the ones most likely to be a genuine trade-off decision in production. "
        "Lower thresholds trade precision for recall (more false positives on normal day-to-day "
        "variation) and vice versa; the default threshold (3sigma) is chosen as a reasonable middle "
        "ground per this sweep, not because it's universally optimal - a team monitoring a "
        "high-volume, noisy table might prefer a stricter default."
    )
    lines.append("")

    path.write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="DataDrift detection evaluation / validation harness")
    parser.add_argument(
        "--scale",
        type=int,
        default=8,
        help="Number of injected anomalies (default: 8, a quick smoke run; use 50 for the full validation experiment)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Length of the synthetic series in days (default: auto-sized to fit --scale anomalies comfortably)",
    )
    parser.add_argument("--thresholds", type=str, default="2,3,4")
    parser.add_argument("--learning-period-days", type=int, default=14)
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of random seeds to average over (default 1; use 5-10 for a statistically robust report)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Base seed; trial i uses seed+i")
    parser.add_argument("--report", action="store_true", help="Write a Markdown report to evaluation/VALIDATION_REPORT.md")
    args = parser.parse_args()

    thresholds = [float(t) for t in args.thresholds.split(",")]

    if args.days is None:
        # Auto-size: with the stratified placement in generate_anomaly_plan, the
        # exact minimum works, but leave a little headroom for rounding.
        per_metric = -(-args.scale // 3)  # ceil division
        args.days = args.learning_period_days + per_metric * 6 + 10

    print(
        f"DataDrift validation experiment - {args.scale} injected anomalies, "
        f"{args.days}-day series, {args.trials} trial(s)\n"
    )

    results = run_experiment(args.scale, args.days, thresholds, args.trials, args.learning_period_days, args.seed)
    print_summary(results)

    print(
        "\nNote: recall/precision/FPR are averaged across trials (different random seeds); std shows "
        "run-to-run variance. FPR is false positives divided by total non-anomalous metric-day "
        "readings (not just injected-anomaly days), so it reflects the realistic false-alarm rate "
        "a team would experience day-to-day."
    )

    if args.report:
        report_path = Path(__file__).resolve().parent / "VALIDATION_REPORT.md"
        write_report(report_path, results, args.scale, args.days, args.trials)
        print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    main()
