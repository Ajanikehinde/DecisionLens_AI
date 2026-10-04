"""Iteration 2 backend tests: CORS, comments, activity, attachments, PATCH, OAuth."""
import os
import io
import uuid
from pathlib import Path

import pytest
import requests

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

pytestmark = pytest.mark.skipif(
    not ADMIN_PASSWORD,
    reason=(
        "ADMIN_PASSWORD is not set. Start the backend with ADMIN_EMAIL/ADMIN_PASSWORD "
        "configured to create the seeded admin account these tests log in as."
    ),
)


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def admin_decision(admin_session):
    payload = {
        "title": "TEST_iter2_decision",
        "description": "for iter2 tests",
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
    # cleanup
    admin_session.delete(f"{API}/decisions/{did}")


# ---------------- CORS ----------------
# NOTE: these target the FastAPI backend directly because an ingress in front of the
# public URL may rewrite CORS headers to '*' (platform behaviour, outside the
# application). The backend itself must answer with an explicit allowed origin and
# allow_credentials=true.
#
# Override LOCAL_API_URL only when the backend runs on a different port than the rest
# of the suite; by default the CORS checks hit the same API under test.
LOCAL_API = os.environ.get("LOCAL_API_URL", "").strip().rstrip("/") or API

# The Origin a browser would actually send. This must be in the backend's
# allow_origins list, which is [FRONTEND_URL, "http://localhost:3000"].
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000").strip().rstrip("/")


class TestCORS:
    def test_cors_preflight_explicit_origin_with_credentials(self):
        r = requests.options(
            f"{LOCAL_API}/auth/me",
            headers={
                "Origin": FRONTEND_URL,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=15,
        )
        assert r.status_code in (200, 204), r.text
        assert r.headers.get("access-control-allow-credentials", "").lower() == "true"
        origin = r.headers.get("access-control-allow-origin", "")
        assert origin == FRONTEND_URL, f"Expected explicit origin, got {origin!r}"

    def test_cors_disallows_other_origins(self):
        r = requests.options(
            f"{LOCAL_API}/auth/me",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
            timeout=15,
        )
        # FastAPI CORS will simply omit ACAO for non-allowed origins
        origin = r.headers.get("access-control-allow-origin", "")
        assert origin != "https://evil.example.com"
        assert origin != "*"


# ---------------- PATCH ----------------
class TestPatchDecision:
    def test_patch_all_fields(self, admin_session, admin_decision):
        body = {
            "title": "TEST_iter2_decision_updated",
            "status": "review",
            "risk": "high",
            "category": "investment",
            "confidence": 88,
            "stakeholders": ["CEO", "CFO", "CTO"],
            "deadline": "2027-01-15",
        }
        r = admin_session.patch(f"{API}/decisions/{admin_decision}", json=body, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        for k, v in body.items():
            assert d[k] == v, f"{k}: {d[k]} != {v}"
        # verify persisted
        g = admin_session.get(f"{API}/decisions/{admin_decision}", timeout=15)
        for k, v in body.items():
            assert g.json()[k] == v


# ---------------- Comments + Activity ----------------
class TestComments:
    def test_post_and_list_comments(self, admin_session, admin_decision):
        r = admin_session.post(
            f"{API}/decisions/{admin_decision}/comments",
            json={"body": "First test comment"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        c = r.json()
        assert c["body"] == "First test comment"
        assert c["decision_id"] == admin_decision
        assert "id" in c and "_id" not in c

        r2 = admin_session.get(f"{API}/decisions/{admin_decision}/comments", timeout=15)
        assert r2.status_code == 200
        comments = r2.json()
        assert any(x["body"] == "First test comment" for x in comments)

    def test_activity_includes_created_and_comment(self, admin_session, admin_decision):
        r = admin_session.get(f"{API}/decisions/{admin_decision}/activity", timeout=15)
        assert r.status_code == 200
        acts = r.json()
        types = {a["type"] for a in acts}
        assert "created" in types
        assert "comment" in types  # from prior test


# ---------------- Attachments ----------------
class TestAttachments:
    def test_upload_txt(self, admin_session, admin_decision):
        files = {"file": ("test.txt", b"hello iteration 2 attachment", "text/plain")}
        r = admin_session.post(
            f"{API}/decisions/{admin_decision}/attachments",
            files=files, timeout=30,
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["filename"] == "test.txt"
        assert out["ext"] == ".txt"
        TestAttachments.txt_id = out["id"]

    def test_upload_md(self, admin_session, admin_decision):
        files = {"file": ("notes.md", b"# Heading\nBody", "text/markdown")}
        r = admin_session.post(
            f"{API}/decisions/{admin_decision}/attachments",
            files=files, timeout=30,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ext"] == ".md"

    def test_upload_exe_rejected(self, admin_session, admin_decision):
        files = {"file": ("evil.exe", b"MZ\x90\x00bad", "application/octet-stream")}
        r = admin_session.post(
            f"{API}/decisions/{admin_decision}/attachments",
            files=files, timeout=30,
        )
        assert r.status_code == 400, r.text

    def test_list_attachments(self, admin_session, admin_decision):
        r = admin_session.get(f"{API}/decisions/{admin_decision}/attachments", timeout=15)
        assert r.status_code == 200
        atts = r.json()
        names = {a["filename"] for a in atts}
        assert "test.txt" in names
        assert "notes.md" in names
        # no extracted_text in list
        assert all("extracted_text" not in a for a in atts)
        assert all("_id" not in a for a in atts)

    def test_delete_attachment(self, admin_session, admin_decision):
        att_id = getattr(TestAttachments, "txt_id", None)
        assert att_id
        r = admin_session.delete(
            f"{API}/decisions/{admin_decision}/attachments/{att_id}", timeout=15
        )
        assert r.status_code == 200
        # verify removal
        r2 = admin_session.get(f"{API}/decisions/{admin_decision}/attachments", timeout=15)
        ids = {a["id"] for a in r2.json()}
        assert att_id not in ids


# ---------------- OAuth (MOCK mode) ----------------
class TestOAuth:
    def test_salesforce_start_mock(self, admin_session):
        r = admin_session.get(f"{API}/oauth/salesforce/start", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("mock") is True
        assert "redirect_url" in d and "salesforce" in d["redirect_url"]

    def test_hubspot_start_mock(self, admin_session):
        r = admin_session.get(f"{API}/oauth/hubspot/start", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("mock") is True

    def test_connections_lists_connected(self, admin_session):
        r = admin_session.get(f"{API}/connections", timeout=15)
        assert r.status_code == 200
        conns = r.json()
        assert "salesforce" in conns
        assert conns["salesforce"]["status"] == "connected"
        assert conns["salesforce"].get("mock") is True
        assert "hubspot" in conns

    def test_salesforce_disconnect(self, admin_session):
        r = admin_session.post(f"{API}/oauth/salesforce/disconnect", timeout=15)
        assert r.status_code == 200
        r2 = admin_session.get(f"{API}/connections", timeout=15)
        assert r2.json()["salesforce"]["status"] == "disconnected"

    def test_oauth_unknown_provider(self, admin_session):
        r = admin_session.get(f"{API}/oauth/foobar/start", timeout=15)
        assert r.status_code == 404
