import type { NextConfig } from "next";

// NEXT_PUBLIC_DEMO=1 builds the static demo served at rodrigofigueiredo.dev/cadence/:
// no server, the API replaced in the browser by lib/demo/server.ts.
const demo = process.env.NEXT_PUBLIC_DEMO === "1";

const config: NextConfig = demo
  ? {
      reactStrictMode: true,
      output: "export",
      basePath: "/cadence",
      trailingSlash: true,
      images: { unoptimized: true },
      env: { NEXT_PUBLIC_API_URL: "https://api.cadence.demo", NEXT_PUBLIC_DEMO: "1" },
    }
  : {
      reactStrictMode: true,
      // Standalone output: the Docker runtime stage copies a self-contained
      // server bundle instead of the whole node_modules tree.
      output: "standalone",
      // The API base is read at build time on the server and injected into the
      // client bundle. Anything not prefixed NEXT_PUBLIC_ stays server-only.
      env: {
        NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
        NEXT_PUBLIC_DEMO: "0",
      },
    };

export default config;
