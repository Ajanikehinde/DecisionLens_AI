"""Backend test suite for DecisionLens AI.

These are live-server integration tests: point BASE_URL at a running backend and
provide the seeded admin credentials. Everything credential- or provider-dependent
is read from the environment so the suite never depends on baked-in defaults.
"""
import os
import uuid
from pathlib import Path

import pytest
import requests

from conftest import purge_user_data

REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").strip().rstrip("/")
if not BASE_URL:
    # Fall back to frontend/.env, resolved relative to this file so the suite works
    # outside the original container layout.
    for candidate in (REPO_ROOT / "frontend" / ".env", REPO_ROOT / ".env"):
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').strip("'").rstrip("/")
                    break
        except OSError:
            continue
        if BASE_URL:
            break

if not BASE_URL:
    BASE_URL = "http://localhost:8000"

API = f"{BASE_URL}/api"

# The seeded admin account is created only when ADMIN_PASSWORD is set, so the suite
# reads the same variable rather than assuming a default password.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@decisionlens.ai").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

# Chat talks to a real LLM. Without a key the endpoint correctly returns 503,
# which the chat tests below assert instead of expecting a fabricated reply.
LLM_KEY_CONFIGURED = bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _cookie_secure_expected() -> bool:
    """Mirror the backend's COOKIE_SECURE derivation to assert against it."""
    override = os.environ.get("COOKIE_SECURE", "").strip()
    if override:
        return override.lower() in {"1", "true", "yes", "on"}
    return os.environ.get("ENVIRONMENT", "development").strip().lower() not in {"development", "dev", "local"}

pytestmark = pytest.mark.skipif(
    not ADMIN_PASSWORD,
    reason=(
        "ADMIN_PASSWORD is not set. Start the backend with ADMIN_EMAIL/ADMIN_PASSWORD "
        "configured to create the seeded admin account these tests log in as."
    ),
)


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def new_user_session():
    s = requests.Session()
    email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": "TestPass@123", "name": "Test User"},
               timeout=30)
    assert r.status_code == 200, f"Register failed: {r.status_code} {r.text}"
    uid = s.get(f"{API}/auth/me", timeout=15).json().get("id")
    yield s, email
    # The CRUD tests below create decisions against this user. Left behind, they
    # accumulate in the dev database every run.
    purge_user_data(uid)


@pytest.fixture(scope="module")
def chat_user_session():
    """A throwaway account for the LLM-backed chat tests.

    These tests post real chat messages, and running them as the shared admin account
    left every message behind in the database. Those rows then counted against admin's
    monthly ai_chats_per_month quota until /chat began returning 402 and the chat tests
    failed for a reason that had nothing to do with the model. A dedicated account lets
    the teardown remove every message the tests produced.
    """
    s = requests.Session()
    email = f"test_chat_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": "TestPass@123", "name": "Chat Tester"},
               timeout=30)
    assert r.status_code == 200, f"Register failed: {r.status_code} {r.text}"
    uid = s.get(f"{API}/auth/me", timeout=15).json().get("id")
    yield s
    purge_user_data(uid)


