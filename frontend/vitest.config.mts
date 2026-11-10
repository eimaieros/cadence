import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  esbuild: { jsx: "automatic" },
  resolve: {
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
  test: {
    // The protocol and token tests have always run in plain Node and still do.
    // Component tests opt into a DOM per file with `@vitest-environment jsdom`,
    // so the fast suites do not pay for a browser they never touch.
    environment: "node",
    setupFiles: ["./vitest.setup.ts"],
  },
});
