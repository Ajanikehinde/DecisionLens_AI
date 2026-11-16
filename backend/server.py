from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import re
import uuid
import json
import logging
import random
import secrets
import bcrypt
import jwt
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, List, Optional, Literal
from uuid import UUID

from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, Field, EmailStr

# ----------------- Config -----------------
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALG = "HS256"
ACCESS_TTL_MIN = 60 * 24  # 1 day for demo
REFRESH_TTL_DAYS = 7
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (ROOT_DIR.parent / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# Google Gemini, called over its public REST API. No provider SDK is required.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# Cap how much stored conversation is replayed on each turn to bound token usage.
CHAT_HISTORY_TURNS = int(os.environ.get("CHAT_HISTORY_TURNS", "20"))
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "60"))
# Stage 2 idempotency. A client may resend the same request_id; the server must not create
# a second user message. Attempts are counted so a failing turn cannot be retried forever.
MAX_CHAT_ATTEMPTS = int(os.environ.get("MAX_CHAT_ATTEMPTS", "3"))
# How long a "processing" row may sit before a repeat treats its worker as dead. This is a
# heuristic, never proof: a paused-but-alive worker is indistinguishable from a dead one,
# which is why the attempt budget bounds the damage (see the contract in AGENTS.md).
CHAT_ATTEMPT_LEASE_SECONDS = int(os.environ.get("CHAT_ATTEMPT_LEASE_SECONDS", "180"))

# A lease shorter than the provider timeout would let a *live* Gemini call be reclaimed and
# re-dispatched, guaranteeing duplicate upstream work. This must fail at import, not be
# discovered in production.
if CHAT_ATTEMPT_LEASE_SECONDS <= GEMINI_TIMEOUT_S:
    raise ValueError(
        f"CHAT_ATTEMPT_LEASE_SECONDS ({CHAT_ATTEMPT_LEASE_SECONDS}) must exceed "
        f"GEMINI_TIMEOUT_S ({GEMINI_TIMEOUT_S}); otherwise a live Gemini call could be "
        "mistaken for an abandoned attempt and dispatched twice."
    )

# Auth cookies only work over HTTPS when Secure is set. Local development runs on
# plain http://localhost, where a Secure cookie is silently dropped by the browser,
# so the flag has to follow the deployment environment instead of being hardcoded.
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").strip().lower()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE")
if COOKIE_SECURE is None or COOKIE_SECURE == "":
    COOKIE_SECURE = ENVIRONMENT not in {"development", "dev", "local"}
else:
    COOKIE_SECURE = COOKIE_SECURE.strip().lower() in {"1", "true", "yes", "on"}
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE") or ("none" if COOKIE_SECURE else "lax")

# Ceiling on documents pulled into memory for aggregate/report endpoints.
MAX_TREND_DECISIONS = 5000

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="DecisionLens AI API")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("decisionlens")


# ----------------- Helpers -----------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": now_utc() + timedelta(minutes=ACCESS_TTL_MIN),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": now_utc() + timedelta(days=REFRESH_TTL_DAYS),
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def set_auth_cookies(response: Response, access: str, refresh: str):
    response.set_cookie("access_token", access, httponly=True, secure=COOKIE_SECURE,
                        samesite=COOKIE_SAMESITE, max_age=ACCESS_TTL_MIN * 60, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=COOKIE_SECURE,
                        samesite=COOKIE_SAMESITE, max_age=REFRESH_TTL_DAYS * 86400, path="/")


def clear_auth_cookies(response: Response):
    # Attributes must match those used in set_auth_cookies, otherwise the browser
    # keeps the original cookie and the session survives logout.
    response.delete_cookie("access_token", path="/", secure=COOKIE_SECURE,
                           httponly=True, samesite=COOKIE_SAMESITE)
    response.delete_cookie("refresh_token", path="/", secure=COOKIE_SECURE,
                           httponly=True, samesite=COOKIE_SAMESITE)


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"id": payload["sub"]})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        user.pop("_id", None)
        user.pop("password_hash", None)
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ----------------- Models -----------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    name: str = Field(min_length=1, max_length=80)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str


class ProfileUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


Status = Literal["draft", "active", "review", "complete"]
Risk = Literal["low", "medium", "high", "critical"]
Category = Literal["business_strategy", "hiring", "investment", "operations", "marketing", "technology"]


class DecisionIn(BaseModel):
    title: str = Field(min_length=2, max_length=140)
    description: str = ""
    category: Category
    risk: Risk = "medium"
    status: Status = "active"
    confidence: int = Field(ge=0, le=100, default=70)
    stakeholders: List[str] = []
    deadline: Optional[str] = None  # ISO date


class DecisionPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[Category] = None
    risk: Optional[Risk] = None
    status: Optional[Status] = None
    confidence: Optional[int] = None
    stakeholders: Optional[List[str]] = None
    deadline: Optional[str] = None


class ChatIn(BaseModel):
    session_id: Optional[str] = None
    message: str
    # Optional idempotency key, generated by the client. Typed as UUID so a malformed value
    # is a 422 rather than silently degrading to "no key", and so equivalent spellings
    # (braces, urn:uuid:, uppercase) canonicalise to one stored value.
    request_id: Optional[UUID] = None


# ----------------- Auth Endpoints -----------------
@api.post("/auth/register", response_model=UserOut)
async def register(body: RegisterIn, response: Response):
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = str(uuid.uuid4())
    doc = {
        "id": user_id,
        "email": email,
        "name": body.name,
        "role": "user",
        "password_hash": hash_password(body.password),
        "created_at": now_iso(),
    }
    await db.users.insert_one(doc)
    access = create_access_token(user_id, email)
    refresh = create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh)
    # Starter decisions are deliberately NOT seeded for real signups. Seeding wrote rows
    # backdated 0-28 days, so a share of them landed inside the current quota window and
    # silently consumed the new user's decisions_per_month allowance before they wrote a
    # word; the count was random, so some signups started already over the cap. The rich
    # dataset still gets seeded, but only onto the admin and demo accounts, which is what
    # seed_users() does. A brand-new account now starts at a true 0 used.
    return UserOut(id=user_id, email=email, name=body.name, role="user")


@api.post("/auth/login", response_model=UserOut)
async def login(body: LoginIn, response: Response):
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    access = create_access_token(user["id"], user["email"])
    refresh = create_refresh_token(user["id"])
    set_auth_cookies(response, access, refresh)
    return UserOut(id=user["id"], email=user["email"], name=user["name"], role=user.get("role", "user"))


