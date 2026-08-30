import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// DataDrift dashboard dev server. Proxies /api to the FastAPI backend
// (run `uvicorn datadrift.api:app --reload --port 8000` alongside this).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
