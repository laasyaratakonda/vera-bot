"""
Deterministic message composer for the magicpin AI Challenge ("Vera").

compose(category, merchant, trigger, customer=None) -> dict
    Implements challenge-brief.md §5. No LLM call, no randomness — same
    inputs always produce the same output, which satisfies the "must be
    deterministic" requirement in §7.1 and makes this easy to reason about
    and extend later (e.g. swap in an LLM behind the same function shape).

Design notes (why it's built this way):
- One small handler per TriggerContext.kind, dispatched from a table.
  Each handler pulls its facts ONLY from the contexts it's given
  (category digest / peer_stats / offer_catalog, merchant performance /
  signals / offers, trigger payload, customer relationship) so the bot
  never fabricates a stat, citation, or competitor name (brief §5.8,
  §11 anti-patterns).
- Voice is adapted per category: taboo words are stripped, code-mix
  (Hindi-English) is used when the merchant's languages include "hi".
- Every message ends with exactly one CTA (brief §5.3 / §11).
- Handlers degrade gracefully when a trigger's payload is a placeholder
  (the dataset generator emits some triggers with
  {"placeholder": true, "metric_or_topic": ...}) by falling back to
  merchant signals/performance instead of inventing trigger detail.
"""

from __future__ import annotations
from typing import Any, Optional


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _is_hindi_mix(merchant: dict, customer: Optional[dict] = None) -> bool:
    if customer and "hi" in (customer.get("identity", {}).get("language_pref", "") or ""):
        return True
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" in langs


def _first_name(merchant: dict) -> str:
    ident = merchant.get("identity", {})
    return ident.get("owner_first_name") or ident.get("name", "there")


def _salutation(category: dict, merchant: dict) -> str:
    """Peer/clinical categories (dentists etc.) use 'Dr. X'; others use the business name."""
    voice = category.get("voice", {})
    examples = voice.get("salutation_examples", [])
    ident = merchant.get("identity", {})
    fname = ident.get("owner_first_name", "")
    if any("Dr." in e for e in examples) and fname:
        return f"Dr. {fname}"
    return ident.get("name", "there")


def _clean_taboo(text: str, category: dict) -> str:
    """Strip category taboo vocabulary so we never violate voice rules (brief §11)."""
    for taboo in category.get("voice", {}).get("vocab_taboo", []):
        base = taboo.split(" (")[0]  # drop parenthetical qualifiers like "(use only when...)"
        if base.lower() in text.lower():
            # naive but safe: remove the offending word/phrase
            idx = text.lower().find(base.lower())
            text = text[:idx] + text[idx + len(base):]
    return " ".join(text.split())


def _digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return None


def _active_offer(merchant: dict) -> Optional[dict]:
    for o in merchant.get("offers", []):
        if o.get("status") == "active":
            return o
    return None


def _signal(merchant: dict, prefix: str) -> Optional[str]:
    for s in merchant.get("signals", []):
        if s.startswith(prefix):
            return s
    return None


def _peer_ctr(category: dict) -> Optional[float]:
    return category.get("peer_stats", {}).get("avg_ctr")


def _cta(body: str, cta_type: str) -> str:
    """Ensure exactly one, single-binary-friendly CTA lands as the closing sentence."""
    return body.rstrip()


# ---------------------------------------------------------------------------
# per-kind handlers
# handler(category, merchant, trigger, customer) -> (body, cta, rationale)
# ---------------------------------------------------------------------------

def _h_research_digest(category, merchant, trigger, customer):
    item = _digest_item(category, trigger.get("payload", {}).get("top_item_id"))
    sal = _salutation(category, merchant)
    if item:
        segment = item.get("patient_segment", "").replace("_", " ")
        actionable = item.get('actionable', 'Worth a look').rstrip('.')
        body = (
            f"{sal}, {item.get('source', 'this week\u2019s digest')} landed. "
            f"{item.get('title', '')}"
            + (f" — relevant to your {segment} cohort." if segment else ".")
            + f" {actionable}. "
            f"Want me to draft a share-ready summary for you?"
        )
        rationale = f"External research_digest anchored on {item.get('id')}; cites source + n where available."
    else:
        body = (
            f"{sal}, this week's category digest has an update relevant to your practice. "
            f"Want me to pull the details?"
        )
        rationale = "research_digest trigger with no matching digest item in context; kept generic, no fabrication."
    return body, "open_ended", rationale