@api.post("/auth/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@api.get("/auth/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return UserOut(id=user["id"], email=user["email"], name=user["name"], role=user.get("role", "user"))


@api.patch("/auth/me", response_model=UserOut)
async def update_profile(body: ProfileUpdateIn, user=Depends(get_current_user)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    await db.users.update_one({"id": user["id"]}, {"$set": {"name": name}})
    return UserOut(id=user["id"], email=user["email"], name=name, role=user.get("role", "user"))


@api.post("/auth/change-password")
async def change_password(body: ChangePasswordIn, user=Depends(get_current_user)):
    doc = await db.users.find_one({"id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")
    if not verify_password(body.current_password, doc["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"password_hash": hash_password(body.new_password)}},
    )
    # Re-issue the session so the current browser keeps working with a fresh token.
    access = create_access_token(user["id"], user["email"])
    refresh = create_refresh_token(user["id"])
    return {"ok": True, "access_token": access, "refresh_token": refresh}


# ----------------- Decisions CRUD -----------------
def _clean(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


@api.get("/decisions")
async def list_decisions(
    user=Depends(get_current_user),
    status: Optional[Status] = None,
    risk: Optional[Risk] = None,
    category: Optional[Category] = None,
    q: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=500),
):
    query: dict = {"user_id": user["id"]}
    if status:
        query["status"] = status
    if risk:
        query["risk"] = risk
    if category:
        query["category"] = category
    if q:
        # Treat the search term as a literal substring. Passing it through unescaped
        # made any malformed pattern (e.g. "[") raise inside the driver and surface
        # as an opaque HTTP 500.
        query["title"] = {"$regex": re.escape(q), "$options": "i"}
    try:
        cursor = db.decisions.find(query).sort("created_at", -1).limit(limit)
        items = [_clean(d) async for d in cursor]
    except Exception as e:
        logger.warning("Decision search failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid search query")
    return items


@api.post("/decisions")
async def create_decision(body: DecisionIn, user=Depends(get_current_user)):
    await _enforce_limit("decisions_per_month", user["id"], user.get("role"))
    doc = body.model_dump()
    doc.update({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    })
    await db.decisions.insert_one(doc)
    return _clean(doc)


@api.get("/decisions/{decision_id}")
async def get_decision(decision_id: str, user=Depends(get_current_user)):
    doc = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="Decision not found")
    return _clean(doc)


@api.patch("/decisions/{decision_id}")
async def update_decision(decision_id: str, body: DecisionPatch, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    # fetch previous state for diff
    prev = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not prev:
        raise HTTPException(status_code=404, detail="Decision not found")
    updates["updated_at"] = now_iso()
    await db.decisions.update_one({"id": decision_id, "user_id": user["id"]}, {"$set": updates})
    # log timeline events for tracked fields
    tracked = {
        "title": "Title",
        "status": "Status",
        "risk": "Risk",
        "category": "Category",
        "confidence": "Confidence",
        "deadline": "Deadline",
    }
    activity_docs = []
    for field, label in tracked.items():
        if field in updates and updates[field] != prev.get(field):
            old_v = prev.get(field)
            new_v = updates[field]
            suffix = "%" if field == "confidence" else ""
            msg = f"{label} changed: {old_v}{suffix} → {new_v}{suffix}"
            activity_docs.append({
                "id": str(uuid.uuid4()),
                "decision_id": decision_id,
                "user_id": user["id"],
                "actor_name": user["name"],
                "type": f"update_{field}",
                "field": field,
                "old_value": str(old_v) if old_v is not None else None,
                "new_value": str(new_v) if new_v is not None else None,
                "message": msg,
                "created_at": now_iso(),
            })
    if activity_docs:
        await db.activity.insert_many(activity_docs)
    doc = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    return _clean(doc)


@api.delete("/decisions/{decision_id}")
async def delete_decision(decision_id: str, user=Depends(get_current_user)):
    doc = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="Decision not found")

    # Children are removed before the parent. The reverse order can leave a live decision
    # whose comments and attachments have already vanished, and if this half-fails the
    # damage is a visible decision missing history rather than invisible orphan rows that
    # no read path can reach.
    #
    # Attachment files are collected first because deleting the rows loses stored_name, and
    # unlink runs afterwards: a file that outlives its row is recoverable, an orphaned row
    # whose file is already gone is not. This also satisfies the erasure expectation that
    # deleting a decision removes the documents attached to it, not just the decision.
    stored_names = [
        a["stored_name"]
        async for a in db.attachments.find(
            {"decision_id": decision_id, "user_id": user["id"]}, {"stored_name": 1}
        )
        if a.get("stored_name")
    ]
    await db.attachments.delete_many({"decision_id": decision_id, "user_id": user["id"]})
    await db.comments.delete_many({"decision_id": decision_id, "user_id": user["id"]})
    await db.activity.delete_many({"decision_id": decision_id, "user_id": user["id"]})
    result = await db.decisions.delete_one({"id": decision_id, "user_id": user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Decision not found")

    for name in stored_names:
        try:
            (UPLOAD_DIR / name).unlink(missing_ok=True)
        except Exception:
            # A file that cannot be removed must not fail the delete; the row is already
            # gone, so nothing can reference it. Logged rather than raised.
            logger.warning("Could not remove upload %s for deleted decision", name)
    return {"ok": True, "deleted": {"attachments": len(stored_names)}}


# ----------------- Dashboard analytics -----------------
@api.get("/dashboard/summary")
async def dashboard_summary(user=Depends(get_current_user)):
    pipeline_all = [{"$match": {"user_id": user["id"]}}]
    total = await db.decisions.count_documents({"user_id": user["id"]})
    active = await db.decisions.count_documents({"user_id": user["id"], "status": "active"})
    completed = await db.decisions.count_documents({"user_id": user["id"], "status": "complete"})
    review = await db.decisions.count_documents({"user_id": user["id"], "status": "review"})

    # avg confidence
    avg_conf_cursor = db.decisions.aggregate(pipeline_all + [
        {"$group": {"_id": None, "avg": {"$avg": "$confidence"}}}
    ])
    avg_conf_doc = await avg_conf_cursor.to_list(length=1)
    avg_confidence = round(avg_conf_doc[0]["avg"]) if avg_conf_doc else 0

    # categories
    cats = db.decisions.aggregate(pipeline_all + [
        {"$group": {"_id": "$category", "count": {"$sum": 1}}}
    ])
    categories = {d["_id"]: d["count"] async for d in cats}
    cat_keys = ["business_strategy", "hiring", "investment", "operations", "marketing", "technology"]
    categories_arr = [{"category": k, "count": categories.get(k, 0)} for k in cat_keys]

    # risk distribution
    risks = db.decisions.aggregate(pipeline_all + [
        {"$group": {"_id": "$risk", "count": {"$sum": 1}}}
    ])
    risk_map = {d["_id"]: d["count"] async for d in risks}
    risk_keys = ["low", "medium", "high", "critical"]
    risk_arr = [{"risk": k, "count": risk_map.get(k, 0)} for k in risk_keys]

    # decision trends (30 days / 12 weeks / 12 months)
    # One bounded fetch feeds all three buckets. Each bucket previously ran its own
    # unbounded scan over the user's entire decision set: 3x the query work and an
    # unbounded number of documents pulled into memory.
    cutoff = (now_utc() - timedelta(days=29)).date()
    now_d = now_utc().date()
    week_cutoff = now_d - timedelta(weeks=11)
    trend_map: dict = {}
    weekly_map: dict = {}
    monthly_map: dict = {}
    trend_cursor = (
        db.decisions.find({"user_id": user["id"]}, {"created_at": 1, "_id": 0})
        .sort("created_at", -1)
        .limit(MAX_TREND_DECISIONS)
    )
    async for d in trend_cursor:
        try:
            day = datetime.fromisoformat(d["created_at"]).date()
        except Exception:
            continue
        if day >= cutoff:
            key = day.isoformat()
            trend_map[key] = trend_map.get(key, 0) + 1
        if day >= week_cutoff:
            monday = day - timedelta(days=day.weekday())
            weekly_map[monday.isoformat()] = weekly_map.get(monday.isoformat(), 0) + 1
        mkey = f"{day.year:04d}-{day.month:02d}"
        monthly_map[mkey] = monthly_map.get(mkey, 0) + 1

    trends_daily = []
    for i in range(30):
        day = (cutoff + timedelta(days=i)).isoformat()
        trends_daily.append({"date": day, "count": trend_map.get(day, 0)})

    trends_weekly = []
    for i in range(12):
        monday = (week_cutoff + timedelta(weeks=i))
        # align to monday
        monday = monday - timedelta(days=monday.weekday())
        key = monday.isoformat()
        trends_weekly.append({"date": key, "count": weekly_map.get(key, 0)})

    trends_monthly = []
    today = now_utc().date()
    y, m = today.year, today.month
    months_back = []
    for _ in range(12):
        months_back.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    for key in reversed(months_back):
        trends_monthly.append({"date": key, "count": monthly_map.get(key, 0)})

    # Decision Health Score signature metric
    # factors:
    data_quality = min(100, total * 2)  # more decisions = more data
    stakeholder_alignment = 90 if total > 0 else 50
    # risk assessment penalizes high+critical
    high_risk = risk_map.get("high", 0) + risk_map.get("critical", 0)
    risk_assessment = max(40, 100 - (high_risk * 5))
    ai_confidence = avg_confidence

    health_factors = [
        {"label": "Data Quality", "score": data_quality, "ok": data_quality >= 60},
        {"label": "Stakeholder Alignment", "score": stakeholder_alignment, "ok": stakeholder_alignment >= 70},
        {"label": "Risk Assessment", "score": risk_assessment, "ok": risk_assessment >= 60},
        {"label": "AI Confidence", "score": ai_confidence, "ok": ai_confidence >= 75},
    ]
    health_score = round((data_quality + stakeholder_alignment + risk_assessment + ai_confidence) / 4)

    # recent decisions
    recent_cursor = db.decisions.find({"user_id": user["id"]}).sort("created_at", -1).limit(6)
    recent = [_clean(d) async for d in recent_cursor]

    # Mock trend deltas vs prior period
    def pct(_): return random.randint(5, 18)

    return {
        "kpis": {
            "total": {"value": total, "delta": 12},
            "active": {"value": active, "delta": 8},
            "completed": {"value": completed, "delta": 15},
            "review": {"value": review, "delta": 4},
            "confidence": {"value": avg_confidence, "delta": 5},
        },
        "health": {
            "score": health_score,
            "factors": health_factors,
        },
        "trends": {
            "daily": trends_daily,
            "weekly": trends_weekly,
            "monthly": trends_monthly,
        },
        "categories": categories_arr,
        "risk_distribution": risk_arr,
        "recent_decisions": recent,
    }


# ----------------- AI Recommendations (static-smart) -----------------
@api.get("/recommendations")
async def get_recommendations(user=Depends(get_current_user)):
    # compute heuristic-driven recs from user's data
    decisions = [d async for d in db.decisions.find({"user_id": user["id"]}).limit(MAX_TREND_DECISIONS)]
    high_risk_count = sum(1 for d in decisions if d.get("risk") in ("high", "critical"))
    low_conf = [d for d in decisions if d.get("confidence", 0) < 80 and d.get("status") in ("active", "review")]
    stuck_review = [d for d in decisions if d.get("status") == "review"]

    recs = []
    if low_conf:
        recs.append({
            "id": "rec-low-conf",
            "title": "Confidence score below 80%",
            "body": f"{len(low_conf)} active decision(s) have low confidence. Gather supporting market data to reduce ambiguity.",
            "severity": "warning",
            "icon": "alert-triangle",
        })
    if high_risk_count:
        recs.append({
            "id": "rec-risk",
            "title": "Reduce risk exposure",
            "body": f"{high_risk_count} decision(s) flagged High/Critical risk. Schedule a risk-mitigation workshop.",
            "severity": "danger",
            "icon": "shield-alert",
        })
    if stuck_review:
        recs.append({
            "id": "rec-review",
            "title": "Schedule stakeholder review",
            "body": f"{len(stuck_review)} decision(s) sitting in Review. Move them forward with a 30-min alignment call.",
            "severity": "info",
            "icon": "users",
        })
    # always-on baseline recs
    baseline = [
        {"id": "rec-baseline-1", "title": "Run a pre-mortem",
         "body": "For every High-risk decision, write a pre-mortem listing the top 3 failure modes before approval.",
         "severity": "info", "icon": "lightbulb"},
        {"id": "rec-baseline-2", "title": "Calibrate confidence",
         "body": "Confidence numbers above 90% are often overconfident. Pair with a 'what would change my mind?' note.",
         "severity": "info", "icon": "target"},
        {"id": "rec-baseline-3", "title": "Track outcomes",
         "body": "Revisit completed decisions after 30 days and log whether the outcome matched the prediction.",
         "severity": "info", "icon": "activity"},
    ]
    while len(recs) < 3:
        recs.append(baseline.pop(0))
    return recs[:4]


# ----------------- Sources (static cards) -----------------
@api.get("/sources")
async def get_sources(user=Depends(get_current_user)):
    return [
        {"id": "salesforce", "name": "Salesforce CRM", "category": "CRM",
         "status": "connected", "last_sync": now_iso(), "records": 12480},
        {"id": "hubspot", "name": "HubSpot Marketing", "category": "Marketing",
         "status": "connected", "last_sync": now_iso(), "records": 8120},
        {"id": "ga4", "name": "Google Analytics 4", "category": "Web Analytics",
         "status": "connected", "last_sync": now_iso(), "records": 45000},
        {"id": "stripe", "name": "Stripe", "category": "Payments",
         "status": "connected", "last_sync": now_iso(), "records": 3140},
        {"id": "snowflake", "name": "Snowflake Warehouse", "category": "Data Warehouse",
         "status": "syncing", "last_sync": now_iso(), "records": 0},
        {"id": "slack", "name": "Slack Signals", "category": "Communication",
         "status": "disconnected", "last_sync": None, "records": 0},
    ]


# ----------------- AI Chat (Gemini via REST) -----------------
CHAT_SYSTEM_PROMPT = (
    "You are DecisionLens AI, a sharp decision-intelligence copilot for executives and product leaders. "
    "Help users analyze, structure, and de-risk business decisions. Be concise, structured, "
    "and back claims with reasoning. Use bullet points where useful. Prefer numbered frameworks."
)


def _gemini_contents(history: list[dict], user_message: str) -> list[dict]:
    """Build a Gemini `contents` array from stored chat rows.

    Gemini names the assistant role `model`, and requires the conversation to start
    with a `user` turn. History is replayed explicitly rather than relying on any
    provider-side session state, so the request is reproducible.
    """
    contents = [
        {
            "role": "user" if m["role"] == "user" else "model",
            "parts": [{"text": m["content"]}],
        }
        for m in history
        if m.get("content")
    ]
    while contents and contents[0]["role"] != "user":
        contents.pop(0)
    contents.append({"role": "user", "parts": [{"text": user_message}]})
    return contents


def _extract_gemini_text(payload: dict) -> str:
    """Pull the concatenated text parts out of a generateContent response."""
    chunks: List[str] = []
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks).strip()


def _call_gemini_sync(contents: list[dict]) -> str:
    """Blocking Gemini call, always executed in a worker thread."""
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": CHAT_SYSTEM_PROMPT}]},
        "generationConfig": {"temperature": 0.7},
    }
    resp = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=GEMINI_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API returned {resp.status_code}: {resp.text[:400]}")
    return _extract_gemini_text(resp.json())


def _call_gemini_json_sync(prompt: str, system: str) -> str:
    """Blocking Gemini call that is constrained to return raw JSON.

    responseMimeType asks the provider for a JSON body; we still defensively strip
    code fences because models occasionally wrap the object anyway.
    """
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
    }
    resp = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=GEMINI_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API returned {resp.status_code}: {resp.text[:400]}")
    return _extract_gemini_text(resp.json())


def _parse_json_object(raw: str) -> dict:
    """Best-effort extraction of a JSON object from a model response."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response")
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON object found")


ANALYSIS_SYSTEM = (
    "You are a decision-intelligence analyst. You return ONLY a JSON object, with no "
    "prose and no code fences, using exactly these keys: executive_summary (string), "
    "recommendation (string), pros (array of strings), cons (array of strings), "
    "risks (array of strings), next_steps (array of strings). "
    "Keep each string concrete and specific to the decision. Markdown is allowed inside "
    "the strings."
)

ANALYSIS_PROMPT = """Analyse this business decision and return the JSON object described in your instructions.

Title: {title}
Category: {category}
Risk: {risk}
Status: {status}
Confidence: {confidence}%
Stakeholders: {stakeholders}
Deadline: {deadline}

Description / context:
{description}

Required JSON shape:
{{
  "executive_summary": "2-4 sentences framing the decision and why it matters now.",
  "recommendation": "A clear call: what to do, or explicitly what the deciding factor is.",
  "pros": ["3-5 short advantages of the proposed path"],
  "cons": ["3-5 short disadvantages or risks of the proposed path"],
  "risks": ["3-5 specific risks with a mitigation hint where useful"],
  "next_steps": ["3-5 concrete actions, most important first"]
}}
"""


def _coerce_analysis(raw: dict) -> dict:
    """Normalise the model output into the stored shape, or raise."""
    def as_text(v, limit=4000):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            v = "\n".join(str(x) for x in v)
        return str(v).strip()[:limit]

    def as_list(v, limit=8):
        if v is None:
            return []
        if isinstance(v, str):
            parts = [p.strip(" -\t") for p in v.splitlines()]
            return [p for p in parts if p][:limit]
        if isinstance(v, dict):
            return [f"{k}: {as_text(x, 300)}" for k, x in list(v.items())[:limit]]
        return [str(x).strip() for x in v if str(x).strip()][:limit]

    out = {
        "executive_summary": as_text(raw.get("executive_summary") or raw.get("summary")),
        "recommendation": as_text(raw.get("recommendation") or raw.get("verdict")),
        "pros": as_list(raw.get("pros")),
        "cons": as_list(raw.get("cons")),
        "risks": as_list(raw.get("risks")),
        "next_steps": as_list(raw.get("next_steps") or raw.get("nextSteps")),
    }
    if not out["executive_summary"] and not out["recommendation"]:
        raise ValueError("model returned neither a summary nor a recommendation")
    return out


# ----------------- Chat message status (Stage 1) -----------------
# Every user message records the outcome of its exchange, so an outage, a crash and a
# genuine answer stay distinguishable long after the request finished. Status lives on
# user rows only; assistant rows are evidence that a reply exists, not state.
MESSAGE_STATUSES = ("pending", "processing", "answered", "failed")

# Only these edges are legal. ``answered`` is absolutely terminal and no row can skip the
# in-flight state, so a lost update is reported instead of silently overwritten. ``failed``
# gained one outgoing edge in Stage 2 so a client can retry the same user message instead of
# creating a duplicate one; that edge is reachable only through ``_retry_message``.
_ALLOWED_TRANSITIONS = {
    "pending": ("processing",),
    "processing": ("answered", "failed"),
    "answered": (),
    "failed": ("processing",),
}

# The only status that may be replayed to the model. "unknown" (a row written before this
# field existed) is deliberately absent: its provenance cannot be established, and the
# pre-503-fix code could return fabricated text as a normal answer.
REPLAYABLE_STATUS = "answered"


class MessageStateError(RuntimeError):
    """A status write found a row that does not allow the requested transition."""


class ReplyPersistenceError(RuntimeError):
    """A reply was generated but could not be stored and acknowledged."""


def _attempt_of(doc: dict) -> int:
    """The attempt counter of a stored row, defaulting to 1 for pre-Stage-2 rows."""
    return int(doc.get("attempt") or 1)


def _attempt_filter(attempt: int) -> dict:
    """Match an attempt number, treating a missing counter as attempt 1.

    Rows written before Stage 2 have no ``attempt`` field. Normalising the *filter* keeps
    them retryable without rewriting or backfilling any stored document.
    """
    if attempt <= 1:
        return {"$or": [{"attempt": 1}, {"attempt": {"$exists": False}}]}
    return {"attempt": attempt}


def _check_transition(current: str, target: str) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current)
    if allowed is None:
        raise MessageStateError(f"unknown message status {current!r}")
    if target not in allowed:
        raise MessageStateError(f"illegal transition {current!r} -> {target!r}")


async def _set_message_status(
    message_id: str, current: str, target: str, attempt: Optional[int] = None, **fields
) -> None:
    """Compare-and-set a user message's status, proving where it started.

    ``current`` is part of the update filter, so the check and the write are one atomic
    operation in MongoDB rather than a read followed by a write. ``matched_count`` of 0
    means the row was not in ``current`` -- another writer got there first, or the row was
    never advanced -- and is raised rather than treated as success.

    ``attempt`` narrows that proof to one *attempt*. Without it, a worker whose lease
    expired and was replaced by a newer attempt could still match on ``status`` alone and
    overwrite the newer attempt's outcome with its own.
    """
    _check_transition(current, target)
    filt: dict = {"id": message_id, "role": "user", "status": current}
    if attempt is not None:
        filt.update(_attempt_filter(attempt))
    result = await db.chat_messages.update_one(
        filt,
        {"$set": {"status": target, "status_updated_at": now_iso(), **fields}},
    )
    if result.matched_count != 1:
        detail = f"attempt {attempt}" if attempt is not None else "any attempt"
        raise MessageStateError(
            f"message {message_id} was not in status {current!r} ({detail}) "
            f"(matched={result.matched_count}); refusing {current!r} -> {target!r}"
        )


async def _mark_failed(
    message_id: str, current: str, error_code: str, attempt: Optional[int] = None
) -> None:
    """Best-effort terminal write. Never raises, so it cannot mask the real failure."""
    try:
        await _set_message_status(
            message_id, current, "failed", attempt=attempt, error_code=error_code
        )
    except Exception:
        logger.exception("Could not mark message %s failed (%s)", message_id, error_code)


async def _insert_reply(session_id: str, user_id: str, content: str) -> str:
    reply_id = str(uuid.uuid4())
    await db.chat_messages.insert_one({
        "id": reply_id,
        "session_id": session_id,
        "user_id": user_id,
        "role": "assistant",
        "content": content,
        "created_at": now_iso(),
    })
    return reply_id


async def _persist_answer(
    message_id: str, session_id: str, user_id: str, content: str, attempt: Optional[int] = None
) -> str:
    """Store the reply and mark the turn answered as one unit.

    A reply that is stored but not acknowledged would leave history showing an answer that
    the tracking says never happened, so the status update belongs to the same step. If it
    fails, the reply row is removed again. That rollback is best effort: if it also fails,
    the orphan assistant row is inert, because replay pairs by ``reply_message_id`` and a
    user row still in ``processing`` is excluded.

    ``attempt`` pins the acknowledgement to the attempt that produced the reply, so a worker
    that resumes after its lease expired cannot mark a newer attempt answered with stale text.
    """
    reply_id = await _insert_reply(session_id, user_id, content)
    try:
        await _set_message_status(
            message_id, "processing", "answered", attempt=attempt, reply_message_id=reply_id
        )
    except Exception as exc:
        try:
            await db.chat_messages.delete_one({"id": reply_id, "role": "assistant"})
        except Exception:
            logger.exception("Rollback of reply %s failed", reply_id)
        raise ReplyPersistenceError(str(exc)) from exc
    return reply_id


def _replayable_history(rows: list[dict]) -> list[dict]:
    """Select only exchanges that provably completed.

    Pairs are joined by id, never by adjacency, so ordering ties cannot mispair them. Rows
    that are failed, pending, processing or legacy (no status) are skipped here and
    nowhere else: the history endpoints still return them, they just never reach the model.
    """
    by_id = {r["id"]: r for r in rows if r.get("id")}
    out: list[dict] = []
    for row in rows:
        if row.get("role") != "user" or row.get("status") != REPLAYABLE_STATUS:
            continue
        reply = by_id.get(row.get("reply_message_id"))
        if not reply or reply.get("role") != "assistant" or not reply.get("content"):
            continue
        if not row.get("content"):
            continue
        out.append({"role": "user", "content": row["content"]})
        out.append({"role": "assistant", "content": reply["content"]})
    return out


def _serialize_chat_message(doc: dict) -> dict:
    """Shape a stored row for the API without rewriting storage.

    A row written before statuses existed has no provable outcome, so it is reported as
    "unknown" rather than being labelled answered. The value is computed at the read
    boundary; the stored document is never modified.

    ``requested_session_id`` is dropped: it exists only to tell an interrupted first
    request (client sent ``null``, server generated an id) apart from a retry against the
    wrong session, and means nothing to a client.
    """
    out = {k: v for k, v in doc.items() if k not in ("_id", "requested_session_id")}
    if out.get("role") == "user" and not out.get("status"):
        out["status"] = "unknown"
    return out


# ----------------- Chat idempotency (Stage 2) -----------------
# A client may resend the same request_id after a dropped connection. The server must then
# answer from durable state instead of calling Gemini again, and must never create a second
# user message. The unique index on (user_id, request_id) is the authority; the read that
# precedes it is only a fast path, so a race cannot produce two rows either.

CHAT_REQUEST_INDEX = "uniq_chat_request_per_user"


def _error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message, **extra}
    )


def _lease_expired(doc: dict) -> bool:
    """Whether a non-terminal row has outlived the attempt lease.

    A heuristic by construction. Durable state cannot distinguish "the worker died" from
    "the worker is paused", so this must never be treated as proof that the previous attempt
    stopped. The caller relies on the attempt budget to bound the cost of guessing wrong.
    """
    stamp = doc.get("status_updated_at")
    if not stamp:
        return True
    try:
        started = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (now_utc() - started).total_seconds() > CHAT_ATTEMPT_LEASE_SECONDS


async def _load_request(user_id: str, request_id: str) -> Optional[dict]:
    """Find one user's message by its idempotency key. Always scoped by user."""
    return await db.chat_messages.find_one(
        {"user_id": user_id, "role": "user", "request_id": request_id}
    )


async def _reply_text_for(doc: dict) -> Optional[str]:
    """The confirmed reply for an answered row, or None if it cannot be produced.

    Deliberately keyed off ``reply_message_id``: an assistant row that no user row points at
    is an orphan from a crash between the two writes, and treating it as this turn's answer
    would report a reply the tracking never confirmed.
    """
    reply_id = doc.get("reply_message_id")
    if not reply_id:
        return None
    reply = await db.chat_messages.find_one({"id": reply_id, "role": "assistant"})
    if not reply or not reply.get("content"):
        return None
    return reply["content"]


async def _prior_history(session_id: str, user_id: str) -> list[dict]:
    """The replayable prefix of a session.

    The current message is never included: while it is pending, processing or failed it is
    not ``answered``, so ``_replayable_history`` drops it. That is what replaced Stage 1's
    positional guess.
    """
    rows = [
        row
        async for row in db.chat_messages.find(
            {"session_id": session_id, "user_id": user_id}
        ).sort("created_at", 1)
    ]
    prior = _replayable_history(rows)
    if CHAT_HISTORY_TURNS > 0:
        prior = prior[-CHAT_HISTORY_TURNS:]
    return prior


async def _retry_message(doc: dict) -> int:
    """Move one failed message back to processing, guarded by its attempt counter.

    ``attempt`` is in the filter *and* incremented in the same operation, so two concurrent
    retries cannot both win, and a worker resuming from an older attempt can never overwrite
    a newer one. Returns the attempt number the caller won.
    """
    _check_transition("failed", "processing")
    current_attempt = _attempt_of(doc)
    next_attempt = current_attempt + 1
    if next_attempt > MAX_CHAT_ATTEMPTS:
        raise MessageStateError(
            f"message {doc.get('id')} has used {current_attempt} of "
            f"{MAX_CHAT_ATTEMPTS} attempts"
        )
    filt: dict = {"id": doc["id"], "role": "user", "status": "failed"}
    filt.update(_attempt_filter(current_attempt))
    result = await db.chat_messages.update_one(
        filt,
        {
            "$set": {
                "status": "processing",
                "status_updated_at": now_iso(),
                "attempt": next_attempt,
            },
            # A retry must not inherit the previous attempt's failure or a stale reply link.
            "$unset": {"error_code": "", "reply_message_id": ""},
        },
    )
    if result.matched_count != 1:
        raise MessageStateError(
            f"message {doc.get('id')} was not failed at attempt {current_attempt} "
            f"(matched={result.matched_count}); another worker owns it"
        )
    return next_attempt


def _in_flight(doc: dict) -> HTTPException:
    return _error(
        409,
        "request_in_flight",
        "This message is already being answered. Poll the request status instead of resending it.",
        session_id=doc.get("session_id"),
        status=doc.get("status"),
        retry_after_ms=1500,
        lookup=f"/api/chat/requests/{doc.get('request_id')}",
    )


def _assert_same_request(doc: dict, body: "ChatIn") -> None:
    """A request id may only ever describe one message in one session.

    ``session_id`` is compared against ``requested_session_id`` -- what the client
    originally sent -- not against the stored ``session_id``. An interrupted first request
    sent ``null`` and the server generated the id, so the client legitimately retries with
    ``null`` again; comparing against the generated value would reject exactly that recovery.
    """
    if body.message != (doc.get("content") or ""):
        raise _error(
            409,
            "idempotency_conflict",
            "This request_id was already used for a different message.",
        )
    if (body.session_id or None) != (doc.get("requested_session_id") or None):
        raise _error(
            409,
            "idempotency_conflict",
            "This request_id was already used for a different session.",
        )


async def _replayed_response(doc: dict) -> dict:
    """The stored answer, shaped exactly like a fresh success."""
    text = await _reply_text_for(doc)
    if text is None:
        raise _error(
            409,
            "request_unrecoverable",
            "This message is marked answered but its reply is missing. Send a new message.",
        )
    return {"session_id": doc["session_id"], "reply": text}


async def _reload(doc: dict, user: dict) -> dict:
    """Re-read a row after losing a compare-and-set race."""
    fresh = await _load_request(user["id"], doc["request_id"])
    if not fresh:
        raise _in_flight(doc)
    return fresh


async def _run_attempt(
    message_id: str, session_id: str, attempt: int, user: dict, message: str
) -> dict:
    """Run one provider attempt for a row that is already ``processing``.

    Every terminal write carries ``attempt``, so an attempt whose lease expired cannot
    acknowledge the row after a newer attempt has taken it over.
    """
    prior = await _prior_history(session_id, user["id"])
    try:
        contents = _gemini_contents(prior, message)
        reply_text = await run_in_threadpool(_call_gemini_sync, contents)
    except Exception as e:
        # Surface the outage as an error. Never substitute canned text for a real answer,
        # otherwise clients cannot tell a genuine reply from an outage.
        logger.exception("LLM error: %s", e)
        await _mark_failed(message_id, "processing", "llm_unavailable", attempt=attempt)
        raise _error(
            503,
            "llm_unavailable",
            "The AI service could not be reached. Please try again shortly.",
        )

    if not reply_text.strip():
        await _mark_failed(message_id, "processing", "llm_empty_response", attempt=attempt)
        raise _error(
            503,
            "llm_empty_response",
            "The AI service returned an empty response. Please try again.",
        )

    try:
        await _persist_answer(message_id, session_id, user["id"], reply_text, attempt=attempt)
    except Exception as e:
        # A reply generated in memory but not durably acknowledged must not be reported as
        # success: the client would render an answer the server cannot replay, and the next
        # turn would not see it either. _persist_answer has already removed the row.
        logger.exception("Could not persist reply for %s: %s", message_id, e)
        await _mark_failed(message_id, "processing", "persistence_failed", attempt=attempt)
        raise _error(
            503,
            "persistence_failed",
            "The AI reply could not be saved. Please try again.",
        )

    return {"session_id": session_id, "reply": reply_text}


async def _handle_repeat(existing: dict, body: "ChatIn", user: dict) -> dict:
    """Decide what a repeated request_id means, from durable state alone.

    Loops only to re-read after losing a compare-and-set race, and is bounded so a
    pathological interleaving cannot spin.
    """
    for _ in range(4):
        _assert_same_request(existing, body)
        status = existing.get("status")
        session_id = existing["session_id"]

        if status == "answered":
            return await _replayed_response(existing)

        if status == "failed":
            attempt = _attempt_of(existing)
            if attempt >= MAX_CHAT_ATTEMPTS:
                raise _error(
                    409,
                    "retry_exhausted",
                    f"This message has used all {MAX_CHAT_ATTEMPTS} attempts. Send a new message.",
                    attempt=attempt,
                    error_code=existing.get("error_code"),
                )
            try:
                next_attempt = await _retry_message(existing)
            except MessageStateError:
                existing = await _reload(existing, user)
                continue
            return await _run_attempt(
                existing["id"], session_id, next_attempt, user, body.message
            )

        if not _lease_expired(existing):
            # Still inside the lease: another worker may be mid-flight, so do nothing.
            raise _in_flight(existing)

        if status == "processing":
            # The lease expired, so the previous worker is presumed gone -- presumed, not
            # proven. End its attempt through the legal edge, then take the ordinary retry
            # path so the attempt budget bounds how often this can happen.
            try:
                await _set_message_status(
                    existing["id"],
                    "processing",
                    "failed",
                    attempt=_attempt_of(existing),
                    error_code="attempt_abandoned",
                )
            except MessageStateError:
                existing = await _reload(existing, user)
                continue
            existing = await _reload(existing, user)
            continue

        # Stale "pending": the row was written but nothing was ever dispatched, so resume
        # without spending an attempt.
        try:
            await _set_message_status(
                existing["id"], "pending", "processing", attempt=_attempt_of(existing)
            )
        except MessageStateError:
            existing = await _reload(existing, user)
            continue
        return await _run_attempt(
            existing["id"], session_id, _attempt_of(existing), user, body.message
        )

    raise _in_flight(existing)


@api.post("/chat")
async def chat(body: ChatIn, user=Depends(get_current_user)):
    request_id = str(body.request_id) if body.request_id else None

    # Fast path: a repeated key is answered from storage before the key and quota checks.
    # Before quota, so an already-paid-for answer still replays at the cap; before the key
    # check, so a stored reply survives a provider misconfiguration. Both are reads.
    if request_id:
        existing = await _load_request(user["id"], request_id)
        if existing:
            return await _handle_repeat(existing, body, user)

    # Fail fast before any write: without a key we cannot produce a real answer, and
    # returning 200 would hand the caller a fabricated reply.
    if not GEMINI_API_KEY.strip():
        raise _error(
            503,
            "llm_not_configured",
            "AI service is not configured. Set GEMINI_API_KEY to enable chat.",
        )

    # Quota is checked before the insert so a blocked caller leaves no rows behind. It is
    # derived from user-message rows, so a repeat that inserts nothing costs nothing.
    await _enforce_limit("ai_chats_per_month", user["id"], user.get("role"))

    session_id = body.session_id or str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    # Written before dispatch, and given a status immediately, so an interruption at any
    # later point leaves a row that says what was attempted instead of one that is
    # indistinguishable from an answered message.
    doc = {
        "id": message_id,
        "session_id": session_id,
        "user_id": user["id"],
        "role": "user",
        "content": body.message,
        "created_at": now_iso(),
        "status": "pending",
        "status_updated_at": now_iso(),
        "attempt": 1,
    }
    if request_id:
        doc["request_id"] = request_id
        # Kept separately from session_id so an interrupted first request can be retried
        # with the null the client still holds, without being mistaken for a conflict.
        doc["requested_session_id"] = body.session_id

    try:
        await db.chat_messages.insert_one(doc)
    except DuplicateKeyError:
        # Two submissions with the same key raced: the unique index picked a winner and this
        # one must not dispatch. Resolve from the winner's row exactly like a repeat.
        existing = await _load_request(user["id"], request_id or "")
        if existing:
            return await _handle_repeat(existing, body, user)
        logger.exception("Duplicate chat request %s but no readable row", request_id)
        raise _error(
            409,
            "request_in_flight",
            "This message is already being answered. Poll the request status instead of resending it.",
        )

    try:
        await _set_message_status(message_id, "pending", "processing", attempt=1)
    except Exception as e:
        # The turn cannot legally reach a terminal state from here, so it is abandoned in
        # "pending": visible in history, excluded from replay, and countable later.
        logger.exception("Could not mark message %s processing: %s", message_id, e)
        raise _error(
            503,
            "persistence_failed",
            "The chat message could not be recorded. Please try again.",
        )

    return await _run_attempt(message_id, session_id, 1, user, body.message)


@api.get("/chat/requests/{request_id}")
async def chat_request_status(request_id: UUID, user=Depends(get_current_user)):
    """Recover one submitted turn from its idempotency key.

    This is how a client recovers a server-generated session id after a dropped response,
    and how it polls a turn that answered 409 ``request_in_flight``. Scoped by user: an id
    belonging to someone else is a 404, so the route cannot be used to probe for existence.
    """
    doc = await _load_request(user["id"], str(request_id))
    if not doc:
        raise _error(
            404, "request_not_found", "No request with that id exists for this account."
        )

    status = doc.get("status") or "unknown"
    attempt = _attempt_of(doc)
    # Only an answered row can expose a reply. An orphan assistant row is never surfaced
    # here, because nothing links it to this turn.
    reply = await _reply_text_for(doc) if status == "answered" else None
    return {
        "request_id": doc.get("request_id"),
        "session_id": doc.get("session_id"),
        "status": status,
        "error_code": doc.get("error_code"),
        "attempt": attempt,
        "max_attempts": MAX_CHAT_ATTEMPTS,
        "reply": reply,
        "can_retry": status == "failed" and attempt < MAX_CHAT_ATTEMPTS,
        "created_at": doc.get("created_at"),
        "status_updated_at": doc.get("status_updated_at"),
    }


@api.get("/chat/{session_id}")
async def chat_history(session_id: str, user=Depends(get_current_user)):
    msgs = []
    async for m in db.chat_messages.find(
        {"session_id": session_id, "user_id": user["id"]}
    ).sort("created_at", 1):
        msgs.append(_serialize_chat_message(m))
    return msgs


@api.get("/chat-sessions/list")
async def chat_sessions(user=Depends(get_current_user)):
    # distinct session_ids with latest msg
    pipeline = [
        {"$match": {"user_id": user["id"]}},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$session_id", "last": {"$first": "$content"}, "ts": {"$first": "$created_at"}}},
        {"$sort": {"ts": -1}},
        {"$limit": 20},
    ]
    out = []
    async for s in db.chat_messages.aggregate(pipeline):
        out.append({"session_id": s["_id"], "preview": s["last"][:60], "last_ts": s["ts"]})
    return out


# ----------------- Decision comments + timeline + attachments -----------------
class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@api.post("/decisions/{decision_id}/analyse")
async def analyse_decision(decision_id: str, user=Depends(get_current_user)):
    """Generate a structured AI analysis for a decision and store it on the record."""
    if not GEMINI_API_KEY.strip():
        raise HTTPException(
            status_code=503,
            detail={"code": "llm_not_configured",
                    "message": "AI service is not configured. Set GEMINI_API_KEY to enable analysis."},
        )

    # Checked after the key check so a missing key still reports llm_not_configured rather
    # than being masked as a quota error. This endpoint calls the same paid Gemini API as
    # chat and previously had no limit on any tier, which made it an uncapped cost.
    await _enforce_limit("analyses_per_month", user["id"], user.get("role"))

    dec = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not dec:
        raise HTTPException(status_code=404, detail="Decision not found")

    prompt = ANALYSIS_PROMPT.format(
        title=dec.get("title", ""),
        category=str(dec.get("category", "")).replace("_", " "),
        risk=dec.get("risk", ""),
        status=dec.get("status", ""),
        confidence=dec.get("confidence", ""),
        stakeholders=", ".join(dec.get("stakeholders") or []) or "not specified",
        deadline=dec.get("deadline") or "not specified",
        description=(dec.get("description") or "").strip() or "(none provided)",
    )

    try:
        raw = await run_in_threadpool(_call_gemini_json_sync, prompt, ANALYSIS_SYSTEM)
        analysis = _coerce_analysis(_parse_json_object(raw))
    except ValueError as e:
        # Malformed or unusable model output. Never persist this as analysis text.
        logger.warning("Analysis parse failed for decision %s: %s", decision_id, e)
        raise HTTPException(
            status_code=502,
            detail={"code": "llm_invalid_response",
                    "message": "The AI service returned a response that could not be read. Try again."},
        )
    except Exception as e:
        logger.exception("Analysis failed for decision %s: %s", decision_id, e)
        raise HTTPException(
            status_code=503,
            detail={"code": "llm_unavailable",
                    "message": "The AI service could not be reached. Please try again shortly."},
        )

    now = now_iso()
    await db.decisions.update_one(
        {"id": decision_id, "user_id": user["id"]},
        {"$set": {"ai_analysis": analysis, "ai_analysis_at": now, "updated_at": now}},
    )
    await db.activity.insert_one({
        "id": str(uuid.uuid4()),
        "decision_id": decision_id,
        "user_id": user["id"],
        "actor_name": user["name"],
        "type": "analysis",
        "message": "AI analysis generated",
        "created_at": now,
    })
    return {"id": decision_id, "ai_analysis": analysis, "ai_analysis_at": now}


@api.get("/decisions/{decision_id}/comments")
async def list_comments(decision_id: str, user=Depends(get_current_user)):
    # ensure user owns decision
    dec = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not dec:
        raise HTTPException(status_code=404, detail="Decision not found")
    out = []
    async for c in db.comments.find({"decision_id": decision_id}).sort("created_at", 1):
        c.pop("_id", None)
        out.append(c)
    return out


@api.post("/decisions/{decision_id}/comments")
async def add_comment(decision_id: str, body: CommentIn, user=Depends(get_current_user)):
    dec = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not dec:
        raise HTTPException(status_code=404, detail="Decision not found")
    doc = {
        "id": str(uuid.uuid4()),
        "decision_id": decision_id,
        "user_id": user["id"],
        "author_name": user["name"],
        "body": body.body,
        "created_at": now_iso(),
    }
    await db.comments.insert_one(doc)
    # also log activity
    await db.activity.insert_one({
        "id": str(uuid.uuid4()),
        "decision_id": decision_id,
        "user_id": user["id"],
        "actor_name": user["name"],
        "type": "comment",
        "message": f"{user['name']} added a comment",
        "created_at": now_iso(),
    })
    doc.pop("_id", None)
    return doc


@api.get("/decisions/{decision_id}/activity")
async def list_activity(decision_id: str, user=Depends(get_current_user)):
    dec = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not dec:
        raise HTTPException(status_code=404, detail="Decision not found")
    out = [
        {"id": "created", "type": "created",
         "message": f"Decision created · {dec.get('category', '').replace('_', ' ')}",
         "created_at": dec.get("created_at"), "actor_name": user["name"]},
    ]
    async for a in db.activity.find({"decision_id": decision_id}).sort("created_at", 1):
        a.pop("_id", None)
        out.append(a)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out


# Attachments — PDF/DOCX upload
ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}

# Magic-byte signatures per extension. DOCX is a ZIP container (PK\x03\x04).
EXT_MAGIC = {
    ".pdf": [b"%PDF-"],
    ".docx": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    # TXT/MD validated as printable UTF-8 (no NUL bytes in first 4KB)
}


def _validate_content(data: bytes, ext: str) -> tuple[bool, str]:
    if not data:
        return False, "Empty file"
    if ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            return False, "File does not look like a valid PDF (missing %PDF- header)"
        return True, ""
    if ext == ".docx":
        if not any(data.startswith(sig) for sig in EXT_MAGIC[".docx"]):
            return False, "File does not look like a valid DOCX (missing ZIP header)"
        # Additional check: must contain word/ in zip listing
        try:
            import zipfile
            from io import BytesIO
            with zipfile.ZipFile(BytesIO(data)) as zf:
                names = zf.namelist()
                if not any(n.startswith("word/") for n in names):
                    return False, "ZIP file does not contain a Word document structure"
        except Exception:
            return False, "Could not parse DOCX archive"
        return True, ""
    if ext in (".txt", ".md"):
        sample = data[:4096]
        if b"\x00" in sample:
            return False, "Text file contains NUL bytes — likely binary"
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            # try latin-1 fallback for legacy text files
            try:
                sample.decode("latin-1")
            except Exception:
                return False, "Text file is not valid UTF-8 / Latin-1"
        return True, ""
    return False, f"Unsupported extension {ext}"


def _extract_text(path: Path, ext: str) -> str:
    """Best-effort text extraction.

    PDF/DOCX need PyPDF2 / python-docx. If either is unavailable the import fails and
    the document is stored without text. That used to be indistinguishable from a
    document that legitimately contained no text, so it is now logged loudly.
    """
    try:
        if ext == ".pdf":
            from PyPDF2 import PdfReader  # type: ignore
            reader = PdfReader(str(path))
            txt = []
            for i, page in enumerate(reader.pages[:20]):  # first 20 pages
                txt.append(page.extract_text() or "")
            return "\n".join(txt).strip()[:4000]
        if ext == ".docx":
            import docx  # type: ignore
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs).strip()[:4000]
        if ext in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except ImportError as e:
        logger.warning(
            "Text extraction unavailable for %s (%s). Install the optional extras "
            "'PyPDF2' and 'python-docx' to enable it: %s",
            ext, path.name, e,
        )
    except Exception as e:
        logger.exception("Extract failed for %s: %s", path.name, e)
    return ""


