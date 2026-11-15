"""Phase 0 regression tests.

Covers the five defects closed in Phase 0:
  * analyses_per_month quota on /decisions/{id}/analyse
  * _get_active_tier failing closed on a missing or unreadable period end
  * the client-supplied checkout redirect being removed
  * cascade deletion of a decision's children and uploaded files
  * the webhook no longer losing a grant, and asking Stripe to retry when it cannot resolve

The tier-resolution tests seed subscription documents directly because the interesting
inputs - an epoch integer, a naive string, a corrupt string - cannot be produced through
the public API without a live Stripe subscription.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from conftest import purge_user_data

BACKEND_DIR = Path(__file__).resolve().parents[1]

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@decisionlens.ai").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
BASE_URL = (
    os.environ.get("REACT_APP_BACKEND_URL", "").strip().rstrip("/") or "http://localhost:8001"
)
API = f"{BASE_URL}/api"
UPLOADS = Path(os.environ.get("UPLOAD_DIR", "").strip() or (BACKEND_DIR.parent / "uploads"))

pytestmark = pytest.mark.skipif(
    not ADMIN_PASSWORD,
    reason=(
        "ADMIN_PASSWORD is not set. Start the backend with ADMIN_EMAIL/ADMIN_PASSWORD "
        "configured to create the seeded admin account these tests log in as."
    ),
)


@pytest.fixture(scope="module")
def mongo():
    from dotenv import load_dotenv
    from pymongo import MongoClient

    load_dotenv(BACKEND_DIR / ".env")
    url = os.environ.get("MONGO_URL", "").strip()
    name = os.environ.get("DB_NAME", "").strip()
    if not url or not name:
        pytest.skip("MONGO_URL/DB_NAME not set; cannot seed subscription rows.")
    c = MongoClient(url, serverSelectionTimeoutMS=8000)
    try:
        c.admin.command("ping")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"MongoDB unreachable: {exc}")
    return c[name]


@pytest.fixture
def user():
    s = requests.Session()
    email = f"TEST_p0_{uuid.uuid4().hex[:10]}@decisionlens.ai"
    r = s.post(
        f"{API}/auth/register",
        json={"email": email, "password": "Test@1234", "name": "P0 Tester"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    uid = s.get(f"{API}/billing/me", timeout=15).json()["user_id"]
    yield {"session": s, "user_id": uid, "email": email}
    purge_user_data(uid)


def set_sub(mongo, uid, **fields):
    mongo.subscriptions.update_one({"user_id": uid}, {"$set": {"user_id": uid, **fields}}, upsert=True)


def new_decision(session, title="P0 probe", category="business_strategy"):
    return session.post(
        f"{API}/decisions",
        json={
            "title": title,
            "description": "p0",
            "category": category,
            "risk": "medium",
            "status": "active",
            "confidence": 70,
        },
        timeout=30,
    )


def seed_analysis_activity(mongo, uid, count, decision_id="seeded"):
    """Insert the rows analyse_decision writes, so quota counting can be asserted."""
    now = datetime.now(timezone.utc).isoformat()
    mongo.activity.insert_many(
        [
            {
                "id": str(uuid.uuid4()),
                "decision_id": decision_id,
                "user_id": uid,
                "actor_name": "P0",
                "type": "analysis",
                "message": "AI analysis generated",
                "created_at": now,
            }
            for _ in range(count)
        ]
    )


class TestAnalyseQuota:
    def test_starter_is_capped_at_five_analyses(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        r = new_decision(s)
        assert r.status_code == 200
        did = r.json()["id"]
        seed_analysis_activity(mongo, uid, 5, did)

        blocked = s.post(f"{API}/decisions/{did}/analyse", timeout=60)
        # Requires a Gemini key to reach the quota check; without one the key check wins
        # and returns 503, which is the documented ordering.
        if blocked.status_code == 503 and blocked.json()["detail"].get("code") == "llm_not_configured":
            pytest.skip("GEMINI_API_KEY not set; quota check not reachable.")
        assert blocked.status_code == 402, blocked.text
        d = blocked.json()["detail"]
        assert d["code"] == "limit_exceeded"
        assert d["resource"] == "analyses_per_month"
        assert d["limit"] == 5
        assert d["used"] == 5
        assert d["upgrade_tier"] == "pro"

    def test_counts_only_analysis_activity(self, mongo, user):
        """activity is shared with field-change and comment events; those must not count."""
        s, uid = user["session"], user["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        seed_analysis_activity(mongo, uid, 2)
        mongo.activity.insert_many(
            [
                {
                    "id": str(uuid.uuid4()),
                    "decision_id": "seeded",
                    "user_id": uid,
                    "actor_name": "P0",
                    "type": "update_title",
                    "message": "Title changed",
                    "created_at": now,
                }
                for _ in range(10)
            ]
        )
        usage = s.get(f"{API}/billing/usage", timeout=30).json()["usage"]["analyses_per_month"]
        assert usage["used"] == 2, "non-analysis activity must not be billed as a model call"
        assert usage["limit"] == 5

    def test_pro_gets_a_higher_cap(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(
            mongo,
            uid,
            tier="pro",
            status="active",
            current_period_end=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        )
        seed_analysis_activity(mongo, uid, 10)
        usage = s.get(f"{API}/billing/usage", timeout=30).json()["usage"]["analyses_per_month"]
        assert usage["limit"] == 100
        assert usage["used"] == 10

    def test_team_is_unlimited(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(
            mongo,
            uid,
            tier="team",
            status="active",
            current_period_end=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        )
        seed_analysis_activity(mongo, uid, 50)
        usage = s.get(f"{API}/billing/usage", timeout=30).json()["usage"]["analyses_per_month"]
        assert usage["unlimited"] is True
        assert usage["remaining"] is None


class TestTierResolutionFailsClosed:
    """A missing or unreadable period end must never become an unlimited paid grant."""

    def _tier(self, session):
        return session.get(f"{API}/billing/me", timeout=15).json()

    def test_epoch_int_is_accepted(self, mongo, user):
        """Stripe sends current_period_end as epoch seconds; fromisoformat would reject it."""
        s, uid = user["session"], user["user_id"]
        epoch = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        set_sub(mongo, uid, tier="pro", status="active", current_period_end=epoch)
        body = self._tier(s)
        assert body["tier"] == "pro", body
        assert body["status"] == "active"

    def test_numeric_string_is_accepted(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        epoch = str(int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()))
        set_sub(mongo, uid, tier="pro", status="active", current_period_end=epoch)
        assert self._tier(s)["tier"] == "pro"

    def test_naive_iso_string_does_not_500(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        naive = (datetime.now(timezone.utc) + timedelta(days=30)).replace(tzinfo=None).isoformat()
        set_sub(mongo, uid, tier="pro", status="active", current_period_end=naive)
        r = s.get(f"{API}/billing/me", timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["tier"] == "pro"

    def test_missing_period_end_downgrades(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(mongo, uid, tier="pro", status="active")
        body = self._tier(s)
        assert body["tier"] == "starter"
        assert body["status"] == "invalid_period_end"

    def test_corrupt_period_end_downgrades(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(mongo, uid, tier="pro", status="active", current_period_end="not-a-date")
        assert self._tier(s)["tier"] == "starter"

    def test_empty_period_end_downgrades(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(mongo, uid, tier="pro", status="active", current_period_end="")
        assert self._tier(s)["tier"] == "starter"

    def test_unknown_status_downgrades(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(
            mongo,
            uid,
            tier="pro",
            status="totally_made_up",
            current_period_end=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        )
        assert self._tier(s)["tier"] == "starter"

    def test_canceled_downgrades(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(
            mongo,
            uid,
            tier="pro",
            status="canceled",
            current_period_end=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        )
        assert self._tier(s)["tier"] == "starter"

    def test_past_due_keeps_access(self, mongo, user):
        """A failed retry must not lock a paying customer out mid-cycle."""
        s, uid = user["session"], user["user_id"]
        set_sub(
            mongo,
            uid,
            tier="pro",
            status="past_due",
            current_period_end=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        )
        assert self._tier(s)["tier"] == "pro"

    def test_expired_period_downgrades(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        set_sub(
            mongo,
            uid,
            tier="pro",
            status="active",
            current_period_end=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        body = self._tier(s)
        assert body["tier"] == "starter"
        assert body["status"] == "expired"

    def test_downgraded_user_cannot_spend_paid_quota(self, mongo, user):
        """The fail-closed path must also gate writes, not just the billing page."""
        s, uid = user["session"], user["user_id"]
        set_sub(
            mongo,
            uid,
            tier="team",
            status="active",
        )  # no period end
        r = new_decision(s, "should be allowed on starter")
        assert r.status_code == 200
        usage = s.get(f"{API}/billing/usage", timeout=30).json()
        assert usage["tier"] == "starter"
        assert usage["usage"]["decisions_per_month"]["limit"] == 10


class TestCheckoutRedirect:
    def test_origin_url_is_no_longer_accepted(self, user):
        """An extra origin_url is ignored, so it cannot steer the Stripe redirect."""
        s = user["session"]
        r = s.post(
            f"{API}/billing/checkout",
            json={"tier": "pro", "origin_url": "https://attacker.example"},
            timeout=30,
        )
        # Without a Stripe key this is 500; with one it creates a real session. Either way
        # the request must not be rejected for an unknown field, and nothing may redirect
        # to the supplied origin.
        assert r.status_code in (200, 500), r.text
        if r.status_code == 200:
            assert "attacker.example" not in r.json().get("url", "")

    def test_checkout_still_requires_auth(self):
        r = requests.post(f"{API}/billing/checkout", json={"tier": "pro"}, timeout=20)
        assert r.status_code in (401, 403)


class TestCascadeDelete:
    def test_children_and_files_are_removed(self, mongo, user):
        s, uid = user["session"], user["user_id"]
        r = new_decision(s, "to be deleted")
        assert r.status_code == 200
        did = r.json()["id"]

        up = s.post(
            f"{API}/decisions/{did}/attachments",
            files={"file": ("notes.txt", b"cascade probe", "text/plain")},
            timeout=30,
        )
        assert up.status_code == 200, up.text
        att = up.json()
        stored = UPLOADS / att["stored_name"]

        c = s.post(
            f"{API}/decisions/{did}/comments", json={"body": "cascade probe"}, timeout=30
        )
        assert c.status_code == 200, c.text

        # A field change produces a non-analysis activity row of its own.
        p = s.patch(f"{API}/decisions/{did}", json={"status": "review"}, timeout=30)
        assert p.status_code == 200, p.text

        assert stored.is_file(), "upload should exist on disk before deletion"

        d = s.delete(f"{API}/decisions/{did}", timeout=30)
        assert d.status_code == 200, d.text

        assert mongo.decisions.count_documents({"id": did}) == 0
        assert mongo.comments.count_documents({"decision_id": did}) == 0
        assert mongo.activity.count_documents({"decision_id": did}) == 0
        assert mongo.attachments.count_documents({"decision_id": did}) == 0
        assert not stored.exists(), "uploaded file must be removed from disk"

    def test_deleting_one_leaves_another_intact(self, mongo, user):
        s = user["session"]
        keep = new_decision(s, "keep me").json()["id"]
        drop = new_decision(s, "drop me").json()["id"]
        assert s.delete(f"{API}/decisions/{drop}", timeout=30).status_code == 200
        assert mongo.decisions.count_documents({"id": keep}) == 1
        assert s.get(f"{API}/decisions/{keep}", timeout=15).status_code == 200

    def test_missing_decision_is_404(self, user):
        r = user["session"].delete(f"{API}/decisions/{uuid.uuid4()}", timeout=20)
        assert r.status_code == 404


class TestWebhookGrantSafety:
    def test_signature_required(self):
        """An unsigned payload must never be able to grant a tier."""
        r = requests.post(
            f"{API}/webhook/stripe",
            json={"type": "checkout.session.completed", "data": {"object": {"id": "cs_x"}}},
            timeout=20,
        )
        assert r.status_code in (400, 503), r.text
        assert r.status_code != 200

    def test_tx_row_exists_before_session_id_is_recorded(self, mongo, user):
        """The row is written under a chosen id so a fast webhook can always resolve it."""
        import stripe

        if not os.environ.get("STRIPE_API_KEY", "").strip():
            pytest.skip("STRIPE_API_KEY not set; cannot exercise checkout creation.")
        s, uid = user["session"], user["user_id"]
        r = s.post(f"{API}/billing/checkout", json={"tier": "pro"}, timeout=60)
        if r.status_code != 200:
            pytest.skip(f"checkout unavailable: {r.status_code}")
        sid = r.json()["session_id"]

        tx = mongo.payment_transactions.find_one({"session_id": sid})
        assert tx is not None, "transaction row must record the session id once created"
        # client_reference_id must carry the row id, which is the whole point of the fix.
        stripe.api_key = os.environ["STRIPE_API_KEY"]
        session = stripe.checkout.Session.retrieve(sid)
        assert session.client_reference_id == tx["id"]
        assert mongo.payment_transactions.count_documents(
            {"session_id": sid, "granted": True}
        ) == 0, "an unpaid session must not be marked granted"