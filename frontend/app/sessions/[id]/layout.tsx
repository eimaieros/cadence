import { DEMO_SESSION_IDS } from "@/lib/demo/server";

/*
  The static demo has no server to render /sessions/<id> on request, so the
  export needs every id up front. The demo hands out ids from a fixed pool
  (lib/demo/server.ts); the normal build pre-renders none and stays dynamic.
*/
export function generateStaticParams() {
  return process.env.NEXT_PUBLIC_DEMO === "1" ? DEMO_SESSION_IDS.map((id) => ({ id })) : [];
}

export default function SessionLayout({ children }: { children: React.ReactNode }) {
  return children;
}