@api.post("/decisions/{decision_id}/attachments")
async def upload_attachment(
    decision_id: str,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    dec = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not dec:
        raise HTTPException(status_code=404, detail="Decision not found")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {ext}. Allowed: PDF, DOCX, TXT, MD")

    # Read at most one byte past the cap instead of buffering the whole upload, so an
    # oversized request is rejected without first being pulled entirely into memory.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 256)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="File too large (max 10MB)")
        chunks.append(chunk)
    data = b"".join(chunks)

    ok, reason = _validate_content(data, ext)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Invalid file content: {reason}")
    # Quota is enforced only once the file has proved valid. Checking earlier would make a
    # corrupt upload at the cap report "upgrade" instead of the real 400, hiding a
    # malformed file behind a paywall; checking later would mean writing it to disk first.
    await _enforce_limit("documents", user["id"], user.get("role"))
    att_id = str(uuid.uuid4())
    safe_name = f"{att_id}{ext}"
    path = UPLOAD_DIR / safe_name
    path.write_bytes(data)
    extracted = _extract_text(path, ext)
    doc = {
        "id": att_id,
        "decision_id": decision_id,
        "user_id": user["id"],
        "filename": file.filename,
        "size": len(data),
        "ext": ext,
        "stored_name": safe_name,
        "extracted_text": extracted,
        "created_at": now_iso(),
    }
    await db.attachments.insert_one(doc)
    await db.activity.insert_one({
        "id": str(uuid.uuid4()),
        "decision_id": decision_id,
        "user_id": user["id"],
        "actor_name": user["name"],
        "type": "attachment",
        "message": f"Uploaded {file.filename}",
        "created_at": now_iso(),
    })
    doc.pop("_id", None)
    # don't return huge extracted text
    out = {k: v for k, v in doc.items() if k != "extracted_text"}
    out["extracted_preview"] = (extracted[:300] + "…") if extracted and len(extracted) > 300 else extracted
    return out


@api.get("/decisions/{decision_id}/attachments")
async def list_attachments(decision_id: str, user=Depends(get_current_user)):
    dec = await db.decisions.find_one({"id": decision_id, "user_id": user["id"]})
    if not dec:
        raise HTTPException(status_code=404, detail="Decision not found")
    out = []
    async for a in db.attachments.find({"decision_id": decision_id}).sort("created_at", -1):
        a.pop("_id", None)
        a.pop("extracted_text", None)
        out.append(a)
    return out


@api.delete("/decisions/{decision_id}/attachments/{att_id}")
async def delete_attachment(decision_id: str, att_id: str, user=Depends(get_current_user)):
    att = await db.attachments.find_one({"id": att_id, "decision_id": decision_id, "user_id": user["id"]})
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    try:
        (UPLOAD_DIR / att["stored_name"]).unlink(missing_ok=True)
    except Exception:
        pass
    await db.attachments.delete_one({"id": att_id})
    return {"ok": True}


# ----------------- OAuth (Salesforce + HubSpot) -----------------
import secrets as _secrets
from urllib.parse import urlencode

OAUTH_PROVIDERS = {
    "salesforce": {
        "client_id_env": "SALESFORCE_CLIENT_ID",
        "client_secret_env": "SALESFORCE_CLIENT_SECRET",
        "redirect_env": "SALESFORCE_REDIRECT_URI",
        "auth_url": "https://login.salesforce.com/services/oauth2/authorize",
        "token_url": "https://login.salesforce.com/services/oauth2/token",
        "scope": "api refresh_token offline_access",
    },
    "hubspot": {
        "client_id_env": "HUBSPOT_CLIENT_ID",
        "client_secret_env": "HUBSPOT_CLIENT_SECRET",
        "redirect_env": "HUBSPOT_REDIRECT_URI",
        "auth_url": "https://app.hubspot.com/oauth/authorize",
        "token_url": "https://api.hubapi.com/oauth/v1/token",
        "scope": "crm.objects.contacts.read",
    },
}


@api.get("/oauth/{provider}/start")
async def oauth_start(provider: str, user=Depends(get_current_user)):
    cfg = OAUTH_PROVIDERS.get(provider)
    if not cfg:
        raise HTTPException(status_code=404, detail="Unknown provider")
    client_id = os.environ.get(cfg["client_id_env"], "")
    redirect_uri = os.environ.get(cfg["redirect_env"], "")
    if not client_id:
        # No real creds — mock-connect immediately
        await db.connections.update_one(
            {"user_id": user["id"], "provider": provider},
            {"$set": {
                "user_id": user["id"], "provider": provider,
                "status": "connected", "mock": True,
                "connected_at": now_iso(),
            }},
            upsert=True,
        )
        return {"mock": True, "redirect_url": f"{FRONTEND_URL}/sources?connected={provider}&mock=1"}
    state = _secrets.token_urlsafe(24)
    await db.oauth_states.insert_one({
        "state": state, "user_id": user["id"], "provider": provider,
        "created_at": now_iso(),
    })
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
    }
    return {"mock": False, "redirect_url": f"{cfg['auth_url']}?{urlencode(params)}"}


