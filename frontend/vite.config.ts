import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // В разработке запросы к API идут на backend, как будто это один сервер
    proxy: { "/api": "http://localhost:8000" },
  },
});
