import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.indexOf("node_modules") === -1) return undefined;
          if (id.indexOf("node_modules/react") !== -1 || id.indexOf("node_modules/scheduler") !== -1) {
            return "react-vendor";
          }
          if (id.indexOf("node_modules/@tiptap") !== -1 || id.indexOf("node_modules/prosemirror") !== -1) {
            return "editor-vendor";
          }
          return "vendor";
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000"
    }
  }
});
