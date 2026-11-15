"""Tier limit enforcement tests (Step 1).

These exercise POST /decisions, POST /chat and POST /decisions/{id}/attachments against
a running server, plus GET /billing/usage.

Chat and document limits are seeded straight into MongoDB rather than driven over HTTP.
Reaching the chat cap by HTTP would mean 20 real Gemini calls, and it would make the
test suite cost money and depend on GEMINI_API_KEY. Seeding also lets the
double-counting guard be asserted precisely: a turn writes two rows, so counting raw
documents would bill every message twice.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

from conftest import purge_user_data

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR / ".env")

from pymongo import MongoClient  # noqa: E402  (must follow load_dotenv)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@decisionlens.ai").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
MONGO_URL = os.environ.get("MONGO_URL", "").strip()
DB_NAME = os.environ.get("DB_NAME", "").strip()

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").strip().rstrip("/")
if not BASE_URL:
    BASE_URL = "http://localhost:8001"
API = f"{BASE_URL}/api"

pytestmark = pytest.mark.skipif(
    not ADMIN_PASSWORD,
    reason=(
        "ADMIN_PASSWORD is not set. Start the backend with ADMIN_EMAIL/ADMIN_PASSWORD "
        "configured to create the seeded admin account these tests log in as."
    ),
)


@pytest.fixture(scope="module")
def mongo():
    if not MONGO_URL or not DB_NAME:
        pytest.skip("MONGO_URL/DB_NAME not set; cannot seed usage rows.")
    c = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    try:
        c.admin.command("ping")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"MongoDB unreachable: {exc}")
    return c[DB_NAME]


@pytest.fixture
def user(mongo):
    """A fresh starter-tier user plus its client session, torn down afterwards."""
    email = f"TEST_limits_{uuid.uuid4().hex[:10]}@decisionlens.ai"
    s = requests.Session()
    r = s.post(
        f"{API}/auth/register",
        json={"email": email, "password": "Test@1234", "name": "Limits Tester"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    uid = s.get(f"{API}/billing/me", timeout=15).json()["user_id"]

    yield {"session": s, "user_id": uid, "email": email}

    purge_user_data(uid)


@pytest.fixture(scope="module")
def showcase():
    """The seeded admin account, which _limits_exempt exempts from every tier limit.

    Module-scoped and never purged: it is the seeded showcase account the rest of the suite
    authenticates as, so it has to survive the run intact.
    """
    s = requests.Session()
    r = s.post(
        f"{API}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    return s


def set_tier(mongo, uid: str, tier: str, days: int = 30) -> None:
    mongo.subscriptions.update_one(
        {"user_id": uid},
        {
            "$set": {
                "user_id": uid,
                "tier": tier,
                "status": "active",
                "current_period_end": (
                    datetime.now(timezone.utc) + timedelta(days=days)
                ).isoformat(),
            }
        },
        upsert=True,
    )


def new_decision(session, title="Limit probe"):
    return session.post(
        f"{API}/decisions",
        json={
            "title": title,
            "description": "limit probe",
            "category": "business_strategy",
            "risk": "medium",
            "status": "active",
            "confidence": 70,
        },
        timeout=30,
    )


class TestDecisionLimit:
    def test_starter_is_capped_at_10_per_month(self, user):
        s = user["session"]
        created = 0
        for i in range(10):
            r = new_decision(s, f"probe {i}")
            assert r.status_code == 200, r.text
            created += 1
        assert created == 10

        blocked = new_decision(s, "the eleventh")
        assert blocked.status_code == 402, blocked.text
        d = blocked.json()["detail"]
        assert d["code"] == "limit_exceeded"
        assert d["resource"] == "decisions_per_month"
        assert d["limit"] == 10
        assert d["used"] == 10
        assert d["reset_at"], "a monthly limit must report when it clears"
        assert d["upgrade_tier"] == "pro"

    def test_cap_does_not_block_reads(self, user):
        """Hitting the write cap must not lock the user out of their own data."""
        s = user["session"]
        r = new_decision(s)
        assert r.status_code == 200
        listed = s.get(f"{API}/decisions", timeout=30)
        assert listed.status_code == 200
        assert any(d["title"] == "Limit probe" for d in listed.json())


class TestChatCounting:
    def test_turn_counts_once_not_twice(self, mongo, user):
        """A turn writes a user row and an assistant row; only the user row may count."""
        s, uid = user["session"], user["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for _ in range(20):
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "session_id": "seeded",
                    "user_id": uid,
                    "role": "user",
                    "content": "seeded",
                    "created_at": now,
                }
            )
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "session_id": "seeded",
                    "user_id": uid,
                    "role": "assistant",
                    "content": "seeded",
                    "content_len": 0,
                    "created_at": now,
                }
            )
        mongo.chat_messages.insert_many(rows)
        assert mongo.chat_messages.count_documents({"user_id": uid}) == 40

        r = s.post(f"{API}/chat", json={"message": "hello", "session_id": "seeded"}, timeout=30)
        assert r.status_code == 402, r.text
        d = r.json()["detail"]
        assert d["resource"] == "ai_chats_per_month"
        # 40 rows exist but only 20 are user turns, so 20 is the correct count.
        assert d["used"] == 20, "assistant rows must not be billed to the user"

    def test_blocked_chat_persists_nothing(self, mongo, user):
        """A blocked request must not leave a user message behind."""
        s, uid = user["session"], user["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        # Distinct dicts: repeating one object would make pymongo stamp the same _id
        # onto all 20 and the insert would fail as a duplicate key.
        mongo.chat_messages.insert_many(
            [
                {
                    "id": str(uuid.uuid4()),
                    "session_id": "blocked",
                    "user_id": uid,
                    "role": "user",
                    "content": "seeded",
                    "created_at": now,
                }
                for _ in range(20)
            ]
        )
        before = mongo.chat_messages.count_documents(
            {"user_id": uid, "session_id": "blocked"}
        )
        r = s.post(f"{API}/chat", json={"message": "hi", "session_id": "blocked"}, timeout=30)
        assert r.status_code == 402, r.text
        after = mongo.chat_messages.count_documents(
            {"user_id": uid, "session_id": "blocked"}
        )
        assert after == before, "quota check must precede the user-message insert"


class TestDocumentLimit:
    def test_starter_is_capped_at_5_documents(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        r = new_decision(s)
        assert r.status_code == 200
        did = r.json()["id"]

        now = datetime.now(timezone.utc).isoformat()
        mongo.attachments.insert_many(
            [
                {
                    "id": str(uuid.uuid4()),
                    "decision_id": did,
                    "user_id": uid,
                    "filename": f"seeded{i}.txt",
                    "stored_name": f"seeded{i}.txt",
                    "size": 1,
                    "ext": ".txt",
                    "created_at": now,
                }
                for i in range(5)
            ]
        )

        up = s.post(
            f"{API}/decisions/{did}/attachments",
            files={"file": ("sixth.txt", b"hello world", "text/plain")},
            timeout=30,
        )
        assert up.status_code == 402, up.text
        d = up.json()["detail"]
        assert d["resource"] == "documents"
        assert d["limit"] == 5
        assert d["used"] == 5


class TestUnlimitedSentinel:
    def test_team_tier_is_not_blocked(self, mongo, user):
        """-1 means unlimited and must never raise, even far past the starter cap."""
        s, uid = user["session"], user["user_id"]
        set_tier(mongo, uid, "team")
        now = datetime.now(timezone.utc).isoformat()
        mongo.decisions.insert_many(
            [
                {
                    "id": str(uuid.uuid4()),
                    "user_id": uid,
                    "title": f"bulk {i}",
                    "created_at": now,
                }
                for i in range(25)
            ]
        )
        r = new_decision(s, "past any starter cap")
        assert r.status_code == 200, r.text

    def test_pro_gets_a_higher_cap(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_tier(mongo, uid, "pro")
        now = datetime.now(timezone.utc).isoformat()
        mongo.decisions.insert_many(
            [
                {"id": str(uuid.uuid4()), "user_id": uid, "title": f"p {i}", "created_at": now}
                for i in range(10)
            ]
        )
        # 10 used equals the starter cap but is under Pro's 200.
        assert new_decision(s, "allowed on pro").status_code == 200


class TestFreshAccountHasNoQuotaUsed:
    """Regression guard for registration-time seeding.

    Seeding backdated rows across 0-28 days put a random share of them inside the current
    month window, so new signups began life with part of decisions_per_month already
    consumed and some started over the cap entirely. A brand-new account must report 0.
    """

    def test_new_account_starts_at_zero(self, user):
        r = user["session"].get(f"{API}/billing/usage", timeout=30)
        assert r.status_code == 200, r.text
        usage = r.json()["usage"]
        assert usage["decisions_per_month"]["used"] == 0
        assert usage["ai_chats_per_month"]["used"] == 0
        assert usage["documents"]["used"] == 0
        assert usage["decisions_per_month"]["remaining"] == 10

    def test_first_decision_counts_one(self, user):
        assert new_decision(user["session"]).status_code == 200
        usage = user["session"].get(f"{API}/billing/usage", timeout=30).json()["usage"]
        assert usage["decisions_per_month"]["used"] == 1
        assert usage["decisions_per_month"]["remaining"] == 9


class TestUsageEndpoint:
    def test_shape(self, user):
        s = user["session"]
        new_decision(s)
        r = s.get(f"{API}/billing/usage", timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tier"] == "starter"
        assert body["upgrade_tier"] == "pro"
        for key in ("decisions_per_month", "ai_chats_per_month", "documents"):
            entry = body["usage"][key]
            assert set(entry) == {"limit", "used", "unlimited", "remaining", "reset_at"}
            assert entry["used"] >= 0
        assert body["usage"]["decisions_per_month"]["used"] == 1
        assert body["usage"]["ai_chats_per_month"]["used"] == 0
        assert body["usage"]["decisions_per_month"]["limit"] == 10
        assert body["usage"]["decisions_per_month"]["remaining"] == 9

    def test_requires_auth(self):
        r = requests.get(f"{API}/billing/usage", timeout=15)
        assert r.status_code in (401, 403)


class TestExemptAccountReporting:
    """The usage payload must describe the same limits the API enforces.

    admin and demo are exempt, so _enforce_limit returns before counting anything. The
    endpoint flagged them `unlimited` but still reported the starter tier's cap, so a
    showcase account already holding more decisions than the cap was described as over a
    live limit, with a reset date, for an account nothing can block.
    """

    def test_no_cap_is_reported_for_an_exempt_account(self, mongo, showcase):
        uid = showcase.get(f"{API}/billing/me", timeout=15).json()["user_id"]
        # The showcase data is backdated across 0-28 days, so how much of it lands inside
        # the current quota window varies run to run. These rows are seeded directly so the
        # "already past the cap" precondition is deterministic rather than lucky.
        now = datetime.now(timezone.utc).isoformat()
        ids = [str(uuid.uuid4()) for _ in range(12)]
        mongo.decisions.insert_many(
            [
                {"id": i, "user_id": uid, "title": f"exempt probe {i}", "created_at": now}
                for i in ids
            ]
        )
        try:
            r = showcase.get(f"{API}/billing/usage", timeout=30)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["tier"] == "starter", "precondition: the account is on a capped tier"
            assert body["usage"]["decisions_per_month"]["used"] >= 12, (
                "precondition: more decisions used than the starter cap of 10"
            )

            for key in ("decisions_per_month", "ai_chats_per_month", "analyses_per_month", "documents"):
                entry = body["usage"][key]
                assert entry["unlimited"] is True, key
                # -1, the sentinel Team uses, so `unlimited` agrees with `limit`.
                assert entry["limit"] == -1, key
                assert entry["remaining"] is None, key
            assert body["upgrade_tier"] is None
        finally:
            mongo.decisions.delete_many({"id": {"$in": ids}})

    def test_writes_past_the_starter_cap_still_succeed(self, showcase):
        created = new_decision(showcase, "showcase probe past the cap")
        assert created.status_code == 200, created.text
        showcase.delete(f"{API}/decisions/{created.json()['id']}", timeout=30)