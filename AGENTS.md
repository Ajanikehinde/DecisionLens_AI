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
- **`/api/chat` never fabricates a reply.** No key → `503 llm_not_configured` (checked before any DB write). Provider error → `503 llm_unavailable`. Empty response → `503 llm_empty_response`. Do not reintroduce a canned fallback string returned as `200`.
- **The Gemini call is blocking `requests`, so it must stay inside `run_in_threadpool`.** Calling it directly from the async handler blocks the event loop and stalls every other request.
- Chat history is replayed explicitly from `chat_messages` (capped by `CHAT_HISTORY_TURNS`, roles mapped `assistant`→Gemini's `model`). Do not reintroduce provider-side session state — the request must stay reproducible from Mongo alone.
- **The Stripe webhook verifies signatures and refuses to run without `STRIPE_WEBHOOK_SECRET`** (503 if unset, 400 if the signature fails). Never add a path that trusts an unverified payload; it would let anyone grant themselves a paid tier. Access fields via `getattr`, since a `StripeObject` raises `AttributeError` on a missing key and `.get()` does not exist on it.
- **There are no default seed passwords.** `ADMIN_PASSWORD` / `DEMO_PASSWORD` must be set (12+ chars) or the account is skipped with a logged warning. The login form starts empty; never prefill credentials or ship them in the bundle.
- `frontend/src/pages/AIChat.jsx` renders failures as `role: "error"` with distinct styling so an outage can't be mistaken for a model reply. Keep that distinction.

## Testing

`backend/tests/` are **live-server integration tests** — they make real HTTP calls. Start the backend and have MongoDB up first. 55 tests total.

```powershell
cd backend
$env:REACT_APP_BACKEND_URL="http://localhost:8000"
$env:ADMIN_EMAIL="admin@decisionlens.ai"
$env:ADMIN_PASSWORD="<your 12+ char password>"
python -m pytest tests -v
```

- Whole suite is `skipif`-gated on `ADMIN_PASSWORD` being unset. Skips are reported with an explanatory reason, not silently.
- `backend_test.py::TestChat` needs `GEMINI_API_KEY`; without it, the LLM happy-path tests skip and a test asserts the `503 llm_not_configured` shape instead.
- `test_iteration3.py` Stripe tests need `STRIPE_API_KEY`; without it they skip and a test asserts checkout fails explicitly. There are **no webhook tests** — signature verification is only covered by `verify_fixes.py` and manual `stripe listen`, so re-verify by hand if you touch that handler.
- Run a focused test: `python -m pytest tests/backend_test.py::TestAuth -v`.
- `FRONTEND_URL` in the tests defaults to `http://localhost:3000` and **must be an allowed CORS origin**. Override `LOCAL_API_URL` only if the backend runs on a different port than the rest of the suite.

### Test runs dirty the working tree

`uploads/` is tracked in git (it ships with 12 committed sample files) and the backend writes there. Running the suite drops new UUID-named files into `uploads/`, which show up as untracked changes. `backend/verify_fixes.py` also resolves `UPLOAD_DIR` there. Clean them up before committing, and don't mistake them for intentional edits.

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