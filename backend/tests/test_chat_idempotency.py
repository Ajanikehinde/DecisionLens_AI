"""Stage 2 chat idempotency tests.

Deterministic and offline: the harness in ``chat_harness.py`` gives every test its own event
loop, Motor client, throwaway user and a patched ``server.db``, and patches
``server.requests.post`` so any real provider call fails the test instead of spending Gemini
credit. Every test also runs against the real unique index, created on the disposable test
database by the ``env`` fixture.

The behaviour under test is the promise that a client may resend the same ``request_id``
after a dropped connection and get the stored answer back, without a second user message, a
second Gemini call, or a second quota charge.
"""
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from chat_harness import env, mongo, no_live_provider  # noqa: F401  (fixtures)

import server  # noqa: E402  (chat_harness puts backend/ on sys.path)


@pytest.fixture(autouse=True)
def default_provider(monkeypatch):
    """Deterministic happy-path provider for tests that are not about provider behaviour.

    ``no_live_provider`` in the harness remains the hard guard: an unpatched provider call
    still raises, so this only supplies a canned answer where none is under test.
    """
    monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "canned answer")


def _rid():
    return uuid.uuid4()


def _boom(*args, **kwargs):
    """A provider call that must never happen on an idempotent path."""
    raise AssertionError("the provider was called; an idempotent path must not dispatch")


def _old_stamp(minutes=10):
    return (server.now_utc() - timedelta(minutes=minutes)).isoformat()


# ----------------- identity and storage -----------------
class TestRequestIdentity:
    def test_new_request_stores_identity_and_keeps_the_response_shape(self, env):
        rid = _rid()
        body = env.chat("first question", request_id=str(rid))

        # Requirement 9: the success contract is unchanged for every client.
        assert set(body) == {"session_id", "reply"}

        row = env.single_user_row()
        assert row["request_id"] == str(rid)
        assert row["attempt"] == 1
        assert row["status"] == "answered"
        assert row["requested_session_id"] is None, "a first request sends no session id"
        assert row["reply_message_id"]

    def test_legacy_clients_without_a_request_id_still_work(self, env, mongo):
        """Two id-less messages must not collide: that is what the partial index buys."""
        first = env.chat("no key one")
        second = env.chat("no key two")

        assert first["reply"] and second["reply"]
        rows = list(
            mongo.chat_messages.find({"user_id": env.user["id"], "role": "user"})
        )
        assert len(rows) == 2
        assert all("request_id" not in r for r in rows)

    @pytest.mark.parametrize("bad", ["", "not-a-uuid", "12345", "  "])
    def test_malformed_request_id_is_rejected_not_ignored(self, bad):
        """Silently treating junk as 'no key' would disable idempotency for that client."""
        with pytest.raises(ValidationError):
            server.ChatIn(message="hi", request_id=bad)


