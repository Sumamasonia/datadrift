import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import AnomalyDetail from "./AnomalyDetail.jsx";

export default function AnomalyTimeline() {
  const [anomalies, setAnomalies] = useState([]);
  const [tables, setTables] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);

  const [statusFilter, setStatusFilter] = useState("");
  const [severityFilter, setSeverityFilter] = useState("");
  const [tableFilter, setTableFilter] = useState("");

  const load = () => {
    setLoading(true);
    api
      .listAnomalies({ status: statusFilter, severity: severityFilter, table_id: tableFilter })
      .then(setAnomalies)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    api.listTables().then(setTables).catch(() => {});
  }, []);

  useEffect(load, [statusFilter, severityFilter, tableFilter]);

  const resolve = async (id) => {
    await api.updateAnomalyStatus(id, "resolved");
    setSelected(null);
    load();
  };

  if (loading && anomalies.length === 0) return <div className="empty-state">Loading anomalies...</div>;
  if (error) return <div className="empty-state">Error: {error}</div>;

  return (
    <div className="panel">
      <h2>Anomaly Timeline</h2>

      <div className="filters">
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">All statuses</option>
          <option value="open">Open</option>
          <option value="acknowledged">Acknowledged</option>
          <option value="resolved">Resolved</option>
        </select>
        <select value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)}>
          <option value="">All severities</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
        <select value={tableFilter} onChange={(e) => setTableFilter(e.target.value)}>
          <option value="">All tables</option>
          {tables.map((t) => (
            <option key={t.id} value={t.id}>
              {t.full_name}
            </option>
          ))}
        </select>
      </div>

      {anomalies.length === 0 ? (
        <div className="empty-state">
          No anomalies match these filters. Run <code>datadrift check</code> after registering a table, or
          seed data with <code>datadrift demo</code>.
        </div>
      ) : (
        <table className="anomaly-table">
          <thead>
            <tr>
              <th>Detected</th>
              <th>Table</th>
              <th>Metric</th>
              <th>Observed</th>
              <th>Expected range</th>
              <th>Z-score</th>
              <th>Severity</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {anomalies.map((a) => (
              <tr key={a.id} className="clickable-row" onClick={() => setSelected(a)}>
                <td>{new Date(a.detected_at).toLocaleString()}</td>
                <td>{a.table_name}</td>
                <td>{a.metric_name}</td>
                <td>{a.observed_value.toFixed(3)}</td>
                <td>{a.expected_range}</td>
                <td>{a.z_score.toFixed(2)}</td>
                <td>
                  <span className={`sev ${a.severity}`}>{a.severity}</span>
                </td>
                <td>{a.status}</td>
                <td>
                  {a.status !== "resolved" && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        resolve(a.id);
                      }}
                    >
                      Mark resolved
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <AnomalyDetail anomaly={selected} onClose={() => setSelected(null)} onResolve={resolve} />
    </div>
  );
}
