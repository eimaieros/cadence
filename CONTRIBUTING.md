# Contributing

Issues and pull requests are welcome. This is a small project maintained by one
person, so a short issue describing what you saw beats a long one speculating
about why.

## Running it

```bash
docker compose up
# → http://localhost:3000
```

That is the whole setup. Without `ANTHROPIC_API_KEY` the backend runs a scripted
interviewer instead of calling a model — every code path is identical, including
the streaming, so you can work on the whole app offline and never spend a cent.

```bash
cd backend && pytest -q          # 70 tests, needs a real PostgreSQL
python -m evals.run              # scorer evals, structural tier, no key needed
cd frontend && npx tsc --noEmit  # types
```

## Four traps this codebase already fell into

Worth reading before you change anything in these areas, because each one works
in development and fails under load.

**`def` versus `async def` in FastAPI.** A `def` endpoint runs in a threadpool;
an `async def` runs on the event loop. One blocking call inside an `async def` —
a sync driver, `requests`, `time.sleep` — stalls the entire loop, and the app
looks perfect locally and falls over with two users. There is no blocking I/O in
any `async def` here. Keep it that way.

**The streaming endpoint opens its own database session.** The request-scoped
session from `Depends(get_db)` is committed and closed when the dependency's
scope exits, which happens *before* the streaming generator finishes producing
output. `test_streamed_question_is_persisted` exists specifically to prove the
write lands, because this is the kind of thing that passes a manual test and
deadlocks under concurrency.

**SSE events do not respect TCP boundaries.** Events are split on a blank line,
and whatever follows the last separator is a *partial* event that must stay in
the buffer. Assuming one read equals one message is the most common SSE bug
there is, and `lib/api.ts` is written the way it is because of it.

**Retries only before the first byte.** The provider backs off with jitter —
without jitter every client that failed together retries together and knocks the
recovering upstream over again. But it only retries if nothing has been emitted.
Once tokens have reached the client, restarting duplicates visible output, and a
partial answer beats a doubled one.

## Tests run against real PostgreSQL, and that is not negotiable

The schema uses JSONB and native enums. A SQLite suite would be faster and would
pass while production broke. `createdb cadence_test` first; the schema is
dropped and recreated per test, so never point `DATABASE_URL` at anything you
care about.

## Changing a prompt

Prompts are code with none of the safety of code — no type checker, no compiler,
and three words can move every output. Run `python -m evals.run` after any edit
to `app/llm/prompts.py`.

Assert on **relationships, not absolute numbers**. `overall == 72` is not a
test, it is a hostage: a prompt improvement that lifts every score by four
points would fail it while being strictly better. "The answer with figures must
out-score the vague one on Specificity" survives a model upgrade.

## Security-sensitive areas

Two places where a well-meaning change can quietly open a hole:

- **Ownership.** Every session route resolves through one dependency. If you
  add a handler, route it through that dependency rather than checking inline —
  the point is that there is no handler that *can* forget.
- **Prompt injection.** Candidate text is data. It goes in a user turn, wrapped
  in a delimiter, never in the system prompt, and speaker labels come from an
  enum so nobody can forge an `Interviewer:` line inside their own answer. If
  you find yourself concatenating user text into an instruction, stop.

## Conduct

Be decent. Assume the other person is doing their best with what they know.
Anything that would be unwelcome in a shared office is unwelcome here — mail
eimaieros@gmail.com if something needs handling privately.
