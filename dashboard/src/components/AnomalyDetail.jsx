import React from "react";

export default function AnomalyDetail({ anomaly, onClose, onResolve }) {
  if (!anomaly) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <button className="close-btn" onClick={onClose}>
          &times;
        </button>
        <h3>
          {anomaly.metric_name} <span className={`sev ${anomaly.severity}`}>{anomaly.severity}</span>
        </h3>
        <div style={{ color: "var(--muted)", fontSize: 13, marginBottom: 14 }}>{anomaly.table_name}</div>

        <div className="field-row">
          <span className="k">Status</span>
          <span>{anomaly.status}</span>
        </div>
        <div className="field-row">
          <span className="k">Observed value</span>
          <span>{anomaly.observed_value.toFixed(4)}</span>
        </div>
        <div className="field-row">
          <span className="k">Expected range</span>
          <span>{anomaly.expected_range}</span>
        </div>
        <div className="field-row">
          <span className="k">Z-score</span>
          <span>{anomaly.z_score.toFixed(2)}</span>
        </div>
        <div className="field-row">
          <span className="k">Detected at</span>
          <span>{new Date(anomaly.detected_at).toLocaleString()}</span>
        </div>
        <div className="field-row">
          <span className="k">Last seen</span>
          <span>{anomaly.last_seen_at ? new Date(anomaly.last_seen_at).toLocaleString() : "-"}</span>
        </div>
        {anomaly.resolved_at && (
          <div className="field-row">
            <span className="k">Resolved at</span>
            <span>{new Date(anomaly.resolved_at).toLocaleString()}</span>
          </div>
        )}

        {anomaly.diagnostic_results && anomaly.diagnostic_results.length > 0 && (
          <>
            <h3 style={{ marginTop: 20, fontSize: 14 }}>Root cause diagnostics</h3>
            <div style={{ marginTop: 6 }}>
              {anomaly.diagnostic_results.map((c) => (
                <div className="diag-check" key={c.name}>
                  <span className={`dot ${c.triggered ? "fired" : "not-fired"}`} />
                  <div>
                    <div style={{ fontWeight: 600 }}>{c.name.replace(/_/g, " ")}</div>
                    <div style={{ color: "var(--muted)" }}>{c.evidence}</div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}

        {!anomaly.diagnostic_results?.length && anomaly.diagnosis && (
          <>
            <h3 style={{ marginTop: 20, fontSize: 14 }}>Diagnosis</h3>
            <div style={{ fontSize: 13, color: "var(--muted)", whiteSpace: "pre-line" }}>{anomaly.diagnosis}</div>
          </>
        )}

        {anomaly.status !== "resolved" && (
          <div style={{ marginTop: 18, display: "flex", gap: 8 }}>
            <button onClick={() => onResolve(anomaly.id)}>Mark resolved</button>
          </div>
        )}
      </div>
    </div>
  );
}
