import Link from "next/link";
import { Typewriter } from "@/components/Typewriter";

const QUESTIONS = [
  "Walk me through a system you shipped that had real users on it. What was the hardest constraint?",
  "Where would that break first if traffic went up ten times overnight, and how would you know?",
  "Tell me about a bug that took you far too long to find. What did you change afterwards?",
];

const DIMENSIONS = [
  { name: "Technical depth", score: 4 },
  { name: "Structure", score: 4 },
  { name: "Specificity", score: 2 },
  { name: "Trade-off reasoning", score: 3 },
  { name: "Communication", score: 4 },
];

function Bar({ score }: { score: number }) {
  return (
    <span className="inline-flex gap-[3px]" aria-hidden>
      {[1, 2, 3, 4, 5].map((n) => (
        <span
          key={n}
          className="h-3 w-[7px]"
          style={{
            background: n <= score ? "var(--color-graphite)" : "transparent",
            border: `1px solid ${n <= score ? "var(--color-graphite)" : "var(--color-paper-edge)"}`,
          }}
        />
      ))}
    </span>
  );
}

export default function Home() {
  return (
    <main className="mx-auto max-w-5xl px-6 pb-24">
      {/* Masthead ------------------------------------------------------- */}
      <header className="flex items-center justify-between py-7">
        <span className="font-display text-[1.35rem] font-extrabold tracking-tight">
          Cadence
        </span>
        <Link
          href="/login"
          className="label transition-colors hover:text-[color:var(--color-ink)]"
        >
          Sign in →
        </Link>
      </header>

      <div className="rule" />

      {/* Hero: the product's core mechanic, running ---------------------- */}
      <section className="grid gap-10 pt-16 pb-20 md:grid-cols-[1.15fr_1fr] md:gap-14">
        <div>
          <p className="label mb-6">Practice interview · question 01</p>

          {/* The thesis: an interviewer typing a real question at you. */}
          <h1 className="font-display text-[1.9rem] leading-[1.25] font-medium tracking-[-0.015em] sm:text-[2.3rem]">
            <Typewriter lines={QUESTIONS} />
          </h1>

          <p className="mt-9 max-w-md text-[0.975rem] leading-relaxed text-[color:var(--color-mute)]">
            Cadence runs a technical interview against you and follows up on what
            you actually said — not a fixed list of questions. Name a technology
            and it will probe that technology. Make a claim without a number and
            it will ask for the number.
          </p>

          <div className="mt-9 flex flex-wrap items-center gap-4">
            <Link
              href="/login"
              className="bg-[color:var(--color-ink)] px-6 py-3 text-sm font-medium text-[color:var(--color-paper)] transition-opacity hover:opacity-90"
              style={{ borderRadius: "var(--radius-card)" }}
            >
              Start an interview
            </Link>
            <span className="label">No card. Runs offline.</span>
          </div>
        </div>

        {/* The artefact you leave with. Every claim about you is in mono. */}
        <aside className="paper-card rise self-start p-6">
          <div className="flex items-baseline justify-between">
            <p className="label">Scorecard</p>
            <p className="tabular text-[2.6rem] leading-none font-bold">68</p>
          </div>

          <div className="rule my-5" />

          <ul className="space-y-3">
            {DIMENSIONS.map((d) => (
              <li key={d.name} className="flex items-center justify-between gap-4">
                <span className="tabular text-[0.7rem] tracking-tight text-[color:var(--color-graphite)]">
                  {d.name}
                </span>
                <Bar score={d.score} />
              </li>
            ))}
          </ul>

          <div className="rule my-5" />

          <p className="text-[0.82rem] leading-relaxed text-[color:var(--color-mute)]">
            <span className="text-[color:var(--color-annotation)]">▸</span> Claims
            about scale mostly arrived without numbers attached. Attach a figure
            to every one — users, requests, latency, or money.
          </p>
        </aside>
      </section>

      <div className="rule" />

      {/* How it works. Numbered because it genuinely is a sequence. ------ */}
      <section className="grid gap-10 py-16 sm:grid-cols-3">
        {[
          {
            n: "01",
            h: "Name the role",
            p: "Job title, seniority, and the areas you want pushed on. The interviewer calibrates to it — a staff-level session asks about trade-offs and failure modes, not syntax.",
          },
          {
            n: "02",
            h: "Answer out loud",
            p: "Questions stream in a word at a time, so you feel the pace of a real conversation instead of reading a list. Each answer shapes the next question.",
          },
          {
            n: "03",
            h: "Read the verdict",
            p: "Five dimensions, each scored with the moment that earned it. Written to be useful rather than encouraging.",
          },
        ].map((s) => (
          <article key={s.n}>
            <p className="tabular mb-3 text-[0.7rem] text-[color:var(--color-annotation)]">
              {s.n}
            </p>
            <h2 className="font-display mb-2 text-[1.05rem] font-semibold">{s.h}</h2>
            <p className="text-[0.9rem] leading-relaxed text-[color:var(--color-mute)]">
              {s.p}
            </p>
          </article>
        ))}
      </section>

      <div className="rule" />

      <footer className="flex flex-wrap items-center justify-between gap-3 pt-8">
        <p className="label">Cadence — built with FastAPI, PostgreSQL and Next.js</p>
        <p className="label">Every session has a spend guardrail</p>
      </footer>
    </main>
  );
}
