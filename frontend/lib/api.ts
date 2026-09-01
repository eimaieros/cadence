import { SseParser } from "./sse";
import type { z } from "zod";
import {
  CostSchema,
  SessionDetailSchema,
  SessionSummarySchema,
  StreamDoneSchema,
  StreamErrorSchema,
  StreamTokenSchema,
  TokenPairSchema,
  UserSchema,
  type Seniority,
  type TokenPair,
} from "./schemas";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const ACCESS_KEY = "cadence.access";
const REFRESH_KEY = "cadence.refresh";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/* Tokens live in memory first and sessionStorage second, so a token does not
   outlive the browser session. localStorage would persist it indefinitely for
   any script on the origin to read. */
let memoryAccess: string | null = null;
let memoryRefresh: string | null = null;

export const auth = {
  set(tokens: TokenPair) {
    memoryAccess = tokens.access_token;
    memoryRefresh = tokens.refresh_token;
    if (typeof window !== "undefined") {
      sessionStorage.setItem(ACCESS_KEY, tokens.access_token);
      sessionStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    }
  },
  get(): string | null {
    if (memoryAccess) return memoryAccess;
    if (typeof window === "undefined") return null;
    memoryAccess = sessionStorage.getItem(ACCESS_KEY);
    return memoryAccess;
  },
  getRefresh(): string | null {
    if (memoryRefresh) return memoryRefresh;
    if (typeof window === "undefined") return null;
    memoryRefresh = sessionStorage.getItem(REFRESH_KEY);
    return memoryRefresh;
  },
  clear() {
    memoryAccess = null;
    memoryRefresh = null;
    if (typeof window !== "undefined") {
      sessionStorage.removeItem(ACCESS_KEY);
      sessionStorage.removeItem(REFRESH_KEY);
    }
  },
};

function headers(json = true, existing?: HeadersInit): Headers {
  const h = new Headers(existing);
  if (json && !h.has("Content-Type")) h.set("Content-Type", "application/json");
  const token = auth.get();
  if (token) h.set("Authorization", `Bearer ${token}`);
  return h;
}

let refreshInFlight: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  const refresh = auth.getRefresh();
  if (!refresh) return false;

  refreshInFlight = (async () => {
    try {
      const res = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!res.ok) {
        auth.clear();
        return false;
      }
      auth.set(TokenPairSchema.parse(await res.json()));
      return true;
    } catch {
      auth.clear();
      return false;
    }
  })().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

async function fetchWithAuth(
  path: string,
  init: RequestInit = {},
  json = true,
): Promise<Response> {
  const accessUsed = auth.get();
  const send = () =>
    fetch(`${BASE}${path}`, { ...init, headers: headers(json, init.headers) });
  let res = await send();

  const sessionEndpoint = !new Set([
    "/auth/register",
    "/auth/login",
    "/auth/refresh",
    "/auth/logout",
  ]).has(path);
  if (res.status === 401 && sessionEndpoint) {
    // Another request may already have rotated the pair while this one was in
    // flight. Retry with that access token before attempting another rotation.
    const accessNow = auth.get();
    const recovered =
      accessNow !== null && accessNow !== accessUsed ? true : await refreshSession();
    if (recovered) res = await send();
  }
  return res;
}

async function request<T extends z.ZodTypeAny>(
  path: string,
  schema: T,
  init?: RequestInit,
): Promise<z.infer<T>> {
  const res = await fetchWithAuth(path, init);

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body; the status text will do */
    }
    throw new ApiError(detail, res.status);
  }

  if (res.status === 204) return undefined as z.infer<T>;
  // parse, not assert — see the note in schemas.ts
  return schema.parse(await res.json());
}

export const api = {
  register: (email: string, password: string, display_name: string) =>
    request("/auth/register", TokenPairSchema, {
      method: "POST",
      body: JSON.stringify({ email, password, display_name }),
    }),

  login: (email: string, password: string) =>
    request("/auth/login", TokenPairSchema, {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  logout: async () => {
    const refresh = auth.getRefresh();
    if (!refresh) return;
    await fetch(`${BASE}/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
  },

  me: () => request("/auth/me", UserSchema),

  listSessions: () => request("/sessions", SessionSummarySchema.array()),

  getSession: (id: string) => request(`/sessions/${id}`, SessionDetailSchema),

  createSession: (role_title: string, focus_areas: string[], seniority: Seniority) =>
    request("/sessions", SessionSummarySchema, {
      method: "POST",
      body: JSON.stringify({ role_title, focus_areas, seniority }),
    }),

  deleteSession: async (id: string) => {
    const res = await fetchWithAuth(`/sessions/${id}`, { method: "DELETE" });
    if (!res.ok) throw new ApiError("Could not delete that session", res.status);
  },

  submitAnswer: (id: string, content: string) =>
    request(`/sessions/${id}/answers`, SessionDetailSchema.shape.turns.element, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),

  complete: (id: string) =>
    request(`/sessions/${id}/complete`, SessionDetailSchema.shape.scorecard.unwrap(), {
      method: "POST",
    }),

  cost: (id: string) => request(`/sessions/${id}/cost`, CostSchema),
};

/* ------------------------------------------------------------------------
   Streaming

   `EventSource` cannot attach an Authorization header, which is why so many
   SSE implementations end up with the token in the query string — where it
   lands in access logs, proxy logs and browser history.

   `fetch` + `ReadableStream` takes headers normally and parses the same wire
   format. It also gives an AbortSignal, so navigating away actually cancels
   the request instead of leaving the server generating tokens for a reader
   that has gone.
   ------------------------------------------------------------------------ */

export interface StreamHandlers {
  onToken: (text: string) => void;
  onDone: (payload: { index: number; content: string; cost_usd: number }) => void;
  onError: (detail: string) => void;
}

export async function streamQuestion(
  sessionId: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetchWithAuth(
    `/sessions/${sessionId}/stream`,
    { signal },
    false,
  );

  if (!res.ok || !res.body) {
    let detail = "The interviewer is unavailable right now.";
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* keep the default */
    }
    handlers.onError(detail);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const sse = new SseParser();

  const entregar = (e: { event: string; data: string }) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(e.data);
    } catch {
      return;
    }

    if (e.event === "token") {
      const r = StreamTokenSchema.safeParse(parsed);
      if (r.success) handlers.onToken(r.data.text);
    } else if (e.event === "done") {
      const r = StreamDoneSchema.safeParse(parsed);
      if (r.success) handlers.onDone(r.data);
    } else if (e.event === "error") {
      const r = StreamErrorSchema.safeParse(parsed);
      handlers.onError(r.success ? r.data.detail : "The interviewer stopped unexpectedly.");
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // stream:true, because a multi-byte character can be split across chunks
    // exactly like an event can. Decoding each chunk independently turns an
    // accented word into a replacement character.
    for (const e of sse.push(decoder.decode(value, { stream: true }))) entregar(e);
  }

  // Flush TextDecoder's internal UTF-8 state before flushing the SSE parser.
  // The final code point may straddle the last two network chunks.
  for (const e of sse.push(decoder.decode())) entregar(e);
  // Whatever the server left unterminated. See SseParser.flush().
  for (const e of sse.flush()) entregar(e);
}
