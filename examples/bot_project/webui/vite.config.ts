import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/webui/",
  server: {
    proxy: {
      "/api": "http://localhost:21800",
      "/ws": {
        target: "ws://localhost:21800",
        ws: true,
      },
    },
  },
  build: {
    outDir: "../bot/web/dist",
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
