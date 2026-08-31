const BASE = "/api";

async function req(path, options) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

export const api = {
  listTables: () => req("/tables"),
  addTable: (payload) => req("/tables", { method: "POST", body: JSON.stringify(payload) }),
  tableHealth: (id) => req(`/tables/${id}`),
  tableMetrics: (id) => req(`/tables/${id}/metrics`),
  tableSnapshots: (id, metric) =>
    req(`/tables/${id}/history${metric ? `?metric_name=${encodeURIComponent(metric)}` : ""}`),
  tableSchema: (id) => req(`/tables/${id}/schema`),
  triggerCheck: (id) => req(`/tables/${id}/check`, { method: "POST" }),
  listAnomalies: (params = {}) => {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v)).toString();
    return req(`/anomalies${qs ? `?${qs}` : ""}`);
  },
  getAnomaly: (id) => req(`/anomalies/${id}`),
  updateAnomalyStatus: (id, status) =>
    req(`/anomalies/${id}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  acknowledgeAnomaly: (id) => req(`/anomalies/${id}/acknowledge`, { method: "POST" }),
};
