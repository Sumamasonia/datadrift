# DataDrift

Automatic data quality monitoring for analytics pipelines. Connects to a
database, learns statistical baselines for six metric types, and raises
alerts when live data drifts from the learned pattern — without hand-written
validation rules.

This repo implements the full feature list from `docs/` end to end: all six
metric types, seasonal decomposition, anomaly dedup/resolution tracking,
ranked root-cause suggestions, and the documented REST API surface. It runs
entirely on free, open-source components and needs zero external services
for the demo path (SQLite + skipped alert channels).

## What's in here

```
datadrift/          Python package: connectors, detection engine, storage,
                     alerting, FastAPI app, Click CLI
dashboard/           React + Recharts dashboard (Vite)
evaluation/          Precision/recall/latency evaluation harness
tests/               Unit tests for the detection engine & root-cause logic
docs/                Original project documentation (.docx)
```

## Metric types (all six, per table, individually toggleable)

| # | Metric | How it's measured |
|---|---|---|
| 1 | Volume | Row count + row delta since last check |
| 2 | Schema drift | Column-level snapshot diff (added/removed/type-changed) |
| 3 | Freshness | Minutes since `MAX(timestamp_column)` |
| 4 | Null rate | Per-column NULL fraction |
| 5 | Referential integrity | % of FK values with no match in a configured reference table |
| 6 | Distribution shift | mean/median/stddev/p5/p95 per numeric column |

Toggle per table with `--metrics volume,null_rate,freshness` on `add-table`,
or `enabled_metrics` in `PATCH /api/tables/{id}`.

## Quickstart (zero-config demo)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Initialize DataDrift's own metadata store (SQLite, local file)
python -m datadrift.cli init

# 2. Generate a synthetic source DB + register it for monitoring
python -m datadrift.cli demo

# 3. Run a check cycle
python -m datadrift.cli check

# 4. Inspect anomaly history / current open anomalies / table status
python -m datadrift.cli history
python -m datadrift.cli anomalies
python -m datadrift.cli status
```

Because a single `datadrift demo` run has no history yet, the first check
won't flag anything — that's correct behavior for a new table's learning
period. To see detection *and* measured precision/recall against known
injected anomalies without waiting out the real learning period, use the
evaluation harness:

```bash
python evaluation/run_evaluation.py
```

This replays ~45 days of synthetic, weekly-seasonal data with known injected
volume-drop / null-rate-spike / freshness-delay anomalies through the real
detection engine, and reports precision/recall/latency across a threshold
sweep (2σ / 3σ / 4σ).

## CLI reference

| Command | Description |
|---|---|
| `datadrift init` | Create DataDrift's metadata store |
| `datadrift add` (or `add-table`) | Register a table for monitoring |
| `datadrift list-tables` | List registered tables |
| `datadrift status [--table ID]` | Show baseline status + open anomaly count per table |
| `datadrift check [--table ID]` | Run one check cycle now |
| `datadrift anomalies [--table ID]` | List currently open anomalies |
| `datadrift history [--table ID]` | Show recent anomalies (open, acknowledged, and resolved) |
| `datadrift start [--interval-minutes N]` | Run the in-process scheduler (APScheduler) |
| `datadrift demo [--days N]` | Build a synthetic source DB and register it |

Point `--connection-url` at any SQLAlchemy-supported database — PostgreSQL,
MySQL, SQLite, or DuckDB all work. Example against a real Postgres table:

```bash
python -m datadrift.cli add \
  --connection-name prod \
  --connection-url "postgresql+psycopg2://user:pass@host/db" \
  --table orders \
  --schema public \
  --timestamp-column created_at \
  --sensitivity 3.0 \
  --metrics volume,schema_drift,null_rate,freshness,distribution
```

Referential integrity checks are configured separately (they need a second
table): `POST /api/tables/{id}/referential-checks` with
`{source_column, ref_table_name, ref_column}`.

## Running the API + dashboard

```bash
# Terminal 1: API
uvicorn datadrift.api:app --reload --port 8000

# Terminal 2: dashboard (dev server, proxies /api to :8000)
cd dashboard
npm install
npm run dev
```

The dashboard has two views:
- **Table Health** — per-table status cards + metric time-series charts (all
  six metric types render automatically, since the chart component is generic)
- **Anomaly Timeline** — all detected anomalies, filterable by status, with
  a resolve action

The full REST API surface (table CRUD, metrics/history/schema/baseline,
anomaly summary/acknowledge, alert rules, test-alert) is implemented in
`datadrift/api.py` — see that file for the endpoint list.

## Anomaly lifecycle (dedup + auto-resolution)

- A newly-breaching metric with no existing open anomaly creates one and
  sends exactly one alert.
- A metric that's still breaching on the next check updates the existing
  open anomaly in place — no duplicate rows, no repeat alert.
- A metric that returns to baseline while an anomaly is open is marked
  `resolved` and triggers exactly one resolution notification.

## Alerting

Email, Slack, and webhook alerting are all wired up but degrade gracefully
with no credentials configured (the alert is logged instead of sent, so the
rest of the demo keeps working). Configure email via environment variables:

```bash
export DATADRIFT_SMTP_HOST=smtp.gmail.com
export DATADRIFT_SMTP_PORT=587
export DATADRIFT_SMTP_USER=you@gmail.com
export DATADRIFT_SMTP_PASSWORD=your-app-password
export DATADRIFT_SMTP_FROM=you@gmail.com
```

Then register an alert rule via the API (`POST /api/tables/{id}/alerts` or
the global `POST /api/alert-rules`) with `channel: "email"|"slack"|"webhook"`
and a destination (address, Slack Incoming Webhook URL, or any HTTP endpoint
that accepts a JSON POST). Test a destination without waiting for a real
anomaly via `POST /api/alerts/test`.

## Configuration

All defaults live in `datadrift/config.py` and can be overridden via
environment variables — see that file for the full list (storage backend,
z-score threshold, learning period, rolling window size, check interval).

## Detection algorithm

Once at least two full weekly cycles of history exist (14+ points), the
detection engine runs `statsmodels.tsa.seasonal.seasonal_decompose`
(period=7, additive) to separate the weekly pattern from the trend, and
computes the z-score of a new observation against the seasonally-adjusted
expected value and residual stddev — so "Sunday is quieter than Friday"
isn't flagged as an anomaly every week. With less history, it falls back to
a plain rolling mean/stddev z-score over the trailing window. No anomalies
are raised during the learning period (default 14 days), and results are
"low confidence" until 28 days of history exist. See
`datadrift/detection.py` and `tests/test_detection.py`.

## Root cause suggestions

For `row_count` anomalies specifically, DataDrift runs four diagnostic
checks and ranks whichever ones fire by how strongly they point to a single
explanation (`datadrift/root_cause.py`):

1. **Source volume** — does a configured upstream table show a matching drop?
2. **Freshness cross-check** — did ingestion stop, or is today's volume just low?
3. **Null-rate spike** — did a normally-populated column suddenly go null?
4. **Schema change** — did the table's schema change around the same time?

Each is labeled "possible cause," not a certainty.

## Explicitly out of scope

BigQuery/Snowflake connectors, PagerDuty, full pipeline lineage tracing, and
ML-based anomaly detection (Isolation Forest etc.) — the statistical
baseline is the core contribution; adding ML would obscure it.

## Running the tests

```bash
pip install pytest
PYTHONPATH=. pytest tests/ -v
```

