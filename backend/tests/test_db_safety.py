"""Safety tests for the test-database guards.

These exist because the integration suite's first act is to empty a database. A
misconfigured run therefore fails destructively rather than loudly, and the guards have
to be trusted before they can be relied on. Three things are covered:

  * the gate that decides whether the identity probe is exposed at all,
  * the probe itself, including the two directions that matter -- a marker written here
    must be visible to the server, and a marker written anywhere else must not be,
  * the upload-directory guard, which is the only thing protecting the 12 committed
    sample files in the repository's ``uploads/`` directory.

``verify_same_database`` is also exercised with mocked responses, because the failures it
is meant to catch (probe hidden, databases divergent, directories divergent) cannot be
produced against a correctly configured server.
"""
import os
import secrets
import uuid
from pathlib import Path

import pytest
import requests

from conftest import (
    TEST_DB_NAME,
    assert_test_upload_directory,
    get_db,
    verify_same_database,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").strip().rstrip("/")
if not BASE_URL:
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
    BASE_URL = "http://localhost:8001"

API = f"{BASE_URL}/api"

PROBE_SECRET = os.environ.get("TEST_DB_PROBE_SECRET", "")
PROBE_ENABLED = (
    os.environ.get("ENABLE_TEST_DB_PROBE", "").strip() == "1"
    and len(PROBE_SECRET) >= 32
)

# Same idiom the suite uses for GEMINI_API_KEY and STRIPE_API_KEY: the disabled path and
# the enabled path are mutually exclusive, so whichever one the running server cannot
# reach is skipped with an explanation rather than silently passing.
needs_probe = pytest.mark.skipif(
    not PROBE_ENABLED,
    reason=(
        "The identity probe is disabled on this server, so the authenticated path cannot "
        "be exercised. Set ENABLE_TEST_DB_PROBE=1 and a 32+ character "
        "TEST_DB_PROBE_SECRET on the server, or run this module alone with the probe on."
    ),
)


@pytest.fixture(scope="module")
def probe_gate():
    """The gate predicate, imported lazily.

    Importing server.py at module import would open a second Motor client and touch the
    upload directory during collection, before the session fixture has decided whether
    this run is allowed to do anything.
    """
    from server import _probe_enabled

    return _probe_enabled


def _probe(marker, secret=PROBE_SECRET, upload_dir=None, timeout=10):
    payload = {"marker": marker}
    if upload_dir is not None:
        payload["upload_dir"] = upload_dir
    headers = {"X-Test-Probe-Secret": secret} if secret is not None else {}
    return requests.post(f"{API}/__test/db-identity", json=payload, headers=headers, timeout=timeout)


class TestProbeGate:
    """All four exposure conditions, tested as a pure predicate.

    The live disabled path cannot be reached from inside this suite: the session fixture
    refuses to start without a working probe, precisely so no run can arrive at a wipe
    unverified. It is therefore checked here directly, and the real 404 was verified out
    of band against a second server started without the flag.
    """

    def test_disabled_by_default(self, probe_gate):
        assert probe_gate("", TEST_DB_NAME, "x" * 40) is False

    def test_requires_the_exact_database_name(self, probe_gate):
        assert probe_gate("1", "decisionlens", "x" * 40) is False
        # The old predicate was `"test" in name`, which accepts all of these.
        assert probe_gate("1", "contest_prod", "x" * 40) is False
        assert probe_gate("1", "latest_billing", "x" * 40) is False
        assert probe_gate("1", f"{TEST_DB_NAME}_backup", "x" * 40) is False
        assert probe_gate("1", TEST_DB_NAME, "x" * 40) is True

    def test_requires_a_strong_secret(self, probe_gate):
        assert probe_gate("1", TEST_DB_NAME, "x" * 31) is False
        assert probe_gate("1", TEST_DB_NAME, "x" * 32) is True

    def test_flag_must_be_exactly_one(self, probe_gate):
        for flag in ("true", "yes", "on", " 1", "1 ", "TRUE"):
            assert probe_gate(flag, TEST_DB_NAME, "x" * 40) is False

    def test_not_advertised_in_openapi(self):
        response = requests.get(f"{BASE_URL}/openapi.json", timeout=15)
        assert response.status_code == 200, response.text
        paths = response.json().get("paths", {})
        assert not any("__test" in path for path in paths), (
            "the probe must stay out of the published schema"
        )


@needs_probe
class TestProbeCredentials:
    def test_missing_secret_is_404(self):
        response = _probe("anything", secret=None)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}, (
            "the rejection must be byte-identical to an unmatched route"
        )

    def test_wrong_secret_is_404(self):
        assert _probe("anything", secret="wrong" * 10).status_code == 404

    def test_non_ascii_secret_is_404_not_500(self):
        """compare_digest raises TypeError on non-ASCII str; a 500 would confirm the route."""
        response = _probe("anything", secret="sécret" * 10)
        assert response.status_code == 404, response.text

    def test_malformed_body_without_credentials_is_404_not_422(self):
        """Validation must happen after the gate, or the 422 leaks the route's existence."""
        response = requests.post(f"{API}/__test/db-identity", json={}, timeout=10)
        assert response.status_code == 404, response.text

    def test_malformed_body_with_credentials_is_400(self):
        response = requests.post(
            f"{API}/__test/db-identity",
            json={},
            headers={"X-Test-Probe-Secret": PROBE_SECRET},
            timeout=10,
        )
        assert response.status_code == 400, response.text