# ----------------- replay of completed turns -----------------
class TestReplay:
    def test_repeat_of_an_answered_request_replays_without_dispatching(
        self, env, monkeypatch
    ):
        rid = _rid()
        calls = []

        def first(contents):
            calls.append(contents)
            return "the stored answer"

        monkeypatch.setattr(server, "_call_gemini_sync", first)
        original = env.chat("question", request_id=str(rid))
        assert len(calls) == 1

        # Any further dispatch is now a hard failure, so a 200 here proves no call.
        monkeypatch.setattr(server, "_call_gemini_sync", _boom)
        replay = env.chat("question", request_id=str(rid))

        assert replay == original, "a replay must be byte-identical to the original response"
        assert len(calls) == 1
        assert len(env.all_user_rows()) == 1, "no second user message may be created"
        assert len(env.assistant_rows(original["session_id"])) == 1

    def test_repeat_replays_before_the_quota_check(self, env, monkeypatch):
        """An answer already paid for must replay at the cap, not 402."""
        rid = _rid()
        env.chat("question", request_id=str(rid))

        charged = []

        async def refuse(resource, user_id, role=None):
            charged.append(resource)
            raise HTTPException(
                status_code=402,
                detail={"code": "limit_exceeded", "resource": resource},
            )

        monkeypatch.setattr(server, "_enforce_limit", refuse)

        replay = env.chat("question", request_id=str(rid))
        assert replay["reply"], "the stored answer must still be returned"
        assert charged == [], "a repeat must not consume quota"

        # ...while a genuinely new message is still blocked, so the policy is intact.
        with pytest.raises(HTTPException) as exc:
            env.chat("a different question")
        assert exc.value.status_code == 402

    def test_repeat_with_different_content_conflicts(self, env, monkeypatch):
        rid = _rid()
        env.chat("original", request_id=str(rid))
        monkeypatch.setattr(server, "_call_gemini_sync", _boom)

        with pytest.raises(HTTPException) as exc:
            env.chat("something else", request_id=str(rid))

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "idempotency_conflict"
        assert len(env.all_user_rows()) == 1

    def test_repeat_with_different_session_conflicts(self, env, monkeypatch):
        """Replaying session A's answer into session B would corrupt B's history."""
        rid = _rid()
        env.chat("question", session_id="session-a", request_id=str(rid))
        monkeypatch.setattr(server, "_call_gemini_sync", _boom)

        with pytest.raises(HTTPException) as exc:
            env.chat("question", session_id="session-b", request_id=str(rid))

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "idempotency_conflict"

    def test_interrupted_first_request_recovers_its_generated_session(self, env, monkeypatch):
        """Requirement 7: the client never saw the generated id, so it retries with null.

        That must resolve to the original session rather than 409, otherwise a dropped
        response during the very first message leaves the client permanently stuck.
        """
        rid = _rid()
        first = env.chat("question", request_id=str(rid))

        monkeypatch.setattr(server, "_call_gemini_sync", _boom)
        recovered = env.chat("question", request_id=str(rid))  # session_id omitted, as before

        assert recovered["session_id"] == first["session_id"]
        assert recovered["reply"] == first["reply"]
        assert len(env.all_user_rows()) == 1

    def test_answered_row_whose_reply_vanished_is_not_reported_as_an_answer(self, env, mongo):
        """Requirement 2: a confirmed status with no reply must not fabricate a 200."""
        rid = _rid()
        body = env.chat("question", request_id=str(rid))
        row = env.single_user_row()

        # Simulate an out-of-band deletion of the assistant row.
        env.delete_row(row["reply_message_id"])

        with pytest.raises(HTTPException) as exc:
            env.chat("question", request_id=str(rid))

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "request_unrecoverable"


# ----------------- partial persistence -----------------
class TestPartialPersistence:
    def test_orphaned_reply_is_never_replayed_as_a_completed_exchange(self, env, monkeypatch):
        """Requirement 2: reply stored, user row not acknowledged -- so no completed pair.

        This is the crash-between-two-writes shape. The orphan must stay invisible to both
        the model and the replay path, even though it is a real row in the session.
        """
        session_id = "orphan_session"
        user_row = env.seed_user_message(
            session_id=session_id,
            requested_session_id=session_id,
            content="question",
            status="processing",
            status_updated_at=_old_stamp(),
            request_id=str(_rid()),
            attempt=1,
        )
        orphan = env.seed_assistant_message(session_id, "reply that was never acknowledged")

        # Replay selection sees nothing: the user row is not answered.
        rows = env.run(
            env.db.chat_messages.find({"session_id": session_id}).to_list(None)
        )
        assert server._replayable_history(rows) == []

        # And the lookup endpoint reports no reply for a non-answered row.
        status = env.lookup(uuid.UUID(user_row["request_id"]))
        assert status["status"] == "processing"
        assert status["reply"] is None

        # Re-posting reclaims the stale attempt and produces a *new* confirmed answer.
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "fresh answer")
        recovered = env.chat(
            "question", session_id=session_id, request_id=user_row["request_id"]
        )
        assert recovered["reply"] == "fresh answer"

        row = env.run(
            env.db.chat_messages.find_one({"id": user_row["id"]})
        )
        assert row["status"] == "answered"
        assert row["attempt"] == 2
        assert row["reply_message_id"] != orphan["id"], "must not adopt the orphan"

        # The orphan is still visible in history but linked to nothing.
        rows = env.run(
            env.db.chat_messages.find({"session_id": session_id}).to_list(None)
        )
        assert server._replayable_history(rows) == [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "fresh answer"},
        ]
        assert len(env.assistant_rows(session_id)) == 2

    def test_crash_between_reply_insert_and_acknowledgement_rolls_back(self, env, monkeypatch):
        """Stage 1's rollback, re-proved with the attempt guard in place."""
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "real reply")

        original = server._set_message_status

        async def lose_ack(message_id, current, target, attempt=None, **fields):
            if target == "answered":
                raise server.MessageStateError("simulated crash before acknowledgement")
            return await original(message_id, current, target, attempt=attempt, **fields)

        monkeypatch.setattr(server, "_set_message_status", lose_ack)

        with pytest.raises(HTTPException) as exc:
            env.chat("question", request_id=str(_rid()))

        assert exc.value.detail["code"] == "persistence_failed"
        row = env.single_user_row()
        assert row["status"] == "failed"
        assert row.get("reply_message_id") is None
        assert env.assistant_rows(row["session_id"]) == [], "the unacknowledged reply is gone"


