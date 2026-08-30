import React, { useEffect, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ResponsiveContainer } from "recharts";
import { api } from "../api.js";

function TableCard({ table, onSelect, selected }) {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    api.tableHealth(table.id).then(setHealth).catch(() => {});
  }, [table.id]);

  const hasOpen = health && health.open_anomaly_count > 0;

  return (
    <div className="card" onClick={() => onSelect(table)} style={selected ? { borderColor: "var(--accent)" } : {}}>
      <div className="name">{table.full_name}</div>
      <div className="meta">{table.connection_name} · {table.check_interval}</div>
      <span className={`badge ${hasOpen ? "anomaly" : "ok"}`}>
        {health ? (hasOpen ? `${health.open_anomaly_count} open anomaly(ies)` : "Healthy") : "..."}
      </span>
    </div>
  );
}

function MetricChart({ tableId, metricName }) {
  const [points, setPoints] = useState([]);

  useEffect(() => {
    api.tableSnapshots(tableId, metricName).then((rows) => {
      setPoints(rows.map((r) => ({ t: new Date(r.recorded_at).toLocaleDateString(), value: r.value })));
    });
  }, [tableId, metricName]);

  if (points.length === 0) return null;

  return (
    <div className="chart-wrap">
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>{metricName}</div>
      <ResponsiveContainer width="100%" height={140}>
        <LineChart data={points}>
          <CartesianGrid stroke="#262b3d" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fontSize: 10, fill: "#8b91a7" }} />
          <YAxis tick={{ fontSize: 10, fill: "#8b91a7" }} />
          <Tooltip contentStyle={{ background: "#161925", border: "1px solid #262b3d" }} />
          <Line type="monotone" dataKey="value" stroke="#5b8def" dot={false} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export default function TableHealth() {
  const [tables, setTables] = useState([]);
  const [selected, setSelected] = useState(null);
  const [health, setHealth] = useState(null);
  const [metrics, setMetrics] = useState([]);
  const [checking, setChecking] = useState(false);

  const loadTables = () => api.listTables().then(setTables);

  useEffect(() => {
    loadTables();
  }, []);

  useEffect(() => {
    if (selected) {
      api.tableHealth(selected.id).then(setHealth);
      api.tableMetrics(selected.id).then(setMetrics);
    }
  }, [selected]);

  const runCheck = async () => {
    if (!selected) return;
    setChecking(true);
    try {
      await api.triggerCheck(selected.id);
      const [h, m] = await Promise.all([api.tableHealth(selected.id), api.tableMetrics(selected.id)]);
      setHealth(h);
      setMetrics(m);
    } finally {
      setChecking(false);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>Monitored Tables</h2>
        {tables.length === 0 ? (
          <div className="empty-state">
            No tables registered. Run <code>datadrift add-table ...</code> or <code>datadrift demo</code>{" "}
            from the CLI.
          </div>
        ) : (
          <div className="grid-cards">
            {tables.map((t) => (
              <TableCard key={t.id} table={t} onSelect={setSelected} selected={selected?.id === t.id} />
            ))}
          </div>
        )}
      </div>

      {selected && health && (
        <div className="panel">
          <h2 style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>{selected.full_name} - Health</span>
            <button onClick={runCheck} disabled={checking}>
              {checking ? "Checking..." : "Run check now"}
            </button>
          </h2>
          {metrics.length === 0 ? (
            <div className="empty-state">No metric snapshots yet. Run a check to collect the first one.</div>
          ) : (
            metrics.map((m) => <MetricChart key={m.metric_name} tableId={selected.id} metricName={m.metric_name} />)
          )}
        </div>
      )}
    </>
  );
}
