import { COST_CEILING_USD, MAX_QUESTIONS, SCRIPTED_SCORE, nextQuestion } from "./scripted";
import type { Scorecard } from "../schemas";

/*
  An in-memory stand-in for the cadence API, for the static demo build.

  `lib/api.ts` sends every request through `demoFetch` when the app is built
  with NEXT_PUBLIC_DEMO=1, so the pages, the Zod parsing, the token refresh and
  the SSE parser all run unchanged; only the network is replaced. The answers
  follow the backend's contract — status codes and `detail` messages included —
  because a demo that behaves differently from the product is a second product.

  State lives in sessionStorage: it survives a reload and disappears with the
  tab. Nothing leaves the browser.
*/

export const DEMO_SESSION_IDS = Array.from({ length: 12 }, (_, i) => `demo-${i + 1}`);

const STATE_KEY = "cadence.demo.state";
const QUESTION_COST_USD = 0.0017; // what one scripted question cost in the e2e run, rounded
const SCORING_COST_USD = 0.0021;

interface DemoUser {
  id: string;
  email: string;
  display_name: string;
  password: string;
  created_at: string;
}

interface DemoTurn {
  id: string;
  index: number;
  speaker: "interviewer" | "candidate";
  content: string;
  created_at: string;
}

interface DemoSession {
  id: string;
  user_id: string;
  role_title: string;
  seniority: string;
  focus_areas: string[];
  status: "active" | "completed" | "abandoned";
  cost_usd: number;
  created_at: string;
  completed_at: string | null;
  turns: DemoTurn[];
  scorecard: Scorecard | null;
}

interface DemoState {
  users: DemoUser[];
  sessions: DemoSession[];
  access: Record<string, string>; // token -> user id
  refresh: Record<string, string>;
}

const empty = (): DemoState => ({ users: [], sessions: [], access: {}, refresh: {} });
let memory: DemoState | null = null;

function load(): DemoState {
  if (memory) return memory;
  try {
    const raw = typeof sessionStorage !== "undefined" ? sessionStorage.getItem(STATE_KEY) : null;
    memory = raw ? (JSON.parse(raw) as DemoState) : empty();
  } catch {
    memory = empty();
  }
  return memory;
}

function save(state: DemoState) {
  memory = state;
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify(state));
  } catch {
    /* private mode or quota: the demo keeps working in memory */
  }
}

/** For tests: forget everything. */
export function resetDemo() {
  memory = null;
  try {
    sessionStorage.removeItem(STATE_KEY);
  } catch {
    /* nothing stored */
  }
}

const now = () => new Date().toISOString();
const randomId = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fail = (status: number, detail: string) => json({ detail }, status);

function issueTokens(state: DemoState, userId: string) {
  const access = `demo-access-${randomId()}`;
  const refresh = `demo-refresh-${randomId()}`;
  state.access[access] = userId;
  state.refresh[refresh] = userId;
  return { access_token: access, refresh_token: refresh, token_type: "bearer", expires_in: 1800 };
}

function summary(s: DemoSession) {
  const { id, role_title, seniority, focus_areas, status, cost_usd, created_at, completed_at } = s;
  return { id, role_title, seniority, focus_areas, status, cost_usd, created_at, completed_at };
}

const detail = (s: DemoSession) => ({ ...summary(s), turns: s.turns, scorecard: s.scorecard });

async function body(init?: RequestInit): Promise<Record<string, unknown>> {
  if (typeof init?.body !== "string") return {};
  try {
    return JSON.parse(init.body) as Record<string, unknown>;
  } catch {
    return {};
  }
}

const sleep = (ms: number, signal?: AbortSignal | null) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("aborted", "AbortError"));
    });
  });

function streamResponse(state: DemoState, s: DemoSession, signal?: AbortSignal | null): Response {
  const text = nextQuestion(s.turns);
  const index = (s.turns.length ? Math.max(...s.turns.map((t) => t.index)) : -1) + 1;
  const encoder = new TextEncoder();
  const event = (name: string, data: unknown) =>
    encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      try {
        controller.enqueue(event("start", { index }));
        for (const word of text.split(" ")) {
          await sleep(22, signal);
          controller.enqueue(event("token", { text: `${word} ` }));
        }
        // Persist only once the question is complete, like the backend does.
        s.turns.push({ id: randomId(), index, speaker: "interviewer", content: text, created_at: now() });
        s.cost_usd = Math.round((s.cost_usd + QUESTION_COST_USD) * 1e6) / 1e6;
        save(state);
        controller.enqueue(event("done", { index, content: text, cost_usd: QUESTION_COST_USD }));
        controller.close();
      } catch {
        // Aborted: the reader went away, so there is nobody to tell.
        try {
          controller.close();
        } catch {
          /* already closed */
        }
      }
    },
  });

  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  });
}

/**
 * Drop-in for `fetch`, answering the paths `lib/api.ts` calls. `base` is the API
 * prefix the client puts in front of every path.
 */
