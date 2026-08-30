import React, { useState } from "react";
import AnomalyTimeline from "./components/AnomalyTimeline.jsx";
import TableHealth from "./components/TableHealth.jsx";

export default function App() {
  const [tab, setTab] = useState("health");

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>DataDrift</h1>
          <div className="tagline">Automatic data quality monitoring</div>
        </div>
      </header>

      <nav className="tabs">
        <button className={tab === "health" ? "active" : ""} onClick={() => setTab("health")}>
          Table Health
        </button>
        <button className={tab === "timeline" ? "active" : ""} onClick={() => setTab("timeline")}>
          Anomaly Timeline
        </button>
      </nav>

      {tab === "health" ? <TableHealth /> : <AnomalyTimeline />}
    </div>
  );
}
