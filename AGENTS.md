# AGENTS.md

Instructions for AI agents working in this repository.

## What this is

Full-stack AI SaaS: React 19 SPA + FastAPI + **MongoDB**.

Correct the record before you trust any assumption or older prose in this repo:

- Database is **MongoDB** (`motor`/`pymongo`), not PostgreSQL.
- LLM is **Gemini 2.5 Flash called directly over its REST API** via `requests` — no provider SDK, no private packages. Payment is Stripe via the official `stripe` SDK.
- Frontend is **plain JSX/JavaScript**. There are zero `.ts`/`.tsx` files and `typescript` is not a dependency.
- The backend is **one file**, `backend/server.py` (~1430 lines), not a layered `api/`/`services/`/`models/` package.
- Entrypoint is **`server:app`** inside `backend/`. Not `app.main:app`.

## Commands

Run backend commands with cwd = `backend/`.

```powershell
pip install -r requirements.txt
uvicorn server:app --reload --port 8000      # API on :8000, docs at /docs
```

Frontend (cwd = `frontend/`):

```powershell
npm install
npm start                                      # CRA dev server on :3000
npm run build
```

There is **no lint and no typecheck script** — `package.json` only has `start`, `build`, `test`. Don't invent a lint step; ESLint is configured inline in `craco.config.js` (react-hooks rules only) and has no config file.

## Environment setup

Two separate env files, both gitignored, both required:

```powershell
copy backend\.env.example backend\.env
copy frontend\.env.example frontend\.env
```

### Traps

- **`backend/.env` must be UTF-8 *without* a BOM.** `python-dotenv` will not strip it, so the first key becomes `\ufeffMONGO_URL` and the app dies at import with a bare `KeyError: MONGO_URL`. Windows PowerShell 5.1's `Set-Content`, `Out-File`, and `>` all add a BOM. Copy the file with Explorer, or strip it:
  ```powershell
  $t = Get-Content .env -Raw
  [System.IO.File]::WriteAllText("$PWD\.env", $t, (New-Object System.Text.UTF8Encoding($false)))
  ```
- `MONGO_URL`, `DB_NAME`, `JWT_SECRET` are read with `os.environ[...]`, so a missing value is a hard crash at import, not a runtime error.
- `load_dotenv` targets **`backend/.env` only**, and runs before config is read. A root-level `.env` is ignored by the backend.
- `REACT_APP_BACKEND_URL` has **no fallback**. If `frontend/.env` is missing, `lib/api.js` builds the literal baseURL `"undefined/api"` and every request goes to the dev server instead of the backend — silently, with no error. Set it explicitly.
- Only `REACT_APP_*` reaches the browser bundle.

## Behavior contracts

These are deliberate and load-bearing. Don't "simplify" them back.

- **Auth cookies follow the environment.** `ENVIRONMENT=development|dev|local` → `Secure` omitted, `SameSite=lax`. Anything else → `Secure` + `SameSite=None`. A `Secure` cookie over plain `http://localhost` is silently dropped by the browser, which yields a successful login followed by 401 on every subsequent request. `COOKIE_SECURE`/`COOKIE_SAMESITE` override.
- `clear_auth_cookies` must keep the **same** attributes as `set_auth_cookies`, otherwise logout does not delete the cookies.
- **`/api/chat` never fabricates a reply.** No key → `503 llm_not_configured` (checked before any DB write). Provider error → `503 llm_unavailable`. Empty response → `503 llm_empty_response`. Reply stored but unacknowledged → `503 persistence_failed`. Do not reintroduce a canned fallback string returned as `200`.
- **Every user chat message carries a status**, written by `_set_message_status` as a compare-and-set (the filter pins `{"id": …, "status": current}` and a `matched_count != 1` raises), never as a read followed by a write:
  - `pending` at insert → `processing` immediately before the provider call → `answered` or `failed`. `answered` is absolutely terminal. Stage 2 added exactly one edge, `failed → processing`, reachable only through `_retry_message`, so a client can retry the **same** message instead of creating a duplicate; `pending` still cannot skip `processing`, and `_ALLOWED_TRANSITIONS` rejects every other edge.
  - `answered` is written **only after** the genuine Gemini reply is persisted, and links to it via `reply_message_id`. Never mark `answered` before the insert, and never infer a pair from adjacency.
  - `failed` records a fixed `error_code` (`llm_unavailable`, `llm_empty_response`, `persistence_failed`) and leaves **no** assistant row. Exception text is logged, never stored — provider errors can quote user content.
