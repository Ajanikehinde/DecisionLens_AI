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
import bcrypt
import jwt
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, List, Optional, Literal

from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
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
    # also seed a couple of starter decisions for new users
    await seed_decisions_for_user(user_id)
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
    result = await db.decisions.delete_one({"id": decision_id, "user_id": user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"ok": True}


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


@api.post("/chat")
async def chat(body: ChatIn, user=Depends(get_current_user)):
    # Fail fast before touching the database: without a key we cannot produce a real
    # answer, and returning 200 would hand the caller a fabricated reply.
    if not GEMINI_API_KEY.strip():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "llm_not_configured",
                "message": "AI service is not configured. Set GEMINI_API_KEY to enable chat.",
            },
        )

    session_id = body.session_id or str(uuid.uuid4())
    # persist user msg
    await db.chat_messages.insert_one({
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "user_id": user["id"],
        "role": "user",
        "content": body.message,
        "created_at": now_iso(),
    })

    # Replay the stored conversation (bounded) so the model keeps context without
    # depending on any provider-managed session.
    prior = []
    async for m in db.chat_messages.find(
        {"session_id": session_id, "user_id": user["id"]}
    ).sort("created_at", 1):
        prior.append({"role": m["role"], "content": m["content"]})
    prior = prior[:-1]  # drop the message just inserted
    if CHAT_HISTORY_TURNS > 0:
        prior = prior[-CHAT_HISTORY_TURNS:]

    try:
        contents = _gemini_contents(prior, body.message)
        reply_text = await run_in_threadpool(_call_gemini_sync, contents)
    except Exception as e:
        # Surface the outage as an error. Never substitute canned text for a real answer,
        # otherwise clients cannot tell a genuine reply from an outage.
        logger.exception("LLM error: %s", e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "llm_unavailable",
                "message": "The AI service could not be reached. Please try again shortly.",
            },
        )

    if not reply_text.strip():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "llm_empty_response",
                "message": "The AI service returned an empty response. Please try again.",
            },
        )

    await db.chat_messages.insert_one({
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "user_id": user["id"],
        "role": "assistant",
        "content": reply_text,
        "created_at": now_iso(),
    })

    return {"session_id": session_id, "reply": reply_text}


@api.get("/chat/{session_id}")
async def chat_history(session_id: str, user=Depends(get_current_user)):
    msgs = []
    async for m in db.chat_messages.find(
        {"session_id": session_id, "user_id": user["id"]}
    ).sort("created_at", 1):
        m.pop("_id", None)
        msgs.append(m)
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
    await db.attachments.create_index([("decision_id", 1), ("created_at", -1)])
    await db.attachments.create_index([("id", 1), ("decision_id", 1), ("user_id", 1)])
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
    try:
        await seed_users()
        logger.info("Seed complete.")
    except Exception as e:
        logger.exception("Startup seeding failed: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    client.close()


@api.get("/")
async def root():
    return {"app": "DecisionLens AI", "version": "1.0"}


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
        "limits": {"decisions_per_month": 10, "ai_chats_per_month": 20, "seats": 1, "documents": 5},
        "features": [
            "10 decisions / month",
            "20 AI Chat messages / month",
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
        "limits": {"decisions_per_month": 200, "ai_chats_per_month": 1000, "seats": 1, "documents": 100},
        "features": [
            "200 decisions / month",
            "1,000 AI Chat messages / month",
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
        "limits": {"decisions_per_month": -1, "ai_chats_per_month": -1, "seats": 5, "documents": -1},
        "features": [
            "Unlimited decisions",
            "Unlimited AI Chat",
            "5 team seats",
            "Stakeholder mentions & shared decisions",
            "Slack alerts (coming soon)",
            "Dedicated success manager",
        ],
    },
}


class CheckoutIn(BaseModel):
    tier: Literal["pro", "team"]
    origin_url: str


async def _get_active_tier(user_id: str) -> dict:
    sub = await db.subscriptions.find_one({"user_id": user_id})
    if not sub:
        return {"tier": "starter", "status": "active", "current_period_end": None}
    now = now_utc()
    end = sub.get("current_period_end")
    end_dt = None
    if end:
        try:
            end_dt = datetime.fromisoformat(end)
        except Exception:
            end_dt = None
    active = end_dt is None or end_dt > now
    return {
        "tier": sub.get("tier", "starter") if active else "starter",
        "status": "active" if active else "expired",
        "current_period_end": end,
        "last_session_id": sub.get("last_session_id"),
    }


@api.get("/billing/tiers")
async def list_tiers():
    # public — used to render Pricing page
    return list(TIERS.values())


@api.get("/billing/me")
async def my_subscription(user=Depends(get_current_user)):
    active = await _get_active_tier(user["id"])
    return {"user_id": user["id"], **active, "tier_meta": TIERS.get(active["tier"], TIERS["starter"])}


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

    origin = (STRIPE_SUCCESS_URL or body.origin_url or FRONTEND_URL).rstrip("/")
    success_url = f"{origin}/pricing?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin}/pricing?canceled=1"

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
            metadata={
                "user_id": user["id"],
                "user_email": user["email"],
                "tier": body.tier,
                "tier_name": pkg["name"],
                "source": "decisionlens_pricing",
            },
        )
    except Exception as e:
        logger.exception("Stripe checkout failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Stripe checkout error: {e}")

    # Create payment_transactions row BEFORE redirect (per playbook)
    await db.payment_transactions.insert_one({
        "id": str(uuid.uuid4()),
        "session_id": session.id,
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
    if session_id and settled and payment_status == "paid":
        tx = await db.payment_transactions.find_one({"session_id": session_id})
        if tx and not tx.get("granted"):
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
            await db.payment_transactions.update_one(
                {"session_id": session_id},
                {"$set": {"payment_status": "paid", "granted": True, "updated_at": now_iso()}},
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
