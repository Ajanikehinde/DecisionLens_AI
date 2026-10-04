# DecisionLens AI â€” PRD

## Original Problem Statement
Build a decision intelligence dashboard called **DecisionLens AI** with sidebar pages (Dashboard, Decisions, New Decision, AI Chat, Analytics, Sources, Pricing, Settings, About). Dashboard signature: **Decision Health Score** (87/100 + 4 factor checks). Dark theme (#0F172A, violet/blue/orange accents). Subscription tiers (Starter free / Pro $29 / Team $99) via Stripe.

## User Personas
- **Executive / Decision Owner** â€” logs decisions, uploads supporting docs, tracks calibration
- **Analyst** â€” reviews categories, risk, AI critique
- **Admin** â€” manages connected data sources

## Core Requirements (cumulative)
- JWT auth with httpOnly cookies, explicit CORS
- Seeded demo data + full Decisions CRUD (create / list / detail / edit / delete)
- Decision detail: timeline (with PATCH events), comments, document attachments
- Signature Decision Health Score
- Gemini 2.5 AI Chat (multi-turn)
- Static-smart AI Recommendations
- OAuth flows (Salesforce, HubSpot) with graceful MOCK fallback
- PDF/DOCX/TXT/MD upload with magic-byte content validation
- Stripe subscription billing (Starter / Pro / Team) on a standalone /pricing page

## What's Been Implemented

### v1.0 â€” 2026-06-11
- FastAPI + MongoDB, JWT cookie auth, seeded admin + demo users
- Full Dashboard with Health Score ring + 4 KPIs + Trends (D/W/M) + Categories + Risk donut + Recent + AI Recommendations
- All 8 original sidebar pages
- Gemini 2.5 multi-turn AI Chat
- Tests: 17/17 backend, frontend 100%

### v1.1 â€” 2026-06-11
- Edit-decision page (PATCH with all fields)
- Decision Detail view with Timeline, Comments, Attachments
- PDF/DOCX/TXT/MD multi-file upload from both New Decision and Detail
- CORS tightened to explicit FRONTEND_URL
- Salesforce + HubSpot OAuth scaffolding (real auth_url + state + callback, MOCK fallback when CLIENT_ID empty)
- Tests: 32/32 backend, frontend 100%

### v1.2 â€” 2026-06-11
- **Timeline PATCH logging** â€” title/status/risk/category/confidence/deadline changes auto-recorded with old â†’ new diff
- **Magic-byte upload validation** â€” %PDF- header check, DOCX ZIP-listing check (must contain `word/`), TXT/MD NUL byte + UTF-8/Latin-1 decode check
- **Stripe subscription billing** â€” 3 tiers (Starter / Pro $29 / Team $99), /pricing page, real Stripe test-mode checkout, payment_transactions ledger, subscriptions collection, /api/billing/me + /tiers + /checkout + /status + /webhook
- **Topbar tier badge** â€” always-visible, links to /pricing, color-coded per tier
- **OAuth setup guide** at /app/memory/OAUTH_SETUP.md for graduating from MOCK
- Tests: 52/52 backend, frontend 100%

## Backlog / Future
- **P1**: Split server.py into routers under /app/backend/routes/ (now 1297 lines â€” flagged by testing agent)
- **P1**: Real Salesforce + HubSpot OAuth credentials (instructions in OAUTH_SETUP.md)
- **P1**: Decision Replay â€” auto-prompt 30 days after Complete with calibration score
- **P2**: Persist OAuth access/refresh tokens encrypted; pull real CRM data
- **P2**: PDF.js inline preview for attached docs
- **P2**: 2FA + real API key issuance
- **P2**: Stripe webhook smoke test using signing secret (currently untested by automation)
- **P3**: Export decision portfolio to PDF; Slack mentions

## Test Credentials
See `/app/memory/test_credentials.md`.

## Stripe Test Mode
- Key: `<Stripe test key configured in backend/.env>` (in backend/.env)
- Test card: `4242 4242 4242 4242` Â· any future date Â· any CVC Â· any ZIP