# ----------------- in-flight and lease recovery -----------------
class TestInFlight:
    def test_fresh_processing_row_refuses_to_dispatch_again(self, env, monkeypatch):
        rid = _rid()
        seeded = env.seed_user_message(
            session_id="s1",
            requested_session_id="s1",
            content="question",
            status="processing",
            status_updated_at=server.now_iso(),
            request_id=str(rid),
            attempt=1,
        )
        monkeypatch.setattr(server, "_call_gemini_sync", _boom)

        with pytest.raises(HTTPException) as exc:
            env.chat("question", session_id="s1", request_id=str(rid))

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "request_in_flight"
        assert exc.value.detail["session_id"] == "s1"
        assert exc.value.detail["retry_after_ms"] > 0
        assert exc.value.detail["lookup"].endswith(str(rid))
        assert len(env.all_user_rows()) == 1
        assert env.assistant_rows("s1") == []
        assert seeded["status"] == "processing"

    def test_stale_processing_row_is_reclaimed_by_a_repeat(self, env, monkeypatch):
        rid = _rid()
        env.seed_user_message(
            session_id="s2",
            requested_session_id="s2",
            content="question",
            status="processing",
            status_updated_at=_old_stamp(),
            request_id=str(rid),
            attempt=1,
        )
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "recovered answer")

        body = env.chat("question", session_id="s2", request_id=str(rid))

        assert body["reply"] == "recovered answer"
        row = env.single_user_row()
        assert row["status"] == "answered"
        assert row["attempt"] == 2, "a reclaimed attempt consumes budget"
        assert "error_code" not in row

    def test_stale_pending_row_resumes_without_spending_an_attempt(self, env, monkeypatch):
        """Nothing was dispatched, so there is nothing to reclaim -- just continue."""
        rid = _rid()
        env.seed_user_message(
            session_id="s3",
            requested_session_id="s3",
            content="question",
            status="pending",
            status_updated_at=_old_stamp(),
            request_id=str(rid),
            attempt=1,
        )
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "answer")

        body = env.chat("question", session_id="s3", request_id=str(rid))

        assert body["reply"] == "answer"
        row = env.single_user_row()
        assert row["status"] == "answered"
        assert row["attempt"] == 1

    def test_stale_processing_with_no_budget_left_is_retry_exhausted(self, env, monkeypatch):
        rid = _rid()
        env.seed_user_message(
            session_id="s4",
            requested_session_id="s4",
            content="question",
            status="processing",
            status_updated_at=_old_stamp(),
            request_id=str(rid),
            attempt=server.MAX_CHAT_ATTEMPTS,
        )
        monkeypatch.setattr(server, "_call_gemini_sync", _boom)

        with pytest.raises(HTTPException) as exc:
            env.chat("question", session_id="s4", request_id=str(rid))

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "retry_exhausted"