@api.get("/oauth/{provider}/callback")
async def oauth_callback(provider: str, code: str = "", state: str = ""):
    cfg = OAUTH_PROVIDERS.get(provider)
    if not cfg:
        raise HTTPException(status_code=404, detail="Unknown provider")
    st = await db.oauth_states.find_one({"state": state, "provider": provider})
    if not st:
        return RedirectResponse(f"{FRONTEND_URL}/sources?error=invalid_state")

    # Exchange the authorization code for tokens. The connection is only recorded as
    # live when the provider actually returns an access token; previously the response
    # was discarded and the connection was marked "connected" even on failure.
    token_ok = False
    try:
        import httpx  # type: ignore
        async with httpx.AsyncClient(timeout=10.0) as client_http:
            resp = await client_http.post(cfg["token_url"], data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": os.environ.get(cfg["client_id_env"], ""),
                "client_secret": os.environ.get(cfg["client_secret_env"], ""),
                "redirect_uri": os.environ.get(cfg["redirect_env"], ""),
            })
        if resp.status_code == 200:
            body = resp.json()
            token_ok = bool(body.get("access_token"))
            if not token_ok:
                logger.warning("OAuth token response for %s had no access_token", provider)
        else:
            logger.warning("OAuth token exchange for %s failed: HTTP %s", provider, resp.status_code)
    except Exception as e:
        logger.warning("OAuth token exchange error for %s: %s", provider, e)

    if not token_ok:
        # Consume the state either way so it cannot be replayed, and tell the UI.
        await db.oauth_states.delete_one({"state": state})
        return RedirectResponse(f"{FRONTEND_URL}/sources?error=token_exchange_failed")

    await db.connections.update_one(
        {"user_id": st["user_id"], "provider": provider},
        {"$set": {
            "user_id": st["user_id"], "provider": provider,
            "status": "connected", "mock": False, "connected_at": now_iso(),
        }},
        upsert=True,
    )
    await db.oauth_states.delete_one({"state": state})
    return RedirectResponse(f"{FRONTEND_URL}/sources?connected={provider}")


