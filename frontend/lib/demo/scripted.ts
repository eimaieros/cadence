import type { Scorecard } from "../schemas";

/*
  The scripted interviewer, ported from the backend.

  The backend falls back to `ScriptedProvider` (backend/app/llm/client.py) when
  there is no API key, so the whole product runs offline. The browser demo runs
  that same interviewer here, against an in-memory stand-in for the API, so a
  visitor sees the real client code paths — Zod at the boundary, the SSE parser,
  the token refresh — without a server.

  Keep this file in step with the backend. `scripted.test.ts` reads the Python
  source and fails if the question bank or the scorecard drift apart.
*/

export const MAX_QUESTIONS = 6; // backend/app/routers/sessions.py
export const COST_CEILING_USD = 0.5; // settings.session_cost_ceiling_usd

export const QUESTION_BANK = [
  "Walk me through a system you've shipped that had real users on it. What was the hardest constraint you were working under?",
  "You mentioned that setup -- where would it break first if traffic went up ten times overnight, and how would you know?",
  "Tell me about a bug that took you far too long to find. What was the actual cause, and what did you change afterwards so it couldn't happen again?",
  "How do you decide when a piece of code is worth abstracting versus leaving duplicated? Give me a case where you got that call wrong.",
  "Describe a time you disagreed with someone on a technical decision. What did you do, and what happened?",
  "Last one: what's something in your current stack you think is a mistake, and what would you replace it with?",
];

export const CLOSING =
  "That's everything I wanted to cover. Thanks -- end the session to see your scorecard.";

export const SCRIPTED_SCORE: Omit<Scorecard, "created_at"> = {
  overall: 68,
  summary:
    "You explain what you built clearly and you're comfortable talking about " +
    "production constraints. Where you lose ground is specificity -- several " +
    "answers described an approach without the numbers or outcome that would " +
    "let an interviewer judge the scale you worked at.",
  dimensions: [
    { name: "Technical depth", score: 4, note: "Comfortable below the framework layer, especially on failure modes." },
    { name: "Structure", score: 4, note: "Answers had a clear beginning and end without rambling." },
    { name: "Specificity", score: 2, note: "Claims about scale and impact mostly arrived without numbers attached." },
    { name: "Trade-off reasoning", score: 3, note: "Named alternatives, but rarely said what each option cost." },
    { name: "Communication", score: 4, note: "Plain language, and you corrected yourself out loud rather than bluffing." },
  ],
  strengths: [
    "You described the failure mode before the happy path, which reads as production experience.",
    "You corrected an answer mid-sentence instead of defending it.",
  ],
  gaps: [
    "Attach a number to every claim about scale -- users, requests, latency, or money.",
    "When you name an alternative approach, say what choosing it would have cost.",
    "Close each answer with the outcome. Several stopped at the method.",
  ],
};

export interface ScriptTurn {
  speaker: "interviewer" | "candidate";
  content: string;
}

/**
 * The next question, by the backend's rules: an answer that made its case
 * without a single number gets a follow-up asking for one (the backend flags it
 * with the `flag_follow_up` tool first); otherwise the next question in the bank,
 * counted by how many questions have been asked, follow-ups included.
 */
export function nextQuestion(turns: ScriptTurn[]): string {
  const asked = turns.filter((t) => t.speaker === "interviewer").length;
  const last = turns[turns.length - 1];
  if (last && last.speaker === "candidate" && !/\d/.test(last.content)) {
    const topic = last.content.split(/\s+/).filter(Boolean).slice(0, 8).join(" ").slice(0, 80);
    return `Before we move on -- you said "${topic}". What number would you put on that: users, requests, latency or money?`;
  }
  return QUESTION_BANK[asked] ?? CLOSING;
}