# ----------------- retries -----------------
class TestRetries:
    def test_failed_message_is_retried_in_place(self, env, monkeypatch):
        """Requirement 6: same request identity, same user row, no duplicate record."""
        rid = _rid()

        def down(contents):
            raise RuntimeError("provider down")

        monkeypatch.setattr(server, "_call_gemini_sync", down)
        with pytest.raises(HTTPException) as first:
            env.chat("question", request_id=str(rid))
        assert first.value.detail["code"] == "llm_unavailable"

        row = env.single_user_row()
        assert row["status"] == "failed"
        assert row["attempt"] == 1

        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "second try")
        body = env.chat("question", request_id=str(rid))

        assert body["reply"] == "second try"
        assert len(env.all_user_rows()) == 1, "a retry must reuse the original user message"

        row = env.single_user_row()
        assert row["status"] == "answered"
        assert row["attempt"] == 2
        assert "error_code" not in row, "a successful retry clears the previous failure"
        assert row["reply_message_id"]

    def test_attempt_budget_is_enforced(self, env, monkeypatch):
        rid = _rid()
        dispatches = []

        def down(contents):
            dispatches.append(1)
            raise RuntimeError("provider down")

        monkeypatch.setattr(server, "_call_gemini_sync", down)

        for expected_attempt in (1, 2, 3):
            with pytest.raises(HTTPException) as exc:
                env.chat("question", request_id=str(rid))
            assert exc.value.detail["code"] == "llm_unavailable"
            assert env.single_user_row()["attempt"] == expected_attempt

        # The fourth submission must not dispatch at all.
        with pytest.raises(HTTPException) as exc:
            env.chat("question", request_id=str(rid))
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "retry_exhausted"
        assert len(dispatches) == server.MAX_CHAT_ATTEMPTS
        assert env.single_user_row()["status"] == "failed"

    def test_concurrent_retries_dispatch_exactly_once(self, env, monkeypatch):
        rid = _rid()
        env.seed_user_message(
            session_id="race",
            requested_session_id="race",
            content="question",
            status="failed",
            status_updated_at=_old_stamp(),
            request_id=str(rid),
            attempt=1,
            error_code="llm_unavailable",
        )

        dispatches = []

        def slow(contents):
            dispatches.append(contents)
            return "only answer"

        monkeypatch.setattr(server, "_call_gemini_sync", slow)

        results = env.run_many(
            server.chat(
                server.ChatIn(message="question", session_id="race", request_id=str(rid)),
                env.user,
            ),
            server.chat(
                server.ChatIn(message="question", session_id="race", request_id=str(rid)),
                env.user,
            ),
        )

        assert len(dispatches) == 1, "only one retry may reach the provider"
        succeeded = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, HTTPException)]
        assert len(succeeded) == 1
        assert len(refused) == 1
        assert refused[0].status_code == 409
        assert refused[0].detail["code"] == "request_in_flight"

        row = env.single_user_row()
        assert row["status"] == "answered"
        assert row["attempt"] == 2