export function createDemoFetch(base: string) {
  return async function demoFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = (url.startsWith(base) ? url.slice(base.length) : url).split("?")[0] ?? "";
    const method = (init.method ?? "GET").toUpperCase();
    const state = load();

    // --- auth, no token needed -------------------------------------------
    if (path === "/auth/register" && method === "POST") {
      const b = await body(init);
      const email = String(b.email ?? "").trim().toLowerCase();
      const password = String(b.password ?? "");
      if (!email.includes("@")) return fail(422, "Enter a valid email address.");
      if (password.length < 10) return fail(422, "Password must be at least 10 characters.");
      if (state.users.some((u) => u.email === email)) return fail(409, "Could not create that account");
      const user: DemoUser = {
        id: randomId(),
        email,
        password,
        display_name: String(b.display_name ?? "").trim() || (email.split("@")[0] ?? email),
        created_at: now(),
      };
      state.users.push(user);
      const tokens = issueTokens(state, user.id);
      save(state);
      return json(tokens, 201);
    }

    if (path === "/auth/login" && method === "POST") {
      const b = await body(init);
      const email = String(b.email ?? "").trim().toLowerCase();
      const user = state.users.find((u) => u.email === email && u.password === b.password);
      if (!user) return fail(401, "Incorrect email or password");
      const tokens = issueTokens(state, user.id);
      save(state);
      return json(tokens);
    }

    if (path === "/auth/refresh" && method === "POST") {
      const b = await body(init);
      const token = String(b.refresh_token ?? "");
      const userId = state.refresh[token];
      if (!userId) return fail(401, "Invalid refresh token");
      delete state.refresh[token]; // rotation: a refresh token works once
      const tokens = issueTokens(state, userId);
      save(state);
      return json(tokens);
    }

    if (path === "/auth/logout" && method === "POST") {
      const b = await body(init);
      delete state.refresh[String(b.refresh_token ?? "")];
      save(state);
      return json(null, 204);
    }

    // --- everything else needs a bearer token -----------------------------
    const header = new Headers(init.headers).get("Authorization") ?? "";
    const userId = state.access[header.replace(/^Bearer\s+/i, "")];
    const user = state.users.find((u) => u.id === userId);
    if (!user) return fail(401, "Not authenticated");

    if (path === "/auth/me" && method === "GET") {
      const { id, email, display_name, created_at } = user;
      return json({ id, email, display_name, created_at });
    }

    if (path === "/sessions" && method === "GET") {
      const mine = state.sessions
        .filter((s) => s.user_id === user.id)
        .sort((a, b) => b.created_at.localeCompare(a.created_at));
      return json(mine.map(summary));
    }

    if (path === "/sessions" && method === "POST") {
      const b = await body(init);
      const taken = new Set(state.sessions.map((s) => s.id));
      const id = DEMO_SESSION_IDS.find((i) => !taken.has(i));
      if (!id) return fail(409, "The demo keeps 12 interviews per tab. Delete one to start another.");
      const s: DemoSession = {
        id,
        user_id: user.id,
        role_title: String(b.role_title ?? "Software Engineer").slice(0, 120),
        seniority: String(b.seniority ?? "mid"),
        focus_areas: Array.isArray(b.focus_areas) ? b.focus_areas.map(String).slice(0, 8) : [],
        status: "active",
        cost_usd: 0,
        created_at: now(),
        completed_at: null,
        turns: [],
        scorecard: null,
      };
      state.sessions.push(s);
      save(state);
      return json(summary(s), 201);
    }

    const m = path.match(/^\/sessions\/([^/]+)(\/[a-z-]+)?$/);
    if (!m) return fail(404, "Not Found");
    // A session that is not yours does not exist, as in the backend.
    const s = state.sessions.find((x) => x.id === m[1] && x.user_id === user.id);
    if (!s) return fail(404, "Session not found");
    const action = m[2] ?? "";

    if (action === "" && method === "GET") return json(detail(s));

    if (action === "" && method === "DELETE") {
      state.sessions = state.sessions.filter((x) => x !== s);
      save(state);
      return json(null, 204);
    }

    if (action === "/cost" && method === "GET") {
      return json({
        spent_usd: s.cost_usd,
        ceiling_usd: COST_CEILING_USD,
        remaining_usd: Math.max(0, Math.round((COST_CEILING_USD - s.cost_usd) * 1e6) / 1e6),
      });
    }

    if (action === "/answers" && method === "POST") {
      if (s.status !== "active") return fail(409, "This session is already closed");
      const b = await body(init);
      const content = String(b.content ?? "").trim();
      if (!content) return fail(422, "An answer cannot be empty.");
      const index = (s.turns.length ? Math.max(...s.turns.map((t) => t.index)) : -1) + 1;
      const turn: DemoTurn = { id: randomId(), index, speaker: "candidate", content, created_at: now() };
      s.turns.push(turn);
      save(state);
      return json(turn, 201);
    }

    if (action === "/stream" && method === "GET") {
      if (s.status !== "active") return fail(409, "This session is already closed");
      if (s.cost_usd >= COST_CEILING_USD)
        return fail(402, "This session has reached its spend guardrail. End it to see your scorecard.");
      const asked = s.turns.filter((t) => t.speaker === "interviewer").length;
      if (asked >= MAX_QUESTIONS)
        return fail(409, "This interview has run its course. End it to see your scorecard.");
      return streamResponse(state, s, init.signal);
    }

    if (action === "/complete" && method === "POST") {
      if (s.status === "completed" && s.scorecard) return json(s.scorecard);
      if (!s.turns.some((t) => t.speaker === "candidate"))
        return fail(400, "Answer at least one question before ending the session.");
      s.scorecard = { ...SCRIPTED_SCORE, created_at: now() };
      s.status = "completed";
      s.completed_at = now();
      s.cost_usd = Math.round((s.cost_usd + SCORING_COST_USD) * 1e6) / 1e6;
      save(state);
      return json(s.scorecard);
    }

    return fail(405, "Method Not Allowed");
  };
}