- **Persistence failures stay observable.** If the reply insert succeeds but the `answered` write fails, `_persist_answer` deletes the reply and the request fails `503 persistence_failed` — never `200`. That rollback is **best-effort**: if the delete also fails, the orphan assistant row survives, inert because replay joins by id. `_mark_failed` is the only status writer allowed to swallow errors, so bookkeeping cannot mask the real cause. A request that dies between the two writes leaves the row in `processing`, and nothing recovers it yet.
- **Replay uses verified completed pairs only.** `_replayable_history` requires `status == "answered"` **and** a matching assistant row found by `reply_message_id` with non-empty content. No positional assumptions — the old `prior[:-1]` hack is gone. History is still replayed explicitly from `chat_messages`, capped by `CHAT_HISTORY_TURNS`, roles mapped `assistant`→Gemini's `model`; do not reintroduce provider-side session state, the request must stay reproducible from Mongo alone. This filter is the only place turns are dropped: the history endpoints still return them.
- **Legacy messages with no `status` report `unknown` and are not replayed.** `_serialize_chat_message` maps absence to `"unknown"` at the read boundary; stored documents are never rewritten or backfilled. The pre-503-fix code could return fabricated text as a `200`, so an old assistant row is not evidence of a genuine exchange and must not teach the model to imitate it.
- **Idempotency is opt-in, per user, and enforced by the database.** `ChatIn.request_id` is an `Optional[UUID]` (junk is a 422, never a silent "no key"), so a client that omits it keeps the legacy behaviour exactly. The unique index `uniq_chat_request_per_user` on `(user_id, request_id)` is the authority — the `_load_request` read that precedes insertion is only a fast path, so two concurrent submissions still cannot create two rows. It is **partial** (`{"request_id": {"$type": "string"}}`) so pre-Stage-2 rows, which have no such field, neither collide nor block the build.
- **The index is created and verified at startup, outside the seeding try/except.** `_ensure_chat_request_index` creates it, then asserts uniqueness, keys and partiality from `index_information()`; every failure raises, so the process refuses to serve chat without the constraint. Do not move the call inside a swallowed `try`.
- **A repeat is resolved from the stored row before the key check and before the quota check.** `answered` → the stored reply is returned as a literal `{"session_id", "reply"}`, with no Gemini call and no quota charge, so an already-paid-for answer still replays during a provider outage and at the quota cap. `pending`/`processing` inside the lease → `409 request_in_flight` with `retry_after_ms` and a `lookup` hint. `failed` → retried in place while attempts remain, else `409 retry_exhausted`. Same id with a different message or a different **requested** session → `409 idempotency_conflict`.
- **A repeat never inserts a second user message, so it cannot consume quota.** `_usage_count` derives usage by counting `role: "user"` rows in the month window and `_enforce_limit` is a pure read — that is why duplicate suppression costs nothing by construction. A **failed** turn still consumes one `ai_chats_per_month` message (`REFUND_CHAT_ON_LLM_FAILURE` stays `False`): Stage 2 changed how retries are counted, not how failures are billed.
- **`requested_session_id` is stored separately from `session_id`.** It records what the client actually sent, so an interrupted *first* request (client sent `null`, server generated the id) can be retried with `null` without being mistaken for a session conflict — otherwise a dropped response on the very first message would leave the client permanently stuck. It is dropped from API responses.
- **`attempt` is part of every retry-sensitive compare-and-set.** Status transitions, `_retry_message` and the reply acknowledgement all pin the attempt; `_attempt_filter` treats a missing counter as 1, so pre-Stage-2 rows stay retryable without a backfill. A worker resuming after its lease expired therefore cannot overwrite a newer attempt's outcome with stale text.
- **The lease is a recovery heuristic, never proof the previous worker stopped.** `CHAT_ATTEMPT_LEASE_SECONDS` (180) is the age at which a `processing` row may be reclaimed through `failed → processing`, and a paused-but-alive worker is indistinguishable from a dead one; `MAX_CHAT_ATTEMPTS` (3) bounds the damage. Import fails if the lease is not longer than `GEMINI_TIMEOUT_S`. Honest limit: idempotency is at-most-once **persistence**, not at-most-once **computation** — a crash after Gemini returns but before the reply is inserted leaves `processing`, and the lease may re-dispatch it, costing one wasted upstream call but never a duplicate message, reply link or quota charge.
- **An orphaned reply is never replayed as an answer.** `_reply_text_for` resolves only through `reply_message_id`, and the lookup route exposes a reply only when `status == "answered"`, so an assistant row left by a crash between the two writes stays inert — visible in history, linked to nothing. The reverse case, `answered` with a missing reply, is `409 request_unrecoverable`, never a fabricated `200`.
- **`GET /api/chat/requests/{request_id}` is the recovery endpoint.** Scoped by user, so another account's id is a 404 rather than a 403; it returns `status`, `error_code`, `attempt`, `max_attempts`, `can_retry`, `session_id`, and the reply only when answered. This is how a client recovers a server-generated session id after a dropped response, and how it polls a `request_in_flight`.
- **Stage 2 still omits quota refunds, atomic multi-document persistence and a background sweeper.** Insert-and-mark remains two writes, and nothing reclaims a stranded `processing` row until that request id is resubmitted. Changing any of these alters billing or retry semantics — get explicit approval first.
- **The Gemini call is blocking `requests`, so it must stay inside `run_in_threadpool`.** Calling it directly from the async handler blocks the event loop and stalls every other request.
- **The Stripe webhook verifies signatures and refuses to run without `STRIPE_WEBHOOK_SECRET`** (503 if unset, 400 if the signature fails). Never add a path that trusts an unverified payload; it would let anyone grant themselves a paid tier. Access fields via `getattr`, since a `StripeObject` raises `AttributeError` on a missing key and `.get()` does not exist on it.
- **There are no default seed passwords.** `ADMIN_PASSWORD` / `DEMO_PASSWORD` must be set (12+ chars) or the account is skipped with a logged warning. The login form starts empty; never prefill credentials or ship them in the bundle.
- `frontend/src/pages/AIChat.jsx` renders failures as `role: "error"` with distinct styling so an outage can't be mistaken for a model reply; retryable failures also carry an explicit Retry button that reuses the **original** `request_id` instead of sending a new message. Keep both. Pending submissions live in `sessionStorage` keyed by user id (`frontend/src/lib/chatRecovery.js`) and are reconciled on mount: a terminal state is read back, while an unsettled one is resubmitted with the same id so the **server** decides whether to resume it or answer `409 request_in_flight` — the browser never decides idempotency itself, and a reload of an already-`failed` message does not silently spend another attempt. Entries are cleared on logout and when another account signs in on the same tab.

