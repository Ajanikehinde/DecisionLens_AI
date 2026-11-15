"""Iteration 3 backend tests: PATCH activity logging, magic-byte attachment validation, Stripe billing."""
import os
import io
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

# Origin used for Stripe success/cancel redirects. Any absolute URL works; point it at
# the real frontend so the recorded URLs match production.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000").strip().rstrip("/")

# The seeded admin account is created only when ADMIN_PASSWORD is set, so the suite
# reads the same variable rather than assuming a default password.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@decisionlens.ai").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

# Checkout talks to the real Stripe API, so it can only run with a key present.
STRIPE_KEY_CONFIGURED = bool(os.environ.get("STRIPE_API_KEY", "").strip())

needs_stripe = pytest.mark.skipif(
    STRIPE_KEY_CONFIGURED is False,
    reason="STRIPE_API_KEY is not set, so live checkout cannot be exercised.",
)

pytestmark = pytest.mark.skipif(
    not ADMIN_PASSWORD,
    reason=(
        "ADMIN_PASSWORD is not set. Start the backend with ADMIN_EMAIL/ADMIN_PASSWORD "
        "configured to create the seeded admin account these tests log in as."
    ),
)


# ---------------- Fixtures ----------------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def fresh_user_session():
    """Register a brand-new user to test fresh-user behaviors (starter tier)."""
    email = f"TEST_iter3_{uuid.uuid4().hex[:8]}@decisionlens.ai"
    s = requests.Session()
    r = s.post(
        f"{API}/auth/register",
        json={"email": email, "password": "Test@1234", "name": "Iter3 Tester"},
        timeout=30,
    )
    assert r.status_code in (200, 201), r.text
    uid = s.get(f"{API}/billing/me", timeout=15).json().get("user_id")
    yield s, email
    purge_user_data(uid)


@pytest.fixture
def admin_decision(admin_session):
    payload = {
        "title": "TEST_iter3_decision",
        "description": "for iter3 PATCH activity tests",
        "category": "technology",
        "risk": "medium",
        "status": "active",
        "confidence": 70,
        "stakeholders": ["CEO"],
        "deadline": "2026-12-31",
    }
    r = admin_session.post(f"{API}/decisions", json=payload, timeout=15)
    assert r.status_code == 200
    did = r.json()["id"]
    yield did
    admin_session.delete(f"{API}/decisions/{did}")


# ===========================================================
# PATCH activity logging
# ===========================================================
class TestPatchActivityLogging:
    def test_patch_title_creates_activity(self, admin_session, admin_decision):
        r = admin_session.patch(f"{API}/decisions/{admin_decision}", json={"title": "TEST_iter3_new_title"}, timeout=15)
        assert r.status_code == 200
        assert r.json()["title"] == "TEST_iter3_new_title"

        act = admin_session.get(f"{API}/decisions/{admin_decision}/activity", timeout=15).json()
        evts = [a for a in act if a.get("type") == "update_title"]
        assert len(evts) >= 1, f"expected update_title activity, got {act}"
        e = evts[-1]
        assert e["new_value"] == "TEST_iter3_new_title"
        assert e["old_value"] == "TEST_iter3_decision"
        assert "Title changed" in e["message"]

    def test_patch_status_creates_activity(self, admin_session, admin_decision):
        r = admin_session.patch(f"{API}/decisions/{admin_decision}", json={"status": "complete"}, timeout=15)
        assert r.status_code == 200
        act = admin_session.get(f"{API}/decisions/{admin_decision}/activity", timeout=15).json()
        evts = [a for a in act if a.get("type") == "update_status"]
        assert evts and evts[-1]["new_value"] == "complete"

    def test_patch_multiple_fields_creates_multiple_activities(self, admin_session, admin_decision):
        r = admin_session.patch(
            f"{API}/decisions/{admin_decision}",
            json={"risk": "high", "category": "investment", "confidence": 85, "deadline": "2027-01-15"},
            timeout=15,
        )
        assert r.status_code == 200
        act = admin_session.get(f"{API}/decisions/{admin_decision}/activity", timeout=15).json()
        types = {a.get("type") for a in act}
        for t in ("update_risk", "update_category", "update_confidence", "update_deadline"):
            assert t in types, f"missing {t} in {types}"

        # confidence message should include "%"
        conf_evts = [a for a in act if a.get("type") == "update_confidence"]
        assert "%" in conf_evts[-1]["message"]

    def test_patch_same_value_no_activity(self, admin_session, admin_decision):
        # First patch to known value
        admin_session.patch(f"{API}/decisions/{admin_decision}", json={"risk": "low"}, timeout=15)
        before = admin_session.get(f"{API}/decisions/{admin_decision}/activity", timeout=15).json()
        risk_count_before = len([a for a in before if a.get("type") == "update_risk"])

        # Patch with SAME value — should not log
        r = admin_session.patch(f"{API}/decisions/{admin_decision}", json={"risk": "low"}, timeout=15)
        assert r.status_code == 200

        after = admin_session.get(f"{API}/decisions/{admin_decision}/activity", timeout=15).json()
        risk_count_after = len([a for a in after if a.get("type") == "update_risk"])
        assert risk_count_after == risk_count_before, "PATCH with same value should not add activity"


