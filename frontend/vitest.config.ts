import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    // Both extensions: a *.test.tsx component spec would otherwise be
    // silently dropped from the run, which reads as "no tests exist".
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
