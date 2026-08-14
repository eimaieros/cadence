"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { z } from "zod";
import { ApiError, api, auth } from "@/lib/api";

/*
  The same constraints the API enforces, mirrored here so the user gets an
  answer without a round trip. The server still validates — client validation
  is a courtesy, never a control.
*/
const Credentials = z.object({
  email: z.email("That doesn't look like an email address."),
  password: z
    .string()
    .min(10, "Passwords need at least 10 characters.")
    .max(72, "Passwords can be at most 72 characters."),
  display_name: z.string().min(1, "Tell us what to call you.").max(120).optional(),
});

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"signin" | "register">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const registering = mode === "register";

  async function submit() {
    setError(null);

    const parsed = Credentials.safeParse({
      email,
      password,
      ...(registering ? { display_name: displayName } : {}),
    });
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? "Check those details.");
      return;
    }

    setBusy(true);
    try {
      const tokens = registering
        ? await api.register(email, password, displayName)
        : await api.login(email, password);
      auth.set(tokens.access_token);
      router.push("/sessions");
    } catch (err) {
      // Errors explain what happened and what to do. They never apologise and
      // they are never vague.
      if (err instanceof ApiError) {
        setError(
          err.status === 401
            ? "That email and password don't match an account."
            : err.status === 409
              ? "An account with those details already exists. Sign in instead."
              : err.message,
        );
      } else {
        setError("Couldn't reach the server. Is the API running on port 8000?");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-16">
      <Link href="/" className="font-display mb-10 text-[1.35rem] font-extrabold tracking-tight">
        Cadence
      </Link>

      <div className="paper-card p-7">
        <p className="label mb-1">{registering ? "New account" : "Welcome back"}</p>
        <h1 className="font-display mb-7 text-[1.5rem] font-semibold tracking-tight">
          {registering ? "Set up your account" : "Sign in to continue"}
        </h1>

        <div className="space-y-4">
          {registering && (
            <Field
              label="Name"
              value={displayName}
              onChange={setDisplayName}
              placeholder="Rodrigo"
              autoComplete="name"
            />
          )}
          <Field
            label="Email"
            type="email"
            value={email}
            onChange={setEmail}
            placeholder="you@example.com"
            autoComplete="email"
          />
          <Field
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            placeholder="At least 10 characters"
            autoComplete={registering ? "new-password" : "current-password"}
            onEnter={submit}
          />
        </div>

        {error && (
          <p
            role="alert"
            className="mt-5 border-l-2 py-1 pl-3 text-[0.85rem] leading-relaxed"
            style={{
              borderColor: "var(--color-annotation)",
              color: "var(--color-annotation)",
            }}
          >
            {error}
          </p>
        )}

        <button
          onClick={submit}
          disabled={busy}
          className="mt-7 w-full bg-[color:var(--color-ink)] py-3 text-sm font-medium text-[color:var(--color-paper)] transition-opacity hover:opacity-90 disabled:opacity-45"
          style={{ borderRadius: "var(--radius-card)" }}
        >
          {busy ? "Working…" : registering ? "Create account" : "Sign in"}
        </button>
      </div>

      <button
        onClick={() => {
          setMode(registering ? "signin" : "register");
          setError(null);
        }}
        className="label mt-6 self-center transition-colors hover:text-[color:var(--color-ink)]"
      >
        {registering ? "Already have an account? Sign in" : "Need an account? Create one"}
      </button>
    </main>
  );
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
  autoComplete,
  onEnter,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
  autoComplete?: string;
  onEnter?: () => void;
}) {
  const id = `field-${label.toLowerCase()}`;
  return (
    <div>
      <label htmlFor={id} className="label mb-2 block">
        {label}
      </label>
      <input
        id={id}
        type={type}
        value={value}
        autoComplete={autoComplete}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && onEnter) onEnter();
        }}
        className="w-full border bg-transparent px-3 py-2.5 text-[0.925rem] transition-colors placeholder:text-[color:var(--color-mute)]/60 focus:border-[color:var(--color-ink)]"
        style={{ borderRadius: "var(--radius-card)" }}
      />
    </div>
  );
}
