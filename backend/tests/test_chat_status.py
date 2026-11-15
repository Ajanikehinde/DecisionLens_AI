"""Stage 1 chat-message status tests.

Deterministic and offline. The harness -- per-test event loop and Motor client, throwaway
user, disposable-database re-check, and the guard that turns any real HTTP call into an
assertion failure -- lives in ``chat_harness.py`` and is shared with the Stage 2 idempotency
suite. See that module's docstring for why these tests run in-process rather than over HTTP.
"""
import os
import uuid

import pytest
from fastapi import HTTPException
from pymongo import MongoClient

from chat_harness import _db_name, env, mongo, no_live_provider  # noqa: F401  (fixtures)

import server  # noqa: E402  (chat_harness puts backend/ on sys.path)

# ----------------- transition rules -----------------
class TestTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            ("pending", "answered"),
            ("pending", "failed"),
            ("processing", "processing"),
            ("answered", "processing"),
            ("answered", "answered"),
            ("failed", "answered"),
        ],
    )
    def test_illegal_edges_are_rejected(self, current, target):
        """Pending must not skip the in-flight state, and answered never moves.

        ``failed -> processing`` was illegal in Stage 1 and is now the one added edge; it is
        covered by ``test_legal_edges_are_accepted`` and by the retry tests.
        """
        with pytest.raises(server.MessageStateError):
            server._check_transition(current, target)

    @pytest.mark.parametrize(
        "current,target",
        [
            ("pending", "processing"),
            ("processing", "answered"),
            ("processing", "failed"),
            # Stage 2: a client may retry the same user message instead of duplicating it.
            ("failed", "processing"),
        ],
    )
    def test_legal_edges_are_accepted(self, current, target):
        server._check_transition(current, target)  # must not raise

    def test_answered_remains_absolutely_terminal(self):
        """Making ``failed`` retryable must never make a confirmed answer retryable."""
        assert server._ALLOWED_TRANSITIONS["answered"] == ()
        assert server.REPLAYABLE_STATUS == "answered"

    def test_unknown_current_status_is_rejected(self):
        with pytest.raises(server.MessageStateError):
            server._check_transition("answered-ish", "failed")

    def test_a_row_that_is_not_in_the_expected_state_is_rejected(self, env, mongo):
        """matched_count proves the compare-and-set, not just the Python table.

        The transition itself (pending -> processing) is legal, so only a lost update can
        make this fail: the stored row is already terminal.
        """
        message_id = str(uuid.uuid4())
        mongo.chat_messages.insert_one(
            {
                "id": message_id,
                "session_id": "terminal",
                "user_id": env.user["id"],
                "role": "user",
                "content": "already answered",
                "created_at": server.now_iso(),
                "status": "answered",
            }
        )
        with pytest.raises(server.MessageStateError):
            env.run(server._set_message_status(message_id, "pending", "processing"))


# ----------------- initial pending state -----------------
class TestPendingState:
    """The insert-time state, which end-state assertions cannot distinguish.

    A finished turn is ``answered`` and a missing key writes no row at all, so neither
    proves the row was ever *recorded* as ``pending``. These two tests observe the window
    itself: the first by reading storage at the moment of the first transition, the second
    by losing that transition and checking what the turn is abandoned as.
    """

    def test_the_row_is_stored_as_pending_before_dispatch(self, env, monkeypatch):
        """Read the row from a separate connection while the handler still holds it pending.

        The first ``_set_message_status`` call is ``pending -> processing``, which is the
        last instant the row is pending. Capturing there is the only way to assert the
        state was written *before* any work started rather than merely inferred later.
        """
        observed = {}
        original = server._set_message_status

        async def spy(message_id, current, target, **fields):
            if current == "pending":
                with MongoClient(os.environ["MONGO_URL"]) as client:
                    row = client[_db_name()].chat_messages.find_one({"id": message_id})
                observed["status"] = row.get("status")
                observed["updated_at"] = row.get("status_updated_at")
                observed["has_reply_id"] = "reply_message_id" in row
            return await original(message_id, current, target, **fields)

        monkeypatch.setattr(server, "_set_message_status", spy)
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "ok")

        body = env.chat("Hello")

        assert observed["status"] == "pending", "the row must exist as pending before dispatch"
        assert observed["updated_at"], "a pending row carries the time it was accepted"
        assert not observed["has_reply_id"], "no reply id can exist before an answer"
        # And it still finishes normally, so the test is not observing a broken path.
        assert env.user_rows(body["session_id"])[0]["status"] == "answered"

    def test_a_lost_processing_write_abandons_the_turn_as_pending(self, env, monkeypatch):
        """If ``pending -> processing`` cannot be written, the provider must never be called.

        The turn is abandoned in ``pending``: visible in history, excluded from replay, and
        distinguishable from a crash *during* the call. Reporting success here would claim
        an exchange that no row records.
        """
        dispatched = []
        monkeypatch.setattr(
            server, "_call_gemini_sync", lambda contents: dispatched.append(contents) or "ok"
        )

        original = server._set_message_status

        async def lost_update(message_id, current, target, **fields):
            if target == "processing":
                raise server.MessageStateError("simulated lost update")
            return await original(message_id, current, target, **fields)

        monkeypatch.setattr(server, "_set_message_status", lost_update)

        with pytest.raises(HTTPException) as exc:
            env.chat("Hello")

        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "persistence_failed"

        user = env.single_user_row()
        assert user["status"] == "pending", "not processing, and certainly not answered"
        assert env.assistant_rows(user["session_id"]) == []
        assert dispatched == [], "the provider must not run without an in-flight marker"


