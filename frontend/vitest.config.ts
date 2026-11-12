import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // Vitest does not read tsconfig `paths`, so specs need the same `@` alias.
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "node",
    // Both extensions, or *.test.tsx specs silently vanish from the run.
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
