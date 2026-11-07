"""Golden cases for job-description retrieval.

Each case is a job description and the questions an interviewer would search it
with, each paired with a phrase that only the right passage contains. The
descriptions are written for this file, for three made-up companies, so the
cases can be read, changed and shared freely.

Most queries reuse the ad's own words, because that is how the interviewer
searches: it has just read the role title and the focus areas. A few do not --
"relational database" against an ad that only says "PostgreSQL" -- and those are
marked ``hard``. They are kept on purpose. They show where the offline embedder
stops, which is the reason a learned embedding model is used when there is a key.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Query:
    text: str
    expected: str  # a phrase found only in the passage that should come back
    hard: bool = False


@dataclass(frozen=True)
class RetrievalCase:
    name: str
    description: str
    queries: list[Query] = field(default_factory=list)


BACKEND = RetrievalCase(
    name="backend-python",
    description="""\
About the team
We build the routing platform that plans 40,000 deliveries a day across the Netherlands and Belgium. Every route is priced, scheduled and re-planned when a truck runs late.

What you'll do
- Design and run Python services on FastAPI that price and schedule deliveries
- Own the PostgreSQL schema behind the planner, including migrations and query tuning
- Build event-driven integrations with carriers
- Keep the on-call rotation calm: alerts, runbooks and post-incident reviews

What we're looking for
- 3+ years building backend services in Python
- Strong SQL: you can read a query plan and explain why an index is not used
- Experience with message queues such as Kafka or RabbitMQ
- Comfortable with Docker and CI pipelines

Nice to have
- Experience with geospatial data and PostGIS
- Some Go

Our stack
- Python 3.12, FastAPI and SQLAlchemy
- PostgreSQL 16 for everything transactional
- Kafka between services, Kubernetes on AWS
- Grafana and Sentry for dashboards and errors

How we work
Small teams own a service end to end. We review every change, merge to main several times a day and deploy behind feature flags, so a bad release is switched off rather than rolled back.

Hiring process
- A 30-minute call with the hiring manager
- A take-home exercise of about three hours, paid
- A system design conversation with two engineers
- Meeting the team over lunch

What we offer
- Hybrid work from our Rotterdam office, two days a week
- A learning budget of 1,500 euros a year
- 26 holidays, a pension plan and a lease bike
""",
    queries=[
        Query("SQL skills and reading query plans", "read a query plan"),
        Query("geospatial PostGIS experience", "PostGIS"),
        Query("office attendance and hybrid work", "Rotterdam office"),
        Query("on-call rotation and incidents", "on-call rotation"),
        Query("learning budget for training", "learning budget"),
        Query("message queues Kafka RabbitMQ", "message queues"),
        Query("relational database design", "query tuning", hard=True),
    ],
)

FRONTEND = RetrievalCase(
    name="frontend-react",
    description="""\
Lumen is a patient portal used by 30 hospitals. Patients book appointments, read results and message their care team, often on old phones and slow connections.

Your work
- Build features in React and TypeScript across the patient and clinician apps
- Grow our design system: components documented and tested in Storybook
- Make every screen usable with a screen reader and a keyboard, to WCAG 2.1 AA
- Keep pages fast on low-end devices, measured with Core Web Vitals

You have
- 3 years of professional frontend development with React
- Experience testing user flows end to end with Playwright or Cypress
- Care for accessibility that goes beyond running a linter

About the role
You join a team of five engineers and a designer. You will pair with the designer on new flows and with the backend team on the APIs those flows need.

Our stack
- React 19 with TypeScript, built with Vite
- TanStack Query for server state
- A REST API in Kotlin, owned by the backend team
- Sentry and real-user monitoring in production

How we ship
We release twice a week. Every pull request gets a preview deployment, and a clinician from our advisory group tries the bigger changes before they go out.

Hiring process
- An introduction call
- A pairing session on a small feature in our codebase
- A conversation about a project you are proud of

Language
English is our working language. Dutch is a plus, not a requirement.

What we offer
- Work from home, with the whole team in our Utrecht office on Tuesdays
- 25 holidays and a yearly team trip
""",
    queries=[
        Query("accessibility screen reader keyboard WCAG", "WCAG 2.1 AA"),
        Query("end-to-end testing with Playwright", "Playwright"),
        Query("is Dutch required, what language", "working language"),
        Query("design system and Storybook components", "Storybook"),
        Query("page speed Core Web Vitals on slow devices", "Core Web Vitals"),
        Query("component library documentation", "Storybook", hard=True),
    ],
)

AI = RetrievalCase(
    name="ai-engineer",
    description="""\
Kestrel helps insurers read claims faster. Adjusters ask questions about a claim and get answers grounded in the documents attached to it.

Responsibilities
- Build retrieval-augmented generation pipelines over claim documents
- Design evaluation sets and track answer quality before every release
- Add guardrails against prompt injection hidden in uploaded documents
- Monitor token costs and latency per customer

Requirements
- Strong Python, including async code
- Experience with a vector database such as pgvector, Qdrant or Weaviate
- Experience deploying on Azure or AWS
- You can explain a design decision to an adjuster as well as to an engineer

Our stack
- Python with FastAPI, Celery for background jobs
- PostgreSQL with pgvector for documents and their embeddings
- Models from more than one provider, chosen per task
- OpenTelemetry traces from request to model call

How we work
Every change to a prompt or a pipeline runs against the evaluation sets in CI, and a drop in answer quality blocks the merge the same way a failing test does.

About the team
Eight engineers, two of them focused on applied research. We publish what we learn on our blog.

Hiring process
- A call with the head of engineering
- A paid take-home: improve a small retrieval pipeline and explain what you measured
- A final conversation with the founders

What we offer
- Remote within Europe, with a week together every quarter
- A home-office budget and a yearly conference
""",
    queries=[
        Query("retrieval-augmented generation pipelines", "retrieval-augmented generation"),
        Query("evaluation sets and answer quality", "Design evaluation sets"),
        Query("prompt injection guardrails", "prompt injection"),
        Query("vector database pgvector Qdrant", "vector database"),
        Query("monitoring token costs", "token costs"),
        Query("which cloud, Azure or AWS", "Azure or AWS"),
        Query("security of model inputs", "prompt injection", hard=True),
    ],
)

CASES: list[RetrievalCase] = [BACKEND, FRONTEND, AI]