## Testing

`backend/tests/` are **live-server integration tests** — they make real HTTP calls. Start the backend and have MongoDB up first.

The suite **empties a database and an upload directory before and after every run**, so it refuses to start unless every guard below holds. Each failure raises `RuntimeError`; none of them skip, because a skip exits the run with status 0 and makes "never ran" look like "passed".

| Guard | Requirement |
|---|---|
| Database name | `DB_NAME` is **exactly** `decisionlens_test`, in both processes |
| Server agreement | `GET /api/` reports `test_db: true` |
| Database identity | a marker written by pytest is readable by the server through its own Motor handle |
| Upload directory | `UPLOAD_DIR` is set, resolves outside `uploads/`, has `test` in its final path segment, and is not near a filesystem root |

```powershell
cd backend
$env:DB_NAME="decisionlens_test"
$env:UPLOAD_DIR="uploads_test"
$env:REACT_APP_BACKEND_URL="http://127.0.0.1:8001"
$env:ENABLE_TEST_DB_PROBE="1"
$env:TEST_DB_PROBE_SECRET="<token_urlsafe(32)>"   # same value in both shells

# terminal 1
python -m uvicorn server:app --host 127.0.0.1 --port 8001
# terminal 2
python -m pytest tests -v
```

- Generate the secret once with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and paste the same value into both shells. If it is shorter than 32 characters, or the two processes disagree, the probe answers 404 and the fixture fails before wiping anything.
- `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `GEMINI_API_KEY` and `STRIPE_API_KEY` are read from `backend/.env` by `conftest.py` **at import**, so they no longer need exporting. That timing matters: `load_dotenv` used to run inside the session fixture, i.e. after every module-level `skipif` had been evaluated, so the suite asserted "unconfigured" paths against a configured server.
- Modules that need the seeded admin are `skipif`-gated on `ADMIN_PASSWORD`; that is a legitimate skip, because those tests cannot execute under their intended conditions. Configuration errors — wrong database, wrong upload directory, missing probe secret — are errors, never skips.
- `backend_test.py::TestChat` needs `GEMINI_API_KEY`; without it, the LLM happy-path tests skip and a test asserts the `503 llm_not_configured` shape instead.
- `backend/tests/test_chat_status.py` is fully offline and in-process. It calls the handler directly with `server.db` monkeypatched, because `httpx` is absent (no `TestClient`) and Motor 3.3.1 binds to the first event loop (so each test gets its own client **and** its own loop). It never calls Gemini or Stripe, and covers the status machine, the `pending`/`processing` window, provider and persistence failures, replay selection and legacy `unknown` rows. Routing and response serialization are **not** covered there — `backend_test.py::TestChat` still does that. Its harness lives in `backend/tests/chat_harness.py` (`ChatEnv`, the `env`/`mongo` fixtures, and `no_live_provider`, which turns any real `requests.post` into an assertion failure) and is shared with the idempotency suites.
- `backend/tests/test_chat_idempotency.py` (31 tests, fully offline) covers identity and the unchanged `{session_id, reply}` shape, replay with no dispatch and no quota, conflicts, interrupted-first-request recovery, orphaned replies, crash-between-writes rollback, in-flight vs. stale-lease recovery, retry budget, concurrent duplicates and concurrent retries, the lookup contract, and the unique index itself. `env.chat()` calls `server.chat` directly; an autouse fixture supplies a canned provider, so **no test in this module can spend a Gemini request**.
- `backend/tests/test_chat_idempotency_http.py` (12 live-HTTP tests) covers routing, auth, 422 on a malformed `request_id`, the `409` codes (`idempotency_conflict`, `request_in_flight`, `retry_exhausted`), cross-user 404s, and the presence of the unique index in the **running** server's database. Every `POST` resolves against a pre-seeded row, so it returns before dispatch and never calls Gemini either.
- `test_iteration3.py` Stripe tests need `STRIPE_API_KEY`; without it they skip and a test asserts checkout fails explicitly. There are **no webhook tests** — signature verification is only covered by `verify_fixes.py` and manual `stripe listen`, so re-verify by hand if you touch that handler.
- Run a focused test: `python -m pytest tests/backend_test.py::TestAuth -v`.
- `FRONTEND_URL` in the tests defaults to `http://localhost:3000` and **must be an allowed CORS origin**. Override `LOCAL_API_URL` only if the backend runs on a different port than the rest of the suite.

