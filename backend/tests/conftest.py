"""Shared setup for the live-server integration suite.

The tests call a running backend over real HTTP, so they need both the server and the
database pointed at a dedicated test database. Two things made that necessary:

  * Test data accumulated in the development database with no teardown, and that already
    caused failures pointing nowhere near their cause -- leaked attachments kept consuming
    the shared admin account's monthly document quota until an unrelated upload test
    started returning 402 "upgrade to Pro".
  * Every run also called the live Gemini and Stripe APIs, which makes a red build
    indistinguishable from an outage.

This module owns the isolation. It refuses to run at all unless both the pytest process
and the server agree the database is a test database, wipes it before and after every
session, and re-provisions it using the application's own ``seed_users`` so the test
schema cannot drift from production. It does not remove the dependency on live external
APIs; that would need mocked or recorded responses.
"""
import os
import secrets
from pathlib import Path

import pytest
import requests

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Collections keyed by user_id. The users collection is separate because it is keyed by id.
OWNED_COLLECTIONS = (
    "decisions",
    "chat_messages",
    "attachments",
    "activity",
    "comments",
    "subscriptions",
    "connections",
    "oauth_states",
    "payment_transactions",
)


def _load_env():
    # load_dotenv does not override variables already present in the environment, which is
    # exactly what makes $env:DB_NAME="decisionlens_test" win over backend/.env in both
    # the server process and this one.
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env")


# At import, not inside a fixture. pytest imports conftest.py before any test module, so
# this is the only point early enough for a module-level `pytest.mark.skipif` to see the
# real configuration. Loaded from the fixture instead, those marks were already evaluated:
# GEMINI_API_KEY and STRIPE_API_KEY looked unset to the suite while the server it was
# talking to had them, so the tests asserting the unconfigured 503/4xx paths ran against a
# configured backend and failed, and the tests that should have run were skipped.
_load_env()