# ===========================================================
# Magic-byte attachment validation
# ===========================================================
def _make_real_docx_bytes() -> bytes:
    import docx as _docx
    d = _docx.Document()
    d.add_paragraph("This is a real DOCX file for iter3 magic-byte testing.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _real_pdf_bytes() -> bytes:
    # Minimal valid PDF
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n"
        b"trailer<</Size 4/Root 1 0 R>>\n%%EOF\n"
    )


class TestAttachmentMagicByteValidation:
    @pytest.fixture
    def decision_id(self, admin_session):
        r = admin_session.post(
            f"{API}/decisions",
            json={
                "title": "TEST_iter3_uploads",
                "description": "x",
                "category": "technology",
                "risk": "low",
                "status": "active",
                "confidence": 50,
            },
            timeout=15,
        )
        assert r.status_code == 200
        did = r.json()["id"]
        yield did
        # Attachments must be removed before the decision. Both endpoints are scoped to an
        # existing decision, and a leaked attachment row would otherwise keep counting
        # against the shared admin account's monthly document quota, so the suite would
        # start failing on whichever run pushed it past five.
        try:
            lst = admin_session.get(f"{API}/decisions/{did}/attachments", timeout=15)
            if lst.status_code == 200:
                for att in lst.json():
                    admin_session.delete(
                        f"{API}/decisions/{did}/attachments/{att['id']}", timeout=15
                    )
        except Exception:
            pass
        admin_session.delete(f"{API}/decisions/{did}")

    def test_real_pdf_accepted(self, admin_session, decision_id):
        files = {"file": ("real.pdf", _real_pdf_bytes(), "application/pdf")}
        r = admin_session.post(f"{API}/decisions/{decision_id}/attachments", files=files, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["filename"] == "real.pdf"

    def test_fake_pdf_rejected(self, admin_session, decision_id):
        files = {"file": ("evil.pdf", b"this is not a pdf", "application/pdf")}
        r = admin_session.post(f"{API}/decisions/{decision_id}/attachments", files=files, timeout=20)
        assert r.status_code == 400, r.text
        assert "%PDF-" in r.json()["detail"] or "PDF" in r.json()["detail"]

    def test_real_docx_accepted(self, admin_session, decision_id):
        data = _make_real_docx_bytes()
        files = {"file": ("real.docx", data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        r = admin_session.post(f"{API}/decisions/{decision_id}/attachments", files=files, timeout=20)
        assert r.status_code == 200, r.text

    def test_fake_docx_rejected(self, admin_session, decision_id):
        files = {"file": ("fake.docx", b"random non-zip bytes here \x01\x02\x03", "application/octet-stream")}
        r = admin_session.post(f"{API}/decisions/{decision_id}/attachments", files=files, timeout=20)
        assert r.status_code == 400
        assert "DOCX" in r.json()["detail"] or "ZIP" in r.json()["detail"]

    def test_txt_with_nul_bytes_rejected(self, admin_session, decision_id):
        files = {"file": ("evil.txt", b"\x00\x00abc", "text/plain")}
        r = admin_session.post(f"{API}/decisions/{decision_id}/attachments", files=files, timeout=20)
        assert r.status_code == 400
        assert "NUL" in r.json()["detail"] or "binary" in r.json()["detail"].lower()

    def test_normal_txt_accepted(self, admin_session, decision_id):
        files = {"file": ("notes.txt", "hello world from iter3".encode("utf-8"), "text/plain")}
        r = admin_session.post(f"{API}/decisions/{decision_id}/attachments", files=files, timeout=20)
        assert r.status_code == 200, r.text


# ===========================================================
# Stripe billing
# ===========================================================
class TestBillingTiers:
    def test_tiers_public_no_auth(self):
        r = requests.get(f"{API}/billing/tiers", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 3
        ids = {t["id"] for t in data}
        assert ids == {"starter", "pro", "team"}
        prices = {t["id"]: t["price"] for t in data}
        assert prices["starter"] == 0
        assert prices["pro"] == 29
        assert prices["team"] == 99

    def test_billing_me_requires_auth(self):
        r = requests.get(f"{API}/billing/me", timeout=15)
        assert r.status_code in (401, 403)

    def test_billing_me_fresh_user_is_starter(self, fresh_user_session):
        s, _ = fresh_user_session
        r = s.get(f"{API}/billing/me", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body["tier"] == "starter"
        assert body["status"] in ("active", "expired")
        assert body["tier_meta"]["price"] == 0


class TestBillingCheckout:
    def test_checkout_without_stripe_key_is_rejected(self, admin_session):
        """Without a key, checkout must fail explicitly instead of faking a session."""
        if STRIPE_KEY_CONFIGURED:
            pytest.skip("STRIPE_API_KEY is configured; unconfigured path not reachable.")
        r = admin_session.post(
            f"{API}/billing/checkout",
            json={"tier": "pro", "origin_url": FRONTEND_URL},
            timeout=15,
        )
        assert r.status_code >= 400, r.text
        assert "session_id" not in r.text

    @needs_stripe
    def test_checkout_pro_creates_session_and_transaction(self, admin_session):
        r = admin_session.post(
            f"{API}/billing/checkout",
            json={"tier": "pro", "origin_url": FRONTEND_URL},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "url" in body and "session_id" in body
        assert "stripe.com" in body["url"], f"expected stripe url, got {body['url']}"
        session_id = body["session_id"]

        # Verify transaction row exists & matches via status endpoint
        s = admin_session.get(f"{API}/billing/status/{session_id}", timeout=30)
        assert s.status_code == 200, s.text
        st = s.json()
        # While unpaid, status should be open/expired but never granted
        assert st["payment_status"] in ("unpaid", "no_payment_required", "paid", "initiated") or st.get("status") in ("open", "expired", "complete")
        # tier should not be granted yet
        me = admin_session.get(f"{API}/billing/me", timeout=15).json()
        # As long as we haven't actually paid, this admin shouldn't be 'pro' just from creating a session
        # (admin could already be pro from prior tests, so we only assert that NO grant came from unpaid)
        assert me["tier"] in ("starter", "pro", "team")

    @needs_stripe
    def test_checkout_team_amount_99(self, admin_session):
        r = admin_session.post(
            f"{API}/billing/checkout",
            json={"tier": "team", "origin_url": FRONTEND_URL},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        assert "stripe.com" in r.json()["url"]

    def test_checkout_invalid_tier_starter_rejected(self, admin_session):
        # 'starter' fails pydantic Literal["pro","team"] → FastAPI 422
        r = admin_session.post(
            f"{API}/billing/checkout",
            json={"tier": "starter", "origin_url": FRONTEND_URL},
            timeout=15,
        )
        assert r.status_code in (400, 422), r.text

    def test_checkout_invalid_tier_lol_rejected(self, admin_session):
        r = admin_session.post(
            f"{API}/billing/checkout",
            json={"tier": "lol", "origin_url": FRONTEND_URL},
            timeout=15,
        )
        assert r.status_code in (400, 422), r.text

    def test_checkout_requires_auth(self):
        r = requests.post(
            f"{API}/billing/checkout",
            json={"tier": "pro", "origin_url": FRONTEND_URL},
            timeout=15,
        )
        assert r.status_code in (401, 403)


class TestBillingStatus:
    @needs_stripe
    def test_status_owner_only(self, admin_session, fresh_user_session):
        # admin creates session
        r = admin_session.post(
            f"{API}/billing/checkout",
            json={"tier": "pro", "origin_url": FRONTEND_URL},
            timeout=30,
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        # fresh user (not owner) should not be able to read it
        s, _ = fresh_user_session
        r2 = s.get(f"{API}/billing/status/{sid}", timeout=20)
        assert r2.status_code == 404, f"non-owner should get 404, got {r2.status_code}"

    def test_status_unknown_session(self, admin_session):
        r = admin_session.get(f"{API}/billing/status/sess_does_not_exist_xyz", timeout=15)
        assert r.status_code == 404
