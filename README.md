# DecisionLens AI

**A full-stack decision-support application with an LLM copilot and an idempotent chat
pipeline.**

DecisionLens AI is a full-stack SaaS prototype — React 19 SPA, FastAPI, MongoDB
— built around two LLM features: a structured analysis of a recorded business decision,
and a multi-turn chat copilot. Everything around those two features is ordinary product
engineering: JWT cookie auth, tiered quotas, file attachments, a dashboard, and Stripe
Checkout.

The parts that are actually hard are not the prompts. They are the failure paths: what
happens when the model is down, when a client retries a request whose response was lost,
and when a worker dies between writing a reply and acknowledging it. That is the
`request_id` and message-status machinery documented in
[Chat reliability](#chat-reliability), and it is the part of this project I would point
at first.

> **Scope note.** This README distinguishes what is implemented, what is partial, and
> what is simulated. It does not claim the application is production-ready: deployment
> has not been verified, the Stripe webhook has not been exercised against a public HTTPS
> endpoint, and there is no automated frontend test suite. See
> [Status](#status-what-is-actually-implemented) and [Limitations](#limitations).

---

## Status: what is actually implemented

| Area | Status | Notes |
| --- | --- | --- |
| Decision CRUD, search, comments, activity log | Implemented | Per-user scoping on every route |
| Dashboard and analytics aggregates | Implemented | Real Mongo aggregations; KPI *deltas* are placeholders, see below |
| Gemini chat copilot | Implemented | With the reliability contract below |
| Gemini decision analysis | Implemented | **Decision metadata only** — no documents are sent |
| File attachments + text extraction | Partial | Validated and stored; extracted text is never used as model context |
| Email/password auth, JWT cookies, quotas | Implemented | |
| OAuth (Salesforce, HubSpot) | Partial | Real authorize/state/code-exchange code; mock-connects without credentials; token exchange needs `httpx`, which is not a declared dependency |
| Stripe billing | Implemented (sandbox) | One-time Checkout; webhook is signature-verified and refuses to run unconfigured |
| Sources catalogue, KPI deltas, recommendations | Simulated / heuristic | Not LLM output, not live integration data |
| Outbound integrations (Slack, Notion, Drive) | Not implemented | Listed in [Roadmap](#roadmap) |
| Deployment | Not verified | No Dockerfile, no CI, no hosted environment |

---

## The problem

Organisations accumulate decision context across meetings, reviews, stakeholder threads
and project retrospectives. That context is unstructured, spread out, and rarely
consulted at the point a decision is actually made — so the same question gets relitigated,
risks are discovered late, and the reasoning behind a choice is lost.

DecisionLens AI addresses a narrower, testable slice of that: a decision is captured as a
structured record (owner, category, risk, confidence, stakeholders, deadline, context),
and the model is asked to produce a structured critique of *that record*. The product
value is in making the critique repeatable, attributable to a stored decision, and
available on demand.

---

## What the AI actually receives

This section exists because "LLM-powered decision intelligence" is easy to overclaim. The
following is what the code does.

### Decision analysis — `POST /api/decisions/{id}/analyse`

The prompt is built from the stored decision record and nothing else. The exact
interpolated fields are:

| Field | Source |
| --- | --- |
| `title` | `decisions.title` |
| `category` | `decisions.category` |
| `risk` | `decisions.risk` |
| `status` | `decisions.status` |
| `confidence` | `decisions.confidence` (percent) |
| `stakeholders` | `decisions.stakeholders` |
| `deadline` | `decisions.deadline` |
| `description` | `decisions.description` |

The system instruction constrains output to a JSON object with exactly six keys:
`executive_summary`, `recommendation`, `pros`, `cons`, `risks`, `next_steps`. The result is
parsed, coerced into that shape, and stored on the decision as `ai_analysis` with an
`ai_analysis_at` timestamp. Malformed model output is rejected (`502`) rather than
persisted as prose.

**Attachments are not part of the analysis.** Uploaded files are stored and best-effort
text-extracted (PDF first 20 pages, capped at 4000 characters), but that `extracted_text`
is written to the attachments collection, stripped from every API response, and never read
into any prompt. So there is **no document-grounded evidence** in analysis output. The
model reasons over the metadata a human typed in, not over the documents attached to the
decision. Treating an analysis as a summary of attached material would be wrong.

### Chat — `POST /api/chat`

The chat request sends a fixed system prompt, up to `CHAT_HISTORY_TURNS` (default 20)
previously stored messages, and the new user message. History is replayed explicitly from
MongoDB on every turn — there is no provider-side session state, so any request can be
reconstructed from stored rows alone. Only completed turns are replayed: a message must be
`answered` **and** linked to an assistant row with non-empty content, so a failed,
in-flight, or legacy turn never reaches the model as if it were a real exchange.

### What is *not* AI

- `GET /api/recommendations` is a set of fixed rules over the user's decisions (low
  confidence, high-risk count, stalled reviews). No model call.
- The dashboard "Decision Health Score" is an arithmetic heuristic over four derived
  factors (data volume, a stakeholder constant, risk penalty, average confidence).
- The KPI delta percentages (`+12%`, `+8%`, …) are literal constants in the backend, not
  period-over-period comparisons.
- `GET /api/sources` returns a hardcoded catalogue describing connected SaaS tools. It
  does not query Salesforce, HubSpot, Snowflake or Slack.

---

## Features

### Decisions

- Create, list, filter, search, update and delete decisions, scoped to the owning user.
- Structured fields: category, risk level, status, confidence, stakeholders, deadline,
  free-text context.
- Comments and an activity log per decision.
- Attachments with extension allow-listing (`.pdf`, `.docx`, `.txt`, `.md`),
  magic-byte content validation, and a 10 MB streamed cap that rejects oversized uploads
  without buffering them fully into memory.

### AI

- Multi-turn chat copilot with replayed history.
- On-demand structured analysis stored on the decision record.
- Explicit `503` failure contracts instead of fabricated model output (see
  [Chat reliability](#chat-reliability)).

### Product surface

- Executive dashboard: totals, average confidence, trend series, category and risk
  distributions, recent decisions.
- Analytics view over the same aggregate endpoint.
- Pricing tiers enforced server-side; quotas counted per calendar month from the
  underlying collections.
- Responsive Tailwind UI built on vendored shadcn/ui + Radix primitives.

### Accounts

- Email/password registration and login, bcrypt hashing, JWT in `HttpOnly` cookies.
- Self-service profile update and password change (`PATCH /api/auth/me`,
  `POST /api/auth/change-password`).

---

## Tech stack

### Frontend

- React 19, plain JavaScript/JSX — there are zero `.ts`/`.tsx` files and `typescript` is
  not a dependency
- Tailwind CSS 3.4, shadcn/ui + Radix UI primitives
- React Router 7, Recharts, Lucide icons, axios
- CRACO over Create React App (`react-scripts` 5)
- **No** TanStack Query and **no** Framer Motion — neither is a dependency

### Backend

- FastAPI + Uvicorn, Python 3.10+
- Single module: `backend/server.py`, entrypoint `server:app`
- Pydantic v2 models, JWT (HS256) via `pyjwt`, bcrypt via `passlib`

### Data

- MongoDB via `motor` / `pymongo`
- Local filesystem for attachment bytes

### External services

- **Gemini 2.5 Flash**, called directly over Google's REST API with `requests` — no
  provider SDK
- **Stripe**, via the official `stripe` SDK

> There is no Dockerfile and no compose file in this repository. See
> [Deployment](#deployment).

---

## Architecture

```text
React 19 SPA (CRA / CRACO dev server)
        |  JSON over /api/*, credentials: "include"
        v
FastAPI (backend/server.py)
        |
        +--> get_current_user: JWT from the access_token cookie, bearer header fallback
        |
        +--> MongoDB: users, decisions, chat_messages, attachments,
        |             activity, subscriptions, payment_transactions, oauth_states
        |
        +--> Gemini 2.5 Flash (REST, run_in_threadpool — never on the event loop)
        |
        +--> Stripe (one-time Checkout + signature-verified webhook)
```

---

## Project structure

```text
.
├── backend
│   ├── server.py                  # the entire FastAPI application
│   ├── requirements.txt
│   ├── .env.example
│   ├── verify_fixes.py            # standalone no-server contract checks
│   └── tests                      # pytest suite (see Testing)
├── frontend
│   ├── src
│   │   ├── pages                  # route-level screens
│   │   ├── components             # Layout, ProtectedRoute, ui/ primitives
│   │   ├── contexts               # AuthContext
│   │   └── lib                    # axios client, chatRecovery helpers
│   ├── package.json
│   ├── craco.config.js
│   └── .env.example
├── uploads                        # attachment storage (12 committed samples)
├── memory                         # PRD and OAuth setup notes
├── test_reports                   # historical run artefacts (stale, see below)
└── README.md
```

`memory/` and `test_reports/` are authoring artefacts, not application code. The JSON
files in `test_reports/` quote default passwords from an earlier iteration; those accounts
no longer exist and those credentials are stale.

---

## Requirements

- Python 3.10+
- Node.js 18+ and npm
- A running MongoDB instance (local or Atlas)

---

## Installation

### 1. Clone

```bash
git clone https://github.com/Ajanikehinde/DecisionLens_AI.git
cd DecisionLens_AI
```

### 2. Backend

```bash
cd backend

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt

copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
```

Start it from the `backend` directory:

```bash
uvicorn server:app --reload --port 8000
```

Interactive docs: <http://localhost:8000/docs>.

A virtual environment is optional — `requirements.txt` installs cleanly into a plain
interpreter — but whichever interpreter launches `uvicorn` is the one that must have the
dependencies.

**Environment variables.** `backend/.env` is loaded at import time. `MONGO_URL`,
`DB_NAME` and `JWT_SECRET` are read with `os.environ[...]`, so a missing value is a hard
crash at import, not a soft degradation.

| Variable | Required | Notes |
| --- | --- | --- |
| `MONGO_URL` | yes | `mongodb://localhost:27017` locally, or an Atlas URI |
| `DB_NAME` | yes | `decisionlens` |
| `JWT_SECRET` | yes | long random string; `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `GEMINI_API_KEY` | for AI | empty → both AI endpoints return `503 llm_not_configured` |
| `GEMINI_MODEL` | no | default `gemini-2.5-flash` |
| `CHAT_HISTORY_TURNS` | no | default `20`; `0` sends only the latest message |
| `GEMINI_TIMEOUT_S` | no | default `60` |
| `MAX_CHAT_ATTEMPTS` | no | default `3` |
| `CHAT_ATTEMPT_LEASE_SECONDS` | no | default `180`; must exceed `GEMINI_TIMEOUT_S` or the app refuses to start |
| `ENVIRONMENT` | no | `development` / `local` → non-`Secure`, `SameSite=Lax` cookies |
| `COOKIE_SECURE`, `COOKIE_SAMESITE` | no | explicit overrides |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | no | seed account; skipped unless password is 12+ chars |
| `DEMO_EMAIL` / `DEMO_PASSWORD` | no | same rule |
| `UPLOAD_DIR` | no | defaults to `<repo>/uploads` |
| `FRONTEND_URL` | for OAuth/CORS | default `http://localhost:3000` |
| `SALESFORCE_*`, `HUBSPOT_*` | no | see [Integrations](#integrations-and-billing) |
| `STRIPE_API_KEY` | for billing | without it checkout reports "not configured" |
| `STRIPE_WEBHOOK_SECRET` | for billing | without it the webhook returns `503` |
| `STRIPE_SUCCESS_URL` | no | pins the redirect origin; recommended in production |

### 3. Frontend

The backend instructions above leave you inside `backend/`, so step back out first:

```bash
cd ../frontend

npm install

copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
```

Then set the backend origin explicitly:

```dotenv
REACT_APP_BACKEND_URL=http://localhost:8000
```

```bash
npm start
```

The app opens at <http://localhost:3000>.

> `REACT_APP_BACKEND_URL` has **no fallback** in `src/lib/api.js` — the base URL is
> `` `${process.env.REACT_APP_BACKEND_URL}/api` `` verbatim. If the variable is missing,
> every request silently goes to `undefined/api` on the dev server origin rather than
> failing loudly. `frontend/.env.example` describes an "empty means same origin"
> behaviour that the client does not implement; set the variable.

`npm test` exits with code 1 and reports `No tests found`. That is expected: there are no
frontend test files. See [Testing](#testing).

### 4. Sign in

Register an account from the sign-up screen, or use a seeded account if you set its
password in `backend/.env`. Seeded accounts are only created when their password is
present; there are no built-in default credentials. The sign-in form starts empty and no
credential is shipped in the JS bundle.

---

## Authentication and cookies

Sessions use two `HttpOnly` cookies, `access_token` and `refresh_token`. Their attributes
follow the deployment environment so login works over both HTTP and HTTPS:

| `ENVIRONMENT` | `Secure` | `SameSite` |
| --- | --- | --- |
| `development` / `dev` / `local` | not set | `lax` |
| anything else (e.g. `production`) | set | `none` |

Overridable explicitly with `COOKIE_SECURE` and `COOKIE_SAMESITE`. Logout clears the
cookies with the same attributes they were issued with, which is what makes the deletion
take effect.

The access token's lifetime is 24 hours and it is the only cookie `get_current_user`
reads (a `Authorization: Bearer` header is accepted as a fallback for non-browser clients).
The refresh token is issued with a 7-day lifetime but **is never exchanged** — there is no
`/auth/refresh` route — so it has no effect on session length today.

`SameSite=None` requires `Secure`, which requires HTTPS. Serving the frontend and backend
from different sites in production therefore also requires HTTPS on both.

---

## Chat reliability

This is the most carefully engineered part of the backend, and the part with the least
obvious behaviour.

### Request IDs and idempotency

`ChatIn.request_id` is an `Optional[UUID]`. A client may attach one to a send and reuse it
verbatim when retrying. A client that omits it gets the original behaviour exactly.

The authority is the database, not the application: a **partial** unique index
`uniq_chat_request_per_user` on `(user_id, request_id)` prevents two rows for the same
retry. It is partial on `request_id: {$type: "string"}` so that pre-idempotency rows
(which have no such field) neither collide with each other nor block the index build. The
index is created and then *verified* — uniqueness, keys and partiality — at startup,
outside the seeding `try/except`; any failure raises and the process refuses to serve chat
without the constraint.

The read that precedes insertion is only a fast path. Two concurrent submissions of the
same id are still resolved by the index, so a race cannot produce a second user message.

### Message states

Every user chat message carries a `status`, written as a compare-and-set:

| State | Meaning |
| --- | --- |
| `pending` | row inserted, dispatch not yet started |
| `processing` | the provider call is in flight |
| `answered` | a genuine reply is persisted and linked via `reply_message_id` |
| `failed` | no assistant row was written; a fixed `error_code` is recorded |

Transitions are `pending → processing → answered | failed`, plus one recovery edge,
`failed → processing`, reachable only through the retry path. `answered` is absolutely
terminal. Every transition is a compare-and-set whose filter pins the id, the expected
current status and the attempt counter, and a `matched_count != 1` raises — never a read
followed by a write. That is what stops two workers from overwriting each other.

`answered` is written only *after* the reply is inserted. The reply is never inferred from
adjacency: it is joined by `reply_message_id`. A legacy row with no `status` is reported
as `unknown` at the read boundary and is never replayed, because an older code path could
return fabricated text as a `200` — so an old assistant row is not evidence of a real
exchange.

### Duplicate detection

A repeat submission is resolved **from the stored row**, before the key check and before
the quota check:

- `answered` → the stored reply is returned literally as `{session_id, reply}`. No model
  call, no quota charge. An already-paid-for answer therefore still replays during a
  provider outage and at the quota cap.
- `pending` / `processing` inside the lease window → `409 request_in_flight`, with
  `retry_after_ms` and a `lookup` path.
- `failed` → retried in place while attempts remain; otherwise `409 retry_exhausted`.
- Same id with a different message, or a different *requested* session →
  `409 idempotency_conflict`.

Because a repeat never inserts a second user message, and usage is derived by counting
`role: "user"` rows, a retry cannot consume quota by construction.

### Retries and the attempt budget

`MAX_CHAT_ATTEMPTS` (default 3) bounds how many times one stored message may be dispatched.
The counter is part of every retry-sensitive compare-and-set, so a worker resuming after
its lease expired cannot overwrite a newer attempt's outcome with stale text. Rows predating
the counter are treated as attempt 1 without a backfill.

### The 180-second lease

`CHAT_ATTEMPT_LEASE_SECONDS` (default 180) is the age at which a `processing` row may be
reclaimed through `failed → processing`. It is a **recovery heuristic, never proof**: a
paused-but-alive worker is indistinguishable from a dead one.

The import-time guard requires the lease to exceed `GEMINI_TIMEOUT_S` (60). That rules out
the common case — a single provider call that times out at its configured limit cannot, by
itself, outlive the lease — but it is **not** an absolute guarantee. The lease measures
wall-clock time since the row entered `processing`, whereas `GEMINI_TIMEOUT_S` bounds only
the HTTP request. Time spent queued for a thread-pool slot, behind a stalled event loop, in
a slow DNS or TLS handshake, or in an OS scheduling delay sits outside that timeout. Under
those conditions a worker that is still alive can be judged dead, and its attempt
re-dispatched, at the cost of one duplicate upstream computation. The attempt budget bounds
that duplication; the unique index and attempt-aware compare-and-sets ensure it never
becomes a second message, a second reply link, or a second charge.

### Session recovery

`requested_session_id` is stored separately from `session_id`. It records what the client
actually sent, so an interrupted *first* request — client sent `null`, server generated an
id — can be retried with `null` without being mistaken for a session conflict. Without
this, a dropped response on the very first message would strand the client permanently.

`GET /api/chat/requests/{request_id}` is the recovery endpoint. It is scoped by user (so
another account's id is a `404`, not a `403`) and returns `status`, `error_code`,
`attempt`, `max_attempts`, `can_retry`, `session_id`, and the reply only when answered.
This is how a client recovers a server-generated session id after a lost response, and how
it polls a `request_in_flight`.

On the client, `AIChat.jsx` generates the request id before sending, writes the pending
entry to `sessionStorage` (keyed by user id, via `src/lib/chatRecovery.js`) *before* the
POST, polls on `409 request_in_flight`, and reconciles unsettled entries on mount.
Recovery entries are cleared on logout and pruned when a different account signs in on the
same tab. The browser never decides idempotency itself — it resubmits with the same id and
lets the server decide whether to resume or reject.

### At-most-once persistence, not at-most-once computation

Persistence is two writes: insert the assistant reply, then CAS the user row to `answered`.
If the second write fails, the reply is deleted and the request fails `503
persistence_failed` rather than returning a success that was never recorded. That rollback
is best-effort: if the delete also fails, an orphan assistant row survives, inert because
replay joins by id and the lookup route only exposes a reply when `status == "answered"`.

The honest statement of the guarantee is therefore:

- **At most one** user message, one reply link, and one quota unit per `request_id` —
  enforced by the unique index and the compare-and-sets.
- **Not** at most one upstream call. A crash after Gemini returns but before the reply is
  inserted leaves the row in `processing`; once the lease expires, a resubmission may
  re-dispatch and spend one extra Gemini request. It cannot duplicate the message, the
  reply link, or the charge.
- The reverse case — `answered` with a missing reply — is `409 request_unrecoverable`,
  never a fabricated `200`.

Failure codes are fixed strings (`llm_unavailable`, `llm_empty_response`,
`persistence_failed`). Exception text is logged, never stored, because provider errors can
quote user content.

---

## Integrations and billing

### Stripe — implemented, sandbox-verified

- `POST /api/billing/checkout` creates a **one-time** Checkout Session
  (`mode="payment"`) priced from the server-side tier table. The client never supplies the
  amount.
- The transaction row is written *before* the session is created, under a
  `client_reference_id` chosen server-side. Stripe can fire `checkout.session.completed`
  from inside `Session.create()`, and a row inserted afterwards does not exist yet when
  the webhook looks for it — which silently dropped grants in an earlier iteration.
- The webhook verifies the signature against `STRIPE_WEBHOOK_SECRET`. Unset secret →
  `503`; failed verification → `400`. No path trusts an unverified payload, which would
  let anyone grant themselves a paid tier.
- Only `checkout.session.completed` and `checkout.session.async_payment_succeeded` grant
  access. A settled session that cannot be matched to a transaction returns `503` so
  Stripe retries, rather than `200` after taking the customer's money.
- The grant is a 30-day `current_period_end`. There is no subscription object, no
  renewal, no invoice handling, and no cancellation flow — this is a paid-access window,
  not recurring billing.

**Not verified:** the webhook has not been exercised against a public HTTPS endpoint. Doing
so requires a deployment decision and a dashboard-configured endpoint. Until then it is
covered only by manual `stripe listen` runs.

### OAuth (Salesforce, HubSpot) — partial

Real pieces exist: an authorize redirect with a random `state` persisted to `oauth_states`,
a callback that consumes the state so it cannot be replayed, and a code-for-token exchange
that only records the connection as live when the provider actually returns an access
token.

Two caveats, both relevant:

1. **With no `*_CLIENT_ID` set, the start route mock-connects immediately** and writes
   `status: "connected", mock: true`. The UI says so explicitly ("MOCK — no OAuth
   credentials configured"). A "connected" badge therefore does not imply a live
   integration unless real credentials were configured.
2. The token exchange uses `httpx`, which is **neither installed nor listed in
   `requirements.txt`**. On a clean install the exchange raises inside a `try`, is logged
   as a warning, and the callback redirects with `?error=token_exchange_failed`. The
   failure is handled honestly — the connection is not granted — but a real OAuth flow
   cannot complete until `httpx` is added.

No data is synced from either provider. Connecting creates a row; nothing reads from it.

### Sources page — placeholder

`GET /api/sources` returns a hardcoded catalogue (Salesforce, HubSpot, Snowflake, Slack,
…) with fabricated record counts. It is presentation scaffolding, not an integration.

---

## Testing

> **Warning — this suite deletes data.**
>
> `backend/tests/` **empties a MongoDB database and an upload directory before and after
> every run.** It is only ever safe against a disposable database. The safeguards below
> are designed to catch a misconfiguration and abort, but they are a safety net, not
> permission to run this against data you care about. The backend process and the pytest
> process must be given identical values for `DB_NAME`, `UPLOAD_DIR`,
> `ENABLE_TEST_DB_PROBE`, `TEST_DB_PROBE_SECRET` and `REACT_APP_BACKEND_URL` — see
> [Running it](#running-it). A mismatch aborts the run before anything is deleted.

### What the suite is

`backend/tests/` is a **pytest** suite. Every module requires a running backend and a
reachable MongoDB, because the session fixture calls the server before it will delete
anything. Within that constraint there are three tiers:

| Tier | Modules | What it does |
| --- | --- | --- |
| Offline, in-process | `test_chat_status.py`, `test_chat_idempotency.py` | Calls handlers directly with `server.db` monkeypatched. Never performs an HTTP request to the app, never calls Gemini or Stripe. |
| Live HTTP | `backend_test.py`, `test_phase0.py`, `test_iteration2.py`, `test_limits.py`, `test_db_safety.py`, `test_chat_idempotency_http.py` | Real HTTP against the started server, covering routing, auth, serialization and status codes. Chat HTTP tests resolve against pre-seeded rows, so they return before dispatch. |
| External service | `backend_test.py::TestChat` (2 tests), `test_iteration3.py` (Stripe) | Genuine Gemini and Stripe API calls. Skipped when the corresponding key is absent; they spend real requests when it is present. Point `STRIPE_API_KEY` at a test-mode key. |

The offline chat suites are possible because of `backend/tests/chat_harness.py`, which
supplies an in-process `ChatEnv`, per-test event loops (Motor 3.3.1 binds to the first loop)
and a `no_live_provider` fixture that turns any real `requests.post` into an assertion
failure.

### Disposable-database safeguards

Because a misconfigured run is destructive rather than merely noisy, four guards must hold
before anything is created or deleted. Each failure raises `RuntimeError`; none of them
skip, because a skip exits `0` and makes "never ran" look like "passed".

| Guard | Requirement |
| --- | --- |
| Database name | `DB_NAME` is **exactly** `decisionlens_test`, in both processes |
| Server agreement | `GET /api/` reports `test_db: true` |
| Database identity | pytest writes a random marker and the server must read it back through its own Motor handle |
| Upload directory | `UPLOAD_DIR` is set, resolves outside `uploads/`, contains `test` in its final path segment, and is not near a filesystem root |

The identity check is the non-obvious one. A matching name only proves the two processes
were *configured* alike; they can still hold connections to different databases. The probe
route that answers this is disabled unless `ENABLE_TEST_DB_PROBE` is set, `DB_NAME` is
exactly `decisionlens_test`, and a shared secret of at least 32 characters matches — and it
returns the same `404` as an unmatched route otherwise, so it does not advertise itself.

`uploads_test/` is gitignored, so a run cannot dirty `uploads/` and its committed sample
files. If UUID-named files appear in `uploads/` after a test run, the upload guard was
bypassed — investigate before committing.

### Running it

Two PowerShell terminals, the same five variables in each, with the same values. Generate
the secret once:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Terminal 1 — the backend.** Start it with the probe enabled. The route stays hidden
unless `ENABLE_TEST_DB_PROBE="1"`, the secret is at least 32 characters, and `DB_NAME` is
exactly `decisionlens_test` — and without it pytest cannot verify which database it is
about to empty.

```powershell
cd backend

$env:DB_NAME="decisionlens_test"
$env:UPLOAD_DIR="uploads_test"
$env:REACT_APP_BACKEND_URL="http://127.0.0.1:8001"
$env:ENABLE_TEST_DB_PROBE="1"
$env:TEST_DB_PROBE_SECRET="<the secret, identical in both terminals>"

python -m uvicorn server:app --host 127.0.0.1 --port 8001
```

**Terminal 2 — the suite.** Every variable above must be repeated here, verbatim. These
are not "backend-only" settings: pytest reads `DB_NAME` to decide whether it may wipe at
all, sends the secret as a header to the probe, and compares its own `UPLOAD_DIR` with the
server's.

```powershell
cd backend

$env:DB_NAME="decisionlens_test"
$env:UPLOAD_DIR="uploads_test"
$env:REACT_APP_BACKEND_URL="http://127.0.0.1:8001"
$env:ENABLE_TEST_DB_PROBE="1"
$env:TEST_DB_PROBE_SECRET="<the secret, identical in both terminals>"

python -m pytest tests -v
```

Why both terminals and not just one:

- `DB_NAME` is checked twice — the suite refuses to run unless its own value is exactly
  `decisionlens_test`, and separately requires the server's `GET /api/` to report
  `test_db: true`. Setting it in one shell only leaves the other process on `.env`'s
  `decisionlens`.
- The probe secret is compared in the server against the header pytest sends. Different
  values produce a `404`, and the run aborts; a secret shorter than 32 characters is
  rejected by the suite before it is even sent.
- `UPLOAD_DIR` is round-tripped: the probe reports whether the server resolves it to the
  same path pytest will empty, and a mismatch aborts.
- `REACT_APP_BACKEND_URL` tells pytest which port to talk to; the default is
  `http://localhost:8001`.

All four guards **raise** rather than skip, and they run before the wipe, so a
misconfiguration stops the run with nothing deleted. `load_dotenv` does not override
variables already present in the environment, which is what lets the shell values win over
`backend/.env` in both processes.

To go back to normal development, remove `DB_NAME`, `UPLOAD_DIR` and the probe variables
from the shell and restart the server.

### Verified result

Last full run: **16 November 2026**, on commit **`5816c05`**. It reported:

```text
192 passed, 2 skipped
```

Both skips are structural, not failures: with `GEMINI_API_KEY` and `STRIPE_API_KEY`
configured, the tests that assert the *unconfigured* `503` paths are unreachable and say
so (`GEMINI_API_KEY is configured; unconfigured path not reachable`,
`STRIPE_API_KEY is configured; unconfigured path not reachable`). No result is reported
here that was not observed on that run, and the number is environment-dependent: with no
keys present the same suite skips a different, larger set and reports fewer passes. The
figure comes from a local run against the configuration above, not from CI — this
repository has no CI workflow.

`backend/verify_fixes.py` is a separate standalone harness (no server, but needs `MONGO_URL`
/ `DB_NAME` / `JWT_SECRET`) asserting the cookie matrix, the `503` contract and the upload
path. Note that it resolves `UPLOAD_DIR` to the repository `uploads/` by default, so it can
leave files there — clean up afterwards.

### Frontend

There is **no automated frontend test suite**. There are no test files, and `npm test`
exits 1 with `No tests found`. This is a real gap, not a configuration quirk. There is also
no lint or typecheck script: ESLint is configured inline in `craco.config.js` (React hooks
rules only) and is not exposed as a command.

`npm run build` completes without warnings. Do not set `CI=true` for a local build — CRA
promotes warnings to errors, which is a CI concern, not a local one.

---

## Deployment

**Not verified.** No hosted deployment of this application has been tested, and this
repository contains no Dockerfile, no compose file and no CI configuration. The steps below
describe what a deployment would require; they are not a record of a working one.

1. Provision MongoDB (Atlas or self-hosted) and set `MONGO_URL` / `DB_NAME`.
2. Set `ENVIRONMENT=production`, a strong `JWT_SECRET`, and distinct seeded-account
   passwords or none at all.
3. Serve the frontend over HTTPS (or set `COOKIE_SECURE` / `COOKIE_SAMESITE` explicitly
   to match your topology). Cross-site cookies require `SameSite=None` **and** `Secure`,
   which requires HTTPS.
4. Set `FRONTEND_URL` to the public frontend origin so CORS and OAuth redirects work.
5. Build the SPA with `REACT_APP_BACKEND_URL` pointing at the public API origin, then
   serve `frontend/build` from a static host or reverse proxy.
6. Register the Stripe webhook endpoint and set `STRIPE_WEBHOOK_SECRET`. Without it the
   endpoint correctly refuses all events (`503`), so upgrades will not apply.

Port 8000 is the documented default, but there is no requirement to keep it; the only
coupling is `REACT_APP_BACKEND_URL` on the client and the OAuth redirect URIs registered
with each provider.

---

## Limitations

Known and deliberate, stated plainly:

- **No document grounding.** Analysis reasons over the decision's own metadata. Attachment
  text is extracted and stored but never enters a prompt, so recommendations cannot cite
  an uploaded file as evidence.
- **Failed chat turns still consume quota.** The user message is persisted before the
  provider call and `REFUND_CHAT_ON_LLM_FAILURE` is `False`, so a dispatch that fails
  (`llm_unavailable`, `llm_empty_response`, `persistence_failed`) costs one
  `ai_chats_per_month` unit. Retrying the *same* message does not cost extra, because a
  repeat inserts no second user message. One exception: `503 llm_not_configured` is
  checked before any write, so a missing key costs nothing. Refunding failures
  deliberately touches billing semantics and has not been done.
- **Reply persistence is not atomic.** Inserting the reply and marking the turn `answered`
  are two writes. Failure after the first is rolled back best-effort; a crash between them
  leaves an orphan reply, and a crash after dispatch leaves the row in `processing`.
- **No background recovery sweeper.** Nothing reclaims a stranded `processing` row until
  that `request_id` is resubmitted. There is no scheduled job scanning for dead leases.
- **No frontend tests, no lint/typecheck command.** Client-side behaviour — including the
  recovery flow — is verified manually.
- **Production webhook unverified.** Signature verification is implemented and refuses to
  run unconfigured, but it has not been exercised against a public HTTPS endpoint.
- **OAuth cannot complete on a clean install** because the token exchange imports `httpx`,
  which is not a declared dependency. Without credentials the flow mock-connects by design.
- **`/api/sources` is placeholder data**, and the dashboard's KPI deltas are constants.
  The Health Score is a heuristic, not a measurement.
- **Refresh tokens are issued but never exchanged.** There is no `/auth/refresh` endpoint;
  `get_current_user` reads only `access_token`, whose lifetime is 24 hours. A user session
  therefore ends when the access token expires, and the `refresh_token` cookie is inert.
- **Quota counting is read-then-check.** `_enforce_limit` derives usage by counting rows;
  it is not a transactional reservation, so concurrent requests at the exact cap could
  each pass. Accepting this was a deliberate trade for simplicity.
- **`ANALYSE_COUNTS_AS_CHAT = False`**: a decision analysis calls the paid model but does
  not consume chat quota. It has its own `analyses_per_month` limit instead.

---

## Troubleshooting

**`KeyError: 'MONGO_URL'` on startup, even though `.env` exists.**
The file was saved with a UTF-8 byte-order mark, so the first key becomes `\ufeffMONGO_URL`.
Windows PowerShell 5.1 adds a BOM with `Set-Content`, `Out-File` and `>`. Copy the template
with Explorer, or strip it:

```powershell
$t = Get-Content .env -Raw
[System.IO.File]::WriteAllText("$PWD\.env", $t, (New-Object System.Text.UTF8Encoding($false)))
```

**Login succeeds but every later request returns `401`.**
The `access_token` cookie was not stored. A `Secure` cookie is silently discarded over
plain `http://`, which produces exactly this symptom. Set `ENVIRONMENT=development` (or
`COOKIE_SECURE=false`) for local HTTP. See
[Authentication and cookies](#authentication-and-cookies).

**The frontend loads but every request 404s against port 3000.**
`REACT_APP_BACKEND_URL` is unset, so the client is posting to `undefined/api` on its own
origin. There is no fallback; set it and restart the dev server (CRA only reads
`REACT_APP_*` at build time).

**AI chat returns `503 llm_not_configured`.**
`GEMINI_API_KEY` is empty. That is the endpoint reporting it cannot reach a model instead
of inventing a reply. Get a key at <https://aistudio.google.com/apikey>.

**AI chat returns `409 request_in_flight`.**
The same `request_id` is still being processed by another worker. Poll
`GET /api/chat/requests/{request_id}`; the UI does this automatically. If it persists past
the 180-second lease, resubmitting reclaims the attempt.

**AI chat returns `409 retry_exhausted`.**
Three attempts for that message have been spent. Send a new message — it will get a new
`request_id` and a fresh budget.

**Stripe webhook returns `503`, or upgrades never apply.**
`STRIPE_WEBHOOK_SECRET` is unset, so the endpoint refuses unsigned payloads. Locally, run
`stripe listen --forward-to localhost:8000/api/webhook/stripe` and copy the printed
`whsec_...` into `backend/.env`. In production, take it from the endpoint's page in the
Stripe dashboard.

**The app refuses to start with a lease/timeout error.**
`CHAT_ATTEMPT_LEASE_SECONDS` must be greater than `GEMINI_TIMEOUT_S`. This is an import-time
guard: a shorter lease would reclaim live provider calls and guarantee duplicate work.

**`Disallowed CORS origin`.**
Only `FRONTEND_URL` and `http://localhost:3000` are allowed. Set `FRONTEND_URL` to the real
frontend origin.

---

## Security notes

- Passwords are bcrypt-hashed; plaintext is never stored or logged.
- Auth cookies are `HttpOnly`, so client JavaScript cannot read the session token.
- Every protected route resolves the user from the token and filters by `user_id`;
  cross-user access to a decision, attachment or chat request id is a `404`.
- Attachments are validated by magic bytes (PDF header, DOCX ZIP structure containing
  `word/`) rather than by extension alone, and capped at 10 MB with a streamed read that
  stops one byte past the cap.
- There are no default passwords. Seeded accounts require an explicit 12+ character
  password, and the sign-in form starts empty.
- The LLM endpoints never fabricate output: unconfigured, unreachable, or empty responses
  fail with `503` and a machine-readable code, and provider exception text is logged but
  never stored, since it can quote user content.
- The Stripe webhook verifies every signature and refuses to process anything when the
  secret is unset. Checkout success and cancel URLs are built only from server-side
  configuration (`STRIPE_SUCCESS_URL` or `FRONTEND_URL`), so a caller cannot steer a payer
  to an attacker-controlled page carrying the session id.
- Every response carries `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy` and a restrictive `Content-Security-Policy`.
  `Strict-Transport-Security` is added only when cookies are issued as `Secure`, so local
  HTTP development is unaffected.
- `ADMIN_PASSWORD`, `DEMO_PASSWORD` and `JWT_SECRET` must all be changed for any real
  deployment.

---

## Branding

Brand artwork is read at runtime from `frontend/public/`:

| File | Used for |
| --- | --- |
| `mark.png` | monogram in the sidebar and sign-in screen |
| `logo.png` | full logo with wordmark (About page) |
| `favicon.png`, `logo192.png`, `logo512.png` | browser tab and installable app |

If an asset is missing, the UI falls back to a placeholder glyph rather than showing a
broken image. The master source is `assets/brand/decisionlens-logo-source.jpg`; the PNGs are
generated from it with the near-black background converted to transparency.

---

## Roadmap

Ordered by what would most reduce risk, not by what is most visible:

1. Document grounding — pass attachment text into the analysis prompt, with explicit
   citations so a recommendation can be traced to a source.
2. A background sweeper for stranded `processing` rows, replacing resubmission-driven
   recovery.
3. Atomic or idempotent reply persistence, and an explicit refund policy for failed turns.
4. A frontend test suite for the chat recovery flow (request ids, 409 polling, mount
   reconciliation).
5. Complete the OAuth integration: add `httpx`, refresh and encrypt stored tokens, and
   ingest real records.
6. A verified production deployment with an HTTPS Stripe webhook endpoint.
7. Streaming chat responses; multi-user workspaces and role-based access.

---

## Author

### Kehinde Azeez Ajani

Data Scientist | AI Engineer | Machine Learning Engineer | Data Analyst

GitHub: <https://github.com/Ajanikehinde>