# ----------------- stale attempts -----------------
class TestStaleAttempts:
    """Requirement 3: a worker whose lease expired must not overwrite a newer attempt.

    These drive the guards directly because the interleaving -- attempt 1's provider call
    returning *after* attempt 2 took the row over -- cannot be produced deterministically
    through the endpoint.
    """

    def _processing_at(self, env, attempt):
        return env.seed_user_message(
            session_id="stale",
            requested_session_id="stale",
            content="question",
            status="processing",
            status_updated_at=server.now_iso(),
            request_id=str(_rid()),
            attempt=attempt,
        )

    def test_stale_attempt_cannot_acknowledge_a_newer_attempt(self, env):
        row = self._processing_at(env, attempt=2)

        with pytest.raises(server.ReplyPersistenceError):
            env.run(
                server._persist_answer(
                    row["id"], "stale", env.user["id"], "stale text", attempt=1
                )
            )

        stored = env.run(env.db.chat_messages.find_one({"id": row["id"]}))
        assert stored["status"] == "processing", "the newer attempt is still in flight"
        assert stored["attempt"] == 2
        assert "reply_message_id" not in stored
        assert env.assistant_rows("stale") == [], "the rejected reply is rolled back"

    def test_stale_attempt_cannot_mark_a_newer_attempt_failed(self, env):
        row = self._processing_at(env, attempt=2)

        env.run(server._mark_failed(row["id"], "processing", "llm_unavailable", attempt=1))

        stored = env.run(env.db.chat_messages.find_one({"id": row["id"]}))
        assert stored["status"] == "processing"
        assert stored["attempt"] == 2
        assert "error_code" not in stored

    def test_current_attempt_still_succeeds(self, env):
        """Positive control: the guard must reject the stale attempt, not every attempt."""
        row = self._processing_at(env, attempt=2)

        env.run(
            server._persist_answer(
                row["id"], "stale", env.user["id"], "real answer", attempt=2
            )
        )

        stored = env.run(env.db.chat_messages.find_one({"id": row["id"]}))
        assert stored["status"] == "answered"
        assert stored["attempt"] == 2
        assert env.run(
            env.db.chat_messages.find_one({"id": stored["reply_message_id"]})
        )["content"] == "real answer"

    def test_pre_stage_2_row_without_an_attempt_counter_is_treated_as_attempt_one(self, env):
        """No backfill: the filter normalises a missing counter, so legacy rows are retryable."""
        row = env.seed_user_message(
            session_id="legacy_row",
            requested_session_id="legacy_row",
            content="question",
            status="processing",
            status_updated_at=server.now_iso(),
            request_id=str(_rid()),
        )
        assert "attempt" not in row

        env.run(
            server._persist_answer(
                row["id"], "legacy_row", env.user["id"], "legacy answer", attempt=1
            )
        )

        stored = env.run(env.db.chat_messages.find_one({"id": row["id"]}))
        assert stored["status"] == "answered"
        assert "attempt" not in stored, "the stored document is never rewritten"


# ----------------- concurrency -----------------
class TestConcurrentDuplicates:
    def test_two_identical_submissions_create_one_turn_and_one_call(self, env, monkeypatch):
        rid = _rid()
        dispatches = []

        def slow(contents):
            dispatches.append(contents)
            return "single answer"

        monkeypatch.setattr(server, "_call_gemini_sync", slow)

        results = env.run_many(
            server.chat(
                server.ChatIn(message="question", request_id=str(rid)), env.user
            ),
            server.chat(
                server.ChatIn(message="question", request_id=str(rid)), env.user
            ),
        )

        assert len(dispatches) == 1, "the unique index must elect exactly one worker"
        assert len(env.all_user_rows()) == 1, "a race must not create two user messages"
        assert len(env.assistant_rows(env.single_user_row()["session_id"])) == 1

        # One worker answers; the other either replays the finished row or is told it is
        # in flight. Both are correct, and neither may dispatch.
        assert all(isinstance(r, dict) or r.status_code == 409 for r in results)
        assert any(isinstance(r, dict) for r in results)

    def test_concurrent_submissions_with_different_content_cannot_both_win(self, env, monkeypatch):
        rid = _rid()
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "answer")

        results = env.run_many(
            server.chat(server.ChatIn(message="first", request_id=str(rid)), env.user),
            server.chat(server.ChatIn(message="second", request_id=str(rid)), env.user),
        )

        assert len(env.all_user_rows()) == 1
        conflicts = [
            r
            for r in results
            if isinstance(r, HTTPException) and r.detail["code"] == "idempotency_conflict"
        ]
        assert len(conflicts) == 1, "the losing payload must be rejected, not merged"


