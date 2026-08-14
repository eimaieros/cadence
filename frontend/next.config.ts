import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Standalone output: the Docker runtime stage copies a self-contained
  // server bundle instead of the whole node_modules tree.
  output: "standalone",
  // The API base is read at build time on the server and injected into the
  // client bundle. Anything not prefixed NEXT_PUBLIC_ stays server-only.
  env: { NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000" },
};

export default config;
