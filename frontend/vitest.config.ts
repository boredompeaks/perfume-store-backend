import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // The `@/*` tsconfig path is Next's only; Vite does not read it, so a test
  // that imports an app module (`@/app/...`) needs the same alias here.
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "node",
    // Both extensions: a *.test.tsx component spec would otherwise be
    // silently dropped from the run, which reads as "no tests exist".
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