@needs_probe
class TestDatabaseIdentity:
    def test_marker_written_here_is_visible_to_the_server(self):
        marker = secrets.token_urlsafe(32)
        db = get_db()
        db["_test_identity"].insert_one({"_id": marker})
        try:
            response = _probe(marker)
        finally:
            db["_test_identity"].delete_one({"_id": marker})
        assert response.status_code == 200, response.text
        assert response.json()["matched"] is True

    def test_unknown_marker_is_not_a_match(self):
        response = _probe(secrets.token_urlsafe(32))
        assert response.status_code == 200
        assert response.json()["matched"] is False

    def test_marker_in_another_database_is_not_a_match(self):
        """The probe proves identity, not reachability.

        A marker in a sibling database is the exact situation the guard exists for: both
        processes can reach MongoDB, but pytest would empty one database while the server
        served another.
        """
        marker = secrets.token_urlsafe(32)
        other_name = f"{TEST_DB_NAME}_probe_{uuid.uuid4().hex[:8]}"
        client = get_db().client
        client[other_name]["_test_identity"].insert_one({"_id": marker})
        try:
            response = _probe(marker)
        finally:
            client.drop_database(other_name)
        assert response.status_code == 200
        assert response.json()["matched"] is False


@needs_probe
class TestUploadDirectoryIdentity:
    def test_reports_a_match_for_this_processes_directory(self):
        response = _probe(
            secrets.token_urlsafe(32), upload_dir=os.environ["UPLOAD_DIR"]
        )
        assert response.status_code == 200, response.text
        assert response.json()["uploads_match"] is True

    def test_reports_a_mismatch_for_another_directory(self, tmp_path):
        elsewhere = tmp_path / "uploads_test_elsewhere"
        elsewhere.mkdir()
        response = _probe(secrets.token_urlsafe(32), upload_dir=str(elsewhere))
        assert response.status_code == 200, response.text
        assert response.json()["uploads_match"] is False

    def test_path_is_never_echoed_back(self):
        response = _probe(secrets.token_urlsafe(32), upload_dir="C:/somewhere/else")
        assert response.status_code == 200
        assert set(response.json()) == {"matched", "uploads_match"}


class TestUploadDirectoryGuard:
    """No server involved: these are the checks that run before _wipe touches disk."""

    def test_rejects_missing_configuration(self):
        with pytest.raises(RuntimeError, match="UPLOAD_DIR must be set"):
            assert_test_upload_directory(None)

    def test_rejects_the_repository_uploads_directory(self):
        with pytest.raises(RuntimeError, match="repository's uploads"):
            assert_test_upload_directory(REPO_ROOT / "uploads")

    def test_rejects_a_subdirectory_of_repository_uploads(self):
        with pytest.raises(RuntimeError, match="repository's uploads"):
            assert_test_upload_directory(REPO_ROOT / "uploads" / "nested_test")

    def test_rejects_a_plausible_name_without_test(self, tmp_path):
        directory = tmp_path / "attachments"
        directory.mkdir()
        with pytest.raises(RuntimeError, match="not clearly a test directory"):
            assert_test_upload_directory(directory)

    def test_rejects_a_path_too_close_to_the_root(self):
        with pytest.raises(RuntimeError, match="too close to the filesystem root"):
            assert_test_upload_directory(Path(Path.cwd().anchor) / "test")

    def test_accepts_a_dedicated_test_directory(self, tmp_path):
        directory = tmp_path / "uploads_test"
        directory.mkdir()
        assert assert_test_upload_directory(directory) == directory.resolve()


class TestFixtureFailsClosed:
    """verify_same_database must never return normally on any doubt."""

    def _dir(self, tmp_path):
        directory = tmp_path / "uploads_test"
        directory.mkdir()
        return directory

    def test_refuses_a_short_secret(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEST_DB_PROBE_SECRET", "too-short")
        with pytest.raises(RuntimeError, match="at least 32 characters"):
            verify_same_database(get_db(), BASE_URL, self._dir(tmp_path))

    def test_refuses_when_the_probe_is_hidden(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEST_DB_PROBE_SECRET", "x" * 40)

        class Hidden:
            status_code = 404
            text = '{"detail":"Not Found"}'

            def json(self):
                return {"detail": "Not Found"}

        monkeypatch.setattr("conftest.requests.post", lambda *a, **k: Hidden())
        with pytest.raises(RuntimeError, match="returned 404"):
            verify_same_database(get_db(), BASE_URL, self._dir(tmp_path))

    def test_refuses_when_the_server_cannot_see_the_marker(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEST_DB_PROBE_SECRET", "x" * 40)

        class Divergent:
            status_code = 200
            text = ""

            def json(self):
                return {"matched": False, "uploads_match": True}

        monkeypatch.setattr("conftest.requests.post", lambda *a, **k: Divergent())
        with pytest.raises(RuntimeError, match="different databases"):
            verify_same_database(get_db(), BASE_URL, self._dir(tmp_path))

    def test_refuses_when_upload_directories_differ(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEST_DB_PROBE_SECRET", "x" * 40)

        class WrongDirectory:
            status_code = 200
            text = ""

            def json(self):
                return {"matched": True, "uploads_match": False}

        monkeypatch.setattr("conftest.requests.post", lambda *a, **k: WrongDirectory())
        with pytest.raises(RuntimeError, match="UPLOAD_DIR does not resolve"):
            verify_same_database(get_db(), BASE_URL, self._dir(tmp_path))

    def test_refuses_when_the_probe_is_unreachable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEST_DB_PROBE_SECRET", "x" * 40)

        def boom(*a, **k):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr("conftest.requests.post", boom)
        with pytest.raises(RuntimeError, match="could not be reached"):
            verify_same_database(get_db(), BASE_URL, self._dir(tmp_path))
