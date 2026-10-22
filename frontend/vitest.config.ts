import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // The app resolves every module through `@/…` (tsconfig `paths`). Vitest
  // does not read that map, so component specs would fail to import the very
  // component under test without the same alias.
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