@api.post("/oauth/{provider}/disconnect")
async def oauth_disconnect(provider: str, user=Depends(get_current_user)):
    await db.connections.update_one(
        {"user_id": user["id"], "provider": provider},
        {"$set": {"status": "disconnected", "disconnected_at": now_iso()}},
        upsert=True,
    )
    return {"ok": True}


@api.get("/connections")
async def list_connections(user=Depends(get_current_user)):
    out = {}
    async for c in db.connections.find({"user_id": user["id"]}):
        c.pop("_id", None)
        out[c["provider"]] = c
    return out


# ----------------- Seed helpers -----------------
CATS = ["business_strategy", "hiring", "investment", "operations", "marketing", "technology"]
RISKS = ["low", "medium", "high", "critical"]
STATUSES = ["draft", "active", "review", "complete"]

SAMPLE_TITLES = [
    ("New CRM rollout", "technology"),
    ("AI Vendor selection", "technology"),
    ("Q1 Marketing Budget", "marketing"),
    ("Hire Head of Data", "hiring"),
    ("Series B fundraise", "investment"),
    ("Expand to EU", "business_strategy"),
    ("Outsource L1 support", "operations"),
    ("Pricing tier overhaul", "marketing"),
    ("Migrate to multi-region", "technology"),
    ("Acquire competitor X", "investment"),
    ("Launch enterprise tier", "business_strategy"),
    ("Hire 5 SDRs", "hiring"),
    ("Rebrand corporate identity", "marketing"),
    ("Adopt OKRs company-wide", "operations"),
    ("Sunset legacy product", "business_strategy"),
    ("Build vs buy: analytics", "technology"),
    ("Change vendor for payroll", "operations"),
    ("Open Bangalore office", "business_strategy"),
    ("Invest in series B startup", "investment"),
    ("Run brand campaign", "marketing"),
]


