# DecisionLens AI

## AI-Powered Decision Intelligence Platform

DecisionLens AI is a full-stack AI SaaS application that turns unstructured business
conversations, meeting notes, stakeholder feedback, and operational discussions into
actionable business intelligence.

It helps organizations identify risks, opportunities, recommendations, and key decision
points using Large Language Models (LLMs).

---

## Business Problem

Organizations generate large amounts of unstructured information through team meetings,
stakeholder discussions, customer feedback, project reviews, and operational reports.
Decision-makers often spend significant time manually reviewing documents before acting.

That process is time-consuming, inconsistent, difficult to scale, and prone to missed
insights. DecisionLens AI automates it by transforming raw conversations into
decision-ready intelligence.

---

## Key Features

### AI Insight Generation

Automatically extracts business risks, strategic opportunities, action items,
recommendations, and key discussion themes.

### Intelligent Decision Support

Structured outputs that help executives and teams make informed decisions faster, with
a multi-turn AI chat copilot for framing and stress-testing decisions.

### Secure Authentication

- JWT authentication (access + refresh tokens in `HttpOnly` cookies)
- Email/password registration
- OAuth connections to Salesforce and HubSpot
- bcrypt password hashing

### Modern SaaS Interface

- Responsive Tailwind UI with an executive dashboard
- Interactive analytics, charts, and filtering
- File attachments with magic-byte content validation

### Billing

Stripe Checkout for paid tiers, driven by webhook-confirmed sessions.

---

## Tech Stack

### Frontend

- React 19
- JavaScript (JSX) — **not** TypeScript; there are no `.ts`/`.tsx` files
- Tailwind CSS 3 + shadcn/ui + Radix UI
- React Router, TanStack Query, Recharts, Framer Motion, Lucide
- CRACO over Create React App (`react-scripts` 5)

### Backend

- FastAPI
- Python 3.10+

### Database

- **MongoDB** (via `motor` / `pymongo`)

### Authentication

- JWT (HS256)
- OAuth 2.0 for Salesforce and HubSpot

### AI Layer

- **Gemini 2.5 Flash**, called directly over Google's public REST API with `requests` (no provider SDK)

### Storage

- Local filesystem for uploaded attachments

