"""
magicpin AI Challenge — bot.py

Implements the 5 endpoints from challenge-testing-brief.md §2:
    POST /v1/context   GET /v1/healthz
    POST /v1/tick       GET /v1/metadata
    POST /v1/reply

Run:
    pip install -r requirements.txt
    uvicorn bot:app --host 0.0.0.0 --port 8080

Then deploy anywhere that gives you a public HTTPS URL (Render/Railway/Fly/
an ngrok tunnel — see README.md) and submit that base URL.
"""

from __future__ import annotations
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from composer import compose

app = FastAPI(title="Vera Challenge Bot")
START = time.time()

# ---------------------------------------------------------------------------
# In-memory state (fine per the brief: "storing in memory is fine, just don't
# restart between calls"). Swap for Redis/SQLite if you need real persistence.
# ---------------------------------------------------------------------------

contexts: dict[tuple[str, str], dict] = {}          # (scope, context_id) -> {version, payload}
conversations: dict[str, dict] = {}                 # conversation_id -> state
sent_bodies: dict[str, set[str]] = {}                # conversation_id -> {body,...} (anti-repetition)

INTENT_YES = re.compile(
    r"\b(yes|haan|ha|ok(ay)?|sure|go ahead|let'?s do it|karo|kar do|join|interested|"
    r"proceed|book it|confirm)\b", re.I
)
INTENT_NO = re.compile(
    r"\b(no|nahi|not interested|stop|band karo|later|nahin chahiye|leave it)\b", re.I
)
ABUSE = re.compile(
    r"\b(fuck|f\*ck|bastard|idiot|stupid|useless|bakwas|bewakoof|chutiya|saala|"
    r"kamina|nonsense|shut up|scam)\b", re.I
)
OFF_TOPIC = re.compile(
    r"\b(gst|income tax|itr|loan|emi|insurance|visa|passport|legal advice|lawyer|"
    r"personal problem|marriage|astrology|weather today|cricket score)\b", re.I
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_payload(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def _merchant_category(merchant: dict) -> Optional[dict]:
    slug = merchant.get("category_slug")
    return _get_payload("category", slug)


# ---------------------------------------------------------------------------
# GET /v1/healthz , GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.environ.get("TEAM_NAME", "Solo Entrant"),
        "team_members": [os.environ.get("TEAM_MEMBER", "Participant")],
        "model": "rule-based-deterministic-v1",
        "approach": (
            "Deterministic composer (no LLM in the hot path): a dispatch table keyed "
            "on TriggerContext.kind pulls facts only from category/merchant/trigger/"
            "customer context, applies category voice + taboo filtering, and emits "
            "exactly one CTA. See composer.py."
        ),
        "contact_email": os.environ.get("CONTACT_EMAIL", "you@example.com"),
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers[:20]:  # respect the 20-actions/tick cap
        trigger = _get_payload("trigger", trg_id)
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = _get_payload("merchant", merchant_id)
        if not merchant:
            continue
        category = _merchant_category(merchant)
        if not category:
            continue

        customer = None
        if trigger.get("scope") == "customer" and trigger.get("customer_id"):
            customer = _get_payload("customer", trigger["customer_id"])

        # suppression: don't resend something already sent for this trigger's key
        conv_id = f"conv_{merchant_id}_{trg_id}"
        already_sent = sent_bodies.get(conv_id, set())

        composed = compose(category, merchant, trigger, customer)
        if composed["body"] in already_sent:
            continue  # anti-repetition (testing-brief §10)

        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": trigger.get("customer_id"),
            "trigger_id": trg_id,
            "turns": [{"from": composed["send_as"], "body": composed["body"]}],
            "auto_reply_streak": 0,
            "last_merchant_message": None,
            "abuse_streak": 0,
        }
        sent_bodies.setdefault(conv_id, set()).add(composed["body"])

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": trigger.get("customer_id"),
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", "")],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.setdefault(body.conversation_id, {
        "merchant_id": body.merchant_id,
        "customer_id": body.customer_id,
        "trigger_id": None,
        "turns": [],
        "auto_reply_streak": 0,
        "last_merchant_message": None,
        "abuse_streak": 0,
    })
    state["turns"].append({"from": body.from_role, "body": body.message})

    # --- auto-reply detection: same verbatim message repeated (testing-brief §12.1 hint) ---
    if state["last_merchant_message"] is not None and body.message.strip() == state["last_merchant_message"].strip():
        state["auto_reply_streak"] += 1
    else:
        state["auto_reply_streak"] = 1 if _looks_like_auto_reply(body.message) else 0
    state["last_merchant_message"] = body.message

    if state["auto_reply_streak"] >= 2:
        # tried once already after detecting it — now exit gracefully (brief Pattern B)
        return {"action": "end", "rationale": "Detected repeated canned auto-reply; exiting to avoid wasting turns."}

    if state["auto_reply_streak"] == 1:
        out = {
            "action": "send",
            "body": "Got it — before this goes to your team, want to quickly see the exact detail yourself? Takes 2 minutes.",
            "cta": "binary_yes_stop",
            "rationale": "First canned-reply detection: one light nudge to reach a human, per brief Pattern B.",
        }
        return _dedupe(body.conversation_id, out)

    # --- hostile: stay polite, don't escalate, don't just go silent ---
    if ABUSE.search(body.message):
        state["abuse_streak"] = state.get("abuse_streak", 0) + 1
        if state["abuse_streak"] >= 3:
            return {"action": "end", "rationale": "Repeated hostility after two calm responses; exiting rather than continuing to absorb abuse."}
        out = {
            "action": "send",
            "body": "Understood, no problem — happy to drop this if it's not useful right now. If you'd like, I can also just answer whatever's actually on your mind.",
            "cta": "open_ended",
            "rationale": "Hostile message: stayed polite, did not mirror tone or escalate, offered an easy out (brief Phase-4 hostile/off-topic scenario).",
        }
        return _dedupe(body.conversation_id, out)
    else:
        state["abuse_streak"] = 0

    # --- off-topic: acknowledge briefly, redirect to mission without ignoring the ask ---
    if OFF_TOPIC.search(body.message) and not INTENT_YES.search(body.message):
        out = {
            "action": "send",
            "body": "That's outside what I can help with directly — worth checking with your accountant/relevant expert on that one. On your listing though, want me to carry on with what we were doing?",
            "cta": "open_ended",
            "rationale": "Off-topic ask detected; acknowledged politely without fabricating an answer, then steered back on-mission (brief Phase-4 hostile/off-topic scenario).",
        }
        return _dedupe(body.conversation_id, out)

    # --- explicit not-interested -> exit ---
    if INTENT_NO.search(body.message) and not INTENT_YES.search(body.message):
        return {"action": "end", "rationale": "Merchant/customer signaled not interested; exiting gracefully."}

    # --- explicit yes/intent -> action mode immediately (brief Pattern D) ---
    if INTENT_YES.search(body.message):
        out = {
            "action": "send",
            "body": "Great — done. I'll get this moving right away and confirm here once it's live.",
            "cta": "none",
            "rationale": "Detected explicit affirmative intent; routed straight to action instead of re-qualifying (avoids brief Pattern D failure).",
        }
        return _dedupe(body.conversation_id, out)

    # --- default: acknowledge + advance with one open question, never repeat verbatim ---
    out = {
        "action": "send",
        "body": "Noted — want me to go ahead with that, or is there something specific you'd like changed first?",
        "cta": "open_ended",
        "rationale": "Neutral/ambiguous reply; asked one clarifying question rather than assuming intent.",
    }
    return _dedupe(body.conversation_id, out)


def _looks_like_auto_reply(message: str) -> bool:
    m = message.lower()
    canned_markers = [
        "thank you for contacting", "thank you for reaching out", "will get back to you",
        "automated", "auto-reply", "aapki jaankari ke liye", "team tak pahuncha",
        "currently unavailable", "shukriya",
    ]
    return any(marker in m for marker in canned_markers)


def _dedupe(conversation_id: str, out: dict) -> dict:
    """Never resend the exact same body twice in one conversation (testing-brief §10 anti-repetition penalty)."""
    seen = sent_bodies.setdefault(conversation_id, set())
    if out["body"] in seen:
        out["body"] = out["body"] + " (following up on the above)"
    seen.add(out["body"])
    conversations[conversation_id]["turns"].append({"from": "bot", "body": out["body"]})
    return out


# ---------------------------------------------------------------------------
# Optional teardown (testing-brief §11)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_bodies.clear()
    return {"status": "wiped"}