### Test runs must not dirty the working tree

`uploads/` is tracked in git (it ships with 12 committed sample files) and the backend writes there in normal use. The suite no longer can: `UPLOAD_DIR` is mandatory and validated before anything is deleted, so attachment writes during a run land in the test directory. If you ever see new UUID-named files in `uploads/` after a run, the upload guard was bypassed — investigate before committing.

`backend/verify_fixes.py` still resolves `UPLOAD_DIR` at the repository `uploads/` by default, so running *that* does drop files there. Clean them up before committing, and don't mistake them for intentional edits.

`backend/tests/test_db_safety.py` covers the guards themselves: the four probe exposure conditions, missing/wrong/non-ASCII secrets, marker visibility in both directions, upload-path verdicts, and the fail-closed behaviour of `verify_same_database`. Run it alone after touching anything in `conftest.py`.

`backend/verify_fixes.py` is a standalone no-server harness (`python verify_fixes.py`, needs `MONGO_URL`/`DB_NAME`/`JWT_SECRET` set) asserting the cookie matrix, the 503 contract, and the upload path. Run it after touching auth, cookies, chat, or seed logic.

## Gotchas

- `frontend/src/constants/testIds/` is **vestigial**. Its docs claim a central registry you must import, but nothing imports it — all 110 `data-testid` values are hardcoded inline in JSX. Don't refactor pages to use it without being asked. The `emergent(kebab-case-testid)` lint rule it used to reference does not exist in this repo.
- `@emergentbase/visual-edits` (devDependency) installs from a remote `.tgz` URL, so `npm install` needs network. `craco.config.js` degrades gracefully if it's missing.
- **Emergent platform branding has been stripped from shipped code.** `public/index.html` must not reintroduce an `<a id="emergent-badge">` fixed-position badge, a `<title>Emergent | Fullstack App</title>`, or the `assets.emergent.sh/scripts/emergent-main.js` tag — that script is third-party and can inject its own UI. Likewise no page should claim "Built with Emergent".
- `npm run build` is warning-clean as of the A–E audit fixes. Still do not set `CI=true` for local builds: CRA promotes any warning to an error.
- `npm test` **exits 1** - there are no frontend test files. That is expected, not a broken setup.
- `uploads/` is gitignored via `uploads/*` with explicit per-file negations for the 12 committed samples. New uploads stay out of version control; if you add a sample you want tracked, add its exact filename to `.gitignore`.
- `react-scripts` lives in `devDependencies`, so `npm audit --omit=dev` reports only the real runtime surface. `ajv` / `ajv-keywords` are pinned as *direct* deps on purpose - do not remove them, that pin is the fix for a past compatibility conflict.
- Brand artwork is loaded at runtime from `frontend/public/{mark,logo,favicon,logo192,logo512}.png` via `src/components/Logo.jsx`, which falls back to a glyph if the file is absent.
- `backend/requirements.txt` lists `black`, `isort`, `flake8`, and `mypy` but there is **no config** for any of them and they are not enforced.
- `tests/__init__.py` at the repo root is vestigial; real tests live in `backend/tests/`.
- `.gitignore` has `.env.*`, which by pattern also matches `.env.example`. Negations for it are already present — if you reorder those lines, both `.env.example` templates silently stop being committable. Verify with `git check-ignore -v` before assuming.
- The root `tests/`, `uploads/`, `test_reports/`, and `memory/` directories are Emergent-platform artifacts, not app code. `test_reports/*.json` still quote the old default passwords and are stale.

## Conventions

- Frontend UI primitives are shadcn/ui + Radix, already vendored in `frontend/src/components/ui/`. Reuse them; don't add a new dependency for a button.
- Styling is Tailwind utilities plus custom classes (`.card`, `.btn`, `.btn-primary`, `.input`, `.label-eyebrow`, `.metric-num`) defined in `src/index.css`.
- Design tokens live in `design_guidelines.json` (dark, violet `#8B5CF6` primary, Sora/Manrope/JetBrains Mono). Read it before inventing colors or type scale.
- Backend API routes are registered on an `APIRouter(prefix="/api")` named `api`, then included on the app. New endpoints belong on `api`.
- A `security_headers` middleware wraps every response. Add new headers with `setdefault` so route-level values win.
- Account self-service lives at `PATCH /api/auth/me` and `POST /api/auth/change-password`; the Settings page is wired to exactly these two.
- Auth is a JWT access token in an `HttpOnly` cookie, with a bearer-header fallback in `get_current_user`.