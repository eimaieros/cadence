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
} from "./schemas";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "cadence.access";

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
let memoryToken: string | null = null;

export const auth = {
  set(token: string) {
    memoryToken = token;
    if (typeof window !== "undefined") sessionStorage.setItem(TOKEN_KEY, token);
  },
  get(): string | null {
    if (memoryToken) return memoryToken;
    if (typeof window === "undefined") return null;
    memoryToken = sessionStorage.getItem(TOKEN_KEY);
    return memoryToken;
  },
  clear() {
    memoryToken = null;
    if (typeof window !== "undefined") sessionStorage.removeItem(TOKEN_KEY);
  },
};

function headers(json = true): HeadersInit {
  const h: Record<string, string> = {};
  if (json) h["Content-Type"] = "application/json";
  const token = auth.get();
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

async function request<T extends z.ZodTypeAny>(
  path: string,
  schema: T,
  init?: RequestInit,
): Promise<z.infer<T>> {
  const res = await fetch(`${BASE}${path}`, { ...init, headers: headers() });

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

  me: () => request("/auth/me", UserSchema),

  listSessions: () => request("/sessions", SessionSummarySchema.array()),

  getSession: (id: string) => request(`/sessions/${id}`, SessionDetailSchema),

  createSession: (role_title: string, focus_areas: string[], seniority: Seniority) =>
    request("/sessions", SessionSummarySchema, {
      method: "POST",
      body: JSON.stringify({ role_title, focus_areas, seniority }),
    }),

  deleteSession: async (id: string) => {
    const res = await fetch(`${BASE}/sessions/${id}`, { method: "DELETE", headers: headers() });
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
  const res = await fetch(`${BASE}/sessions/${sessionId}/stream`, {
    headers: headers(false),
    signal,
  });

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
