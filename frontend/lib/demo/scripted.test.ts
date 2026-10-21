import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { COST_CEILING_USD, MAX_QUESTIONS, QUESTION_BANK, SCRIPTED_SCORE, nextQuestion } from "./scripted";

/*
  The demo runs a TypeScript copy of the backend's scripted interviewer. A copy
  nobody compares is a second version waiting to diverge, so these tests read
  the Python source and fail the moment the two say different things.
*/

const backend = (rel: string) => readFileSync(new URL(`../../../backend/app/${rel}`, import.meta.url), "utf8");
const client = backend("llm/client.py");

/** The Python literals inside `[ ... ]` after `name =`, adjacent strings joined. */
function pythonStrings(block: string): string[] {
  const joined = block.replace(/"\s*\n\s*"/g, ""); // "a " "b" across lines -> one literal
  return [...joined.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => (m[1] ?? "").replace(/\\'/g, "'"));
}

function between(src: string, start: string, end: string): string {
  const i = src.indexOf(start);
  expect(i, `${start} not found`).toBeGreaterThanOrEqual(0);
  const j = src.indexOf(end, i + start.length);
  return src.slice(i + start.length, j);
}

describe("the demo's scripted interviewer matches the backend", () => {
  it("asks the same questions in the same order", () => {
    expect(QUESTION_BANK).toEqual(pythonStrings(between(client, "_QUESTION_BANK = [", "\n]")));
  });

  it("returns the same scorecard", () => {
    const block = between(client, "_SCRIPTED_SCORE = {", "\n}\n");
    expect(Number(block.match(/"overall": (\d+)/)![1])).toBe(SCRIPTED_SCORE.overall);
    const summary = pythonStrings(between(block, '"summary": (', ")"))[0];
    expect(summary).toBe(SCRIPTED_SCORE.summary);
    const dims = [...block.matchAll(/\{"name": "([^"]+)", "score": (\d), "note": "([^"]+)"\}/g)].map((m) => ({
      name: m[1],
      score: Number(m[2]),
      note: m[3],
    }));
    expect(dims).toEqual(SCRIPTED_SCORE.dimensions);
    expect(pythonStrings(between(block, '"strengths": [', "]"))).toEqual(SCRIPTED_SCORE.strengths);
    expect(pythonStrings(between(block, '"gaps": [', "]"))).toEqual(SCRIPTED_SCORE.gaps);
  });

  it("words the follow-up the same way", () => {
    const first = client.match(/f'(Before we move on[^']*)'/)?.[1] ?? "";
    const second = client.match(/f"(that: users[^"]*)"/)?.[1] ?? "";
    const answer = "I kept the site up through the whole festival without any outage at all";
    const topic = "I kept the site up through the whole"; // the first eight words, as in Python
    expect(nextQuestion([{ speaker: "interviewer", content: QUESTION_BANK[0] ?? "" }, { speaker: "candidate", content: answer }])).toBe(
      first.replace("{topic}", topic) + second,
    );
  });

  it("uses the backend's question limit and spend ceiling", () => {
    expect(Number(backend("routers/sessions.py").match(/^MAX_QUESTIONS = (\d+)/m)![1])).toBe(MAX_QUESTIONS);
    expect(Number(backend("config.py").match(/session_cost_ceiling_usd: float = Field\(default=([\d.]+)/)![1])).toBe(
      COST_CEILING_USD,
    );
  });
});