def get_db():
    """Return the database handle, or None if it cannot be reached."""
    try:
        from pymongo import MongoClient

        url = os.environ.get("MONGO_URL", "").strip()
        name = os.environ.get("DB_NAME", "").strip()
        if not url or not name:
            return None
        client = MongoClient(url, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        return client[name]
    except Exception:
        return None


def purge_user_data(user_id) -> None:
    """Remove a test user and everything it owns. Never raises.

    Still useful between tests within a run; the session fixture handles the wider reset.
    """
    if not user_id:
        return
    db = get_db()
    if db is None:
        return
    try:
        for name in OWNED_COLLECTIONS:
            db[name].delete_many({"user_id": user_id})
        db.users.delete_one({"id": user_id})
    except Exception:
        pass


def _explicit_upload_dir():
    """The upload directory when UPLOAD_DIR is set, else None.

    Only an explicit directory is ever emptied. The default uploads/ path holds committed
    sample files, so wiping it would destroy tracked data.
    """
    raw = os.environ.get("UPLOAD_DIR", "").strip()
    return Path(raw) if raw else None


def _wipe(db, upload_dir) -> None:
    for name in db.list_collection_names():
        db[name].delete_many({})
    if upload_dir and upload_dir.is_dir():
        for item in upload_dir.iterdir():
            if item.is_file():
                item.unlink()


# Must match TEST_DB_NAME in backend/server.py. An exact name is used rather than a
# substring: "test" in name also accepts `contest_prod` and `latest_billing`, and this
# predicate, together with the server's copy of it, is the only thing standing between
# the suite and a real database.
TEST_DB_NAME = "decisionlens_test"


def _is_test_db(name: str) -> bool:
    return (name or "").strip().lower() == TEST_DB_NAME


def assert_test_upload_directory(upload_dir):
    """Resolve UPLOAD_DIR and refuse anything that is not clearly disposable.

    Two separate risks. The committed `uploads/` directory holds 12 sample files the app
    is expected to serve, so emptying it destroys tracked data. And a short path would
    turn _wipe's `iterdir/unlink` loop into something far wider than intended.

    Nothing here creates or deletes anything; it only decides whether the caller may.
    """
    if upload_dir is None:
        raise RuntimeError(
            "UPLOAD_DIR must be set to a dedicated test directory. The suite empties it "
            "before and after every run, so it will not fall back to the application's "
            'uploads/ directory. Set $env:UPLOAD_DIR="uploads_test".'
        )

    path = Path(upload_dir).resolve()
    repo_uploads = (BACKEND_DIR.parent / "uploads").resolve()

    if path == repo_uploads or repo_uploads in path.parents:
        raise RuntimeError(
            f"UPLOAD_DIR resolves to {path}, inside the repository's uploads/ directory, "
            "which contains committed sample files. Refusing to run."
        )
    if "test" not in path.name.lower():
        raise RuntimeError(
            f"UPLOAD_DIR '{path.name}' is not clearly a test directory; its name must "
            "contain 'test' so a misconfiguration is obvious rather than plausible."
        )
    if path == Path(path.anchor) or len(path.parts) < 3:
        raise RuntimeError(
            f"Refusing to use {path} as an upload directory: too close to the filesystem root."
        )
    return path


def verify_same_database(db, base_url, upload_dir):
    """Prove the server reads the database this process is about to empty.

    The name check only proves both processes were *configured* the same way. This asks
    the running server to find a marker written here through its own live Mongo handle,
    which is the only way to catch a server that loaded a different env file. It runs
    before _wipe and fails closed.
    """
    secret = os.environ.get("TEST_DB_PROBE_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError(
            "TEST_DB_PROBE_SECRET must be at least 32 characters. Generate one with "
            'python -c "import secrets;print(secrets.token_urlsafe(32))", put it in '
            "backend/.env (gitignored) with ENABLE_TEST_DB_PROBE=1, or export the same "
            "value to both the server and this process."
        )

    marker = secrets.token_urlsafe(32)
    db["_test_identity"].insert_one({"_id": marker})
    try:
        try:
            response = requests.post(
                f"{base_url}/api/__test/db-identity",
                json={"marker": marker, "upload_dir": str(upload_dir)},
                headers={"X-Test-Probe-Secret": secret},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"The test-database probe could not be reached at {base_url}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise RuntimeError(
                "The test-database probe returned 404. It stays hidden unless "
                "ENABLE_TEST_DB_PROBE=1 and TEST_DB_PROBE_SECRET (32+ chars) are set on "
                "the *server* process and the server's DB_NAME is exactly "
                f"'{TEST_DB_NAME}'."
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"The test-database probe returned {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "The test-database probe returned a non-JSON body; the server may have "
                "crashed while handling it."
            ) from exc

        if body.get("matched") is not True:
            raise RuntimeError(
                f"The server at {base_url} cannot see a marker written to this process's "
                f"database ('{os.environ.get('DB_NAME')}'). The two are pointed at "
                "different databases; refusing to wipe either."
            )
        if body.get("uploads_match") is not True:
            raise RuntimeError(
                "The server's UPLOAD_DIR does not resolve to this process's UPLOAD_DIR, "
                "or the server predates the upload check. Attachment writes and the "
                "cleanup below would touch different directories. Pass the same absolute "
                "path to both processes."
            )
    finally:
        # Not strictly required: _wipe empties every collection a few lines later. It
        # matters on the failure paths, where the fixture raises before reaching _wipe
        # and the marker would otherwise be left behind.
        db["_test_identity"].delete_one({"_id": marker})


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database():
    db_name = os.environ.get("DB_NAME", "").strip()
    base_url = (
        os.environ.get("REACT_APP_BACKEND_URL", "").strip().rstrip("/") or "http://localhost:8001"
    )

    # Every guard below raises rather than skipping. A skip in a session-scoped autouse
    # fixture exits the run with status 0, so "the suite never ran" was indistinguishable
    # from "the suite passed" in CI. Only tests that cannot execute under their intended
    # conditions -- a missing Gemini or Stripe key -- still skip, and they say why.

    # 1. Designation, pytest side: is this process allowed to empty the database at all?
    if not _is_test_db(db_name):
        raise RuntimeError(
            f"DB_NAME is '{db_name}', which is not the disposable test database. This "
            f"suite wipes its database before and after every run and refuses to touch "
            f'anything else. Set $env:DB_NAME="{TEST_DB_NAME}" and start the server with '
            "the same value."
        )

    try:
        health = requests.get(f"{base_url}/api/", timeout=10).json()
    except Exception as exc:
        raise RuntimeError(
            f"Backend not reachable at {base_url} ({exc}). Start it before running pytest."
        ) from exc

    # 2. Designation, server side. The pytest process and the server must agree, or the
    # suite would wipe one database while testing against another.
    if not health.get("test_db"):
        raise RuntimeError(
            f"The server at {base_url} is not running against the disposable test "
            f'database. Restart it with $env:DB_NAME="{db_name}" before running the suite.'
        )

    db = get_db()
    if db is None:
        raise RuntimeError("MongoDB is unreachable; cannot provision the test database.")

    # 3. Local safety checks. Nothing is created, written or deleted until both the
    # upload directory and the database identity have been established.
    upload_dir = assert_test_upload_directory(_explicit_upload_dir())

    # 4. Identity: the server must be able to read a marker written here, through its own
    # connection, before either side is emptied.
    verify_same_database(db, base_url, upload_dir)

    upload_dir.mkdir(parents=True, exist_ok=True)
    _wipe(db, upload_dir)

    # Re-provision with the application's own seeding rather than a copy of it, so the
    # indexes the tests run against are the same ones production creates. This also
    # restores the admin and demo accounts that the login tests authenticate as, which a
    # wipe performed after server startup would otherwise have destroyed.
    import asyncio

    from server import seed_users

    asyncio.run(seed_users())

    yield

    _wipe(db, upload_dir)