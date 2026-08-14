import { z } from "zod";

/*
  Runtime validation at the network boundary.

  TypeScript types are erased at build time. `as SessionDetail` on a fetch
  result is a promise the compiler cannot keep — if the API changes a field,
  the app compiles cleanly and then breaks at runtime, somewhere far away from
  the cause.

  Parsing every response with Zod means a contract mismatch surfaces at the
  fetch call with a readable path, and the inferred types below are derived
  from the same schema, so there is one source of truth rather than two that
  drift apart.
*/

export const TokenPairSchema = z.object({
  access_token: z.string().min(1),
  refresh_token: z.string().min(1),
  token_type: z.literal("bearer"),
  expires_in: z.number().int().positive(),
});

export const UserSchema = z.object({
  id: z.string(),
  email: z.string(),
  display_name: z.string(),
  created_at: z.string(),
});

export const SpeakerSchema = z.enum(["interviewer", "candidate"]);
export const StatusSchema = z.enum(["active", "completed", "abandoned"]);

export const TurnSchema = z.object({
  id: z.string(),
  index: z.number().int(),
  speaker: SpeakerSchema,
  content: z.string(),
  created_at: z.string(),
});

export const DimensionSchema = z.object({
  name: z.string(),
  score: z.number().int().min(1).max(5),
  note: z.string(),
});

export const ScorecardSchema = z.object({
  overall: z.number().int().min(0).max(100),
  summary: z.string(),
  dimensions: z.array(DimensionSchema),
  strengths: z.array(z.string()),
  gaps: z.array(z.string()),
  created_at: z.string(),
});

export const SessionSummarySchema = z.object({
  id: z.string(),
  role_title: z.string(),
  seniority: z.string(),
  focus_areas: z.array(z.string()),
  status: StatusSchema,
  cost_usd: z.number(),
  created_at: z.string(),
  completed_at: z.string().nullable(),
});

export const SessionDetailSchema = SessionSummarySchema.extend({
  turns: z.array(TurnSchema),
  scorecard: ScorecardSchema.nullable(),
});

export const CostSchema = z.object({
  spent_usd: z.number(),
  ceiling_usd: z.number(),
  remaining_usd: z.number(),
});

/* Shapes carried by the SSE stream. Validated too — the stream is just another
   untrusted boundary, and a malformed event should fail loudly. */
export const StreamStartSchema = z.object({ index: z.number().int() });
export const StreamTokenSchema = z.object({ text: z.string() });
export const StreamDoneSchema = z.object({
  index: z.number().int(),
  content: z.string(),
  cost_usd: z.number(),
});
export const StreamErrorSchema = z.object({ detail: z.string() });

export type TokenPair = z.infer<typeof TokenPairSchema>;
export type User = z.infer<typeof UserSchema>;
export type Turn = z.infer<typeof TurnSchema>;
export type Dimension = z.infer<typeof DimensionSchema>;
export type Scorecard = z.infer<typeof ScorecardSchema>;
export type SessionSummary = z.infer<typeof SessionSummarySchema>;
export type SessionDetail = z.infer<typeof SessionDetailSchema>;
export type Cost = z.infer<typeof CostSchema>;
export type Seniority = "junior" | "mid" | "senior" | "staff";