# ----------------- success -----------------
class TestSuccessfulReply:
    def test_turn_is_marked_answered_only_after_the_reply_is_stored(self, env, monkeypatch):
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "A genuine answer.")

        body = env.chat("Hello")

        assert set(body) == {"session_id", "reply"}, "response contract must not change"
        assert body["reply"] == "A genuine answer."

        user = env.user_rows(body["session_id"])[0]
        assert user["status"] == "answered"
        assert user.get("error_code") is None, "a successful turn carries no error code"

        replies = env.assistant_rows(body["session_id"])
        assert len(replies) == 1
        assert replies[0]["id"] == user["reply_message_id"]
        assert replies[0]["content"] == "A genuine answer."

    def test_the_message_is_processing_while_the_provider_runs(self, env, monkeypatch):
        """Read the row from the provider thread at call time, not afterwards."""
        observed = {}

        def spy(contents):
            with MongoClient(os.environ["MONGO_URL"]) as client:
                row = client[_db_name()].chat_messages.find_one(
                    {"user_id": env.user["id"], "role": "user"}
                )
            observed["status"] = row["status"]
            return "fine"

        monkeypatch.setattr(server, "_call_gemini_sync", spy)
        env.chat("Hello")
        assert observed["status"] == "processing"

    def test_the_completed_pair_is_replayed_on_the_next_turn(self, env, monkeypatch):
        """Proves selection and shape together: the pair, and only the pair, is sent."""
        calls = []

        def capture(contents):
            calls.append(contents)
            return f"answer {len(calls)}"

        monkeypatch.setattr(server, "_call_gemini_sync", capture)
        first = env.chat("first question")
        env.chat("second question", session_id=first["session_id"])

        second_call = calls[1]
        assert [turn["role"] for turn in second_call] == ["user", "model", "user"]
        assert second_call[0]["parts"][0]["text"] == "first question"
        assert second_call[1]["parts"][0]["text"] == "answer 1"
        assert second_call[2]["parts"][0]["text"] == "second question"


# ----------------- provider failures -----------------
class TestProviderFailures:
    def test_provider_exception_marks_failed_and_stores_no_reply(self, env, monkeypatch):
        def boom(contents):
            raise RuntimeError("provider down")

        monkeypatch.setattr(server, "_call_gemini_sync", boom)

        with pytest.raises(HTTPException) as exc:
            env.chat("Hello")

        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "llm_unavailable"

        user = env.single_user_row()
        assert user["status"] == "failed"
        assert user["error_code"] == "llm_unavailable"
        assert env.assistant_rows(user["session_id"]) == [], "no reply may be fabricated"

    def test_empty_response_marks_failed_and_stores_no_reply(self, env, monkeypatch):
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "   ")

        with pytest.raises(HTTPException) as exc:
            env.chat("Hello")

        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "llm_empty_response"
        user = env.single_user_row()
        assert user["status"] == "failed"
        assert user["error_code"] == "llm_empty_response"
        assert env.assistant_rows(user["session_id"]) == []

    def test_missing_key_writes_no_message_at_all(self, env, mongo, monkeypatch):
        monkeypatch.setattr(server, "GEMINI_API_KEY", "")

        with pytest.raises(HTTPException) as exc:
            env.chat("Hello")

        assert exc.value.detail["code"] == "llm_not_configured"
        assert mongo.chat_messages.count_documents({"user_id": env.user["id"]}) == 0


