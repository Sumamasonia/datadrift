# DataDrift Detection Validation Report

## Methodology

50 anomalies were injected into a synthetic 126-day daily series, spread evenly across the three metric types (row-count volume drops, null-rate spikes on a single column, and freshness delays) and three severity tiers (mild/moderate/severe), with at least a 6-day gap between two injected anomalies on the same metric so each has time to settle out of the rolling baseline before the next arrives. The series was replayed day-by-day through the production detection engine (`datadrift.detection.detect`), unmodified from the code path used in the live system. A flagged anomaly counts as a true positive if it lands within 2 days of an injected anomaly on the same metric that hasn't already been matched; anything else flagged is a false positive. The whole experiment was repeated across 5 random seeds and results are reported as mean +/- standard deviation to avoid presenting a single lucky or unlucky run as representative.

## Results

| Threshold | Precision | Recall | False Positive Rate | Avg. Detection Latency (days) |
|---|---|---|---|---|
| 2.0sigma | 46.8% +/- 4.0% | 70.8% +/- 5.4% | 14.196% +/- 2.480% | 0.27 +/- 0.07 |
| 3.0sigma | 54.3% +/- 2.1% | 58.4% +/- 3.8% | 8.601% +/- 0.530% | 0.32 +/- 0.07 |
| 4.0sigma | 61.6% +/- 7.3% | 51.6% +/- 5.4% | 5.804% +/- 1.860% | 0.32 +/- 0.10 |

## Recall breakdown at 3.0sigma

### By metric type

| Metric | Recall |
|---|---|
| row_count | 82.4% +/- 4.2% |
| null_rate_customer_email | 45.9% +/- 4.9% |
| freshness_minutes | 46.2% +/- 7.1% |

### By severity tier

| Tier | Recall |
|---|---|
| mild | 32.2% +/- 6.1% |
| moderate | 56.5% +/- 5.3% |
| severe | 92.0% +/- 5.6% |

## Interpretation

As expected, recall on mild-tier anomalies (barely above the detection threshold by construction) is meaningfully lower than on moderate/severe-tier anomalies - these are the hardest cases and the ones most likely to be a genuine trade-off decision in production. Lower thresholds trade precision for recall (more false positives on normal day-to-day variation) and vice versa; the default threshold (3sigma) is chosen as a reasonable middle ground per this sweep, not because it's universally optimal - a team monitoring a high-volume, noisy table might prefer a stricter default.
