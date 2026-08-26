# Security

## Reporting

Email **eimaieros@gmail.com**, or open a
[private advisory](https://github.com/eimaieros/cadence/security/advisories/new).
I will reply within a week. If you have not heard back in two, assume the mail
went astray and open a normal issue saying only that you are waiting.

Please do not open a public issue for anything that lets one user reach another
user's data.

## What this application handles

Unlike the two libraries alongside it, this one has a database, accounts and an
outbound API call, so the surface is real. What it stores:

- Email addresses and bcrypt password hashes.
- Interview transcripts — free text the user typed.
- Scorecards and per-session spend.

No payment data, no third-party identity providers, no analytics, no tracking.

## What has been deliberately built for

**Tenancy.** Every session route resolves through a single ownership
dependency. There is no handler that can forget, because forgetting would mean
not using the dependency at all.

**Enumeration.** Requesting another user's session returns **404, not 403**. A
403 confirms the resource exists and hands an attacker a working oracle for
mapping the id space. Four tests cover this — read, delete, answer, stream.

**Prompt injection.** A tool that takes free text from a user and feeds it to a
model that also decides how to score them is an injection target by
construction. Two layers, in `app/llm/prompts.py`: instructions live only in the
system prompt and candidate text only ever occupies a user turn, wrapped in a
delimiter and labelled as transcript; and the scorer must answer in a fixed JSON
schema validated afterwards with bounds enforced in code. Neither layer is
sufficient alone, and both are there.

**Cost.** The per-session ceiling is checked *before* each call, not after. An
autonomous process against a paid API does not fail loudly — it fails
expensively and quietly.

**Password hashing.** bcrypt directly rather than through passlib, which has had
no release since 2020 and raises against bcrypt ≥ 4.1. One fewer unmaintained
dependency in the authentication path.

## Known gaps, stated plainly

These are real and they are not fixed:

- **No refresh token rotation or revocation.** Short access TTLs are the
  mitigation. A stolen refresh token is valid until it expires; a server-side
  logout would need a token store.
- **Rate limiting bounds one instance.** `app/ratelimit.py` is an in-process
  sliding window. Behind two replicas a caller gets twice the budget, and a
  restart clears every window. The expensive path is separately bounded by the
  cost ceiling, which lives in the database.
- **No account lockout or CAPTCHA.** The rate limiter slows credential stuffing;
  it does not stop a patient attacker.
- **Secrets come from the environment.** `.env.example` ships defaults that are
  fine for local development and must not survive contact with a server.

## Supported versions

The `main` branch. There is no release train to backport to.
