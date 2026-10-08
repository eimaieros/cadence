import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

// Unmount between component tests. Without it a rendered tree from one test
// keeps its timers and handlers alive into the next, and a query in test B can
// match text that test A left in the document.
afterEach(async () => {
  if (typeof document === "undefined") return;
  const { cleanup } = await import("@testing-library/react");
  cleanup();
});
