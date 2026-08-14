"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, auth, streamQuestion } from "@/lib/api";
import type { Scorecard, SessionDetail } from "@/lib/schemas";

/*
  The signature surface.

  Everything else in Cadence is porcelain and paper. Here the page inverts to
  graphite, because being interviewed does not feel like reviewing an interview
  — the world narrows to one question at a time. The inversion is the one place
  boldness is spent; the layout underneath stays plain on purpose.

  The transcript has a monospace timecode gutter and the incoming question types
  itself in with a live caret, so the SSE transport is visible rather than
  hidden behind a spinner. Waiting is the honest state of a streaming system.
*/

function timecode(iso: string, start: string): string {
  const secs = Math.max(0, Math.floor((Date.parse(iso) - Date.parse(start)) / 1000));
  const m = String(Math.floor(secs / 60)).padStart(2, "0");
  const s = String(secs % 60).padStart(2, "0");
  return `${m}:${s}`;
}

export default function InterviewRoom() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = params.id;

  const [session, setSession] = useState<SessionDetail | null>(null);
  const [streaming, setStreaming] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [cost, setCost] = useState<{ spent: number; ceiling: number } | null>(null);
  const [scorecard, setScorecard] = useState<Scorecard | null>(null);
  const [scoring, setScoring] = useState(false);

  const bottom = useRef<HTMLDivElement>(null);
  const abort = useRef<AbortController | null>(null);
  const started = useRef(false);

  const refreshCost = useCallback(async () => {
    try {
      const c = await api.cost(id);
      setCost({ spent: c.spent_usd, ceiling: c.ceiling_usd });
    } catch {
      /* the meter is informational; a failure here must not break the room */
    }
  }, [id]);

  const askNext = useCallback(async () => {
    setError(null);
    setStreaming("");
    setIsStreaming(true);

    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;

    await streamQuestion(
      id,
      {
        onToken: (text) => setStreaming((prev) => prev + text),
        onDone: async () => {
          setIsStreaming(false);
          setStreaming("");
          try {
            setSession(await api.getSession(id));
          } catch {
            /* the turn is persisted server-side; a refetch failure is cosmetic */
          }
          void refreshCost();
        },
        onError: (detail) => {
          setIsStreaming(false);
          setStreaming("");
          setError(detail);
        },
      },
      controller.signal,
    );
  }, [id, refreshCost]);

  // Load the session, and open with a question if the transcript is empty.
  useEffect(() => {
    if (!auth.get()) {
      router.push("/login");
      return;
    }
    if (started.current) return;
    started.current = true;

    (async () => {
      try {
        const detail = await api.getSession(id);
        setSession(detail);
        setScorecard(detail.scorecard);
        void refreshCost();
        if (detail.turns.length === 0 && detail.status === "active") {
          void askNext();
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          setError("That session doesn't exist, or it isn't yours.");
        } else if (err instanceof ApiError && err.status === 401) {
          auth.clear();
          router.push("/login");
        } else {
          setError("Couldn't load this session.");
        }
      }
    })();

    return () => abort.current?.abort();
  }, [id, router, askNext, refreshCost]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [session?.turns.length, streaming, scorecard]);

  async function send() {
    const text = answer.trim();
    if (!text || isStreaming) return;
    setAnswer("");
    setError(null);
    try {
      await api.submitAnswer(id, text);
      setSession(await api.getSession(id));
      await askNext();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't send that answer.");
      setAnswer(text);
    }
  }

  async function finish() {
    setScoring(true);
    setError(null);
    try {
      setScorecard(await api.complete(id));
      setSession(await api.getSession(id));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Couldn't produce a scorecard just now.",
      );
    } finally {
      setScoring(false);
    }
  }

  const openedAt = session?.created_at ?? new Date().toISOString();
  const answered = session?.turns.filter((t) => t.speaker === "candidate").length ?? 0;
  const closed = session?.status === "completed";

  return (
    <div
      className="min-h-screen"
      style={{ background: "var(--color-graphite)", color: "var(--color-porcelain)" }}
    >
      {/* Control strip -------------------------------------------------- */}
      <header
        className="sticky top-0 z-10 border-b backdrop-blur"
        style={{
          borderColor: "var(--color-graphite-line)",
          background: "color-mix(in srgb, var(--color-graphite) 88%, transparent)",
        }}
      >
        <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3 px-6 py-4">
          <div className="flex min-w-0 items-center gap-3">
            {!closed && (
              <span
                className="inline-block h-2 w-2 shrink-0 rounded-full"
                style={{
                  background: "var(--color-annotation)",
                  animation: "blink 1.6s ease-in-out infinite",
                }}
                aria-hidden
              />
            )}
            <p className="tabular truncate text-[0.72rem] tracking-tight">
              {session?.role_title ?? "Loading…"}
            </p>
          </div>

          <div className="flex items-center gap-5">
            {cost && (
              <span
                className="tabular text-[0.64rem]"
                style={{ color: "var(--color-mute)" }}
                title="Spend on this session against its hard ceiling"
              >
                ${cost.spent.toFixed(4)} / ${cost.ceiling.toFixed(2)}
              </span>
            )}
            <Link
              href="/sessions"
              className="tabular text-[0.64rem] uppercase tracking-widest transition-colors hover:text-[color:var(--color-porcelain)]"
              style={{ color: "var(--color-mute)" }}
            >
              Leave
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 pt-10 pb-40">
        {/* Transcript --------------------------------------------------- */}
        <ol className="space-y-8">
          {session?.turns.map((turn) => (
            <li key={turn.id} className="rise grid grid-cols-[3.2rem_1fr] gap-4">
              <span
                className="tabular pt-1 text-[0.64rem]"
                style={{ color: "var(--color-graphite-line)" }}
              >
                {timecode(turn.created_at, openedAt)}
              </span>

              <div>
                <p
                  className="tabular mb-2 text-[0.6rem] uppercase tracking-[0.16em]"
                  style={{
                    color:
                      turn.speaker === "interviewer"
                        ? "var(--color-annotation)"
                        : "var(--color-mute)",
                  }}
                >
                  {turn.speaker === "interviewer" ? "Interviewer" : "You"}
                </p>
                <p
                  className={
                    turn.speaker === "interviewer"
                      ? "font-display text-[1.22rem] leading-[1.45] font-medium"
                      : "text-[0.95rem] leading-relaxed"
                  }
                  style={
                    turn.speaker === "candidate" ? { color: "var(--color-mute)" } : undefined
                  }
                >
                  {turn.content}
                </p>
              </div>
            </li>
          ))}

          {/* The live turn. Same shape as a settled one, plus the caret. */}
          {isStreaming && (
            <li className="grid grid-cols-[3.2rem_1fr] gap-4">
              <span
                className="tabular pt-1 text-[0.64rem]"
                style={{ color: "var(--color-graphite-line)" }}
              >
                --:--
              </span>
              <div>
                <p
                  className="tabular mb-2 text-[0.6rem] uppercase tracking-[0.16em]"
                  style={{ color: "var(--color-annotation)" }}
                >
                  Interviewer
                </p>
                <p className="font-display text-[1.22rem] leading-[1.45] font-medium">
                  <span className="caret">{streaming}</span>
                </p>
              </div>
            </li>
          )}
        </ol>

        {error && (
          <p
            role="alert"
            className="mt-8 border-l-2 py-1 pl-3 text-[0.875rem] leading-relaxed"
            style={{ borderColor: "var(--color-annotation)", color: "var(--color-annotation)" }}
          >
            {error}
          </p>
        )}

        {/* Scorecard ---------------------------------------------------- */}
        {scorecard && <ScorecardPanel card={scorecard} />}

        <div ref={bottom} />
      </main>

      {/* Composer ------------------------------------------------------- */}
      {!closed && (
        <div
          className="fixed inset-x-0 bottom-0 border-t backdrop-blur"
          style={{
            borderColor: "var(--color-graphite-line)",
            background: "color-mix(in srgb, var(--color-graphite) 92%, transparent)",
          }}
        >
          <div className="mx-auto max-w-3xl px-6 py-4">
            <textarea
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={3}
              disabled={isStreaming}
              placeholder={
                isStreaming ? "Wait for the question…" : "Answer as if you were speaking."
              }
              aria-label="Your answer"
              className="w-full resize-none border bg-transparent px-4 py-3 text-[0.95rem] leading-relaxed transition-colors disabled:opacity-40"
              style={{
                borderRadius: "var(--radius-card)",
                borderColor: "var(--color-graphite-line)",
                color: "var(--color-porcelain)",
              }}
            />

            <div className="mt-3 flex items-center justify-between gap-4">
              <span className="tabular text-[0.62rem]" style={{ color: "var(--color-mute)" }}>
                ⌘↵ to send · {answered} answered
              </span>

              <div className="flex items-center gap-3">
                {answered > 0 && (
                  <button
                    onClick={finish}
                    disabled={scoring || isStreaming}
                    className="tabular border px-4 py-2 text-[0.64rem] uppercase tracking-widest transition-colors disabled:opacity-40"
                    style={{
                      borderRadius: "var(--radius-card)",
                      borderColor: "var(--color-graphite-line)",
                      color: "var(--color-mute)",
                    }}
                  >
                    {scoring ? "Scoring…" : "End & score"}
                  </button>
                )}
                <button
                  onClick={send}
                  disabled={isStreaming || answer.trim().length === 0}
                  className="px-6 py-2.5 text-sm font-medium transition-opacity hover:opacity-90 disabled:opacity-35"
                  style={{
                    borderRadius: "var(--radius-card)",
                    background: "var(--color-porcelain)",
                    color: "var(--color-graphite)",
                  }}
                >
                  Send
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ScorecardPanel({ card }: { card: Scorecard }) {
  return (
    <section
      className="rise mt-16 border-t pt-10"
      style={{ borderColor: "var(--color-graphite-line)" }}
    >
      <div className="flex items-baseline justify-between gap-6">
        <p
          className="tabular text-[0.6rem] uppercase tracking-[0.16em]"
          style={{ color: "var(--color-mute)" }}
        >
          Scorecard
        </p>
        <p className="tabular text-[3.4rem] leading-none font-bold">{card.overall}</p>
      </div>

      <p className="font-display mt-6 text-[1.12rem] leading-relaxed">{card.summary}</p>

      <ul className="mt-10 space-y-4">
        {card.dimensions.map((d) => (
          <li key={d.name}>
            <div className="flex items-center justify-between gap-4">
              <span className="tabular text-[0.72rem]">{d.name}</span>
              <span className="inline-flex gap-[3px]" aria-label={`${d.score} out of 5`}>
                {[1, 2, 3, 4, 5].map((n) => (
                  <span
                    key={n}
                    className="h-3 w-[7px]"
                    style={{
                      background: n <= d.score ? "var(--color-porcelain)" : "transparent",
                      border: `1px solid ${
                        n <= d.score ? "var(--color-porcelain)" : "var(--color-graphite-line)"
                      }`,
                    }}
                  />
                ))}
              </span>
            </div>
            <p className="mt-1.5 text-[0.85rem]" style={{ color: "var(--color-mute)" }}>
              {d.note}
            </p>
          </li>
        ))}
      </ul>

      <div className="mt-12 grid gap-10 sm:grid-cols-2">
        <div>
          <p
            className="tabular mb-4 text-[0.6rem] uppercase tracking-[0.16em]"
            style={{ color: "var(--color-sage)" }}
          >
            What worked
          </p>
          <ul className="space-y-3">
            {card.strengths.map((s, i) => (
              <li key={i} className="text-[0.9rem] leading-relaxed">
                {s}
              </li>
            ))}
          </ul>
        </div>

        <div>
          <p
            className="tabular mb-4 text-[0.6rem] uppercase tracking-[0.16em]"
            style={{ color: "var(--color-annotation)" }}
          >
            Work on this
          </p>
          <ul className="space-y-3">
            {card.gaps.map((g, i) => (
              <li key={i} className="text-[0.9rem] leading-relaxed">
                {g}
              </li>
            ))}
          </ul>
        </div>
      </div>

      <Link
        href="/sessions"
        className="tabular mt-12 inline-block border px-5 py-2.5 text-[0.64rem] uppercase tracking-widest transition-colors"
        style={{
          borderRadius: "var(--radius-card)",
          borderColor: "var(--color-graphite-line)",
          color: "var(--color-mute)",
        }}
      >
        Run another →
      </Link>
    </section>
  );
}
