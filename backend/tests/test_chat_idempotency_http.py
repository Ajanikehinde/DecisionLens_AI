"""HTTP-level tests for Stage 2 idempotency.

These go through real routing, auth, Pydantic validation and response serialisation, which
the in-process suites deliberately cannot cover. They never call Gemini: every ``POST /chat``
here resolves against a pre-seeded row (replay, conflict, in-flight, exhausted), and all of
those paths return before dispatch. The ``no live provider`` guarantee therefore comes from
the payloads, not from a mock.
"""
import os
import uuid

import pytest
import requests

from conftest import get_db, purge_user_data

BASE_URL = (
    os.environ.get("REACT_APP_BACKEND_URL", "").strip().rstrip("/")
    or "http://localhost:8001"
)
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def user_session():
    s = requests.Session()
    email = f"test_idem_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(
        f"{API}/auth/register",
        json={"email": email, "password": "TestPass@123", "name": "Idempotency Tester"},
        timeout=30,
    )
    assert r.status_code == 200, f"Register failed: {r.status_code} {r.text}"
    uid = s.get(f"{API}/auth/me", timeout=15).json()["id"]
    yield s, uid
    purge_user_data(uid)


@pytest.fixture
def db():
    handle = get_db()
    if handle is None:
        raise RuntimeError("MongoDB is unreachable")
    return handle


def _ts(offset_seconds=0):
    from datetime import datetime, timezone, timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _seed(db, user_id, *, content="question", status="answered", request_id=None,
          attempt=1, reply=None, session_id=None, status_age_s=0, error_code=None,
          requested_session_id=None):
    """Seed one turn directly, so a POST can resolve without dispatching.

    ``requested_session_id`` defaults to None because that is what a client sends on its
    first message; the conflict check compares against that, not against the generated id.
    """
    session_id = session_id or f"ht_{uuid.uuid4().hex[:10]}"
    rid = request_id or str(uuid.uuid4())
    doc = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "requested_session_id": requested_session_id,
        "user_id": user_id,
        "role": "user",
        "content": content,
        "created_at": _ts(),
        "status": status,
        "status_updated_at": _ts(status_age_s),
        "attempt": attempt,
        "request_id": rid,
    }
    if reply is not None:
        reply_id = str(uuid.uuid4())
        db.chat_messages.insert_one(
            {
                "id": reply_id,
                "session_id": session_id,
                "user_id": user_id,
                "role": "assistant",
                "content": reply,
                "created_at": _ts(),
            }
        )
        doc["reply_message_id"] = reply_id
    if error_code:
        doc["error_code"] = error_code
    db.chat_messages.insert_one(doc)
    return rid, session_id


class TestLookupOverHttp:
    def test_answered_request_returns_its_session_and_reply(self, user_session, db):
        s, uid = user_session
        rid, session_id = _seed(db, uid, reply="stored answer")

        r = s.get(f"{API}/chat/requests/{rid}", timeout=15)

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["request_id"] == rid
        assert body["session_id"] == session_id
        assert body["status"] == "answered"
        assert body["reply"] == "stored answer"
        assert body["attempt"] == 1
        assert body["can_retry"] is False

    def test_in_flight_request_reports_no_reply(self, user_session, db):
        s, uid = user_session
        rid, _ = _seed(db, uid, status="processing")

        r = s.get(f"{API}/chat/requests/{rid}", timeout=15)

        assert r.status_code == 200
        assert r.json()["status"] == "processing"
        assert r.json()["reply"] is None

    def test_unknown_request_is_404(self, user_session):
        s, _ = user_session
        r = s.get(f"{API}/chat/requests/{uuid.uuid4()}", timeout=15)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "request_not_found"

    def test_another_users_request_is_404(self, user_session, db):
        s, _ = user_session
        foreign = f"foreign_{uuid.uuid4().hex[:10]}"
        rid, _ = _seed(db, foreign, reply="not yours")
        try:
            r = s.get(f"{API}/chat/requests/{rid}", timeout=15)
            assert r.status_code == 404
            assert r.json()["detail"]["code"] == "request_not_found"
        finally:
            db.chat_messages.delete_many({"user_id": foreign})

    def test_lookup_requires_authentication(self, db):
        anon = f"anon_{uuid.uuid4().hex[:10]}"
        rid, _ = _seed(db, anon, reply="secret")
        try:
            r = requests.get(f"{API}/chat/requests/{rid}", timeout=15)
            assert r.status_code == 401
        finally:
            db.chat_messages.delete_many({"user_id": anon})


class TestPostOverHttp:
    def test_repeat_of_an_answered_request_replays_without_dispatch(self, user_session, db):
        """No Gemini key is needed for this POST: the answer is already stored."""
        s, uid = user_session
        rid, session_id = _seed(db, uid, reply="replayed answer")

        r = s.post(
            f"{API}/chat", json={"message": "question", "request_id": rid}, timeout=20
        )

        assert r.status_code == 200, r.text
        assert set(r.json()) == {"session_id", "reply"}, "success contract must be unchanged"
        assert r.json()["session_id"] == session_id
        assert r.json()["reply"] == "replayed answer"
        # Still exactly one user message: a replay never inserts.
        assert db.chat_messages.count_documents(
            {"user_id": uid, "role": "user", "request_id": rid}
        ) == 1

    def test_conflicting_payload_returns_409(self, user_session, db):
        s, uid = user_session
        rid, _ = _seed(db, uid, content="original", reply="answer")

        r = s.post(
            f"{API}/chat", json={"message": "different", "request_id": rid}, timeout=20
        )

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "idempotency_conflict"

    def test_in_flight_request_returns_409_with_a_lookup_hint(self, user_session, db):
        s, uid = user_session
        rid, session_id = _seed(db, uid, status="processing", status_age_s=0)

        r = s.post(
            f"{API}/chat", json={"message": "question", "request_id": rid}, timeout=20
        )

        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "request_in_flight"
        assert detail["session_id"] == session_id
        assert detail["retry_after_ms"] > 0
        assert detail["lookup"] == f"/api/chat/requests/{rid}"

    def test_exhausted_retries_return_409(self, user_session, db):
        s, uid = user_session
        rid, _ = _seed(db, uid, status="failed", attempt=3, error_code="llm_unavailable")

        r = s.post(
            f"{API}/chat", json={"message": "question", "request_id": rid}, timeout=20
        )

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "retry_exhausted"
        assert r.json()["detail"]["attempt"] == 3

    @pytest.mark.parametrize("bad", ["not-a-uuid", "12345"])
    def test_malformed_request_id_is_rejected_with_422(self, user_session, bad):
        s, _ = user_session
        r = s.post(f"{API}/chat", json={"message": "hi", "request_id": bad}, timeout=15)
        assert r.status_code == 422


class TestIndexOnTheLiveServer:
    def test_unique_index_exists_in_the_running_test_database(self, db):
        """Proves startup created and verified the constraint, not just the test harness."""
        spec = db.chat_messages.index_information()["uniq_chat_request_per_user"]
        assert spec["unique"] is True
        assert dict(spec["key"]) == {"user_id": 1, "request_id": 1}
        assert spec.get("partialFilterExpression")