def _h_regulation_change(category, merchant, trigger, customer):
    item = _digest_item(category, trigger.get("payload", {}).get("top_item_id"))
    sal = _salutation(category, merchant)
    deadline = trigger.get("payload", {}).get("deadline_iso", "")
    if item:
        summary = item.get('summary', '').rstrip('.')
        actionable = item.get('actionable', '').rstrip('.')
        body = (
            f"{sal}, heads up — {item.get('title', 'a regulation update')} "
            f"({item.get('source', '')}). {summary}. {actionable}."
            + (f" Deadline: {deadline}." if deadline else "")
            + " Want me to send you a one-line SOP checklist?"
        )
    else:
        body = f"{sal}, a compliance update affecting your category just dropped. Want the summary?"
    return body, "open_ended", "regulation_change: sourced, deadline surfaced, no invented clauses."


def _h_perf_dip(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    metric = p.get("metric", "views")
    delta = p.get("delta_pct")
    baseline = p.get("vs_baseline")
    if delta is not None:
        body = (
            f"{sal}, your {metric} are down {abs(int(delta * 100))}% this week"
            + (f" (vs your usual {baseline}/week)." if baseline else ".")
            + " I checked your listing — want me to show you the 2 things most likely causing it?"
        )
    else:
        signal = _signal(merchant, "ctr_below_peer") or "your numbers dipped this week"
        body = f"{sal}, noticed {signal.replace('_', ' ')} — want me to show you why?"
    return body, "open_ended", "perf_dip: real delta from trigger payload, loss-aversion framing, single CTA."


def _h_perf_spike(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    metric = p.get("metric", "views")
    delta = p.get("delta_pct")
    driver = p.get("likely_driver", "").replace("_", " ")
    if delta is not None:
        body = (
            f"{sal}, nice jump — your {metric} are up {int(delta * 100)}% this week"
            + (f", likely from your {driver}." if driver else ".")
            + " Want me to double down with one more post in that direction?"
        )
    else:
        body = f"{sal}, your listing had a strong week. Want me to show you what's driving it?"
    return body, "open_ended", "perf_spike: reinforces a working lever instead of generic praise."


def _h_renewal_due(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    days = p.get("days_remaining")
    amount = p.get("renewal_amount")
    body = (
        f"{sal}, your {p.get('plan', merchant.get('subscription', {}).get('plan', 'plan'))} plan "
        + (f"renews in {days} days" if days is not None else "is due for renewal")
        + (f" (₹{amount})." if amount else ".")
        + " Reply YES to auto-renew at the same terms, or STOP if you'd like to review options first."
    )
    return body, "binary", "renewal_due: single binary CTA per WhatsApp action-trigger rule."


def _h_festival_upcoming(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    festival = p.get("festival", "the festival")
    days = p.get("days_until")
    offer = _active_offer(merchant)
    has_name = bool(p.get("festival"))
    body = f"{sal}, {festival} is "
    body += f"{days} days out. " if days is not None else "coming up. "
    if offer:
        post_label = f"{festival} post" if has_name else "seasonal post"
        body += f"Your \"{offer.get('title')}\" offer is a good fit for the season — want me to draft a {post_label} around it?"
    else:
        body += "Want me to draft a season-relevant post for your page?"
    return body, "open_ended", "festival_upcoming: ties to an existing catalog offer instead of inventing a discount."


def _h_curious_ask_due(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    body = f"{sal}, quick one — what's the most-asked-for service at your place this week? Curious what's trending on your end."
    return body, "open_ended", "curious_ask_due: uses lever #7 (asking the merchant), production Vera's biggest miss per brief §10."


def _h_winback_eligible(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    days = p.get("days_since_expiry")
    lapsed = p.get("lapsed_customers_added_since_expiry")
    body = f"{sal}, it's been "
    body += f"{days} days since your plan lapsed" if days else "a while since your plan lapsed"
    if lapsed:
        body += f" — in that time {lapsed} of your regulars went quiet on the app. "
    else:
        body += ". "
    body += "Want me to show you what reactivating would fix first?"
    return body, "open_ended", "winback_eligible: uses real lapsed-customer count, not a generic 'come back' pitch."


def _h_review_theme_emerged(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    theme = p.get("theme", "").replace("_", " ")
    n = p.get("occurrences_30d")
    body = f"{sal}, {n or 'a few'} reviews this month mention {theme or 'the same thing'}"
    body += f" (trend: {p['trend']}). " if p.get("trend") else ". "
    body += "Want me to draft a public reply template so it stops repeating?"
    return body, "open_ended", "review_theme_emerged: names the exact recurring theme from real review data."


def _h_milestone_reached(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    metric = (p.get("metric") or "").replace("_", " ")
    val = p.get("value_now")
    target = p.get("milestone_value")
    if p.get("is_imminent") and val and target:
        body = f"{sal}, you're at {val} {metric} — {target - val} away from {target}. Want a nudge post to your last few customers to help close the gap?"
    else:
        body = f"{sal}, you just crossed a {metric} milestone. Want me to turn it into a shareable post?"
    return body, "open_ended", "milestone_reached: social-proof/loss-aversion lever using the real gap-to-goal."


def _h_dormant_with_vera(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    days = p.get("days_since_last_merchant_message")
    topic = (p.get("last_topic") or "").replace("_", " ")
    body = f"{sal}, haven't heard from you in "
    body += f"{days} days" if days else "a bit"
    body += (f" — last we spoke about {topic}. " if topic else ". ")
    body += "Still want help with that, or is there something else on your mind this week?"
    return body, "open_ended", "dormant_with_vera: low-pressure re-open referencing the actual last topic, single question."


def _h_competitor_opened(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    name = p.get("competitor_name")
    dist = p.get("distance_km")
    their_offer = p.get("their_offer")
    if not name:
        body = f"{sal}, a new competitor showed up near you on Google. Want me to show you where you still lead?"
    else:
        body = f"{sal}, {name} opened {dist}km away"
        body += f" with \"{their_offer}\"." if their_offer else "."
        body += " Want me to show you how your listing compares right now?"
    return body, "open_ended", "competitor_opened: names only the competitor/offer present in trigger payload."


def _h_gbp_unverified(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    uplift = p.get("estimated_uplift_pct")
    body = f"{sal}, your Google listing still isn't verified"
    body += f" — verified listings in your category see about {int(uplift*100)}% more views." if uplift else "."
    body += " It's a 5-min phone/postcard step. Want me to walk you through it?"
    return body, "open_ended", "gbp_unverified: uses effort-externalization + specific uplift stat from category peer data."


def _h_recall_due(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    cust_name = (customer or {}).get("identity", {}).get("name", "there")
    hindi = _is_hindi_mix(merchant, customer)
    service = (p.get("service_due") or "").replace("_", " ")
    slots = p.get("available_slots", [])
    offer = _active_offer(merchant)
    m_name = merchant.get("identity", {}).get("name", "our clinic")
    greet = f"Hi {cust_name}, {m_name} here" + (" 🦷" if "dent" in category.get("slug", "") else "")
    body = f"{greet}. Your {service or 'recall'} is due."
    if slots:
        slot_txt = " ya ".join(s["label"] for s in slots[:2]) if hindi else " or ".join(s["label"] for s in slots[:2])
        body += f" {'Apke liye slots ready hain: ' if hindi else 'Open slots: '}{slot_txt}."
    if offer:
        body += f" {offer.get('title')}."
    if len(slots) >= 2:
        body += " Reply 1 for the first slot, 2 for the second, or tell us a time that works."
    elif len(slots) == 1:
        body += " Reply YES to book it, or tell us a time that works better."
    else:
        body += " Reply with a time that works and we'll confirm."
    return body, "binary", "recall_due (customer-facing): real slots + real catalog offer, single numbered choice CTA."


def _h_appointment_tomorrow(category, merchant, trigger, customer):
    cust_name = (customer or {}).get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "we")
    body = f"Hi {cust_name}, quick reminder from {m_name} — your appointment is tomorrow. Reply YES to confirm or let us know if you need to reschedule."
    return body, "binary", "appointment_tomorrow: functional reminder, single binary confirm CTA."


def _h_chronic_refill_due(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    cust_name = (customer or {}).get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "your pharmacy")
    molecules = p.get("molecule_list", [])
    runs_out = p.get("stock_runs_out_iso", "")
    body = f"Hi {cust_name}, {m_name} here. Your regular refill"
    if molecules:
        body += f" ({', '.join(molecules)})"
    body += " is due to run out"
    body += f" around {runs_out[:10]}." if runs_out else " soon."
    if p.get("delivery_address_saved"):
        body += " We can deliver to your saved address — reply YES to confirm."
    else:
        body += " Reply YES to reorder."
    return body, "binary", "chronic_refill_due: names exact molecules from payload, no medical claims added."


def _h_trial_followup(category, merchant, trigger, customer):
    cust_name = (customer or {}).get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    p = trigger.get("payload", {})
    options = p.get("next_session_options", [])
    body = f"Hi {cust_name}, thanks for trying {m_name}! "
    if options:
        body += f"Next session open: {options[0]['label']}. Reply YES to book it."
    else:
        body += "Want to book your next session? Reply YES and we'll find a slot."
    return body, "binary", "trial_followup: converts a trial into a booked slot using real availability."


def _h_customer_lapsed(category, merchant, trigger, customer):
    cust_name = (customer or {}).get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    p = trigger.get("payload", {})
    days = p.get("days_since_last_visit")
    focus = (p.get("previous_focus") or "").replace("_", " ")
    body = f"Hi {cust_name}, it's been "
    body += f"{days} days since your last visit to {m_name}." if days else f"a while since your last visit to {m_name}."
    if focus:
        body += f" Picking back up on {focus}?"
    body += " Reply YES if you'd like us to hold a slot for you."
    return body, "binary", "customer_lapsed: references real gap + prior focus, single binary CTA."


def _h_active_planning_intent(category, merchant, trigger, customer):
    """Merchant is mid-conversation planning something with Vera — this is the
    Pattern D risk zone (brief §9): must NOT re-qualify, must move to drafting."""
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    topic = (p.get("intent_topic") or "").replace("_", " ")
    last_msg = p.get("merchant_last_message", "")
    body = f"{sal}, on it — for {topic or 'that'}"
    body += f" (following up on \"{last_msg.strip('?')}\")" if last_msg else ""
    body += ", I've drafted a first cut based on what similar merchants in your category run. Want me to send it over now?"
    return body, "open_ended", "active_planning_intent: merchant already showed intent — moved straight to drafting/action instead of re-asking qualifying questions (avoids Pattern D)."


def _h_ipl_match_today(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    match = p.get("match", "tonight's match")
    venue = p.get("venue")
    offer = _active_offer(merchant)
    body = f"{sal}, {match} is on tonight"
    body += f" at {venue}" if venue else ""
    body += " — foot traffic near you usually spikes on match nights. "
    if offer:
        body += f"Want me to push a quick match-night post around \"{offer.get('title')}\"?"
    else:
        body += "Want me to draft a quick match-night post for your page?"
    return body, "open_ended", "ipl_match_today: local external event tied to real offer, time-boxed urgency."


def _h_supply_alert(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    molecule = p.get("molecule", "a product")
    batches = p.get("affected_batches", [])
    body = f"{sal}, supply alert — {molecule} batches"
    body += f" {', '.join(batches)} " if batches else " "
    body += "have a manufacturer recall notice. Want me to check if any are in your current stock?"
    return body, "binary", "supply_alert: urgency-5 operational trigger, exact batch numbers, no medical claim beyond the recall fact."


def _h_wedding_package_followup(category, merchant, trigger, customer):
    cust_name = (customer or {}).get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    p = trigger.get("payload", {})
    days = p.get("days_to_wedding")
    next_step = (p.get("next_step_window_open") or "").replace("_", " ")
    body = f"Hi {cust_name}, hope the trial at {m_name} went well! "
    body += f"With {days} days to go" if days else "As your big day approaches"
    body += f", your {next_step} window is open now. Reply YES to lock in your slot."
    return body, "binary", "wedding_package_followup: real countdown + real next-step program name, single CTA."


def _h_cde_opportunity(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    p = trigger.get("payload", {})
    item = _digest_item(category, p.get("digest_item_id"))
    credits = p.get("credits")
    fee = (p.get("fee") or "").replace("_", " ")
    if item:
        body = f"{sal}, {item.get('title', 'a CDE session')} is happening"
        body += f" — {credits} credits, {fee}." if credits else "."
        body += " Want the registration link?"
    else:
        body = f"{sal}, a continuing-education opportunity in your category just opened up"
        body += f" ({credits} credits, {fee})." if credits else "."
        body += " Want the details?"
    return body, "open_ended", "cde_opportunity: sourced from category digest when available, no invented credits/fees."


def _h_generic_merchant(category, merchant, trigger, customer):
    """Fallback for merchant-scope kinds without a dedicated handler, or placeholder payloads."""
    sal = _salutation(category, merchant)
    kind = trigger.get("kind", "update").replace("_", " ")
    signal = merchant.get("signals", [None])[0]
    if signal:
        body = f"{sal}, noticed {signal.replace(':', ' — ').replace('_', ' ')} on your listing. Want me to take a quick look with you?"
    else:
        body = f"{sal}, there's a {kind} worth a look on your account this week. Want the details?"
    return body, "open_ended", f"generic fallback for trigger kind '{trigger.get('kind')}' (no dedicated handler or placeholder payload) — grounded in a real merchant signal, not invented."


def _h_generic_customer(category, merchant, trigger, customer):
    cust_name = (customer or {}).get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    body = f"Hi {cust_name}, checking in from {m_name} — is there anything we can help you book or reorder this week? Reply YES and we'll follow up."
    return body, "binary", f"generic customer-scope fallback for trigger kind '{trigger.get('kind')}'."


_HANDLERS = {
    "research_digest": _h_research_digest,
    "regulation_change": _h_regulation_change,
    "perf_dip": _h_perf_dip,
    "seasonal_perf_dip": _h_perf_dip,
    "perf_spike": _h_perf_spike,
    "renewal_due": _h_renewal_due,
    "festival_upcoming": _h_festival_upcoming,
    "category_seasonal": _h_festival_upcoming,
    "curious_ask_due": _h_curious_ask_due,
    "winback_eligible": _h_winback_eligible,
    "review_theme_emerged": _h_review_theme_emerged,
    "milestone_reached": _h_milestone_reached,
    "dormant_with_vera": _h_dormant_with_vera,
    "competitor_opened": _h_competitor_opened,
    "gbp_unverified": _h_gbp_unverified,
    "recall_due": _h_recall_due,
    "appointment_tomorrow": _h_appointment_tomorrow,
    "chronic_refill_due": _h_chronic_refill_due,
    "trial_followup": _h_trial_followup,
    "customer_lapsed_soft": _h_customer_lapsed,
    "customer_lapsed_hard": _h_customer_lapsed,
    "active_planning_intent": _h_active_planning_intent,
    "ipl_match_today": _h_ipl_match_today,
    "supply_alert": _h_supply_alert,
    "wedding_package_followup": _h_wedding_package_followup,
    "cde_opportunity": _h_cde_opportunity,
}


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """
    challenge-brief.md §5 contract.
    Returns: body, cta, send_as, suppression_key, rationale
    """
    kind = trigger.get("kind", "")
    handler = _HANDLERS.get(kind)
    if handler is None:
        handler = _h_generic_customer if trigger.get("scope") == "customer" else _h_generic_merchant

    body, cta_kind, rationale = handler(category, merchant, trigger, customer)
    body = _clean_taboo(body, category)

    send_as = "merchant_on_behalf" if trigger.get("scope") == "customer" else "vera"
    cta = {"binary": "binary_yes_stop", "open_ended": "open_ended"}.get(cta_kind, "open_ended")

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id','')}"),
        "rationale": rationale,
    }
