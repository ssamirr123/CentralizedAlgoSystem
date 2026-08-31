import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The FastAPI backend is a separate service. In dev we proxy `/api` (and the
// legacy `/strategies`, `/health` routes) to it so the browser makes
// same-origin requests and there is no CORS involved — the backend is left
// completely untouched. In production the app is served behind the same
// nginx that fronts the API (see trading/infrastructure/backend/nginx/),
// so `/api/*` is already same-origin there too.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.VITE_API_PROXY_TARGET || "http://13.232.95.211";
  return {
    plugins: [react()],
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    server: {
      port: 5173,
      proxy: {
        "/api": { target, changeOrigin: true },
        "/strategies": { target, changeOrigin: true },
        "/health": { target, changeOrigin: true },
      },
    },
    preview: { port: 4173 },
  };
});