async def seed_decisions_for_user(user_id: str, count: int = 22):
    existing = await db.decisions.count_documents({"user_id": user_id})
    if existing > 0:
        return
    docs = []
    for i in range(count):
        title, cat = SAMPLE_TITLES[i % len(SAMPLE_TITLES)]
        risk = random.choices(RISKS, weights=[3, 5, 3, 1])[0]
        status = random.choices(STATUSES, weights=[1, 4, 2, 3])[0]
        confidence = random.randint(55, 98)
        created = now_utc() - timedelta(days=random.randint(0, 28), hours=random.randint(0, 23))
        docs.append({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "title": f"{title} #{i+1}",
            "description": f"Strategic evaluation of '{title}' including stakeholders, risks, and projected impact.",
            "category": cat,
            "risk": risk,
            "status": status,
            "confidence": confidence,
            "stakeholders": random.sample(
                ["CEO", "CFO", "CTO", "Head of Product", "Head of Sales", "Head of People"],
                k=random.randint(2, 4)
            ),
            "deadline": (created + timedelta(days=random.randint(7, 60))).date().isoformat(),
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
        })
    if docs:
        await db.decisions.insert_many(docs)


async def _ensure_chat_request_index() -> None:
    """Create and verify the per-user idempotency index.

    Uniqueness is the only thing stopping two concurrent submissions from creating two
    turns, so a missing or non-unique index is a correctness failure rather than a
    performance regression. Verification is therefore part of the guarantee, and every
    failure raises: startup treats this as fatal rather than serving without it.
    """
    try:
        await db.chat_messages.create_index(
            [("user_id", 1), ("request_id", 1)],
            name=CHAT_REQUEST_INDEX,
            unique=True,
            # Partial: rows written before Stage 2 have no request_id. A plain unique index
            # would treat every one of them as null and collapse them into a single value,
            # so the build would fail on existing history -- and if it did not, no user
            # could ever store a second pre-Stage-2 message. `$type: "string"` also excludes
            # an accidental explicit null.
            partialFilterExpression={"request_id": {"$type": "string"}},
        )
    except Exception as exc:
        raise RuntimeError(
            f"Could not create the chat idempotency index {CHAT_REQUEST_INDEX!r}: {exc}"
        ) from exc

    spec = (await db.chat_messages.index_information()).get(CHAT_REQUEST_INDEX)
    if not spec:
        raise RuntimeError(f"Chat idempotency index {CHAT_REQUEST_INDEX!r} is missing")
    if not spec.get("unique"):
        raise RuntimeError(f"Chat idempotency index {CHAT_REQUEST_INDEX!r} is not unique")
    if dict(spec.get("key") or []) != {"user_id": 1, "request_id": 1}:
        raise RuntimeError(
            f"Chat idempotency index {CHAT_REQUEST_INDEX!r} has unexpected keys: "
            f"{spec.get('key')}"
        )
    if not spec.get("partialFilterExpression"):
        raise RuntimeError(
            f"Chat idempotency index {CHAT_REQUEST_INDEX!r} is not partial, so legacy "
            "messages without a request_id would collide"
        )


MIN_SEED_PASSWORD_LEN = 12


async def seed_users():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id")
    await db.decisions.create_index([("user_id", 1), ("created_at", -1)])
    await db.decisions.create_index("id")
    await db.chat_messages.create_index([("session_id", 1), ("created_at", 1)])
    await db.chat_messages.create_index([("user_id", 1), ("created_at", -1)])
    await db.comments.create_index([("decision_id", 1), ("created_at", 1)])
    await db.activity.create_index([("decision_id", 1), ("created_at", 1)])
    # Tier limits count a user's analyses in a month window. Analyses are recorded in
    # activity (type="analysis"), so the decision_id-leading index above cannot serve it.
    await db.activity.create_index([("user_id", 1), ("created_at", -1)])
    await db.attachments.create_index([("decision_id", 1), ("created_at", -1)])
    await db.attachments.create_index([("id", 1), ("decision_id", 1), ("user_id", 1)])
    # Tier limits count a user's attachments in a month window. The decision_id index
    # above cannot serve that query, so the user_id-leading compound index is required.
    await db.attachments.create_index([("user_id", 1), ("created_at", -1)])
    await db.payment_transactions.create_index([("session_id", 1), ("user_id", 1)])
    await db.subscriptions.create_index("user_id", unique=True)
    await db.connections.create_index([("user_id", 1), ("provider", 1)], unique=True)
    # Abandoned OAuth handshakes must not accumulate; the callback deletes its state
    # on use, so anything older than a day was never completed.
    await db.oauth_states.create_index("created_at", expireAfterSeconds=86400)

    async def ensure_user(email: str, password: str, name: str, role: str) -> Optional[str]:
        # Seeded accounts must never be created with a password baked into source.
        # Without an explicit, strong enough password the account is skipped.
        if not password:
            logger.warning(
                "Skipping seed user %s: set %s_PASSWORD in your environment to create it.",
                email, role.upper(),
            )
            return None
        if len(password) < MIN_SEED_PASSWORD_LEN:
            raise ValueError(
                f"{role.upper()}_PASSWORD must be at least {MIN_SEED_PASSWORD_LEN} characters "
                f"(got {len(password)})."
            )
        existing = await db.users.find_one({"email": email})
        if existing:
            # keep password in sync with .env
            if not verify_password(password, existing["password_hash"]):
                await db.users.update_one({"email": email}, {"$set": {"password_hash": hash_password(password)}})
            return existing["id"]
        user_id = str(uuid.uuid4())
        await db.users.insert_one({
            "id": user_id, "email": email, "name": name, "role": role,
            "password_hash": hash_password(password), "created_at": now_iso(),
        })
        logger.info("Seeded %s account %s.", role, email)
        return user_id

    admin_id = await ensure_user(
        os.environ.get("ADMIN_EMAIL", "admin@decisionlens.ai").strip().lower(),
        os.environ.get("ADMIN_PASSWORD", ""),
        "Admin", "admin",
    )
    demo_id = await ensure_user(
        os.environ.get("DEMO_EMAIL", "demo@decisionlens.ai").strip().lower(),
        os.environ.get("DEMO_PASSWORD", ""),
        "Demo User", "user",
    )
    if admin_id:
        await seed_decisions_for_user(admin_id, 28)
    if demo_id:
        await seed_decisions_for_user(demo_id, 22)


