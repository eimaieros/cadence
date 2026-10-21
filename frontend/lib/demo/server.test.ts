import { beforeEach, describe, expect, it } from "vitest";

import { SseParser, type SseEvent } from "../sse";
import { COST_CEILING_USD, MAX_QUESTIONS, QUESTION_BANK, SCRIPTED_SCORE } from "./scripted";
import { createDemoFetch, resetDemo } from "./server";

/*
  The demo's stand-in API, checked against the backend's contract: the same
  routes, status codes and `detail` messages, and the scripted interviewer's
  rules for what it asks next. The pages never know which one they are talking
  to, so any difference here is a bug the demo would show to a visitor.
*/

const BASE = "https://api.cadence.demo";
const demoFetch = createDemoFetch(BASE);

let token = "";
const call = (path: string, init: RequestInit = {}) =>
  demoFetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
const post = (path: string, body?: unknown) =>
  call(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

async function events(res: Response): Promise<SseEvent[]> {
  const parser = new SseParser();
  const out: SseEvent[] = [];
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    out.push(...parser.push(decoder.decode(value, { stream: true })));
  }
  return [...out, ...parser.flush()];
}

async function ask(id: string): Promise<string> {
  const res = await call(`/sessions/${id}/stream`);
  expect(res.status).toBe(200);
  expect(res.headers.get("Content-Type")).toBe("text/event-stream");
  const evs = await events(res);
  expect(evs[0]?.event).toBe("start");
  const done = evs.find((e) => e.event === "done");
  expect(done).toBeDefined();
  const tokens = evs.filter((e) => e.event === "token").map((e) => JSON.parse(e.data).text).join("");
  const content = JSON.parse(done!.data).content as string;
  expect(tokens.trim()).toBe(content);
  return content;
}

async function newSession(): Promise<string> {
  const res = await post("/sessions", { role_title: "Backend Engineer", focus_areas: ["PostgreSQL"], seniority: "mid" });
  expect(res.status).toBe(201);
  return (await res.json()).id as string;
}

async function register(email = "ana@example.com") {
  token = "";
  const res = await post("/auth/register", { email, password: "correct horse battery", display_name: "Ana" });
  expect(res.status).toBe(201);
  const pair = await res.json();
  token = pair.access_token;
  return pair as { access_token: string; refresh_token: string };
}

beforeEach(() => {
  resetDemo();
  token = "";
});

describe("demo API", () => {
  it("registers, opens a session and streams the first question of the scripted bank", async () => {
    await register();
    const me = await (await call("/auth/me")).json();
    expect(me.email).toBe("ana@example.com");
    const id = await newSession();
    expect(id).toMatch(/^demo-\d+$/);
    expect(await ask(id)).toBe(QUESTION_BANK[0]);
    const detail = await (await call(`/sessions/${id}`)).json();
    expect(detail.turns).toHaveLength(1);
    expect(detail.turns[0].speaker).toBe("interviewer");
  });

  it("follows up on an answer with no number, and the follow-up counts as a question", async () => {
    await register();
    const id = await newSession();
    await ask(id);
    expect((await post(`/sessions/${id}/answers`, { content: "I rebuilt the ticketing page for a big festival" })).status).toBe(201);
    const followUp = await ask(id);
    expect(followUp).toBe(
      'Before we move on -- you said "I rebuilt the ticketing page for a big". What number would you put on that: users, requests, latency or money?',
    );
    await post(`/sessions/${id}/answers`, { content: "1.9 million users over the cycle" });
    // Two questions asked, so the bank continues at index 2 — as in the backend.
    expect(await ask(id)).toBe(QUESTION_BANK[2]);
  });

  it(`stops after ${MAX_QUESTIONS} questions with the backend's 409`, async () => {
    await register();
    const id = await newSession();
    for (let i = 0; i < MAX_QUESTIONS; i++) {
      await ask(id);
      await post(`/sessions/${id}/answers`, { content: `answer ${i} with 3 numbers` });
    }
    const res = await call(`/sessions/${id}/stream`);
    expect(res.status).toBe(409);
    expect((await res.json()).detail).toBe("This interview has run its course. End it to see your scorecard.");
  });

  it("scores only after an answer, returns the scripted scorecard, then closes the session", async () => {
    await register();
    const id = await newSession();
    await ask(id);
    const early = await post(`/sessions/${id}/complete`);
    expect(early.status).toBe(400);
    expect((await early.json()).detail).toBe("Answer at least one question before ending the session.");

    await post(`/sessions/${id}/answers`, { content: "Postgres with 40 ms p95" });
    const card = await (await post(`/sessions/${id}/complete`)).json();
    expect(card.overall).toBe(SCRIPTED_SCORE.overall);
    expect(card.dimensions).toEqual(SCRIPTED_SCORE.dimensions);
    // Scoring twice returns the same card instead of a second one.
    expect((await (await post(`/sessions/${id}/complete`)).json()).created_at).toBe(card.created_at);

    for (const res of [await call(`/sessions/${id}/stream`), await post(`/sessions/${id}/answers`, { content: "late" })]) {
      expect(res.status).toBe(409);
      expect((await res.json()).detail).toBe("This session is already closed");
    }
  });

  it("needs a token, and another person's session does not exist", async () => {
    expect((await call("/sessions")).status).toBe(401);
    await register("ana@example.com");
    const id = await newSession();
    await register("rui@example.com");
    const res = await call(`/sessions/${id}`);
    expect(res.status).toBe(404);
    expect(await (await call("/sessions")).json()).toEqual([]);
  });

  it("rotates refresh tokens: each one works once", async () => {
    const pair = await register();
    token = "";
    const first = await post("/auth/refresh", { refresh_token: pair.refresh_token });
    expect(first.status).toBe(200);
    const replay = await post("/auth/refresh", { refresh_token: pair.refresh_token });
    expect(replay.status).toBe(401);
  });

  it("meters cost per question against the backend's ceiling", async () => {
    await register();
    const id = await newSession();
    await ask(id);
    await post(`/sessions/${id}/answers`, { content: "about 200 requests a second" });
    await ask(id);
    const cost = await (await call(`/sessions/${id}/cost`)).json();
    expect(cost.ceiling_usd).toBe(COST_CEILING_USD);
    expect(cost.spent_usd).toBeGreaterThan(0);
    expect(cost.spent_usd + cost.remaining_usd).toBeCloseTo(COST_CEILING_USD, 6);
  });

  it("does not persist a question whose stream was aborted", async () => {
    await register();
    const id = await newSession();
    const controller = new AbortController();
    const res = await call(`/sessions/${id}/stream`, { signal: controller.signal });
    const reader = res.body!.getReader();
    await reader.read(); // the start event
    controller.abort();
    await reader.read().catch(() => undefined);
    await new Promise((r) => setTimeout(r, 60));
    const detail = await (await call(`/sessions/${id}`)).json();
    expect(detail.turns).toHaveLength(0);
  });
});
