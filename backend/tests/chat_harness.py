"""Shared in-process harness for the chat status and idempotency suites.

Two constraints shaped it, and both were verified rather than assumed:

  * ``httpx`` is not installed, so ``fastapi.testclient`` is unavailable, and a patched
    provider call cannot cross into a separate uvicorn process. Handlers are therefore
    invoked in-process, which also means FastAPI routing/serialization is NOT covered by
    these modules -- ``backend_test.py::TestChat`` still covers the HTTP layer live.
  * Motor binds its client to the first event loop it sees and raises "Event loop is
    closed" on any other (reproduced against motor 3.3.1). The session fixture already
    spends that one loop on ``asyncio.run(seed_users())``, so each test gets its own
    client AND its own loop, and ``server.db`` is patched for the duration.

The session-scoped autouse fixture in conftest still runs first and still enforces the
disposable-database and upload-directory guards. ``_db_name`` repeats the check locally so
a module cannot touch a real database even when run in isolation.

The fixtures live here rather than in conftest because they are only meaningful for chat
tests; importing ``env`` into a test module registers it in that module's namespace.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient

from conftest import TEST_DB_NAME, _is_test_db

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # noqa: E402  (imported after sys.path is made explicit)


def _db_name():
    name = os.environ.get("DB_NAME", "").strip()
    if not _is_test_db(name):
        raise RuntimeError(
            f"DB_NAME is {name!r}; chat tests refuse to run outside {TEST_DB_NAME!r}."
        )
    return name


@pytest.fixture(scope="module")
def mongo():
    """Synchronous handle for assertions and seeding, outside the test's event loop."""
    client = MongoClient(os.environ["MONGO_URL"])
    try:
        yield client[_db_name()]
    finally:
        client.close()


@pytest.fixture(autouse=True)
def no_live_provider(monkeypatch):
    """Any real HTTP call in these modules is a bug, not a fallback.

    Gemini's free tier is 20 requests/day, and these suites must never spend one.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "a real provider HTTP call was attempted; every test must patch the client"
        )

    monkeypatch.setattr(server.requests, "post", forbidden)


class ChatEnv:
    """One event loop, one Motor client, one throwaway user."""

    def __init__(self, loop, db, user):
        self.loop = loop
        self.db = db
        self.user = user

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def run_many(self, *coros):
        """Run coroutines concurrently, returning results/exceptions in order.

        ``gather`` is created inside the loop: since Python 3.12 there is no implicit current
        event loop in the main thread, so calling it directly raises RuntimeError.
        """

        async def _gather():
            return await asyncio.gather(*coros, return_exceptions=True)

        return self.loop.run_until_complete(_gather())

    def chat(self, message, session_id=None, request_id=None):
        return self.run(
            server.chat(
                server.ChatIn(
                    message=message, session_id=session_id, request_id=request_id
                ),
                self.user,
            )
        )

    def lookup(self, request_id):
        return self.run(server.chat_request_status(request_id, self.user))

    def user_rows(self, session_id):
        return self.run(
            self.db.chat_messages.find(
                {"session_id": session_id, "role": "user"}
            ).to_list(None)
        )

    def assistant_rows(self, session_id):
        return self.run(
            self.db.chat_messages.find(
                {"session_id": session_id, "role": "assistant"}
            ).to_list(None)
        )

    def all_user_rows(self):
        return self.run(
            self.db.chat_messages.find(
                {"user_id": self.user["id"], "role": "user"}
            ).to_list(None)
        )

    def single_user_row(self):
        rows = self.all_user_rows()
        assert len(rows) == 1, f"expected exactly one user row, got {len(rows)}"
        return rows[0]

    def seed_user_message(self, **fields):
        """Insert a user row directly, for states that are hard to reach through the API."""
        doc = {
            "id": str(uuid.uuid4()),
            "session_id": fields.pop("session_id", "seeded_session"),
            "user_id": self.user["id"],
            "role": "user",
            "content": fields.pop("content", "seeded"),
            "created_at": fields.pop("created_at", server.now_iso()),
        }
        doc.update(fields)
        self.run(self.db.chat_messages.insert_one(doc))
        return doc

    def seed_assistant_message(self, session_id, content, **fields):
        doc = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "user_id": self.user["id"],
            "role": "assistant",
            "content": content,
            "created_at": fields.pop("created_at", server.now_iso()),
        }
        doc.update(fields)
        self.run(self.db.chat_messages.insert_one(doc))
        return doc

    def delete_row(self, row_id):
        self.run(self.db.chat_messages.delete_one({"id": row_id}))


@pytest.fixture
def env(monkeypatch):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[_db_name()]
    loop = asyncio.new_event_loop()

    # Both are module globals read at call time, so this swaps what the handlers use
    # without disturbing the module's own client or any seeded data.
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key-never-used")

    user = {
        "id": f"status_{uuid.uuid4().hex}",
        "email": f"{uuid.uuid4().hex[:8]}@example.com",
        "name": "Chat Tester",
        "role": "user",
    }
    environment = ChatEnv(loop, db, user)
    try:
        # Every chat test runs against the real uniqueness constraint, not a Python-side
        # approximation of it. Idempotent, so creating it repeatedly is harmless.
        loop.run_until_complete(server._ensure_chat_request_index())
        yield environment
    finally:
        try:
            loop.run_until_complete(db.chat_messages.delete_many({"user_id": user["id"]}))
        except Exception:
            pass
        try:
            loop.run_until_complete(client.close())
        except Exception:
            pass
        loop.close()
