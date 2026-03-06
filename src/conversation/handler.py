"""
src/conversation/handler.py — Autonomous conversation handler.

Given a lead and its full message thread, uses Claude to classify the reply
and take the appropriate action: respond, invoice, escalate, or stop.
"""
import json
import logging
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from config import cfg
from src.db import models

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are managing inbound replies for a real estate data service.
We send agents a free insight email about a property they're listing, then offer
a full market-intelligence lead report for $49.

Given the conversation thread and lead context, respond with a JSON object:
{
  "classification": "SIMPLE" | "PAYMENT_READY" | "COMPLEX" | "STOP",
  "draft_reply": "...",
  "reason": "..."
}

Classification rules:
- SIMPLE        — straightforward interest, a question answerable from the context,
                  a minor objection you can handle with one confident reply.
- PAYMENT_READY — agent has clearly agreed, asked how to pay, or said they want
                  the report. Send the payment link immediately.
- COMPLEX       — price negotiation, unusual legal/data questions, multiple
                  conflicting asks, hostility, or anything requiring judgment
                  beyond standard FAQ answers.
- STOP          — unsubscribe request, "not interested", "remove me", cease-contact.

Always include draft_reply even for COMPLEX (used as an operator suggestion).
Keep draft_reply professional, brief (2-4 sentences), and first-person.
Do not mention the price in SIMPLE replies unless the agent brought it up.
"""


def _build_thread_text(thread: list[dict]) -> str:
    parts = []
    for msg in thread:
        direction = "AGENT" if msg["direction"] == "inbound" else "US"
        parts.append(f"[{direction}] {msg.get('body', '')}")
    return "\n\n".join(parts)


def handle_reply(lead: dict, thread: list[dict]) -> str:
    """
    Classify the conversation and take action.
    Returns the classification string.
    """
    try:
        import anthropic
    except ImportError:
        logger.error("[handler] anthropic package not installed")
        return "ERROR"

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

    thread_text = _build_thread_text(thread)
    lead_context = (
        f"Property: {lead.get('property_address')}\n"
        f"Signal: {lead.get('signal_type')} | Score: {lead.get('signal_score')}\n"
        f"List price: ${lead.get('list_price', 0):,.0f} | "
        f"Days on market: {lead.get('days_on_market', 'unknown')}\n"
        f"Agent: {lead.get('agent_name')} <{lead.get('agent_email')}>"
    )

    user_message = (
        f"Lead context:\n{lead_context}\n\n"
        f"Conversation thread:\n{thread_text}\n\n"
        "Classify the latest agent reply and draft our response."
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
    except Exception as e:
        logger.error(f"[handler] Claude API error for lead {lead['id']}: {e}")
        return "ERROR"

    classification = result.get("classification", "COMPLEX")
    draft_reply = result.get("draft_reply", "")
    reason = result.get("reason", "")

    logger.info(
        f"[handler] Lead #{lead['id']} classified as {classification} — {reason}"
    )

    lead_id = lead["id"]

    if classification == "STOP":
        _handle_stop(lead_id, draft_reply)

    elif classification == "SIMPLE":
        _handle_simple(lead, draft_reply)

    elif classification == "PAYMENT_READY":
        _handle_payment_ready(lead, draft_reply)

    elif classification == "COMPLEX":
        _handle_complex(lead, thread_text, draft_reply)

    else:
        logger.warning(f"[handler] Unknown classification '{classification}' for lead {lead_id}")

    return classification


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _handle_stop(lead_id: int, draft_reply: str):
    """Agent wants to opt out — mark stale and never contact again."""
    models.mark_stale(lead_id, reason="agent requested stop")
    logger.info(f"[handler] Lead #{lead_id} marked STALE (STOP request)")


def _handle_simple(lead: dict, draft_reply: str):
    """Send the draft reply and advance to NEGOTIATING."""
    from src.outreach.sender import send_reply

    lead_id = lead["id"]
    subject = f"Re: {lead.get('email_subject', 'Your listing')}"

    sent = send_reply(
        lead_id=lead_id,
        to_email=lead["agent_email"],
        to_name=lead.get("agent_name", ""),
        subject=subject,
        body=draft_reply,
    )

    if sent:
        # Advance state only if still in a pre-NEGOTIATING state
        current = models.get_lead_by_id(lead_id)
        if current and current["state"] == "EMAILED_FREE":
            models.mark_negotiating(lead_id)
        models.store_message(
            lead_id=lead_id,
            direction="outbound",
            from_email=cfg.MAIL_FROM,
            to_email=lead["agent_email"],
            subject=subject,
            body=draft_reply,
        )
        logger.info(f"[handler] SIMPLE reply sent to {lead['agent_email']}")
    else:
        logger.error(f"[handler] Failed to send SIMPLE reply for lead {lead_id}")


def _handle_payment_ready(lead: dict, draft_reply: str):
    """Send Stripe payment link and advance to INVOICED."""
    from src.payments.stripe_client import create_payment_link
    from src.outreach.sender import send_reply

    lead_id = lead["id"]

    result = create_payment_link(lead_id, lead["property_address"])
    if not result:
        logger.error(f"[handler] Could not create Stripe link for lead {lead_id}")
        _handle_complex(lead, "", draft_reply)  # escalate instead
        return

    # Advance through NEGOTIATING → INVOICED
    current = models.get_lead_by_id(lead_id)
    if current and current["state"] == "EMAILED_FREE":
        models.mark_negotiating(lead_id)

    models.mark_invoiced(lead_id, result["url"], result["session_id"])

    payment_body = (
        f"{draft_reply}\n\n"
        f"Here's your secure checkout link to access the full report:\n"
        f"{result['url']}\n\n"
        f"— {cfg.MAIL_FROM_NAME}"
    ).strip()

    subject = f"Your Lead Report — {lead['property_address']}"
    sent = send_reply(
        lead_id=lead_id,
        to_email=lead["agent_email"],
        to_name=lead.get("agent_name", ""),
        subject=subject,
        body=payment_body,
    )

    if sent:
        models.store_message(
            lead_id=lead_id,
            direction="outbound",
            from_email=cfg.MAIL_FROM,
            to_email=lead["agent_email"],
            subject=subject,
            body=payment_body,
        )
        logger.info(f"[handler] Payment link sent to {lead['agent_email']} for lead {lead_id}")
    else:
        logger.error(f"[handler] Failed to send payment email for lead {lead_id}")


def _handle_complex(lead: dict, thread_text: str, suggested_draft: str):
    """Email operator with the thread + suggested draft, advance to PENDING_OPERATOR."""
    from src.outreach.sender import send_reply

    lead_id = lead["id"]

    # Advance state
    current = models.get_lead_by_id(lead_id)
    if current:
        if current["state"] == "EMAILED_FREE":
            models.mark_negotiating(lead_id)
        if models.get_lead_by_id(lead_id)["state"] == "NEGOTIATING":
            models.mark_pending_operator(lead_id)

    operator_body = (
        f"A lead requires your attention before the bot responds.\n\n"
        f"Lead #{lead_id}: {lead.get('property_address')}\n"
        f"Agent: {lead.get('agent_name')} <{lead.get('agent_email')}>\n\n"
        f"--- Thread ---\n{thread_text}\n\n"
        f"--- Suggested Reply ---\n{suggested_draft}\n\n"
        f"To approve the suggested reply:\n"
        f"  python scripts/respond_lead.py {lead_id} --approve\n\n"
        f"To send a custom reply:\n"
        f"  python scripts/respond_lead.py {lead_id} --reply \"your text\"\n"
    )

    send_reply(
        lead_id=lead_id,
        to_email=cfg.OPERATOR_EMAIL,
        to_name="",
        subject=f"[ACTION REQUIRED] Lead #{lead_id} needs review — {lead.get('property_address')}",
        body=operator_body,
    )
    logger.info(f"[handler] Lead #{lead_id} escalated to operator at {cfg.OPERATOR_EMAIL}")
