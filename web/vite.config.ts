import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the app proxies /api to the local FastAPI server so cookies/hosts
// stay same-origin from the browser's point of view.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8420",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
