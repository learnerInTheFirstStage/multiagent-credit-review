import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev-time proxy: when `npm run dev` runs locally (port 5173), /api/* calls
// get forwarded to the gateway at localhost:8080, which routes them to Auth
// and Orchestrator. In production the React bundle is served by the same
// nginx container as the gateway, so no proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