# ----------------- reply persistence failures -----------------
class TestReplyPersistence:
    def test_reply_insert_failure_is_not_reported_as_an_answer(self, env, monkeypatch):
        """A Gemini failure and a storage failure must not be conflated."""
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "A real reply.")

        async def broken_insert(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(server, "_insert_reply", broken_insert)

        with pytest.raises(HTTPException) as exc:
            env.chat("Hello")

        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "persistence_failed"
        user = env.single_user_row()
        assert user["status"] == "failed"
        assert user["error_code"] == "persistence_failed"
        assert env.assistant_rows(user["session_id"]) == []

    def test_answered_status_failure_rolls_back_the_stored_reply(self, env, monkeypatch):
        """The requirement-1 case: reply stored, acknowledgement failed, so not a 200."""
        monkeypatch.setattr(server, "_call_gemini_sync", lambda contents: "A real reply.")

        original = server._set_message_status

        async def lost_update(message_id, current, target, **fields):
            if target == "answered":
                raise server.MessageStateError("simulated lost update")
            return await original(message_id, current, target, **fields)

        monkeypatch.setattr(server, "_set_message_status", lost_update)

        with pytest.raises(HTTPException) as exc:
            env.chat("Hello")

        assert exc.value.status_code == 503, "a stored reply with no answered status is not success"
        assert exc.value.detail["code"] == "persistence_failed"

        user = env.single_user_row()
        assert user["status"] == "failed"
        assert user["error_code"] == "persistence_failed"
        assert user.get("reply_message_id") is None
        assert env.assistant_rows(user["session_id"]) == [], "the reply must be rolled back"

    def test_a_failed_update_does_not_mask_the_provider_error(self, env, monkeypatch):
        """If marking failed itself fails, the client must still see the real cause.

        Only the terminal write is broken here. Breaking *every* status write would fail
        the earlier ``pending -> processing`` step and produce ``persistence_failed``,
        which is a different (also correct) path -- so the filter is what makes this test
        about ``_mark_failed``'s best-effort behaviour specifically.
        """

        def boom(contents):
            raise RuntimeError("provider down")

        monkeypatch.setattr(server, "_call_gemini_sync", boom)

        original = server._set_message_status

        async def broken_terminal(message_id, current, target, **fields):
            if target == "failed":
                raise RuntimeError("mongo down")
            return await original(message_id, current, target, **fields)

        monkeypatch.setattr(server, "_set_message_status", broken_terminal)

        with pytest.raises(HTTPException) as exc:
            env.chat("Hello")

        assert exc.value.detail["code"] == "llm_unavailable", "the real cause must win"
        # The bookkeeping write was lost, so the row is stranded in "processing": still
        # visible, still excluded from replay, and countable by Stage 3. Critically it is
        # never "answered".
        user = env.single_user_row()
        assert user["status"] == "processing"
        assert env.assistant_rows(user["session_id"]) == []


# ----------------- replay selection -----------------
class TestReplaySelection:
    def test_only_completed_pairs_are_replayed(self):
        rows = [
            {"id": "u1", "role": "user", "status": "answered", "content": "q1", "reply_message_id": "a1"},
            {"id": "a1", "role": "assistant", "content": "r1"},
            {"id": "u2", "role": "user", "status": "failed", "content": "q2", "error_code": "llm_unavailable"},
            {"id": "u3", "role": "user", "status": "pending", "content": "q3"},
            {"id": "u5", "role": "user", "status": "processing", "content": "q5"},
            {"id": "u4", "role": "user", "content": "legacy q4"},
            {"id": "a4", "role": "assistant", "content": "legacy r4"},
        ]
        assert server._replayable_history(rows) == [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "r1"},
        ]

    def test_an_answered_row_without_its_reply_is_dropped(self):
        rows = [
            {"id": "u1", "role": "user", "status": "answered", "content": "q", "reply_message_id": "missing"},
        ]
        assert server._replayable_history(rows) == [], "never send a user turn without its answer"

    def test_legacy_rows_report_unknown(self):
        doc = {
            "_id": "mongo-id",
            "id": "u1",
            "role": "user",
            "content": "old question",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        out = server._serialize_chat_message(doc)
        assert out["status"] == "unknown", "absence of status must not become 'answered'"
        assert "_id" not in out

    def test_legacy_rows_are_excluded_from_replay(self):
        rows = [
            {"id": "u1", "role": "user", "content": "old question"},
            {"id": "a1", "role": "assistant", "content": "possibly fabricated"},
        ]
        assert server._replayable_history(rows) == []

    def test_legacy_history_is_still_visible_through_the_endpoint(self, env, mongo):
        session_id = "legacy_history"
        mongo.chat_messages.insert_many(
            [
                {
                    "id": f"legacy_u_{uuid.uuid4().hex}",
                    "session_id": session_id,
                    "user_id": env.user["id"],
                    "role": "user",
                    "content": "old question",
                    "created_at": server.now_iso(),
                },
                {
                    "id": f"legacy_a_{uuid.uuid4().hex}",
                    "session_id": session_id,
                    "user_id": env.user["id"],
                    "role": "assistant",
                    "content": "possibly fabricated",
                    "created_at": server.now_iso(),
                },
            ]
        )

        msgs = env.run(server.chat_history(session_id, env.user))

        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["status"] == "unknown"
        assert msgs[0]["content"] == "old question", "history must not lose the exchange"


# ----------------- history -----------------
class TestHistory:
    def test_failed_messages_remain_visible(self, env, monkeypatch):
        def boom(contents):
            raise RuntimeError("provider down")

        monkeypatch.setattr(server, "_call_gemini_sync", boom)

        with pytest.raises(HTTPException):
            env.chat("Hello", session_id="history_session")

        msgs = env.run(server.chat_history("history_session", env.user))

        assert [m["role"] for m in msgs] == ["user"]
        assert msgs[0]["status"] == "failed"
        assert msgs[0]["content"] == "Hello"
        assert "_id" not in msgs[0]
