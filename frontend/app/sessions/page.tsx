"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, auth } from "@/lib/api";
import type { SessionSummary, Seniority } from "@/lib/schemas";

const SENIORITIES: Seniority[] = ["junior", "mid", "senior", "staff"];

const SUGGESTED = [
  "Fullstack Developer (Python/React)",
  "Backend Engineer",
  "Frontend Engineer",
  "Platform Engineer",
];

export default function SessionsPage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [name, setName] = useState<string>("");
  const [role, setRole] = useState("");
  const [focus, setFocus] = useState("");
  const [seniority, setSeniority] = useState<Seniority>("mid");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [me, list] = await Promise.all([api.me(), api.listSessions()]);
      setName(me.display_name);
      setSessions(list);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        auth.clear();
        router.push("/login");
        return;
      }
      setError("Couldn't load your sessions. Check the API is running.");
      setSessions([]);
    }
  }, [router]);

  useEffect(() => {
    if (!auth.get()) {
      router.push("/login");
      return;
    }
    void load();
  }, [load, router]);

  async function start() {
    if (role.trim().length < 2) {
      setError("Give the role a name first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await api.createSession(
        role.trim(),
        focus
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
          .slice(0, 6),
        seniority,
      );
      router.push(`/sessions/${created.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't start that session.");
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-4xl px-6 pb-24">
      <header className="flex items-center justify-between py-7">
        <Link href="/" className="font-display text-[1.35rem] font-extrabold tracking-tight">
          Cadence
        </Link>
        <div className="flex items-center gap-5">
          {name && <span className="label">{name}</span>}
          <button
            onClick={async () => {
              try {
                await api.logout();
              } finally {
                auth.clear();
                router.push("/");
              }
            }}
            className="label transition-colors hover:text-[color:var(--color-ink)]"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="rule" />

      {/* Composer ------------------------------------------------------- */}
      <section className="py-10">
        <h1 className="font-display mb-6 text-[1.6rem] font-semibold tracking-tight">
          Start an interview
        </h1>

        <div className="paper-card p-6">
          <label htmlFor="role" className="label mb-2 block">
            Role
          </label>
          <input
            id="role"
            value={role}
            onChange={(e) => setRole(e.target.value)}
            placeholder="Fullstack Developer (Python/React)"
            className="w-full border bg-transparent px-3 py-2.5 text-[0.95rem] focus:border-[color:var(--color-ink)]"
            style={{ borderRadius: "var(--radius-card)" }}
          />

          <div className="mt-3 flex flex-wrap gap-2">
            {SUGGESTED.map((s) => (
              <button
                key={s}
                onClick={() => setRole(s)}
                className="tabular border px-2.5 py-1 text-[0.68rem] transition-colors hover:border-[color:var(--color-ink)] hover:text-[color:var(--color-ink)]"
                style={{ borderRadius: "var(--radius-card)" }}
              >
                {s}
              </button>
            ))}
          </div>

          <div className="mt-6 grid gap-5 sm:grid-cols-2">
            <div>
              <label htmlFor="focus" className="label mb-2 block">
                Focus areas
              </label>
              <input
                id="focus"
                value={focus}
                onChange={(e) => setFocus(e.target.value)}
                placeholder="FastAPI, PostgreSQL, streaming"
                className="w-full border bg-transparent px-3 py-2.5 text-[0.95rem] focus:border-[color:var(--color-ink)]"
                style={{ borderRadius: "var(--radius-card)" }}
              />
              <p className="label mt-2 normal-case tracking-normal">
                Comma separated. Up to six.
              </p>
            </div>

            <div>
              <span className="label mb-2 block">Seniority</span>
              <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Seniority">
                {SENIORITIES.map((s) => {
                  const on = s === seniority;
                  return (
                    <button
                      key={s}
                      role="radio"
                      aria-checked={on}
                      onClick={() => setSeniority(s)}
                      className="tabular border px-3 py-2 text-[0.7rem] uppercase transition-colors"
                      style={{
                        borderRadius: "var(--radius-card)",
                        background: on ? "var(--color-graphite)" : "transparent",
                        color: on ? "var(--color-paper)" : "var(--color-mute)",
                        borderColor: on ? "var(--color-graphite)" : "var(--color-paper-edge)",
                      }}
                    >
                      {s}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          {error && (
            <p
              role="alert"
              className="mt-5 border-l-2 py-1 pl-3 text-[0.85rem]"
              style={{ borderColor: "var(--color-annotation)", color: "var(--color-annotation)" }}
            >
              {error}
            </p>
          )}

          <button
            onClick={start}
            disabled={busy}
            className="mt-6 bg-[color:var(--color-ink)] px-6 py-3 text-sm font-medium text-[color:var(--color-paper)] transition-opacity hover:opacity-90 disabled:opacity-45"
            style={{ borderRadius: "var(--radius-card)" }}
          >
            {busy ? "Starting…" : "Begin"}
          </button>
        </div>
      </section>

      <div className="rule" />

      {/* History -------------------------------------------------------- */}
      <section className="pt-10">
        <h2 className="label mb-5">Past sessions</h2>

        {sessions === null && <p className="label">Loading…</p>}

        {sessions?.length === 0 && (
          /* An empty screen is an invitation to act, not an apology. */
          <p className="max-w-md text-[0.925rem] leading-relaxed text-[color:var(--color-mute)]">
            Nothing here yet. Your first session takes about ten minutes, and the
            scorecard is more useful than it is kind.
          </p>
        )}

        <ul className="space-y-px">
          {sessions?.map((s) => (
            <li key={s.id}>
              <Link
                href={`/sessions/${s.id}`}
                className="paper-card flex items-center justify-between gap-4 p-4 transition-colors hover:border-[color:var(--color-ink)]"
              >
                <div className="min-w-0">
                  <p className="font-display truncate text-[1rem] font-medium">{s.role_title}</p>
                  <p className="label mt-1 normal-case tracking-normal">
                    {s.seniority} ·{" "}
                    {new Date(s.created_at).toLocaleDateString(undefined, {
                      day: "numeric",
                      month: "short",
                      year: "numeric",
                    })}
                    {s.focus_areas.length > 0 && ` · ${s.focus_areas.join(", ")}`}
                  </p>
                </div>
                <span
                  className="tabular shrink-0 border px-2 py-1 text-[0.62rem] uppercase"
                  style={{
                    borderRadius: "var(--radius-card)",
                    color:
                      s.status === "completed"
                        ? "var(--color-sage)"
                        : "var(--color-annotation)",
                    borderColor:
                      s.status === "completed"
                        ? "var(--color-sage)"
                        : "var(--color-annotation)",
                  }}
                >
                  {s.status === "completed" ? "scored" : "open"}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