@app.on_event("startup")
async def on_startup():
    # Checked before the seeding try/except so it cannot be swallowed. Without an explicit
    # success URL, production would fall back to FRONTEND_URL; failing loudly here makes it
    # impossible to ship that misconfiguration silently. This mirrors the webhook, which
    # refuses to run without its signing secret.
    # Keyed off ENVIRONMENT rather than COOKIE_SECURE, which can be overridden by its own
    # env var and so is not a reliable proxy for "this is production".
    if ENVIRONMENT not in {"development", "dev", "local"} and not STRIPE_SUCCESS_URL:
        raise RuntimeError(
            "STRIPE_SUCCESS_URL must be set when ENVIRONMENT is not development/local. "
            "Checkout redirects would otherwise fall back to FRONTEND_URL."
        )
    # Deliberately outside the seeding try/except: without the uniqueness guarantee two
    # concurrent submissions could create duplicate turns, so a failure here must abort
    # startup instead of being logged and forgotten.
    await _ensure_chat_request_index()
    try:
        await seed_users()
        logger.info("Seed complete.")
    except Exception as e:
        logger.exception("Startup seeding failed: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    client.close()


# Exactly one name is treated as disposable. A substring match on "test" also accepts
# `contest_prod` and `latest_billing`, and this predicate is what tells the test suite it
# may empty the database, so it has to be a name nobody gets by accident. conftest.py
# holds a copy of this constant and must be kept in step.
TEST_DB_NAME = "decisionlens_test"


def _is_disposable_db(name: str) -> bool:
    return (name or "").strip().lower() == TEST_DB_NAME


@api.get("/")
async def root():
    # test_db lets the test suite confirm the server it is about to wipe is actually
    # pointed at a test database, rather than trusting that an environment variable was
    # set consistently in two separate processes. It is a boolean and deliberately not
    # the name, so no configuration detail is published.
    return {
        "app": "DecisionLens AI",
        "version": "1.0",
        "test_db": _is_disposable_db(DB_NAME),
    }


# ----------------- Test-only database identity probe -----------------
# GET /api/ above reports the DB_NAME the server was *started* with. That is a statement
# about configuration, not about the connection this process actually opened, and the
# suite needs the latter before it deletes anything: if pytest writes a marker to one
# database while the server answers from another, pytest would wipe a database the
# server is still serving. This endpoint asks the server to look a marker up through its
# own live Motor handle, so a 200 proves both processes share one database.
#
# It is disabled by default, hidden from OpenAPI, and gated on four independent things:
# the enable flag, the exact disposable database name, a secret of at least 32 chars, and
# a constant-time comparison. Every rejection is the same 404 FastAPI returns for an
# unmatched route, so the endpoint cannot be discovered by probing.
class TestDBProbe(BaseModel):
    marker: str = Field(min_length=1, max_length=200)
    # Optional, so the probe stays a pure identity check when the caller only cares about
    # the database. When supplied, the server resolves its own UPLOAD_DIR and reports
    # whether they point at the same place: a matching marker proves the two processes
    # share a database, but says nothing about the directory the cleanup will empty.
    # Only the boolean is returned -- neither path crosses the wire.
    upload_dir: Optional[str] = Field(default=None, max_length=4096)


def _probe_enabled(flag: str, db_name: str, secret: str) -> bool:
    """True only when every independent condition for exposing the probe holds."""
    return flag == "1" and _is_disposable_db(db_name) and len(secret) >= 32


def _same_path(a: str, b: str) -> bool:
    """Compare two paths after resolving and case-folding them."""
    def norm(p: str) -> str:
        try:
            return os.path.normcase(str(Path(p).resolve()))
        except (OSError, ValueError):
            return os.path.normcase(str(p))

    return norm(a) == norm(b)


@api.post("/__test/db-identity", include_in_schema=False)
async def verify_test_database(request: Request):
    expected = os.environ.get("TEST_DB_PROBE_SECRET", "")
    if not _probe_enabled(os.environ.get("ENABLE_TEST_DB_PROBE", ""), DB_NAME, expected):
        raise HTTPException(status_code=404, detail="Not Found")

    supplied = request.headers.get("X-Test-Probe-Secret", "")
    # compare_digest raises TypeError when handed non-ASCII str, which would surface as a
    # 500 and confirm the route exists. Comparing UTF-8 bytes accepts any input and is
    # still constant time.
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=404, detail="Not Found")

    # Parsed after the gate on purpose. A body declared directly on the signature would be
    # validated before this function runs, so an unauthenticated caller could send a
    # malformed payload and get 422 instead of 404 -- enough to confirm the route exists.
    try:
        body = TestDBProbe(**await request.json())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid probe payload")

    found = await db["_test_identity"].find_one({"_id": body.marker})
    result = {"matched": found is not None}
    if body.upload_dir is not None:
        result["uploads_match"] = _same_path(body.upload_dir, str(UPLOAD_DIR))
    return result


# ----------------- Stripe Billing (Subscription tiers) -----------------
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
# Signing secret from `stripe listen` (dev) or the dashboard webhook endpoint (prod).
# Webhook payloads are refused without it; signatures are never skipped.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "").strip()

# Tiers defined on backend only (price never trusted from frontend)
TIERS = {
    "starter": {
        "id": "starter",
        "name": "Starter",
        "price": 0.0,
        "billed": "Free forever",
        "tagline": "For solo operators getting started",
        "limits": {"decisions_per_month": 10, "ai_chats_per_month": 20, "analyses_per_month": 5, "seats": 1, "documents": 5},
        "features": [
            "10 decisions / month",
            "20 AI Chat messages / month",
            "5 AI decision analyses / month",
            "Decision Health Score",
            "Basic charts",
            "1 user",
        ],
    },
    "pro": {
        "id": "pro",
        "name": "Pro",
        "price": 29.00,
        "billed": "$29 / month",
        "tagline": "For founders & product leaders",
        "limits": {"decisions_per_month": 200, "ai_chats_per_month": 1000, "analyses_per_month": 100, "seats": 1, "documents": 100},
        "features": [
            "200 decisions / month",
            "1,000 AI Chat messages / month",
            "100 AI decision analyses / month",
            "Unlimited document uploads (10MB each)",
            "Advanced Analytics + Radar",
            "Priority email support",
        ],
    },
    "team": {
        "id": "team",
        "name": "Team",
        "price": 99.00,
        "billed": "$99 / month",
        "tagline": "For leadership teams making big calls",
        "limits": {"decisions_per_month": -1, "ai_chats_per_month": -1, "analyses_per_month": -1, "seats": 5, "documents": -1},
        "features": [
            "Unlimited decisions",
            "Unlimited AI Chat",
            "Unlimited AI analyses",
            "5 team seats",
            "Stakeholder mentions & shared decisions",
            "Slack alerts (coming soon)",
            "Dedicated success manager",
        ],
    },
}


class CheckoutIn(BaseModel):
    tier: Literal["pro", "team"]


# Stripe statuses that still entitle the customer to the tier they paid for. Anything
# else - canceled, unpaid, incomplete, or a value we do not recognise - is treated as no
# entitlement. past_due is included deliberately: see the note in _get_active_tier.
_STATUS_GRANTS_ACCESS = {"active", "trialing", "past_due"}