# ----------------- lookup endpoint -----------------
class TestLookupEndpoint:
    def test_reports_the_stored_answer(self, env):
        rid = _rid()
        body = env.chat("question", request_id=str(rid))

        status = env.lookup(rid)

        assert status["request_id"] == str(rid)
        assert status["session_id"] == body["session_id"]
        assert status["status"] == "answered"
        assert status["reply"] == body["reply"]
        assert status["attempt"] == 1
        assert status["max_attempts"] == server.MAX_CHAT_ATTEMPTS
        assert status["can_retry"] is False

    def test_reports_a_failed_row_as_retryable(self, env, monkeypatch):
        rid = _rid()

        def down(contents):
            raise RuntimeError("provider down")

        monkeypatch.setattr(server, "_call_gemini_sync", down)
        with pytest.raises(HTTPException):
            env.chat("question", request_id=str(rid))

        status = env.lookup(rid)
        assert status["status"] == "failed"
        assert status["error_code"] == "llm_unavailable"
        assert status["reply"] is None
        assert status["can_retry"] is True

    def test_unknown_request_is_a_404(self, env):
        with pytest.raises(HTTPException) as exc:
            env.lookup(_rid())
        assert exc.value.status_code == 404
        assert exc.value.detail["code"] == "request_not_found"

    def test_another_users_request_is_indistinguishable_from_missing(self, env):
        """A 403 would confirm the id exists; 404 leaks nothing."""
        rid = _rid()
        env.chat("question", request_id=str(rid))

        with pytest.raises(HTTPException) as exc:
            env.run(server.chat_request_status(rid, {"id": "someone_else"}))

        assert exc.value.status_code == 404
        assert exc.value.detail["code"] == "request_not_found"


# ----------------- the constraint itself -----------------
class TestUniqueIndex:
    def test_index_is_present_and_correctly_configured(self, env):
        info = env.run(env.db.chat_messages.index_information())
        spec = info[server.CHAT_REQUEST_INDEX]

        assert spec["unique"] is True
        assert dict(spec["key"]) == {"user_id": 1, "request_id": 1}
        assert spec.get("partialFilterExpression"), "legacy rows must stay out of the index"

    def test_database_rejects_a_duplicate_key_even_without_the_handler(self, env, mongo):
        """The guarantee must come from MongoDB, not from application-side bookkeeping."""
        rid = str(_rid())
        base = {
            "session_id": "dupe",
            "user_id": env.user["id"],
            "role": "user",
            "content": "same",
            "created_at": server.now_iso(),
            "status": "pending",
            "attempt": 1,
            "request_id": rid,
        }
        mongo.chat_messages.insert_one({"id": str(uuid.uuid4()), **base})

        with pytest.raises(DuplicateKeyError):
            mongo.chat_messages.insert_one({"id": str(uuid.uuid4()), **base})

    def test_legacy_rows_without_the_field_do_not_collide(self, env, mongo):
        for _ in range(3):
            mongo.chat_messages.insert_one(
                {
                    "id": str(uuid.uuid4()),
                    "session_id": "legacy",
                    "user_id": env.user["id"],
                    "role": "user",
                    "content": "old message",
                    "created_at": server.now_iso(),
                }
            )
        assert mongo.chat_messages.count_documents(
            {"user_id": env.user["id"], "role": "user", "request_id": {"$exists": False}}
        ) == 3

    def test_the_same_key_is_allowed_for_a_different_user(self, env, mongo):
        rid = str(_rid())
        for user_id in (env.user["id"], "another_user"):
            mongo.chat_messages.insert_one(
                {
                    "id": str(uuid.uuid4()),
                    "session_id": "shared",
                    "user_id": user_id,
                    "role": "user",
                    "content": "same id, different account",
                    "created_at": server.now_iso(),
                    "status": "pending",
                    "attempt": 1,
                    "request_id": rid,
                }
            )
        assert mongo.chat_messages.count_documents({"request_id": rid}) == 2
        mongo.chat_messages.delete_many({"user_id": "another_user"})
