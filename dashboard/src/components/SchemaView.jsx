import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function SchemaView({ tableId }) {
  const [schema, setSchema] = useState(null);
  const [lastChange, setLastChange] = useState(null);

  useEffect(() => {
    api.tableSchema(tableId).then(setSchema).catch(() => {});
    api
      .listAnomalies({ table_id: tableId })
      .then((rows) => {
        const drift = rows
          .filter((a) => a.metric_name === "schema_drift")
          .sort((a, b) => new Date(b.detected_at) - new Date(a.detected_at))[0];
        setLastChange(drift || null);
      })
      .catch(() => {});
  }, [tableId]);

  if (!schema || !schema.schema_snapshot) {
    return <div className="empty-state">No schema snapshot yet - run a check to capture one.</div>;
  }

  const columns = Object.entries(schema.schema_snapshot);
  const changedColumns = new Set();
  if (lastChange && lastChange.diagnosis) {
    // best-effort: pull column names out of the diff summary text to highlight them
    const matches = lastChange.diagnosis.match(/[a-zA-Z_][a-zA-Z0-9_]*(?=:|,|\.|\s|$)/g) || [];
    matches.forEach((m) => changedColumns.add(m));
  }

  return (
    <div>
      {lastChange ? (
        <div
          style={{
            fontSize: 13,
            color: lastChange.severity === "high" ? "var(--high)" : "var(--low)",
            marginBottom: 10,
            whiteSpace: "pre-line",
          }}
        >
          Last change ({new Date(lastChange.detected_at).toLocaleString()}): {lastChange.diagnosis}
        </div>
      ) : (
        <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 10 }}>
          No schema changes detected since monitoring began.
        </div>
      )}
      <table className="schema-table">
        <thead>
          <tr>
            <th>Column</th>
            <th>Type</th>
          </tr>
        </thead>
        <tbody>
          {columns.map(([name, type]) => (
            <tr key={name} style={changedColumns.has(name) ? { color: "var(--medium)" } : {}}>
              <td>{name}</td>
              <td>{type}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {schema.schema_snapshot_updated_at && (
        <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8 }}>
          Snapshot captured {new Date(schema.schema_snapshot_updated_at).toLocaleString()}
        </div>
      )}
    </div>
  );
}