def _coerce_period_end(raw):
    """Parse a stored period end into an aware UTC datetime, or None.

    Accepts all three shapes this data has taken or will take:
      * epoch seconds as an int or numeric string - Stripe's own representation, which a
        naive fromisoformat would reject, turning a valid future period into None and
        silently granting a paid tier forever;
      * an ISO-8601 string, as written by the current checkout and webhook grants;
      * a naive ISO string, which would otherwise compare against an aware `now` and
        raise TypeError, 500-ing every entitlement check for that user.

    Anything unparseable returns None, and every caller treats that as no entitlement.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            try:
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def _get_active_tier(user_id: str) -> dict:
    sub = await db.subscriptions.find_one({"user_id": user_id})
    if not sub:
        return {"tier": "starter", "status": "active", "current_period_end": None}

    raw_end = sub.get("current_period_end")
    status = (sub.get("status") or "").strip().lower()
    end_dt = _coerce_period_end(raw_end)

    def downgrade(reason: str) -> dict:
        # Every uncertain path lands here. The entitlement gate must never fail toward
        # keeping money: a missing or unreadable expiry would otherwise be an unlimited
        # paid grant, silently and forever.
        return {
            "tier": "starter",
            "status": reason,
            "current_period_end": raw_end,
            "last_session_id": sub.get("last_session_id"),
        }

    # past_due deliberately still grants access: a failed retry should not lock a paying
    # customer out mid-cycle. The grace window is Stripe's, not ours.
    if status not in _STATUS_GRANTS_ACCESS:
        return downgrade(status or "unknown_status")
    if end_dt is None:
        return downgrade("invalid_period_end")
    if end_dt <= now_utc():
        return downgrade("expired")
    return {
        "tier": sub.get("tier", "starter"),
        "status": status,
        "current_period_end": raw_end,
        "last_session_id": sub.get("last_session_id"),
    }


# ----------------- Tier limit enforcement -----------------
# Three product questions were raised and not answered, so each is isolated as a constant
# here rather than being silently baked into the logic. Flip the constant, not the code.
#
#   DOCUMENTS_LIMIT_IS_MONTHLY - "documents" carries no _per_month suffix in TIERS, unlike
#       its two siblings. Default True to match them. As a lifetime cap, existing users
#       with more uploads than the limit are locked out permanently.
#   ANALYSE_COUNTS_AS_CHAT - POST /decisions/{id}/analyse calls the model but writes no
#       chat_messages row, and chat quota is counted from that collection. Charging it
#       there would need a synthetic marker document polluting chat history, so it is
#       excluded by default and needs a deliberate decision.
#   REFUND_CHAT_ON_LLM_FAILURE - the user message is persisted before the model is called,
#       so a failed call consumes a unit. Default False (no refund).
DOCUMENTS_LIMIT_IS_MONTHLY = True
ANALYSE_COUNTS_AS_CHAT = False
REFUND_CHAT_ON_LLM_FAILURE = False

# Collection backing each limitable resource.
_LIMIT_COLLECTION = {
    "decisions_per_month": "decisions",
    "ai_chats_per_month": "chat_messages",
    "analyses_per_month": "activity",
    "documents": "attachments",
}
_LIMIT_LABEL = {
    "decisions_per_month": "decisions this month",
    "ai_chats_per_month": "AI Chat messages this month",
    "analyses_per_month": "AI decision analyses this month",
    "documents": "documents",
}
# Filters that scope a resource to the rows which actually represent it. Analyses live in
# the shared activity collection alongside field-change and comment events, so the type
# has to be pinned or unrelated activity would be billed as a model call.
_LIMIT_EXTRA_FILTER = {
    "analyses_per_month": {"type": "analysis"},
}
# Where the next step up lives, so a 402 can tell the client where to send the user.
_LIMIT_UPGRADE = {"starter": "pro", "pro": "team", "team": None}


def _limit_is_monthly(resource: str) -> bool:
    return DOCUMENTS_LIMIT_IS_MONTHLY if resource == "documents" else True


def _month_window() -> tuple[str, str]:
    """(start, end) ISO bounds of the current calendar month, half-open.

    now_iso() is always timezone-aware UTC with a constant "+00:00", so these strings sort
    lexicographically against stored created_at values and a plain range query is exact.
    """
    start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start.isoformat(), end.isoformat()


async def _usage_count(resource: str, user_id: str) -> int:
    query: dict = {"user_id": user_id}
    if resource == "ai_chats_per_month":
        # A turn writes two documents: the user message and the assistant reply. Counting
        # raw rows would bill every message twice, so only user turns are counted.
        query["role"] = "user"
    query.update(_LIMIT_EXTRA_FILTER.get(resource, {}))
    if _limit_is_monthly(resource):
        start, end = _month_window()
        query["created_at"] = {"$gte": start, "$lt": end}
    return await db[_LIMIT_COLLECTION[resource]].count_documents(query)


# Seeded showcase accounts are deliberately full of data - admin gets 28 decisions, demo
# 22 - which is incompatible with a 10-per-month starter cap. Some of those backdated rows
# always land inside the current window, so the cap was blocking the very accounts that
# exist to demonstrate the product. These are not customers, so they are exempt.
_SHOWCASE_ROLES = {"admin", "demo"}


def _limits_exempt(role) -> bool:
    return (role or "").strip().lower() in _SHOWCASE_ROLES


async def _enforce_limit(resource: str, user_id: str, role=None) -> None:
    if _limits_exempt(role):
        return
    active = await _get_active_tier(user_id)
    tier = active["tier"]
    limits = TIERS.get(tier, TIERS["starter"])["limits"]
    limit = limits.get(resource)
    # -1 is the unlimited sentinel used by the Team tier.
    if limit is None or limit < 0:
        return
    used = await _usage_count(resource, user_id)
    if used < limit:
        return
    upgrade = _LIMIT_UPGRADE.get(tier)
    raise HTTPException(
        status_code=402,
        detail={
            "code": "limit_exceeded",
            "resource": resource,
            "limit": limit,
            "used": used,
            "reset_at": _month_window()[1] if _limit_is_monthly(resource) else None,
            "upgrade_tier": upgrade,
            "message": (
                f"You've used all {limit} {_LIMIT_LABEL[resource]} on the "
                f"{TIERS.get(tier, TIERS['starter'])['name']} plan."
                + (f" Upgrade to {TIERS[upgrade]['name']} for more." if upgrade else "")
            ),
        },
    )


@api.get("/billing/tiers")
async def list_tiers():
    # public — used to render Pricing page
    return list(TIERS.values())


@api.get("/billing/me")
async def my_subscription(user=Depends(get_current_user)):
    active = await _get_active_tier(user["id"])
    return {"user_id": user["id"], **active, "tier_meta": TIERS.get(active["tier"], TIERS["starter"])}


@api.get("/billing/usage")
async def billing_usage(user=Depends(get_current_user)):
    """Remaining quota per resource, so the UI can show it before the user is blocked."""
    active = await _get_active_tier(user["id"])
    tier = active["tier"]
    meta = TIERS.get(tier, TIERS["starter"])
    exempt = _limits_exempt(user.get("role"))
    usage = {}
    for resource in _LIMIT_COLLECTION:
        used = await _usage_count(resource, user["id"])
        # Exempt accounts are reported with the -1 sentinel the Team tier uses rather than
        # their tier's number, because that is what _enforce_limit actually does with it:
        # it never counts and never blocks. Reporting the tier's cap alongside
        # "unlimited": true produced a self-contradictory payload - limit 10, used 28,
        # remaining null - which a client reading `limit` would render as a live cap on an
        # account that cannot be capped.
        limit = -1 if exempt else meta["limits"].get(resource)
        unlimited = limit is not None and limit < 0
        usage[resource] = {
            "limit": limit,
            "used": used,
            "unlimited": unlimited,
            "remaining": None if unlimited or limit is None else max(0, limit - used),
            "reset_at": _month_window()[1] if _limit_is_monthly(resource) else None,
        }
    return {
        "tier": tier,
        "tier_name": meta["name"],
        # None for exempt accounts: limit_exceeded can never be raised for them, so an
        # upgrade_tier would point at a paywall they will not ever be shown.
        "upgrade_tier": None if exempt else _LIMIT_UPGRADE.get(tier),
        "usage": usage,
    }


@api.post("/billing/checkout")
async def create_checkout(body: CheckoutIn, http_request: Request, user=Depends(get_current_user)):
    if body.tier not in ("pro", "team"):
        raise HTTPException(status_code=400, detail="Invalid tier")
    if not STRIPE_API_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    pkg = TIERS[body.tier]
    amount = float(pkg["price"])
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Free tier does not need checkout")

    # Redirects are built only from server-side configuration. This used to accept a
    # client-supplied origin_url, which meant anyone could generate a legitimate Stripe
    # checkout for the real amount and then have the payer bounced to an attacker-hosted
    # page carrying the session id - a ready-made card-phishing kit built on this checkout.
    origin = (STRIPE_SUCCESS_URL or FRONTEND_URL).rstrip("/")
    success_url = f"{origin}/pricing?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin}/pricing?canceled=1"

    # The transaction row is written BEFORE the Stripe session exists, under an id we choose.
    # Stripe can fire checkout.session.completed from inside Session.create(), so a row
    # inserted afterwards does not exist yet when the webhook looks for it - the grant was
    # silently dropped and the handler returned 200, so Stripe never retried. The webhook
    # resolves this row via client_reference_id, which is known before the session exists.
    tx_id = str(uuid.uuid4())
    await db.payment_transactions.insert_one({
        "id": tx_id,
        "session_id": None,
        "user_id": user["id"],
        "user_email": user["email"],
        "tier": body.tier,
        "amount": amount,
        "currency": "usd",
        "payment_status": "initiated",
        "status": "open",
        "metadata": {"tier": body.tier, "user_id": user["id"]},
        "created_at": now_iso(),
    })

    try:
        import stripe

        stripe.api_key = STRIPE_API_KEY
        # Price is resolved server-side from TIERS; the client never supplies an amount.
        session = await run_in_threadpool(
            stripe.checkout.Session.create,
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(round(amount * 100)),
                    "product_data": {
                        "name": f"DecisionLens AI - {pkg['name']}",
                        "description": pkg.get("tagline", ""),
                    },
                },
                "quantity": 1,
            }],
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=user["email"],
            client_reference_id=tx_id,
            metadata={
                "tx_id": tx_id,
                "user_id": user["id"],
                "user_email": user["email"],
                "tier": body.tier,
                "tier_name": pkg["name"],
                "source": "decisionlens_pricing",
            },
        )
    except Exception as e:
        logger.exception("Stripe checkout failed: %s", e)
        # The pending row is marked failed rather than deleted, so an abandoned or errored
        # attempt stays visible instead of vanishing from the reconciliation query.
        await db.payment_transactions.update_one(
            {"id": tx_id}, {"$set": {"status": "error", "updated_at": now_iso()}}
        )
        raise HTTPException(status_code=500, detail=f"Stripe checkout error: {e}")

    await db.payment_transactions.update_one(
        {"id": tx_id}, {"$set": {"session_id": session.id, "updated_at": now_iso()}}
    )
    return {"url": session.url, "session_id": session.id}


@api.get("/billing/status/{session_id}")
async def check_status(session_id: str, http_request: Request, user=Depends(get_current_user)):
    tx = await db.payment_transactions.find_one({"session_id": session_id, "user_id": user["id"]})
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    # already finalized — don't re-grant credits
    if tx.get("payment_status") == "paid" and tx.get("granted"):
        return {"payment_status": "paid", "status": tx.get("status"), "tier": tx.get("tier"), "already_granted": True}

    try:
        import stripe

        stripe.api_key = STRIPE_API_KEY
        s = await run_in_threadpool(stripe.checkout.Session.retrieve, session_id)
    except Exception as e:
        logger.exception("Status check failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Stripe status error: {e}")

    # Stripe reports session.status as open|complete|expired and payment_status as
    # unpaid|paid|no_payment_required. Persist both verbatim.
    payment_status = s.payment_status or "unpaid"
    session_status = s.status or "open"
    update = {
        "status": session_status,
        "payment_status": payment_status,
        "amount_total": s.amount_total,
        "currency": s.currency,
        "updated_at": now_iso(),
    }
    granted = False
    if payment_status == "paid" and not tx.get("granted"):
        # Grant tier access for 30 days
        tier = tx.get("tier", "pro")
        period_end = (now_utc() + timedelta(days=30)).isoformat()
        await db.subscriptions.update_one(
            {"user_id": user["id"]},
            {"$set": {
                "user_id": user["id"],
                "tier": tier,
                "status": "active",
                "current_period_end": period_end,
                "last_session_id": session_id,
                "updated_at": now_iso(),
            }},
            upsert=True,
        )
        update["granted"] = True
        granted = True
    await db.payment_transactions.update_one({"session_id": session_id}, {"$set": update})
    return {"payment_status": payment_status, "status": session_status, "tier": tx.get("tier"),
            "amount_total": s.amount_total, "currency": s.currency, "granted_now": granted}


@api.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    if not STRIPE_API_KEY:
        return {"ok": False, "reason": "stripe_not_configured"}
    if not STRIPE_WEBHOOK_SECRET:
        # Never accept an unverified payload: it would let anyone grant themselves
        # a paid tier by POSTing a forged event.
        raise HTTPException(
            status_code=503,
            detail="stripe_webhook_secret_not_configured",
        )
    try:
        import stripe

        payload = await request.body()
        sig = request.headers.get("Stripe-Signature", "")
        evt = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.warning("Stripe webhook rejected: %s", e)
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    # Grant only on events that mean money actually settled. getattr is used because a
    # StripeObject raises AttributeError on an absent key, and this endpoint must not
    # 500 on an unexpected payload shape.
    event_type = getattr(evt, "type", "") or ""
    settled = event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
    session = getattr(evt, "data", None)
    session = getattr(session, "object", None)
    session_id = getattr(session, "id", None)
    payment_status = getattr(session, "payment_status", None)
    # client_reference_id carries the id of the transaction row, which was written before
    # this session existed. It is the only lookup that can succeed when Stripe fires from
    # inside Session.create(), before session_id has been recorded locally.
    client_ref = getattr(session, "client_reference_id", None)

    if session_id and settled and payment_status == "paid":
        tx = None
        if client_ref:
            tx = await db.payment_transactions.find_one({"id": client_ref})
        if tx is None and session_id:
            tx = await db.payment_transactions.find_one({"session_id": session_id})

        if tx is None:
            # A paid event we cannot resolve is a lost grant, not a no-op. Returning 200
            # would tell Stripe the event was delivered and it would never be retried, so
            # the customer pays and never receives access. 503 makes Stripe retry for days.
            # Only events that should have granted reach here; anything else still 200s.
            logger.error(
                "Stripe webhook for settled session %s could not be matched to a "
                "transaction (client_reference_id=%r); Stripe will retry",
                session_id,
                client_ref,
            )
            raise HTTPException(
                status_code=503,
                detail="payment_transaction_not_found_retry",
            )

        if not tx.get("granted"):
            tier = tx.get("tier", "pro")
            period_end = (now_utc() + timedelta(days=30)).isoformat()
            await db.subscriptions.update_one(
                {"user_id": tx["user_id"]},
                {"$set": {
                    "user_id": tx["user_id"], "tier": tier,
                    "status": "active",
                    "current_period_end": period_end,
                    "last_session_id": session_id,
                    "updated_at": now_iso(),
                }},
                upsert=True,
            )
            # Keyed on the transaction id, not session_id: when this event was resolved
            # via client_reference_id the local session_id had not been written yet, so a
            # session_id-keyed update would silently match nothing and leave the grant
            # unrecorded.
            await db.payment_transactions.update_one(
                {"id": tx["id"]},
                {
                    "$set": {
                        "session_id": session_id or tx.get("session_id"),
                        "payment_status": "paid",
                        "granted": True,
                        "updated_at": now_iso(),
                    }
                },
            )
    return {"ok": True, "event_type": event_type}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening headers on every response.

    HSTS is only meaningful over HTTPS, so it is emitted in production only; sending
    it in local development would make the browser refuse plain http://localhost.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'",
    )
    if COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[FRONTEND_URL, "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
