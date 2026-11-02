import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8800",
      "/files": "http://127.0.0.1:8800",
      "/ws": { target: "ws://127.0.0.1:8800", ws: true },
    },
  },
});
