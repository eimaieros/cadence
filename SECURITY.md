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
construction. Two layers, in `app/llm/prompts.py`: the live interviewer's system
prompt is fixed while role context and candidate answers stay in user-role
messages; and the scorer receives a delimited transcript and must answer in a
fixed JSON schema validated afterwards with bounds enforced in code. Neither
layer is sufficient alone, and both are there.

**Cost.** Recorded spend is checked before each new interview call. This is a
guardrail, not a hard reservation: an in-flight call can cross the threshold,
and the final scoring call remains available so a user is not locked out of the
result they already paid to generate.

**Password hashing.** bcrypt directly rather than through passlib, which has had
no release since 2020 and raises against bcrypt ≥ 4.1. One fewer unmaintained
dependency in the authentication path.

**Refresh replay.** Refresh credentials rotate through server-side families.
Each token is single-use; presenting a consumed token revokes its descendants,
and logout revokes the family under the same database row lock. The database
stores the random token identifier and lifecycle metadata, never the bearer
token itself.

## Known gaps, stated plainly

These are real and they are not fixed:

- **Access tokens are not individually revoked.** Logout stops future refreshes
  immediately, but an access token already issued remains valid until its
  configured short TTL expires. Browser tokens live in session storage and are
  therefore still inside the origin's XSS threat model.
- **Rate limiting bounds one instance.** `app/ratelimit.py` is an in-process
  sliding window. Behind two replicas a caller gets twice the budget, and a
  restart clears every window. The expensive path is separately bounded by the
  recorded-spend guardrail, which lives in the database.
- **No account lockout or CAPTCHA.** The rate limiter slows credential stuffing;
  it does not stop a patient attacker.
- **Secrets come from the environment.** `.env.example` ships defaults that are
  fine for local development and must not survive contact with a server.

## Supported versions

The `main` branch. There is no release train to backport to.
