import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";

// Component tests (ported agentmesh console tests). Pure unit tests stay on
// `tsx --test` as `*.test.ts`; `pnpm test` runs both.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.tsx"],
    // Same-origin API paths, as in production.
    env: { VITE_BACKEND_ORIGIN: "" },
  },
});
