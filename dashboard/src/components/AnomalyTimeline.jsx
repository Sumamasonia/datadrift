import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function AnomalyTimeline() {
  const [anomalies, setAnomalies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = () => {
    setLoading(true);
    api
      .listAnomalies()
      .then(setAnomalies)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const resolve = async (id) => {
    await api.updateAnomalyStatus(id, "resolved");
    load();
  };

  if (loading) return <div className="empty-state">Loading anomalies...</div>;
  if (error) return <div className="empty-state">Error: {error}</div>;

  return (
    <div className="panel">
      <h2>Anomaly Timeline</h2>
      {anomalies.length === 0 ? (
        <div className="empty-state">
          No anomalies recorded yet. Run <code>datadrift check</code> after registering a table, or seed
          data with <code>datadrift demo</code>.
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
              <tr key={a.id}>
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
                    <button onClick={() => resolve(a.id)}>Mark resolved</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