> **Note:** there is no Dockerfile or compose file in this repository. See
> [Deployment](#deployment) for how to actually run this.

---

## System Architecture

```text
React 19 SPA (CRACO dev server)
        |  /api/*  (cookies: access_token, refresh_token)
        v
FastAPI backend (backend/server.py)
        |
        v
JWT auth layer  +  OAuth (Salesforce / HubSpot)
        |
        v
MongoDB (decisions, users, chat_messages, billing transactions)
        |
        v
Gemini 2.5 Flash (direct REST) + Stripe Checkout (official `stripe` SDK)
```

---

## Project Structure

The backend is a single module, not a layered package:

```text
.
├── backend
│   ├── server.py            # entire FastAPI application
│   ├── requirements.txt
│   ├── .env.example
│   └── tests                # live-server integration tests (pytest)
├── frontend
│   ├── src
│   │   ├── pages            # route-level screens
│   │   ├── components       # Layout, ProtectedRoute, shadcn/ui primitives
│   │   ├── contexts         # AuthContext
│   │   ├── lib              # axios client and helpers
│   │   └── constants        # test ids
│   ├── package.json
│   ├── craco.config.js
│   └── .env.example
├── uploads                  # attachment storage (gitignored)
├── memory                   # PRD and OAuth setup notes
├── test_reports             # historical iteration reports
└── README.md
```

---

## Requirements

- Python 3.10+
- Node.js 18+ and npm (or yarn)
- A running MongoDB instance (local or Atlas)

---

## Installation

### 1. Backend

A virtual environment can be used, but it is optional: `requirements.txt` installs
cleanly into a normal interpreter too. Note that `backend/venv` (Python 3.12) and the
system interpreter may differ - whichever one you launch `uvicorn` from is the one that
must have the dependencies installed.

```bash
git clone https://github.com/Ajanikehinde/DECISIONLENS_AI.git
cd DECISIONLENS_AI
cd backend

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt

copy .env.example .env       # Windows
# cp .env.example .env       # macOS / Linux
```

The backend reads `backend/.env` automatically at import time.

Edit `.env` and set at minimum:

| Variable | Required | Notes |
| --- | --- | --- |
| `MONGO_URL` | yes | e.g. `mongodb://localhost:27017` |
| `DB_NAME` | yes | e.g. `decisionlens` |
| `JWT_SECRET` | yes | long random string |
| `GEMINI_API_KEY` | for AI chat | without it `/api/chat` returns `503` |
| `ADMIN_PASSWORD` | for the admin account | no built-in default, 12+ chars |
| `DEMO_PASSWORD` | for the demo account | no built-in default, 12+ chars |

Then start the API from the `backend` directory:

```bash
uvicorn server:app --reload --port 8000
```

The entrypoint is `server:app` inside `backend/`, **not** `app.main:app`.

Interactive API docs are at <http://localhost:8000/docs>.

### 2. Frontend

```bash
cd frontend

npm install

copy .env.example .env       # Windows
# cp .env.example .env       # macOS / Linux
```

Set `REACT_APP_BACKEND_URL=http://localhost:8000` in `frontend/.env`.

```bash
npm start
```

The app opens at <http://localhost:3000>.

### 3. Log in

Register any account from the sign-up screen, or use the seeded admin account defined
by `ADMIN_EMAIL` / `ADMIN_PASSWORD`. The demo account is created from
`DEMO_EMAIL` / `DEMO_PASSWORD`.

> Seeded accounts are only created when their password is present in the environment.
> There are no built-in default credentials — if `ADMIN_PASSWORD` is unset, the admin
> account is skipped and a warning is logged.

---

## Authentication and Cookies

Sessions use two `HttpOnly` cookies, `access_token` and `refresh_token`. Their attributes
follow the deployment environment so that login works over both HTTP and HTTPS:

| `ENVIRONMENT` | `Secure` | `SameSite` |
| --- | --- | --- |
| `development` / `dev` / `local` | not set | `lax` |
| anything else (e.g. `production`) | set | `none` |

Overridable explicitly with `COOKIE_SECURE` and `COOKIE_SAMESITE`.

This matters: a cookie marked `Secure` is silently discarded by the browser when the
page is served over plain `http://localhost`, which produces a successful login
followed by a `401` on every subsequent request.

When serving the frontend and backend on different sites in production, both must be on
HTTPS so that `SameSite=None` is accepted.

---

## Deployment

There is no Dockerfile in this repository, so `docker compose up --build` does not work.
To deploy manually:

1. Set `ENVIRONMENT=production` and a strong `JWT_SECRET`.
2. Serve both services over HTTPS.
3. Set `FRONTEND_URL` to the public frontend origin so OAuth callbacks redirect correctly.
4. Set `REACT_APP_BACKEND_URL` at build time and run `npm run build` in `frontend/`.

If you need containerization you will have to author the Dockerfiles yourself.

---

## Testing

There is currently **no frontend test suite** - `npm test` exits with code 1 and reports
`No tests found`. All automated coverage lives in the backend.

The suite in `backend/tests/` consists of **live-server integration tests**: they make
real HTTP requests to a running backend. Start the backend first, then:

```bash
cd backend
pip install -r requirements.txt

# Point at your running API and supply the seeded admin credentials
set REACT_APP_BACKEND_URL=http://localhost:8000
set ADMIN_EMAIL=admin@decisionlens.ai
set ADMIN_PASSWORD=<your 12+ char password>

pytest tests -v
```

Notes:

- If `ADMIN_PASSWORD` is unset the entire suite is skipped with an explanatory reason.
- Chat tests are skipped unless `GEMINI_API_KEY` is set; in their place a test asserts
  that the endpoint returns `503` with code `llm_not_configured` rather than a fabricated
  reply.
- Stripe checkout tests are skipped unless `STRIPE_API_KEY` is set.
- `tests/test_reports/` holds XML output from earlier runs.

---

## Troubleshooting

**`KeyError: 'MONGO_URL'` on startup, even though `.env` exists.**
The `.env` file was saved with a UTF-8 byte-order mark, which makes the first key
unreadable (it becomes `\ufeffMONGO_URL`). This happens easily in Windows PowerShell
5.1, where `Set-Content`, `Out-File`, and `>` add a BOM. Copy `.env.example` with
Explorer instead, or strip the BOM:

```powershell
$t = Get-Content .env -Raw
[System.IO.File]::WriteAllText("$PWD\.env", $t, (New-Object System.Text.UTF8Encoding($false)))
```

**Login succeeds but every later request returns 401.**
The `access_token` cookie is not being stored. Check that `ENVIRONMENT` is not set to a
production value while serving over plain `http://` — a `Secure` cookie is discarded by
the browser in that case. See [Authentication and Cookies](#authentication-and-cookies).

**AI Chat returns `503 llm_not_configured`.**
`GEMINI_API_KEY` is empty or unreadable. This is the endpoint correctly reporting that
it cannot reach a model, rather than inventing a reply. Get a key at
<https://aistudio.google.com/apikey>.

**Stripe webhook returns 503, or upgrades never apply.**
`STRIPE_WEBHOOK_SECRET` is unset, so the endpoint refuses to process events rather than
trusting an unsigned payload. For local testing run `stripe listen --forward-to
localhost:8000/api/webhook/stripe` and copy the printed `whsec_...` value into
`backend/.env`. In production take it from the webhook endpoint's page in the Stripe
dashboard.

Login pastes the seeded credentials into the sign-in form, so no account exists by
default. If those accounts were never created, register a new one.

**Seeded admin or demo account was not created.**
Both require an explicit `ADMIN_PASSWORD` / `DEMO_PASSWORD` of at least 12 characters.
There is no fallback password, and a warning is logged when an account is skipped.

**`Disallowed CORS origin`.**
Only `FRONTEND_URL` and `http://localhost:3000` are allowed. Set `FRONTEND_URL` to your
actual frontend origin.

---

## Branding

Brand artwork is read from `frontend/public/` at runtime:

| File | Used for |
| --- | --- |
| `mark.png` | monogram in the sidebar and sign-in screen |
| `logo.png` | full logo with wordmark (About page) |
| `favicon.png`, `logo192.png`, `logo512.png` | browser tab and installable app |

The source artwork has a near-black background, which was converted to transparency so
the mark sits directly on the dark chrome with no plate behind it. If an asset is
missing, the UI falls back to a placeholder glyph rather than showing a broken image.

The master source is `assets/brand/decisionlens-logo-source.jpg`. The PNGs above are
generated from it (near-black background converted to transparency); regenerate them if
the source artwork changes.

## Security Notes

- Passwords are hashed with bcrypt; plaintext is never stored or logged.
- Auth cookies are `HttpOnly`, so client JavaScript cannot read the session token.
- Attachments are validated by magic bytes, not just by file extension, and capped at
  10 MB.
- There are **no default passwords**. Seeded accounts require an explicit password of at
  least 12 characters, and the sign-in form starts empty rather than shipping a
  credential in the JS bundle.
- `ADMIN_PASSWORD` and `JWT_SECRET` must be changed for any real deployment.
- The AI endpoint never fabricates a response: if the provider is unconfigured,
  unreachable, or returns nothing, it fails with `503` and a machine-readable error code.
- The Stripe webhook verifies every signature against `STRIPE_WEBHOOK_SECRET` and refuses
  to process anything when that secret is unset, so a forged event cannot grant a paid
  tier.
- Every response carries `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy` and a restrictive `Content-Security-Policy`.
  `Strict-Transport-Security` is added only when cookies are issued as `Secure`, so local
  HTTP development is unaffected.
- Document text extraction needs `PyPDF2` and `python-docx`. Both are declared in
  `requirements.txt`; without them PDF/DOCX uploads are stored but yield no searchable
  text, and the failure is logged rather than silent.

> The JSON files in `test_reports/` are historical records from earlier runs and still
> quote the old default passwords. Those accounts no longer exist, so the credentials
> listed there are stale and should not be relied on.

---

## Future Enhancements

- Multi-user collaboration and shared workspaces
- Enterprise role-based access control
- Deployment to AWS
- Streaming chat responses
- Decision history tracking and audit trails
- Additional integrations (Slack, Notion, Google Drive)

---

## Author

### Kehinde Azeez Ajani

Data Scientist | AI Engineer | Machine Learning Engineer | Data Analyst

GitHub: <https://github.com/Ajanikehinde>