# ---------- Auth ----------
class TestAuth:
    def test_root(self):
        r = requests.get(f"{API}/", timeout=15)
        assert r.status_code == 200
        assert r.json().get("app") == "DecisionLens AI"

    def test_login_success_sets_cookies(self):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == ADMIN_EMAIL
        assert data["role"] == "admin"
        # cookies
        assert "access_token" in s.cookies.get_dict()
        assert "refresh_token" in s.cookies.get_dict()
        # check httpOnly via Set-Cookie header
        sc = r.headers.get("set-cookie", "")
        assert "HttpOnly" in sc

        # Regression guard: over plain http a `Secure` cookie is dropped by the
        # browser, so it must only be set when the deployment expects HTTPS.
        if not _cookie_secure_expected():
            assert "Secure" not in sc, f"Secure set in dev mode would break login: {sc}"

    def test_cookie_secure_flag_follows_environment(self, admin_session):
        r = requests.post(f"{API}/auth/login",
                          json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
        assert r.status_code == 200
        sc = r.headers.get("set-cookie", "")
        if _cookie_secure_expected():
            assert "Secure" in sc, f"Secure must be set in production: {sc}"

    def test_login_invalid(self):
        r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"}, timeout=15)
        assert r.status_code == 401

    def test_me_authenticated(self, admin_session):
        r = admin_session.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN_EMAIL

    def test_me_unauthenticated(self):
        r = requests.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 401

    def test_logout(self):
        s = requests.Session()
        s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
        r = s.post(f"{API}/auth/logout", timeout=15)
        assert r.status_code == 200
        # cookies cleared
        r2 = s.get(f"{API}/auth/me", timeout=15)
        assert r2.status_code == 401

    def test_register_new_user(self, new_user_session):
        s, email = new_user_session
        r = s.get(f"{API}/auth/me", timeout=15)
        assert r.status_code == 200
        assert r.json()["email"] == email
        # Registration no longer seeds decisions. Seeding wrote rows backdated 0-28 days,
        # so a random share fell inside the current quota window and consumed part of the
        # new account's decisions_per_month allowance before the user wrote anything.
        # A brand-new account must therefore start empty; the demo data belongs to the
        # admin and demo accounts only.
        r2 = s.get(f"{API}/decisions", timeout=15)
        assert r2.status_code == 200
        assert len(r2.json()) == 0


# ---------- Decisions CRUD ----------
class TestDecisions:
    def test_list_decisions(self, admin_session):
        r = admin_session.get(f"{API}/decisions", timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        assert len(items) >= 20
        # no mongo _id
        assert all("_id" not in d for d in items)
        for d in items[:5]:
            for k in ("id", "title", "category", "risk", "status", "confidence"):
                assert k in d

    def test_filter_by_status(self, admin_session):
        r = admin_session.get(f"{API}/decisions?status=active", timeout=15)
        assert r.status_code == 200
        assert all(d["status"] == "active" for d in r.json())

    def test_filter_by_risk(self, admin_session):
        r = admin_session.get(f"{API}/decisions?risk=high", timeout=15)
        assert r.status_code == 200
        assert all(d["risk"] == "high" for d in r.json())

    def test_create_get_update_delete(self, admin_session):
        payload = {
            "title": "TEST_decision_e2e",
            "description": "test",
            "category": "technology",
            "risk": "medium",
            "status": "active",
            "confidence": 75,
            "stakeholders": ["CEO", "CTO"],
            "deadline": "2026-12-31",
        }
        r = admin_session.post(f"{API}/decisions", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        created = r.json()
        did = created["id"]
        assert created["title"] == payload["title"]

        # GET single
        rg = admin_session.get(f"{API}/decisions/{did}", timeout=15)
        assert rg.status_code == 200
        assert rg.json()["title"] == payload["title"]

        # PATCH
        ru = admin_session.patch(f"{API}/decisions/{did}", json={"confidence": 90, "status": "complete"}, timeout=15)
        assert ru.status_code == 200
        assert ru.json()["confidence"] == 90
        assert ru.json()["status"] == "complete"

        # DELETE
        rd = admin_session.delete(f"{API}/decisions/{did}", timeout=15)
        assert rd.status_code == 200

        # GET 404 after delete
        rg2 = admin_session.get(f"{API}/decisions/{did}", timeout=15)
        assert rg2.status_code == 404

    def test_decisions_unauthenticated(self):
        r = requests.get(f"{API}/decisions", timeout=15)
        assert r.status_code == 401


# ---------- Dashboard ----------
class TestDashboard:
    def test_dashboard_summary(self, admin_session):
        r = admin_session.get(f"{API}/dashboard/summary", timeout=15)
        assert r.status_code == 200
        data = r.json()
        # KPIs
        kpis = data["kpis"]
        for k in ("total", "active", "completed", "confidence"):
            assert k in kpis
            assert "value" in kpis[k]
        assert kpis["total"]["value"] >= 20
        # Health
        h = data["health"]
        assert 0 <= h["score"] <= 100
        assert len(h["factors"]) == 4
        # Trends
        t = data["trends"]
        assert len(t["daily"]) == 30
        assert len(t["weekly"]) == 12
        assert len(t["monthly"]) == 12
        # Categories
        assert len(data["categories"]) == 6
        # Risk
        assert len(data["risk_distribution"]) == 4
        risks_keys = {r["risk"] for r in data["risk_distribution"]}
        assert risks_keys == {"low", "medium", "high", "critical"}
        # Recent
        assert len(data["recent_decisions"]) <= 6


# ---------- Recommendations ----------
class TestRecommendations:
    def test_recs(self, admin_session):
        r = admin_session.get(f"{API}/recommendations", timeout=15)
        assert r.status_code == 200
        recs = r.json()
        assert len(recs) >= 3
        for rec in recs:
            for k in ("id", "title", "body", "severity"):
                assert k in rec


# ---------- Sources ----------
class TestSources:
    def test_sources(self, admin_session):
        r = admin_session.get(f"{API}/sources", timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 6
        statuses = {s["status"] for s in items}
        assert "connected" in statuses


# ---------- Chat (Gemini) ----------
needs_llm = pytest.mark.skipif(
    LLM_KEY_CONFIGURED is False,
    reason="GEMINI_API_KEY is not set, so the LLM-backed happy path cannot run.",
)


class TestChat:
    def test_chat_without_llm_key_returns_503(self, admin_session):
        """Without a key the endpoint must fail loudly, never fake a 200 reply."""
        if LLM_KEY_CONFIGURED:
            pytest.skip("GEMINI_API_KEY is configured; unconfigured path not reachable.")
        r = admin_session.post(f"{API}/chat",
                               json={"message": "Give me a 1-line framework for choosing between two vendors."},
                               timeout=30)
        assert r.status_code == 503, r.text
        detail = r.json().get("detail", {})
        assert detail.get("code") == "llm_not_configured", r.text
        assert detail.get("message")

    @needs_llm
    def test_chat_first_message(self, chat_user_session):
        r = chat_user_session.post(f"{API}/chat",
                                   json={"message": "Give me a 1-line framework for choosing between two vendors."},
                                   timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("reply"), "Empty reply"
        assert len(data["reply"]) > 5
        assert data.get("session_id")
        # save for next test
        TestChat.session_id = data["session_id"]

    @needs_llm
    def test_chat_multi_turn(self, chat_user_session):
        sid = getattr(TestChat, "session_id", None)
        assert sid, "No session id from previous test"
        r = chat_user_session.post(f"{API}/chat",
                                   json={"session_id": sid, "message": "Now expand point 1 in 2 sentences."},
                                   timeout=90)
        assert r.status_code == 200
        assert r.json().get("reply")

        # history endpoint
        h = chat_user_session.get(f"{API}/chat/{sid}", timeout=15)
        assert h.status_code == 200
        msgs = h.json()
        assert len(msgs) >= 4  # 2 user + 2 assistant
