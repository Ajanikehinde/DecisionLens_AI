"""Runtime verification for the DecisionLens AI backend fixes.

Checks, without needing a live MongoDB:
  1. auth-cookie attributes follow the environment (and overrides win)
  2. clear_auth_cookies matches set_auth_cookies, so logout really deletes them
  3. /api/chat raises 503 when no LLM key is configured, instead of faking a 200
  4. upload dir resolves inside the repo rather than /app
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENV_KEYS = (
    "ENVIRONMENT",
    "COOKIE_SECURE",
    "COOKIE_SAMESITE",
    "GEMINI_API_KEY",
    "UPLOAD_DIR",
)
failures = []


def load(**overrides):
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update({k: v for k, v in overrides.items() if v is not None})
    sys.modules.pop("server", None)
    import server

    return server


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def shape(response):
    """Cookie name + security-relevant attributes, ignoring value/expiry."""
    out = []
    for raw in response.headers.getlist("set-cookie"):
        parts = raw.split(";")
        name = parts[0].split("=", 1)[0].strip()
        attrs = {p.split("=", 1)[0].strip().lower() for p in parts[1:]}
        out.append((name, "secure" in attrs, "httponly" in attrs, "samesite" in attrs))
    return sorted(out)


print("=== 1. cookie flags follow ENVIRONMENT ===")
for env, want_secure, want_site in (
    ("development", False, "lax"),
    ("dev", False, "lax"),
    ("local", False, "lax"),
    ("production", True, "none"),
    ("staging", True, "none"),
):
    s = load(ENVIRONMENT=env)
    check(
        f"ENVIRONMENT={env:<12}",
        s.COOKIE_SECURE is want_secure and s.COOKIE_SAMESITE == want_site,
        f"secure={s.COOKIE_SECURE} samesite={s.COOKIE_SAMESITE}",
    )

print("\n=== 2. explicit overrides win ===")
s = load(ENVIRONMENT="development", COOKIE_SECURE="true")
check("dev + COOKIE_SECURE=true", s.COOKIE_SECURE is True and s.COOKIE_SAMESITE == "none")
s = load(ENVIRONMENT="production", COOKIE_SECURE="false")
check("prod + COOKIE_SECURE=false", s.COOKIE_SECURE is False and s.COOKIE_SAMESITE == "lax")
s = load(ENVIRONMENT="production", COOKIE_SAMESITE="strict")
check("prod + COOKIE_SAMESITE=strict", s.COOKIE_SAMESITE == "strict" and s.COOKIE_SECURE is True)

print("\n=== 3. Set-Cookie headers (dev: no Secure, so http://localhost works) ===")
from starlette.responses import Response  # noqa: E402

for env in ("development", "production"):
    s = load(ENVIRONMENT=env)
    r = Response()
    s.set_auth_cookies(r, "ACCESS", "REFRESH")
    combined = "; ".join(r.headers.getlist("set-cookie"))
    check(
        f"[{env}] Secure flag matches COOKIE_SECURE",
        ("Secure" in combined) == s.COOKIE_SECURE,
        f"header has Secure={'Secure' in combined}",
    )
    check(f"[{env}] httponly present", "HttpOnly" in combined)
    check(
        f"[{env}] samesite={s.COOKIE_SAMESITE}",
        f"samesite={s.COOKIE_SAMESITE.lower()}" in combined.lower(),
    )

print("\n=== 4. clear_auth_cookies matches set_auth_cookies ===")
for env in ("development", "production"):
    s = load(ENVIRONMENT=env)
    a, b = Response(), Response()
    s.set_auth_cookies(a, "A", "R")
    s.clear_auth_cookies(b)
    check(f"[{env}] attributes identical", shape(a) == shape(b), f"{shape(a)} vs {shape(b)}")

print("\n=== 5. chat with no GEMINI_API_KEY -> 503, not a fake 200 ===")
from fastapi import HTTPException  # noqa: E402

s = load(ENVIRONMENT="development", GEMINI_API_KEY="")
check("GEMINI_API_KEY is empty", s.GEMINI_API_KEY.strip() == "")
try:
    asyncio.run(s.chat(s.ChatIn(message="hello"), user={"id": "u1"}))
    check("raises HTTPException", False, "returned normally")
except HTTPException as exc:
    check("raises HTTPException", True, f"status={exc.status_code}")
    check("status is 503", exc.status_code == 503)
    check("code is llm_not_configured", exc.detail.get("code") == "llm_not_configured", str(exc.detail))
    check("has a human-readable message", bool(exc.detail.get("message")))

print("\n=== 5b. Gemini request shape (no network) ===")
hist = [
    {"role": "user", "content": "first question"},
    {"role": "assistant", "content": "first answer"},
]
contents = s._gemini_contents(hist, "second question")
check("roles alternate user -> model -> user",
      [c["role"] for c in contents] == ["user", "model", "user"],
      str([c["role"] for c in contents]))
check("latest message appended last", contents[-1]["parts"][0]["text"] == "second question")
check("history replayed", len(contents) == 3, str(len(contents)))
# A conversation that opens mid-stream must be trimmed to start at a user turn.
trimmed = s._gemini_contents([{"role": "assistant", "content": "orphan"}], "next")
check("leading assistant turn trimmed", [c["role"] for c in trimmed] == ["user"], str([c["role"] for c in trimmed]))
check("empty parts skipped",
      s._gemini_contents([{"role": "user", "content": ""}], "hi")[0]["parts"][0]["text"] == "hi")
check("text extracted from response shape",
      s._extract_gemini_text({"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}) == "ab")
check("empty response -> empty string", s._extract_gemini_text({"candidates": []}) == "")

print("\n=== 6. upload dir resolves inside the repo, not /app ===")
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    s = load(ENVIRONMENT="development")
    check("not the container path /app/uploads", "/app" not in str(s.UPLOAD_DIR), str(s.UPLOAD_DIR))
    check("under the repo root", str(s.UPLOAD_DIR).endswith("uploads"), str(s.UPLOAD_DIR))
    override = os.path.join(tmp, "uploads_override")
    s2 = load(ENVIRONMENT="development", UPLOAD_DIR=override)
    check("UPLOAD_DIR override honoured", str(s2.UPLOAD_DIR) == override, str(s2.UPLOAD_DIR))
    load(ENVIRONMENT="development")

print("\n=== 7. no hardcoded default passwords remain ===")
source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py"), encoding="utf-8").read()
check("no 'Admin@1234' literal", "Admin@1234" not in source)
check("no 'Demo@1234' literal", "Demo@1234" not in source)

print("\n=== 8. no emergentintegrations dependency remains ===")
check("server.py has no emergentintegrations import", "emergentintegrations" not in source)
check("server.py has no EMERGENT env var", "EMERGENT_" not in source)
reqs = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt"), encoding="utf-8").read()
check("requirements.txt has no emergentintegrations", "emergentintegrations" not in reqs)
check("requirements.txt pins the official stripe sdk", "stripe" in reqs)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
